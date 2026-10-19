from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.core.errors import StateError
from xray_vps_manager.db import database, schema


class SQLiteDatabaseInfrastructureTests(unittest.TestCase):
    def test_open_database_creates_file_and_initializes_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            connection = database.open_database(db_path)
            try:
                self.assertTrue(db_path.exists())
                self.assertEqual(schema.schema_version(connection), schema.CURRENT_SCHEMA_VERSION)
                row = connection.execute("SELECT 1 AS value").fetchone()
                self.assertEqual(row["value"], 1)
                self.assertEqual(database.quick_check(connection), "ok")
            finally:
                connection.close()

    def test_open_database_uses_full_busy_timeout(self) -> None:
        connection = database.open_database(":memory:")
        try:
            row = connection.execute("PRAGMA busy_timeout").fetchone()
            self.assertEqual(row[0], database.DEFAULT_BUSY_TIMEOUT_MS)
            self.assertEqual(database.DEFAULT_BUSY_TIMEOUT_MS, 30000)
        finally:
            connection.close()

    def test_transaction_commits_and_rolls_back(self) -> None:
        connection = database.open_database(":memory:")
        try:
            connection.execute("CREATE TABLE sample(value TEXT NOT NULL)")

            with database.transaction(connection):
                connection.execute("INSERT INTO sample(value) VALUES ('committed')")

            self.assertEqual(
                [row["value"] for row in connection.execute("SELECT value FROM sample")],
                ["committed"],
            )

            with self.assertRaisesRegex(RuntimeError, "boom"):
                with database.transaction(connection):
                    connection.execute("INSERT INTO sample(value) VALUES ('rolled-back')")
                    raise RuntimeError("boom")

            self.assertEqual(
                [row["value"] for row in connection.execute("SELECT value FROM sample")],
                ["committed"],
            )
        finally:
            connection.close()

    def test_nested_transaction_uses_savepoint(self) -> None:
        connection = database.open_database(":memory:")
        try:
            connection.execute("CREATE TABLE sample(value TEXT NOT NULL)")
            with database.transaction(connection):
                connection.execute("INSERT INTO sample(value) VALUES ('outer')")
                with self.assertRaisesRegex(RuntimeError, "nested"):
                    with database.transaction(connection):
                        connection.execute("INSERT INTO sample(value) VALUES ('inner')")
                        raise RuntimeError("nested")
                connection.execute("INSERT INTO sample(value) VALUES ('after')")

            self.assertEqual(
                [row["value"] for row in connection.execute("SELECT value FROM sample")],
                ["outer", "after"],
            )
        finally:
            connection.close()

    def test_backup_database_copies_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            backup_dir = Path(tmp_dir) / "backups"
            connection = database.open_database(db_path)
            try:
                connection.execute(
                    "INSERT INTO manager_metadata(key, value) VALUES ('server-name', 'demo')"
                )
                connection.commit()
            finally:
                connection.close()

            backup_path = database.backup_database(db_path, backup_dir=backup_dir, label="pre migration")

            self.assertIsNotNone(backup_path)
            self.assertTrue(backup_path.exists())
            with sqlite3.connect(str(backup_path)) as backup:
                row = backup.execute("SELECT value FROM manager_metadata WHERE key = 'server-name'").fetchone()
            self.assertEqual(row[0], "demo")

    def test_initialize_database_backs_up_before_destructive_pending_migration(self) -> None:
        destructive = schema.Migration(
            version=schema.CURRENT_SCHEMA_VERSION + 1,
            name="destructive_test",
            statements=("CREATE TABLE destructive_test(value TEXT)",),
            requires_backup=True,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            connection = database.open_database(db_path)
            connection.close()

            with mock.patch.object(schema, "CURRENT_SCHEMA_VERSION", destructive.version), mock.patch.object(
                schema, "MIGRATIONS", schema.MIGRATIONS + (destructive,)
            ):
                migrated = database.initialize_database(db_path, backup_dir=Path(tmp_dir) / "backups")
                try:
                    self.assertEqual(schema.schema_version(migrated), destructive.version)
                finally:
                    migrated.close()

            backups = list((Path(tmp_dir) / "backups").glob("*.db"))
            self.assertEqual(len(backups), 1)

    def test_backup_database_returns_none_when_source_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertIsNone(database.backup_database(Path(tmp_dir) / "missing.db"))


def open_with_pending_wal_frames(db_path: Path, value: str) -> sqlite3.Connection:
    """Открытое соединение, последняя запись которого лежит только в `-wal` (без checkpoint)."""
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute("INSERT OR REPLACE INTO manager_metadata(key, value) VALUES ('sample', ?)", (value,))
    connection.commit()
    return connection


def journal_mode_of(db_path: Path) -> str:
    connection = sqlite3.connect(str(db_path))
    try:
        return connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()


class SQLiteWalModeTests(unittest.TestCase):
    """VIREI-245: manager.db работает в WAL с synchronous=NORMAL."""

    def test_initialized_database_uses_wal_with_normal_sync_on_every_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            database.open_database(db_path).close()

            for connection in (database.open_database(db_path), database.open_database(db_path, initialize=False)):
                try:
                    self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                    self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 1)
                    self.assertEqual(
                        connection.execute("PRAGMA journal_size_limit").fetchone()[0],
                        128 * 1024 * 1024,
                    )
                finally:
                    connection.close()

    def test_existing_delete_mode_database_switches_only_on_initialize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            database.open_database(db_path).close()
            raw = sqlite3.connect(str(db_path))
            raw.execute("PRAGMA journal_mode = DELETE")
            raw.close()

            # Read-пути (xray-test, sqlite status) открывают базу без инициализации и не пишут её.
            database.open_database(db_path, initialize=False).close()
            self.assertEqual(journal_mode_of(db_path), "delete")

            database.open_database(db_path).close()
            self.assertEqual(journal_mode_of(db_path), "wal")

    def test_busy_database_keeps_previous_mode_without_failing_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            database.open_database(db_path).close()
            raw = sqlite3.connect(str(db_path))
            raw.execute("PRAGMA journal_mode = DELETE")
            raw.execute("BEGIN")
            raw.execute("SELECT COUNT(*) FROM manager_metadata").fetchall()

            with mock.patch.object(database, "DEFAULT_BUSY_TIMEOUT_MS", 50):
                connection = database.open_database(db_path)
            connection.close()
            raw.rollback()
            raw.close()

            self.assertEqual(journal_mode_of(db_path), "delete")
            database.open_database(db_path).close()
            self.assertEqual(journal_mode_of(db_path), "wal")

    def test_exclusive_write_lock_does_not_block_reader(self) -> None:
        """Фиксация записи держит EXCLUSIVE: в режиме delete читатель получал `database is locked` (VIREI-237)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            writer = database.open_database(db_path)
            reader = database.open_database(db_path, initialize=False)
            try:
                writer.execute("BEGIN EXCLUSIVE")
                writer.execute("INSERT INTO manager_metadata(key, value) VALUES ('pending', 'value')")
                reader.execute("PRAGMA busy_timeout = 100")

                rows = reader.execute("SELECT value FROM manager_metadata WHERE key = 'pending'").fetchall()

                self.assertEqual(rows, [])
                writer.commit()
            finally:
                reader.close()
                writer.close()

    def test_ensure_database_permissions_covers_wal_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            database.open_database(db_path).close()
            holder = open_with_pending_wal_frames(db_path, "value")
            try:
                sidecars = [Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
                for path in (db_path, *sidecars):
                    path.chmod(0o600)

                with mock.patch.object(database, "chown_xray") as chown:
                    database.ensure_database_permissions(db_path)

                for path in (db_path, *sidecars):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o640, path.name)
                self.assertEqual([call.args[0] for call in chown.call_args_list], [db_path, *sidecars])
            finally:
                holder.close()

    def test_backup_database_copies_pending_wal_into_single_delete_mode_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            database.open_database(db_path).close()
            holder = open_with_pending_wal_frames(db_path, "from-wal")
            try:
                backup_path = database.backup_database(db_path, backup_dir=root / "backups", label="pre-restore")
            finally:
                holder.close()

            # Формат копии как до WAL: один файл в режиме delete, без соседних `-wal`/`-shm`.
            self.assertEqual(backup_path.read_bytes()[18:20], b"\x01\x01")
            alone = root / "alone" / "manager.db"
            alone.parent.mkdir()
            alone.write_bytes(backup_path.read_bytes())
            connection = sqlite3.connect(str(alone))
            try:
                row = connection.execute("SELECT value FROM manager_metadata WHERE key = 'sample'").fetchone()
                self.assertEqual(row[0], "from-wal")
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                connection.close()

    def test_remove_journal_files_keeps_database_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            db_path.write_text("db")
            for suffix in ("-wal", "-shm", "-journal"):
                Path(f"{db_path}{suffix}").write_text("stale")

            removed = database.remove_journal_files(db_path)

            self.assertEqual(
                removed,
                (Path(f"{db_path}-wal"), Path(f"{db_path}-shm"), Path(f"{db_path}-journal")),
            )
            self.assertEqual(sorted(path.name for path in Path(tmp_dir).iterdir()), ["manager.db"])
            self.assertEqual(database.remove_journal_files(db_path), ())

    def test_ensure_free_space_counts_wal_and_rejects_short_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            db_path.write_bytes(b"x" * 1000)
            Path(f"{db_path}-wal").write_bytes(b"x" * 24)
            self.assertEqual(database.database_disk_bytes(db_path), 1024)

            with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=3071)):
                with self.assertRaises(StateError) as caught:
                    database.ensure_free_space(db_path, 3072, purpose="activity cleanup")
            self.assertIn("activity cleanup", caught.exception.message)
            self.assertIn("not changed", caught.exception.hint)

            with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=3072)):
                database.ensure_free_space(db_path, 3072, purpose="activity cleanup")

    def test_database_disk_bytes_tolerates_wal_removed_between_checks(self) -> None:
        """245-R03: `exists()` видит `-wal`, а к `stat()` его уже удалило закрытие чужого соединения."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            db_path.write_bytes(b"x" * 1000)
            wal_path = Path(f"{db_path}-wal")
            original_exists = Path.exists

            def wal_seen_then_gone(self, *args, **kwargs):
                return True if self == wal_path else original_exists(self, *args, **kwargs)

            with mock.patch.object(Path, "exists", wal_seen_then_gone):
                self.assertEqual(database.database_disk_bytes(db_path), 1000)


if __name__ == "__main__":
    unittest.main()
