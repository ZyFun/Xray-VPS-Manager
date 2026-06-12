from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.commands import sqlite as sqlite_command
from xray_vps_manager.core.server_env import read_server_env, write_server_env
from xray_vps_manager.db import database, json_import
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.storage import SQLITE_READS_SERVER_ENV
from xray_vps_manager import runner


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
            self.assertIn("Status: missing", stdout.getvalue())

    def test_status_reports_ready_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            connection = database.open_database(db_path)
            try:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
            finally:
                connection.close()

            stdout = StringIO()
            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout):
                code = sqlite_command.status()

            self.assertEqual(code, 0)
            output = stdout.getvalue()
            self.assertIn("Schema: 1", output)
            self.assertIn("Quick check: ok", output)
            self.assertIn("Import ready: yes", output)

    def test_set_server_env_flag_writes_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "server.env"
            env_path.write_text("SERVER_ADDR=example.com\n")

            stdout = StringIO()
            with mock.patch.object(sqlite_command, "SERVER_ENV_PATH", env_path), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), redirect_stdout(stdout):
                sqlite_command.set_server_env_flag(SQLITE_READS_SERVER_ENV, True)

            values = read_server_env(env_path)
            self.assertEqual(values["SERVER_ADDR"], "example.com")
            self.assertEqual(values[SQLITE_READS_SERVER_ENV], "true")

    def test_import_json_creates_pre_import_backup_when_database_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            connection = database.open_database(db_path)
            connection.close()

            summary = json_import.ImportSummary(counts={"clients": 1}, warnings=["sample warning"])
            stdout = StringIO()
            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.object(
                sqlite_command.json_import, "import_json_files", return_value=summary
            ) as import_json_files, redirect_stdout(stdout):
                code = sqlite_command.import_json()

            self.assertEqual(code, 0)
            import_json_files.assert_called_once_with(db_path=db_path, replace=True)
            self.assertTrue(list((db_path.parent / "manager-db-backups").glob("*.db")))
            output = stdout.getvalue()
            self.assertIn("Pre-import SQLite backup:", output)
            self.assertIn("JSON-to-SQLite import complete.", output)
            self.assertIn("sample warning", output)

    def test_cutover_runs_safe_sequence_and_enables_sqlite_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            env_path = root / "server.env"
            write_server_env({"SERVER_ADDR": "example.com"}, env_path)

            def import_json_files(*, db_path, replace):
                connection = database.open_database(db_path)
                try:
                    sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
                finally:
                    connection.close()
                return json_import.ImportSummary(counts={"clients": 1})

            stdout = StringIO()
            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.object(
                sqlite_command, "SERVER_ENV_PATH", env_path
            ), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.object(
                sqlite_command, "stop_writers"
            ) as stop_writers, mock.patch.object(
                sqlite_command, "start_writers"
            ) as start_writers, mock.patch.object(
                sqlite_command.backup_command, "create_backup", return_value=root / "backup.tar.gz"
            ) as create_backup, mock.patch.object(
                sqlite_command.json_import, "import_json_files", side_effect=import_json_files
            ) as import_mock, mock.patch.object(
                sqlite_command, "run_xray_test", return_value="xray-test passed"
            ) as run_xray_test, redirect_stdout(stdout):
                code = sqlite_command.cutover(yes=True)

            self.assertEqual(code, 0)
            stop_writers.assert_called_once_with()
            start_writers.assert_called_once_with()
            create_backup.assert_called_once_with(path_only=False, quiet=True, sync=False)
            import_mock.assert_called_once_with(db_path=db_path, replace=True)
            run_xray_test.assert_called_once_with()
            values = read_server_env(env_path)
            self.assertEqual(values["MANAGER_SQLITE_READS_ENABLED"], "true")
            self.assertEqual(values["MANAGER_SQLITE_WRITES_ENABLED"], "true")
            self.assertIn("SQLite cutover complete.", stdout.getvalue())

    def test_cutover_disables_flags_and_restarts_writers_when_validation_fails_after_enable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            env_path = root / "server.env"
            write_server_env({"SERVER_ADDR": "example.com"}, env_path)
            connection = database.open_database(db_path)
            try:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
            finally:
                connection.close()

            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.object(
                sqlite_command, "SERVER_ENV_PATH", env_path
            ), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.object(
                sqlite_command, "stop_writers"
            ), mock.patch.object(
                sqlite_command, "start_writers"
            ) as start_writers, mock.patch.object(
                sqlite_command.backup_command, "create_backup", return_value=root / "backup.tar.gz"
            ), mock.patch.object(
                sqlite_command.json_import,
                "import_json_files",
                return_value=json_import.ImportSummary(counts={"clients": 1}),
            ), mock.patch.object(
                sqlite_command, "run_xray_test", side_effect=RuntimeError("test failed")
            ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    sqlite_command.cutover(yes=True)

            self.assertEqual(caught.exception.code, 1)
            self.assertGreaterEqual(start_writers.call_count, 1)
            values = read_server_env(env_path)
            self.assertEqual(values["MANAGER_SQLITE_READS_ENABLED"], "false")
            self.assertEqual(values["MANAGER_SQLITE_WRITES_ENABLED"], "false")

    def test_cutover_requires_yes_in_non_interactive_mode(self) -> None:
        with mock.patch.object(sqlite_command.os, "geteuid", return_value=0), mock.patch.object(
            sqlite_command.sys.stdin, "isatty", return_value=False
        ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as caught:
                sqlite_command.cutover(yes=False)

        self.assertEqual(caught.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
