from pathlib import Path
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.commands import test as test_command
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.repositories import telegram as sqlite_telegram
from xray_vps_manager.db.repositories import traffic as sqlite_traffic


class SQLiteDiagnosticsTests(unittest.TestCase):
    def make_diag(self) -> test_command.Diagnostics:
        diag = test_command.Diagnostics()
        diag.context["client_db"] = {
            "connections": {
                "vless-reality": {},
            },
            "clients": {
                "alice": {},
            },
        }
        diag.context["traffic_db"] = {
            "clients": {
                "alice": {},
            }
        }
        diag.context["activity_exceptions_db"] = {
            "items": [
                {"value": "*.example.com"},
            ]
        }
        diag.context["telegram_bot_db"] = {
            "clientSubscriptions": {
                "111": {"clientId": "00000000-0000-0000-0000-000000000001"},
            }
        }
        return diag

    def make_sqlite_db(self, path: Path, *, ready: bool = True, client_name: str = "alice") -> None:
        connection = database.open_database(path)
        try:
            sqlite_connections.upsert_connection(
                connection,
                "vless-reality",
                {
                    "tag": "vless-reality",
                    "name": "default",
                    "created": "2026-06-12T07:00:00Z",
                    "port": 443,
                    "sni": "example.com",
                    "dest": "example.com:443",
                    "fingerprint": "chrome",
                },
            )
            sqlite_clients.upsert_client(
                connection,
                client_name,
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "created": "2026-06-12T07:01:00Z",
                    "enabled": True,
                    "connection": "vless-reality",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "email": f"{client_name}|created=2026-06-12T07:01:00Z",
                    },
                },
            )
            sqlite_traffic.upsert_traffic_entry(
                connection,
                client_name,
                {
                    "email": f"{client_name}|created=2026-06-12T07:01:00Z",
                    "incoming": 1,
                    "outgoing": 2,
                },
            )
            sqlite_activity.upsert_exception(
                connection,
                {
                    "value": "*.example.com",
                    "kind": "mask",
                    "source": "manual",
                    "createdAt": "2026-06-12T08:00:00Z",
                },
            )
            sqlite_telegram.upsert_subscription(
                connection,
                {
                    "chatId": "111",
                    "clientName": client_name,
                    "clientUuid": "00000000-0000-0000-0000-000000000001",
                    "connection": "vless-reality",
                    "linkSignature": {"linkHash": "hash"},
                    "enabled": True,
                    "createdAt": "2026-06-12T08:01:00Z",
                },
            )
            if ready:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def test_missing_sqlite_database_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "missing.db"
            diag = self.make_diag()

            with mock.patch.object(test_command, "MANAGER_DB_PATH", db_path):
                with self.assertRaisesRegex(RuntimeError, "not found"):
                    test_command.check_sqlite_database(diag)

            self.assertFalse(db_path.exists())

    def test_ready_sqlite_database_skips_full_integrity_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)
            diag = self.make_diag()

            with mock.patch.object(test_command, "MANAGER_DB_PATH", db_path), mock.patch.object(
                test_command.sqlite_database, "quick_check"
            ) as quick_check:
                result = test_command.check_sqlite_database(diag)

            self.assertIn("schema=4", result)
            self.assertIn("quick_check=skipped", result)
            self.assertIn("sqliteReady=yes", result)
            self.assertIn("clients=1", result)
            quick_check.assert_not_called()

    def test_ready_sqlite_database_full_integrity_runs_quick_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)
            diag = self.make_diag()

            with mock.patch.object(test_command, "MANAGER_DB_PATH", db_path):
                result = test_command.check_sqlite_database(diag, full_integrity=True)

            self.assertIn("schema=4", result)
            self.assertIn("quick_check=ok", result)
            self.assertIn("sqliteReady=yes", result)
            self.assertIn("clients=1", result)

    def test_sqlite_full_integrity_reports_quick_check_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)
            diag = self.make_diag()

            with mock.patch.object(test_command, "MANAGER_DB_PATH", db_path), mock.patch.object(
                test_command.sqlite_database, "quick_check", return_value="database disk image is malformed"
            ):
                with self.assertRaisesRegex(RuntimeError, "PRAGMA quick_check returned"):
                    test_command.check_sqlite_database(diag, full_integrity=True)

    def test_main_routes_all_argument_to_full_integrity(self) -> None:
        with mock.patch.object(test_command, "run_diagnostics", return_value=0) as run_diagnostics:
            with mock.patch.object(test_command.sys, "argv", ["xray-test"]):
                with self.assertRaises(SystemExit) as default_exit:
                    test_command.main()
            self.assertEqual(default_exit.exception.code, 0)
            self.assertFalse(run_diagnostics.call_args.kwargs["full_integrity"])

        with mock.patch.object(test_command, "run_diagnostics", return_value=0) as run_diagnostics:
            with mock.patch.object(test_command.sys, "argv", ["xray-test", "--all"]):
                with self.assertRaises(SystemExit) as all_exit:
                    test_command.main()
            self.assertEqual(all_exit.exception.code, 0)
            self.assertTrue(run_diagnostics.call_args.kwargs["full_integrity"])

    def test_ready_sqlite_database_reports_alignment_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)
            diag = self.make_diag()
            diag.context["client_db"]["clients"]["alice"]["connection"] = "missing-connection"

            with mock.patch.object(test_command, "MANAGER_DB_PATH", db_path):
                with self.assertRaisesRegex(RuntimeError, "missing connection"):
                    test_command.check_sqlite_database(diag)

    def test_sqlite_database_requires_read_ready_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path, ready=False)
            diag = self.make_diag()

            with mock.patch.object(test_command, "MANAGER_DB_PATH", db_path):
                with self.assertRaisesRegex(RuntimeError, "read-ready metadata"):
                    test_command.check_sqlite_database(diag)

    def test_sqlite_allows_multiple_telegram_subscriptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)
            connection = database.open_database(db_path)
            try:
                sqlite_telegram.upsert_subscription(
                    connection,
                    {
                        "chatId": "222",
                        "clientName": "alice",
                        "clientUuid": "00000000-0000-0000-0000-000000000002",
                        "connection": "vless-reality",
                        "linkSignature": {"linkHash": "hash-2"},
                        "enabled": True,
                        "createdAt": "2026-06-13T08:01:00Z",
                    },
                )
            finally:
                connection.close()
            diag = self.make_diag()

            with mock.patch.object(test_command, "MANAGER_DB_PATH", db_path):
                result = test_command.check_sqlite_database(diag)

            self.assertIn("telegramSubscriptions=2", result)

    def test_diagnostics_load_runtime_state_from_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)
            diag = test_command.Diagnostics()

            with mock.patch.object(test_command, "MANAGER_DB_PATH", db_path):
                clients_result = test_command.check_client_db(diag)
                traffic_result = test_command.check_traffic_db(diag)
                exceptions_result = test_command.check_activity_exceptions_db(diag)
                telegram_result = test_command.check_telegram_bot_db(diag)

            self.assertIn("clients loaded from SQLite", clients_result)
            self.assertIn("traffic loaded from SQLite", traffic_result)
            self.assertIn("activity exceptions loaded from SQLite", exceptions_result)
            self.assertIn("Telegram settings loaded from SQLite", telegram_result)
            self.assertIn("alice", diag.context["client_db"]["clients"])
            self.assertIn("alice", diag.context["traffic_db"]["clients"])
            self.assertEqual(diag.context["activity_exceptions_db"]["items"][0]["value"], "*.example.com")
            self.assertIn("111", diag.context["telegram_bot_db"]["clientSubscriptions"])


if __name__ == "__main__":
    unittest.main()
