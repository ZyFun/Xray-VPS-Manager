from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import os
import subprocess
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
            backup_path = root / "backup.tar.gz"
            backup_path.write_bytes(b"backup")
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
            ), mock.patch.dict(
                os.environ, {}, clear=True
            ), mock.patch.object(
                sqlite_command, "stop_writers"
            ) as stop_writers, mock.patch.object(
                sqlite_command, "verify_writers_stopped"
            ) as verify_writers_stopped, mock.patch.object(
                sqlite_command, "start_writers"
            ) as start_writers, mock.patch.object(
                sqlite_command, "verify_writers_started"
            ) as verify_writers_started, mock.patch.object(
                sqlite_command.backup_command, "create_backup", return_value=backup_path
            ) as create_backup, mock.patch.object(
                sqlite_command.json_import, "import_json_files", side_effect=import_json_files
            ) as import_mock, mock.patch.object(
                sqlite_command, "run_xray_test", return_value="xray-test passed"
            ) as run_xray_test, redirect_stdout(stdout):
                code = sqlite_command.cutover(yes=True)

            self.assertEqual(code, 0)
            stop_writers.assert_called_once_with()
            verify_writers_stopped.assert_called_once_with()
            start_writers.assert_called_once_with()
            verify_writers_started.assert_called_once_with()
            create_backup.assert_called_once_with(path_only=False, quiet=True, sync=False)
            import_mock.assert_called_once_with(db_path=db_path, replace=True)
            run_xray_test.assert_called_once_with()
            values = read_server_env(env_path)
            self.assertEqual(values["MANAGER_SQLITE_READS_ENABLED"], "true")
            self.assertEqual(values["MANAGER_SQLITE_WRITES_ENABLED"], "true")
            output = stdout.getvalue()
            self.assertIn("Validating SQLite cutover...", output)
            self.assertIn("OK SQLite cutover validation passed.", output)
            self.assertIn("SQLite cutover complete.", output)

    def test_cutover_already_active_skips_json_import_and_validates_current_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            connection = database.open_database(db_path)
            try:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
            finally:
                connection.close()

            stdout = StringIO()
            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ), mock.patch.object(
                sqlite_command, "confirm_cutover"
            ) as confirm_cutover, mock.patch.object(
                sqlite_command, "stop_writers"
            ) as stop_writers, mock.patch.object(
                sqlite_command, "start_writers"
            ) as start_writers, mock.patch.object(
                sqlite_command.backup_command, "create_backup"
            ) as create_backup, mock.patch.object(
                sqlite_command.json_import, "import_json_files"
            ) as import_json_files, mock.patch.object(
                sqlite_command, "run_xray_test", return_value="xray-test passed"
            ) as run_xray_test, redirect_stdout(stdout):
                code = sqlite_command.cutover(yes=False)

            self.assertEqual(code, 0)
            confirm_cutover.assert_not_called()
            stop_writers.assert_not_called()
            start_writers.assert_not_called()
            create_backup.assert_not_called()
            import_json_files.assert_not_called()
            run_xray_test.assert_called_once_with()
            output = stdout.getvalue()
            self.assertIn("Skipping JSON import", output)
            self.assertIn("OK SQLite cutover validation passed.", output)
            self.assertIn("SQLite cutover is already active.", output)

    def test_cutover_with_enabled_flags_and_missing_database_fails_without_json_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "missing-manager.db"
            stderr = StringIO()
            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ), mock.patch.object(
                sqlite_command, "stop_writers"
            ) as stop_writers, mock.patch.object(
                sqlite_command.json_import, "import_json_files"
            ) as import_json_files, redirect_stdout(StringIO()), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    sqlite_command.cutover(yes=True)

            self.assertEqual(caught.exception.code, 1)
            stop_writers.assert_not_called()
            import_json_files.assert_not_called()
            self.assertIn("SQLite cutover validation failed", stderr.getvalue())

    def test_preflight_imports_to_temporary_database_without_touching_manager_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            manager_db_path = root / "manager.db"
            xray_test = root / "xray-test"
            xray_test.write_text("#!/bin/sh\n")
            created_paths = []

            def import_json_files(*, db_path, replace):
                created_paths.append(Path(db_path))
                connection = database.open_database(db_path)
                try:
                    sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
                finally:
                    connection.close()
                return json_import.ImportSummary(counts={"clients": 1})

            stdout = StringIO()
            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", manager_db_path), mock.patch.object(
                sqlite_command, "XRAY_TEST", xray_test
            ), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.object(
                sqlite_command.json_import, "import_json_files", side_effect=import_json_files
            ) as import_mock, redirect_stdout(stdout):
                code = sqlite_command.preflight()

            self.assertEqual(code, 0)
            import_mock.assert_called_once()
            self.assertEqual(created_paths[0].name, "manager-preflight.db")
            self.assertFalse(created_paths[0].exists())
            self.assertFalse(manager_db_path.exists())
            self.assertIn("OK SQLite preflight passed.", stdout.getvalue())

    def test_preflight_fails_when_xray_test_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            missing_xray_test = root / "missing-xray-test"

            def import_json_files(*, db_path, replace):
                connection = database.open_database(db_path)
                try:
                    sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
                finally:
                    connection.close()
                return json_import.ImportSummary(counts={"clients": 1})

            stdout = StringIO()
            with mock.patch.object(sqlite_command, "XRAY_TEST", missing_xray_test), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.object(
                sqlite_command.json_import, "import_json_files", side_effect=import_json_files
            ), redirect_stdout(stdout):
                code = sqlite_command.preflight()

            self.assertEqual(code, 1)
            self.assertIn("ERROR xray-test not found", stdout.getvalue())

    def test_stop_writers_stops_units_individually_and_skips_missing_units(self) -> None:
        def run(command, **_kwargs):
            unit = command[-1]
            if unit == "xray-client-expire.service":
                return subprocess.CompletedProcess(command, 5, stdout="", stderr="Unit xray-client-expire.service not loaded.")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        stdout = StringIO()
        with mock.patch.object(sqlite_command.subprocess, "run", side_effect=run) as run_mock, redirect_stdout(stdout):
            sqlite_command.stop_writers()

        calls = [call.args[0] for call in run_mock.call_args_list]
        self.assertEqual(calls, [["systemctl", "stop", unit] for unit in sqlite_command.WRITER_STOP_UNITS])
        self.assertIn("WARNING systemd unit skipped: stop xray-client-expire.service", stdout.getvalue())

    def test_start_writers_starts_units_individually_and_skips_missing_units(self) -> None:
        def run(command, **_kwargs):
            unit = command[-1]
            if unit == "xray-telegram-poller.service":
                return subprocess.CompletedProcess(command, 5, stdout="", stderr="Unit xray-telegram-poller.service could not be found.")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        stdout = StringIO()
        with mock.patch.object(sqlite_command.subprocess, "run", side_effect=run) as run_mock, redirect_stdout(stdout):
            sqlite_command.start_writers()

        calls = [call.args[0] for call in run_mock.call_args_list]
        self.assertEqual(calls, [["systemctl", "enable", "--now", unit] for unit in sqlite_command.WRITER_START_UNITS])
        self.assertIn("WARNING systemd unit skipped: enable --now xray-telegram-poller.service", stdout.getvalue())

    def test_run_systemctl_keeps_real_failures_fatal(self) -> None:
        result = subprocess.CompletedProcess(["systemctl", "stop", "xray-traffic-sync.service"], 1, stdout="", stderr="Access denied")

        with mock.patch.object(sqlite_command.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "Access denied"):
                sqlite_command.run_systemctl(["stop", "xray-traffic-sync.service"], allow_missing=True)

    def test_verify_writers_stopped_accepts_inactive_and_missing_units(self) -> None:
        def run(command, **_kwargs):
            unit = command[-1]
            if unit == "xray-client-expire.service":
                return subprocess.CompletedProcess(command, 4, stdout="", stderr="Unit xray-client-expire.service could not be found.")
            return subprocess.CompletedProcess(command, 3, stdout="inactive\n", stderr="")

        stdout = StringIO()
        with mock.patch.object(sqlite_command.subprocess, "run", side_effect=run), redirect_stdout(stdout):
            sqlite_command.verify_writers_stopped()

        self.assertIn("WARNING systemd unit skipped: is-active xray-client-expire.service", stdout.getvalue())

    def test_verify_writers_stopped_fails_when_unit_is_active(self) -> None:
        def run(command, **_kwargs):
            unit = command[-1]
            if unit == "xray-telegram-poller.service":
                return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")
            return subprocess.CompletedProcess(command, 3, stdout="inactive\n", stderr="")

        with mock.patch.object(sqlite_command.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "xray-telegram-poller.service=active"):
                sqlite_command.verify_writers_stopped()

    def test_verify_writers_started_accepts_active_and_missing_units(self) -> None:
        def run(command, **_kwargs):
            unit = command[-1]
            if unit == "xray-client-expire.timer":
                return subprocess.CompletedProcess(command, 4, stdout="", stderr="Unit xray-client-expire.timer not found.")
            return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")

        stdout = StringIO()
        with mock.patch.object(sqlite_command.subprocess, "run", side_effect=run), redirect_stdout(stdout):
            sqlite_command.verify_writers_started()

        self.assertIn("WARNING systemd unit skipped: is-active xray-client-expire.timer", stdout.getvalue())

    def test_verify_writers_started_fails_when_unit_is_inactive(self) -> None:
        def run(command, **_kwargs):
            unit = command[-1]
            if unit == "xray-traffic-sync.timer":
                return subprocess.CompletedProcess(command, 3, stdout="inactive\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")

        with mock.patch.object(sqlite_command.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "xray-traffic-sync.timer=inactive"):
                sqlite_command.verify_writers_started()

    def test_cutover_stops_before_backup_when_writer_verification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            env_path = root / "server.env"
            write_server_env({"SERVER_ADDR": "example.com"}, env_path)

            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.object(
                sqlite_command, "SERVER_ENV_PATH", env_path
            ), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.object(
                sqlite_command, "stop_writers"
            ) as stop_writers, mock.patch.object(
                sqlite_command, "verify_writers_stopped", side_effect=RuntimeError("writers still active")
            ) as verify_writers_stopped, mock.patch.object(
                sqlite_command, "start_writers"
            ) as start_writers, mock.patch.object(
                sqlite_command.backup_command, "create_backup"
            ) as create_backup, mock.patch.object(
                sqlite_command.json_import, "import_json_files"
            ) as import_json_files, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    sqlite_command.cutover(yes=True)

            self.assertEqual(caught.exception.code, 1)
            stop_writers.assert_called_once_with()
            verify_writers_stopped.assert_called_once_with()
            start_writers.assert_called_once_with()
            create_backup.assert_not_called()
            import_json_files.assert_not_called()

    def test_cutover_stops_before_import_when_backup_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            env_path = root / "server.env"
            missing_backup = root / "missing-backup.tar.gz"
            write_server_env({"SERVER_ADDR": "example.com"}, env_path)

            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.object(
                sqlite_command, "SERVER_ENV_PATH", env_path
            ), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.object(
                sqlite_command, "stop_writers"
            ) as stop_writers, mock.patch.object(
                sqlite_command, "verify_writers_stopped"
            ) as verify_writers_stopped, mock.patch.object(
                sqlite_command, "start_writers"
            ) as start_writers, mock.patch.object(
                sqlite_command.backup_command, "create_backup", return_value=missing_backup
            ) as create_backup, mock.patch.object(
                sqlite_command.json_import, "import_json_files"
            ) as import_json_files, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    sqlite_command.cutover(yes=True)

            self.assertEqual(caught.exception.code, 1)
            stop_writers.assert_called_once_with()
            verify_writers_stopped.assert_called_once_with()
            start_writers.assert_called_once_with()
            create_backup.assert_called_once_with(path_only=False, quiet=True, sync=False)
            import_json_files.assert_not_called()

    def test_verify_backup_file_rejects_empty_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "empty.tar.gz"
            path.write_bytes(b"")

            with self.assertRaisesRegex(RuntimeError, "backup is empty"):
                sqlite_command.verify_backup_file(path, "Test")

    def test_cutover_disables_flags_and_restarts_writers_when_cutover_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            env_path = root / "server.env"
            backup_path = root / "backup.tar.gz"
            backup_path.write_bytes(b"backup")
            write_server_env({"SERVER_ADDR": "example.com"}, env_path)

            def import_json_files(*, db_path, replace):
                connection = database.open_database(db_path)
                try:
                    sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
                finally:
                    connection.close()
                return json_import.ImportSummary(counts={"clients": 1})

            with mock.patch.object(sqlite_command, "MANAGER_DB_PATH", db_path), mock.patch.object(
                sqlite_command, "SERVER_ENV_PATH", env_path
            ), mock.patch.object(
                sqlite_command.os, "geteuid", return_value=0
            ), mock.patch.object(
                sqlite_command, "stop_writers"
            ), mock.patch.object(
                sqlite_command, "verify_writers_stopped"
            ), mock.patch.object(
                sqlite_command, "start_writers"
            ) as start_writers, mock.patch.object(
                sqlite_command.backup_command, "create_backup", return_value=backup_path
            ), mock.patch.object(
                sqlite_command.json_import, "import_json_files", side_effect=import_json_files
            ), mock.patch.object(
                sqlite_command, "run_cutover_validation", side_effect=RuntimeError("validation failed")
            ), mock.patch.object(
                sqlite_command, "run_xray_test"
            ) as run_xray_test, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    sqlite_command.cutover(yes=True)

            self.assertEqual(caught.exception.code, 1)
            start_writers.assert_called_once_with()
            run_xray_test.assert_not_called()
            values = read_server_env(env_path)
            self.assertEqual(values["MANAGER_SQLITE_READS_ENABLED"], "false")
            self.assertEqual(values["MANAGER_SQLITE_WRITES_ENABLED"], "false")

    def test_cutover_disables_flags_and_restarts_writers_when_validation_fails_after_enable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            env_path = root / "server.env"
            backup_path = root / "backup.tar.gz"
            backup_path.write_bytes(b"backup")
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
                sqlite_command, "verify_writers_stopped"
            ), mock.patch.object(
                sqlite_command, "start_writers"
            ) as start_writers, mock.patch.object(
                sqlite_command, "verify_writers_started"
            ), mock.patch.object(
                sqlite_command.backup_command, "create_backup", return_value=backup_path
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
