from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
