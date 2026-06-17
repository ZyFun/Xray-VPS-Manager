from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from xray_vps_manager import runner
from xray_vps_manager.commands import sqlite as sqlite_command
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import settings as sqlite_settings


class SQLiteCommandTests(unittest.TestCase):
    def test_runner_exposes_sqlite_command(self) -> None:
        self.assertIn("sqlite", runner.COMMAND_MODULES)
        self.assertTrue(callable(runner.command_main("sqlite")))

    def test_status_reports_missing_database_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            stdout = StringIO()

            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), redirect_stdout(stdout):
                code = sqlite_command.status()

            self.assertEqual(code, 1)
            self.assertFalse(db_path.exists())
            output = stdout.getvalue()
            self.assertIn("Status: missing", output)
            self.assertIn("Run install.sh or restore a backup", output)

    def test_status_reports_ready_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            connection = database.open_database(db_path)
            try:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
            finally:
                connection.close()

            stdout = StringIO()
            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), redirect_stdout(stdout):
                code = sqlite_command.status()

            self.assertEqual(code, 0)
            output = stdout.getvalue()
            self.assertIn("Schema: 3", output)
            self.assertIn("Quick check: ok", output)
            self.assertIn("SQLite ready: yes", output)
            self.assertIn("clients: 0", output)

    def test_main_rejects_removed_cutover_command(self) -> None:
        stdout = StringIO()
        with mock.patch.object(sqlite_command.sys, "argv", ["sqlite", "validate-cutover"]), redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as caught:
                sqlite_command.main()

        self.assertEqual(caught.exception.code, 1)
        self.assertIn("xray-vps-manager sqlite status", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
