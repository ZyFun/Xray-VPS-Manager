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
from xray_vps_manager.db.storage import sqlite_writes_enabled


def client_db() -> dict:
    return {
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
                "paymentType": "paid",
            }
        },
    }


class ClientRepositoryWriteSwitchTests(unittest.TestCase):
    def make_sqlite_db(self, path: Path, *, ready: bool = True) -> None:
        connection = database.open_database(path)
        try:
            sqlite_connections.upsert_connection(
                connection,
                "old-connection",
                {
                    "tag": "old-connection",
                    "name": "old",
                    "created": "2026-06-12T07:00:00Z",
                    "port": 8443,
                    "sni": "old.example.com",
                    "dest": "old.example.com:443",
                    "fingerprint": "safari",
                },
            )
            sqlite_clients.upsert_client(
                connection,
                "old_client",
                {
                    "id": "00000000-0000-0000-0000-000000000009",
                    "created": "2026-06-12T07:01:00Z",
                    "enabled": True,
                    "connection": "old-connection",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000009",
                        "email": "old_client|created=2026-06-12T07:01:00Z",
                    },
                },
            )
            if ready:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def read_json_file(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def write_json_file(self, path: Path, db: dict) -> None:
        path.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n")

    def save_with_mocked_permissions(self, db: dict, json_path: Path, db_path: Path) -> None:
        with mock.patch.object(client_repository.shutil, "chown"), mock.patch.object(client_repository.os, "chmod"):
            client_repository.save_db(db, json_path, db_path=db_path)

    def test_sqlite_writes_are_disabled_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sqlite_writes_enabled())

    def test_save_writes_json_only_when_sqlite_write_flag_is_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "clients.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                self.save_with_mocked_permissions(client_db(), json_path, db_path)

            self.assertIn("json_client", self.read_json_file(json_path)["clients"])
            connection = database.open_database(db_path)
            try:
                self.assertIn("old_client", sqlite_clients.list_clients(connection))
                self.assertNotIn("json_client", sqlite_clients.list_clients(connection))
            finally:
                connection.close()

    def test_save_writes_clients_and_connections_to_sqlite_when_write_flag_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "clients.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True):
                self.save_with_mocked_permissions(client_db(), json_path, db_path)

            connection = database.open_database(db_path)
            try:
                clients = sqlite_clients.list_clients(connection)
                connections = sqlite_connections.list_connections(connection)
            finally:
                connection.close()
            self.assertEqual(set(clients), {"json_client"})
            self.assertEqual(set(connections), {"json-connection"})
            self.assertEqual(clients["json_client"]["paymentType"], "paid")
            self.assertEqual(connections["json-connection"]["fingerprint"], "chrome")
            self.assertFalse(json_path.exists())

    def test_save_uses_sqlite_as_primary_when_read_and_write_flags_are_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "clients.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            self.write_json_file(
                json_path,
                {
                    "connections": {},
                    "clients": {
                        "rollback_client": {
                            "id": "00000000-0000-0000-0000-000000000099",
                            "created": "2026-06-12T06:00:00Z",
                            "enabled": True,
                            "client": {
                                "id": "00000000-0000-0000-0000-000000000099",
                                "email": "rollback_client",
                            },
                        }
                    },
                },
            )

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                self.save_with_mocked_permissions(client_db(), json_path, db_path)

            connection = database.open_database(db_path)
            try:
                clients = sqlite_clients.list_clients(connection)
                connections = sqlite_connections.list_connections(connection)
            finally:
                connection.close()
            self.assertEqual(set(clients), {"json_client"})
            self.assertEqual(set(connections), {"json-connection"})
            self.assertEqual(set(self.read_json_file(json_path)["clients"]), {"rollback_client"})

    def test_save_fails_when_sqlite_write_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "clients.json"
            missing_db_path = root / "missing.db"

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "manager database is missing"):
                    self.save_with_mocked_permissions(client_db(), json_path, missing_db_path)

            self.assertFalse(json_path.exists())
            self.assertFalse(missing_db_path.exists())

    def test_save_fails_when_sqlite_primary_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "clients.json"
            missing_db_path = root / "missing.db"

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "manager database is missing"):
                    self.save_with_mocked_permissions(client_db(), json_path, missing_db_path)

            self.assertFalse(json_path.exists())
            self.assertFalse(missing_db_path.exists())

    def test_save_fails_when_sqlite_write_import_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "clients.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path, ready=False)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "JSON import is not marked ready"):
                    self.save_with_mocked_permissions(client_db(), json_path, db_path)

            self.assertFalse(json_path.exists())
            connection = database.open_database(db_path)
            try:
                clients = sqlite_clients.list_clients(connection)
                connections = sqlite_connections.list_connections(connection)
            finally:
                connection.close()
            self.assertEqual(set(clients), {"old_client"})
            self.assertEqual(set(connections), {"old-connection"})

    def test_save_fails_when_sqlite_primary_import_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "clients.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path, ready=False)

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "JSON import is not marked ready"):
                    self.save_with_mocked_permissions(client_db(), json_path, db_path)

            self.assertFalse(json_path.exists())
            connection = database.open_database(db_path)
            try:
                clients = sqlite_clients.list_clients(connection)
                connections = sqlite_connections.list_connections(connection)
            finally:
                connection.close()
            self.assertEqual(set(clients), {"old_client"})
            self.assertEqual(set(connections), {"old-connection"})


if __name__ == "__main__":
    unittest.main()
