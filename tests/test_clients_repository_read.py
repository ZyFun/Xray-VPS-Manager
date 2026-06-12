from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.storage import sqlite_reads_enabled


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class ClientRepositoryReadSwitchTests(unittest.TestCase):
    def make_json_db(self, path: Path) -> None:
        write_json(
            path,
            {
                "connections": {
                    "json-connection": {
                        "tag": "json-connection",
                        "name": "json",
                        "created": "2026-06-12T08:00:00Z",
                        "port": 443,
                        "sni": "json.example.com",
                        "dest": "json.example.com:443",
                        "fingerprint": "chrome",
                    }
                },
                "clients": {
                    "json_client": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "created": "2026-06-12T08:01:00Z",
                        "enabled": True,
                        "connection": "json-connection",
                        "client": {
                            "id": "00000000-0000-0000-0000-000000000001",
                            "email": "json_client|created=2026-06-12T08:01:00Z",
                        },
                    }
                },
            },
        )

    def make_sqlite_db(self, path: Path) -> None:
        connection = database.open_database(path)
        try:
            sqlite_connections.upsert_connection(
                connection,
                "sqlite-connection",
                {
                    "tag": "sqlite-connection",
                    "name": "sqlite",
                    "created": "2026-06-12T09:00:00Z",
                    "port": 8443,
                    "sni": "sqlite.example.com",
                    "dest": "sqlite.example.com:443",
                    "fingerprint": "safari",
                },
            )
            sqlite_clients.upsert_client(
                connection,
                "sqlite_client",
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "created": "2026-06-12T09:01:00Z",
                    "enabled": True,
                    "connection": "sqlite-connection",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000002",
                        "email": "sqlite_client|created=2026-06-12T09:01:00Z",
                    },
                },
            )
            sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def test_sqlite_reads_are_disabled_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sqlite_reads_enabled())

    def test_read_uses_json_when_sqlite_flag_is_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "clients.json"
            db_path = Path(tmp_dir) / "manager.db"
            self.make_json_db(json_path)
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                result = client_repository.load_db_for_read_result(json_path, db_path=db_path)

            self.assertEqual(result.source, "json")
            self.assertIn("json_client", result.db["clients"])
            self.assertNotIn("sqlite_client", result.db["clients"])

    def test_read_uses_sqlite_when_flag_is_enabled_and_database_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "clients.json"
            db_path = Path(tmp_dir) / "manager.db"
            self.make_json_db(json_path)
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True):
                result = client_repository.load_db_for_read_result(json_path, db_path=db_path)

            self.assertEqual(result.source, "sqlite")
            self.assertIn("sqlite_client", result.db["clients"])
            self.assertNotIn("json_client", result.db["clients"])
            self.assertEqual(result.db["connections"]["sqlite-connection"]["fingerprint"], "safari")

    def test_read_falls_back_to_json_when_sqlite_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "clients.json"
            missing_db_path = Path(tmp_dir) / "missing.db"
            self.make_json_db(json_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True):
                result = client_repository.load_db_for_read_result(json_path, db_path=missing_db_path)

            self.assertEqual(result.source, "json")
            self.assertIn("json_client", result.db["clients"])
            self.assertFalse(missing_db_path.exists())

    def test_read_falls_back_to_json_when_sqlite_import_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "clients.json"
            db_path = Path(tmp_dir) / "manager.db"
            self.make_json_db(json_path)
            connection = database.open_database(db_path)
            try:
                sqlite_connections.upsert_connection(
                    connection,
                    "sqlite-connection",
                    {
                        "tag": "sqlite-connection",
                        "name": "sqlite",
                        "port": 8443,
                        "sni": "sqlite.example.com",
                        "dest": "sqlite.example.com:443",
                        "fingerprint": "safari",
                    },
                )
            finally:
                connection.close()

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True):
                result = client_repository.load_db_for_read_result(json_path, db_path=db_path)

            self.assertEqual(result.source, "json")
            self.assertIn("json_client", result.db["clients"])


if __name__ == "__main__":
    unittest.main()
