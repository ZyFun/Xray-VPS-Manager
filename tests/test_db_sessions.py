"""Контракты доступа к существующей БД и транзакций R2.1."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import shutil
import sqlite3
from threading import Event
import unittest
from unittest.mock import patch

from tests.support.sandbox import Sandbox
from xray_vps_manager.core.errors import StateError
from xray_vps_manager.db import database, schema


class DatabaseSessionTests(unittest.TestCase):
    def test_connect_does_not_create_or_migrate(self):
        """B2.1: connect осматривает версию 0, рабочие соединения больше не создают в таком файле схему."""
        with Sandbox() as box:
            missing = box.root / "missing" / "db?#.sqlite"
            with self.assertRaises(StateError):
                database.connect(missing)
            self.assertFalse(missing.parent.exists())
            missing.parent.mkdir()
            missing.touch()
            with closing(database.connect(missing, timeout=0.1)) as conn:
                self.assertIs(conn.autocommit, True)
                self.assertEqual(schema.schema_version(conn), 0)
                self.assertEqual(schema.table_names(conn), set())
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 100)
            for session in (database.session, database.write_session):
                with self.subTest(session=session.__name__), self.assertRaisesRegex(StateError, "not initialized"):
                    with session(missing):
                        self.fail("пустой файл допущен в рабочий сеанс")
            for initialize in (True, False):
                with self.subTest(initialize=initialize), self.assertRaisesRegex(StateError, "not initialized"):
                    database.open_database(missing, initialize=initialize)
            self.assertEqual(missing.read_bytes(), b"")
            with closing(database.initialize(missing)) as conn:
                self.assertEqual(schema.schema_version(conn), 8)
            with database.session(missing) as conn:
                self.assertEqual(schema.schema_version(conn), 8)
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_session_preserves_connection_settings_and_revalidates_replaced_db(self):
        with Sandbox() as box:
            for _ in range(2):
                with database.session() as conn:
                    self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                    self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
                    self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                    self.assertEqual(conn.execute("PRAGMA journal_size_limit").fetchone()[0], 128 * 1024**2)
                    self.assertIs(conn.row_factory, sqlite3.Row)
            replacement = box.root / "replacement.db"
            with closing(sqlite3.connect(replacement)) as conn:
                conn.execute("PRAGMA user_version = 9")
            replacement.replace(box.paths.manager_db)
            with self.assertRaisesRegex(StateError, "newer than supported"):
                database.ensure_initialized()

    def test_session_rechecks_schema_after_restore_into_the_same_inode(self):
        """116-SR-003: restore копирует поверх файла, поэтому совпадение inode недостаточно."""
        with Sandbox() as box:
            path = box.paths.manager_db
            with database.session(path):
                pass
            inode = path.stat().st_ino
            archived = box.root / "older.db"
            with closing(sqlite3.connect(archived)) as conn, conn:
                schema.apply_migration(conn, schema.MIGRATIONS[0])
            shutil.copy2(archived, path)
            self.assertEqual(path.stat().st_ino, inode)
            with database.session(path) as conn:
                self.assertEqual(schema.schema_version(conn), schema.CURRENT_SCHEMA_VERSION)
            with closing(database.connect(path)) as conn:
                conn.execute(f"PRAGMA user_version={schema.CURRENT_SCHEMA_VERSION + 1}")
            with self.assertRaisesRegex(StateError, "newer than supported"):
                with database.write_session(path):
                    self.fail("новая версия допущена к записи")

    def test_failed_migration_rolls_back_ddl_and_keeps_pre_migration_backup(self):
        with Sandbox() as box:
            migration = schema.Migration(9, "fixture", ("CREATE TABLE scratch(n)", "INVALID SQL"), True)
            with patch.object(schema, "CURRENT_SCHEMA_VERSION", 9), patch.object(
                schema, "MIGRATIONS", (*schema.MIGRATIONS, migration)
            ), self.assertRaises(StateError):
                database.ensure_initialized()
            with closing(database.connect()) as conn:
                self.assertEqual(schema.schema_version(conn), 8)
                self.assertNotIn("scratch", schema.table_names(conn))
            backup, = box.paths.db_backup_dir.glob("*-pre-migration-*.db")
            with closing(sqlite3.connect(backup)) as conn:
                self.assertEqual(schema.schema_version(conn), 8)
                self.assertEqual(conn.execute("SELECT name FROM clients").fetchone()[0], "alice")

    def test_write_session_rolls_back_and_nested_savepoint_preserves_outer_write(self):
        with Sandbox() as box:
            with database.write_session() as conn:
                conn.execute("CREATE TABLE sample(n INTEGER)")
                with self.assertRaises(ValueError), database.transaction(conn):
                    conn.execute("INSERT INTO sample VALUES(1)")
                    raise ValueError
                conn.execute("INSERT INTO sample VALUES(2)")
            with self.assertRaises(ValueError), database.write_session() as conn:
                conn.execute("INSERT INTO sample VALUES(3)")
                raise ValueError
            with database.session() as conn:
                self.assertEqual([r[0] for r in conn.execute("SELECT n FROM sample")], [2])

    def test_writers_serialize_before_read_modify_write(self):
        with Sandbox(), ThreadPoolExecutor(max_workers=1) as pool:
            started = Event()

            def increment():
                started.set()
                with database.write_session() as writer:
                    previous = writer.execute("SELECT n FROM counter").fetchone()[0]
                    writer.execute("UPDATE counter SET n=?", (previous + 1,))
                    return previous

            with database.write_session() as conn:
                conn.execute("CREATE TABLE counter(n INTEGER)")
                conn.execute("INSERT INTO counter VALUES(0)")
            with database.write_session() as conn:
                # До первой записи конкурент уже не может открыть BEGIN IMMEDIATE.
                with closing(database.connect(timeout=0)) as other, self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    other.execute("BEGIN IMMEDIATE")
                future = pool.submit(increment)
                self.assertTrue(started.wait(timeout=2))
                conn.execute("UPDATE counter SET n=1")
            self.assertEqual(future.result(timeout=5), 1)
            with database.session() as conn:
                self.assertEqual(conn.execute("SELECT n FROM counter").fetchone()[0], 2)
