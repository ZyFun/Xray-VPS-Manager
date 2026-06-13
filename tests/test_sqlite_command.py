from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from xray_vps_manager import runner
from xray_vps_manager.commands import sqlite as sqlite_command
from xray_vps_manager.core.server_env import read_server_env
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.storage import SQLITE_READS_SERVER_ENV


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
            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.dict(
                os.environ, {}, clear=True
            ), redirect_stdout(stdout):
                code = sqlite_command.status()

            self.assertEqual(code, 0)
            output = stdout.getvalue()
            self.assertIn("Schema: 1", output)
            self.assertIn("Quick check: ok", output)
            self.assertIn("SQLite ready: yes", output)

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

    def test_validate_cutover_passes_when_database_and_flags_are_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            connection = database.open_database(db_path)
            try:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
            finally:
                connection.close()

            stdout = StringIO()
            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ), redirect_stdout(stdout):
                code = sqlite_command.validate_cutover()

            self.assertEqual(code, 0)
            output = stdout.getvalue()
            self.assertIn("OK SQLite cutover validation passed.", output)
            self.assertIn("clients_source: sqlite", output)
            self.assertIn("traffic_source: sqlite", output)
            self.assertIn("telegram_source: sqlite", output)
            self.assertIn("activity_source: sqlite", output)
            self.assertIn("runtime_scenarios: ok", output)

    def test_validate_cutover_fails_when_flags_are_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            connection = database.open_database(db_path)
            try:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
            finally:
                connection.close()

            stdout = StringIO()
            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "0", "XRAY_MANAGER_SQLITE_WRITES": "0"},
                clear=True,
            ), redirect_stdout(stdout):
                code = sqlite_command.validate_cutover()

            self.assertEqual(code, 1)
            output = stdout.getvalue()
            self.assertIn("ERROR SQLite reads flag is disabled", output)
            self.assertIn("ERROR SQLite writes flag is disabled", output)
            self.assertIn("ERROR clients read layer is not using SQLite", output)

    def test_cleanup_legacy_dry_run_lists_files_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            clients_path = root / "clients.json"
            traffic_path = root / "traffic.json"
            client_log_dir = root / "activity" / "clients"
            client_log_dir.mkdir(parents=True)
            client_log = client_log_dir / "alice.jsonl"
            clients_path.write_text("{}\n")
            traffic_path.write_text("{}\n")
            client_log.write_text("{}\n")

            stdout = StringIO()
            with mock.patch.object(sqlite_command, "CLIENT_DB_PATH", clients_path), mock.patch.object(
                sqlite_command, "TRAFFIC_PATH", traffic_path
            ), mock.patch.object(
                sqlite_command, "ACTIVITY_PATH", root / "missing-activity.json"
            ), mock.patch.object(
                sqlite_command, "ACTIVITY_EXCEPTIONS_PATH", root / "missing-exceptions.json"
            ), mock.patch.object(
                sqlite_command, "TELEGRAM_DB_PATH", root / "missing-telegram.json"
            ), mock.patch.object(
                sqlite_command, "CLIENT_LOG_DIR", client_log_dir
            ), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), redirect_stdout(stdout):
                code = sqlite_command.cleanup_legacy(yes=False)

            self.assertEqual(code, 0)
            self.assertTrue(clients_path.exists())
            self.assertTrue(traffic_path.exists())
            self.assertTrue(client_log.exists())
            output = stdout.getvalue()
            self.assertIn(str(clients_path), output)
            self.assertIn(str(client_log), output)
            self.assertIn("Dry run only", output)

    def test_cleanup_legacy_validates_backs_up_and_deletes_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            connection = database.open_database(db_path)
            try:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
            finally:
                connection.close()

            clients_path = root / "clients.json"
            traffic_path = root / "traffic.json"
            activity_path = root / "activity.json"
            exceptions_path = root / "activity-exceptions.json"
            telegram_path = root / "telegram-bot.json"
            for path in (clients_path, traffic_path, activity_path, exceptions_path, telegram_path):
                path.write_text("{}\n")
            client_log_dir = root / "activity" / "clients"
            client_log_dir.mkdir(parents=True)
            client_log = client_log_dir / "alice.jsonl"
            client_log.write_text("{}\n")
            backup_path = root / "backup.tar.gz"
            backup_path.write_bytes(b"backup")

            stdout = StringIO()
            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.object(
                sqlite_command, "CLIENT_DB_PATH", clients_path
            ), mock.patch.object(
                sqlite_command, "TRAFFIC_PATH", traffic_path
            ), mock.patch.object(
                sqlite_command, "ACTIVITY_PATH", activity_path
            ), mock.patch.object(
                sqlite_command, "ACTIVITY_EXCEPTIONS_PATH", exceptions_path
            ), mock.patch.object(
                sqlite_command, "TELEGRAM_DB_PATH", telegram_path
            ), mock.patch.object(
                sqlite_command, "CLIENT_LOG_DIR", client_log_dir
            ), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ), mock.patch.object(
                sqlite_command.backup_command, "create_backup", return_value=backup_path
            ) as create_backup, redirect_stdout(stdout):
                code = sqlite_command.cleanup_legacy(yes=True)

            self.assertEqual(code, 0)
            create_backup.assert_called_once_with(path_only=False, quiet=True, sync=True)
            self.assertTrue(db_path.exists())
            for path in (clients_path, traffic_path, activity_path, exceptions_path, telegram_path, client_log):
                self.assertFalse(path.exists())
            output = stdout.getvalue()
            self.assertIn("OK SQLite cutover validation passed.", output)
            self.assertIn("Pre-cleanup backup:", output)
            self.assertIn("Deleted legacy JSON/JSONL state files: 6", output)

    def test_cleanup_legacy_rejects_non_file_legacy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            clients_path = root / "clients.json"
            clients_path.mkdir()

            with mock.patch.object(sqlite_command, "CLIENT_DB_PATH", clients_path), mock.patch.object(
                sqlite_command, "TRAFFIC_PATH", root / "missing-traffic.json"
            ), mock.patch.object(
                sqlite_command, "ACTIVITY_PATH", root / "missing-activity.json"
            ), mock.patch.object(
                sqlite_command, "ACTIVITY_EXCEPTIONS_PATH", root / "missing-exceptions.json"
            ), mock.patch.object(
                sqlite_command, "TELEGRAM_DB_PATH", root / "missing-telegram.json"
            ), mock.patch.object(
                sqlite_command, "CLIENT_LOG_DIR", root / "missing-logs"
            ), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.object(
                sqlite_command.backup_command, "create_backup"
            ) as create_backup, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    sqlite_command.cleanup_legacy(yes=True)

            self.assertEqual(caught.exception.code, 1)
            create_backup.assert_not_called()
            self.assertTrue(clients_path.exists())

    def test_verify_backup_file_rejects_empty_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "empty.tar.gz"
            path.write_bytes(b"")

            with self.assertRaisesRegex(RuntimeError, "backup is empty"):
                sqlite_command.verify_backup_file(path, "Test")

    def test_runtime_scenario_issues_reports_cross_section_problems(self) -> None:
        with mock.patch.object(
            sqlite_command.client_repository,
            "load_db_sql_result",
            return_value=mock.Mock(
                db={"connections": {}, "clients": {"alice": {"connection": "missing-connection"}}},
                source="sqlite",
            ),
        ), mock.patch.object(
            sqlite_command.traffic_repository,
            "load_traffic_db_for_read_result",
            return_value=mock.Mock(db={"clients": {"ghost": {"incoming": 1, "outgoing": 1, "history": {}}}}, source="sqlite"),
        ), mock.patch.object(
            sqlite_command.activity_repository,
            "event_client_names_for_read",
            return_value=["alice"],
        ), mock.patch.object(
            sqlite_command.activity_repository,
            "iter_events_for_read",
            return_value=iter(()),
        ), mock.patch.object(
            sqlite_command.telegram_settings,
            "load_db_sql_result",
            return_value=mock.Mock(db={"clientSubscriptions": {"123": {"client": "ghost"}}}, source="sqlite"),
        ):
            issues = sqlite_command.runtime_scenario_issues()

        self.assertIn("client runtime connection is missing: alice -> missing-connection", issues)
        self.assertIn("traffic runtime client is missing from clients: ghost", issues)
        self.assertIn("Telegram runtime subscription client is missing: 123 -> ghost", issues)


if __name__ == "__main__":
    unittest.main()
