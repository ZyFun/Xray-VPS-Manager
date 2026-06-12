from pathlib import Path
import os
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

    def test_ready_sqlite_database_passes_with_matching_json_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)
            diag = self.make_diag()

            with mock.patch.object(test_command, "MANAGER_DB_PATH", db_path), mock.patch.dict(os.environ, {}, clear=True):
                result = test_command.check_sqlite_database(diag)

            self.assertIn("schema=1", result)
            self.assertIn("quick_check=ok", result)
            self.assertIn("importReady=yes", result)
            self.assertIn("clients=1", result)

    def test_ready_sqlite_database_reports_alignment_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path, client_name="bob")
            diag = self.make_diag()

            with mock.patch.object(test_command, "MANAGER_DB_PATH", db_path), mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "SQLite clients differ"):
                    test_command.check_sqlite_database(diag)

    def test_enabled_sqlite_flag_requires_completed_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path, ready=False)
            diag = self.make_diag()

            with mock.patch.object(test_command, "MANAGER_DB_PATH", db_path), mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "jsonImport.completed"):
                    test_command.check_sqlite_database(diag)


if __name__ == "__main__":
    unittest.main()
