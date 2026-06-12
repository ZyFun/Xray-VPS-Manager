from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.repositories import traffic as sqlite_traffic
from xray_vps_manager.traffic import repository as traffic_repository


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class TrafficRepositoryReadSwitchTests(unittest.TestCase):
    def make_json_db(self, path: Path) -> None:
        write_json(
            path,
            {
                "clients": {
                    "json_client": {
                        "email": "json_client|created=2026-06-12T08:00:00Z",
                        "incoming": 100,
                        "outgoing": 200,
                        "last": {"uplink": 100, "downlink": 200},
                        "history": {"2026-06-12": {"08": {"incoming": 100, "outgoing": 200}}},
                    }
                }
            },
        )

    def make_sqlite_db(self, path: Path, *, ready: bool = True) -> None:
        connection = database.open_database(path)
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
            sqlite_clients.upsert_client(
                connection,
                "sqlite_client",
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "created": "2026-06-12T09:00:00Z",
                    "connection": "sqlite-connection",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000002",
                        "email": "sqlite_client|created=2026-06-12T09:00:00Z",
                    },
                },
            )
            sqlite_traffic.upsert_traffic_entry(
                connection,
                "sqlite_client",
                {
                    "email": "sqlite_client|created=2026-06-12T09:00:00Z",
                    "incoming": 300,
                    "outgoing": 400,
                    "last": {"uplink": 300, "downlink": 400},
                    "history": {"2026-06-12": {"09": {"incoming": 300, "outgoing": 400}}},
                },
            )
            if ready:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def test_read_uses_json_when_sqlite_flag_is_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "traffic.json"
            db_path = Path(tmp_dir) / "manager.db"
            self.make_json_db(json_path)
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                result = traffic_repository.load_traffic_db_for_read_result(json_path, db_path=db_path)

            self.assertEqual(result.source, "json")
            self.assertIn("json_client", result.db["clients"])
            self.assertNotIn("sqlite_client", result.db["clients"])

    def test_read_uses_sqlite_when_flag_is_enabled_and_database_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "traffic.json"
            db_path = Path(tmp_dir) / "manager.db"
            self.make_json_db(json_path)
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True):
                result = traffic_repository.load_traffic_db_for_read_result(json_path, db_path=db_path)

            self.assertEqual(result.source, "sqlite")
            self.assertIn("sqlite_client", result.db["clients"])
            self.assertNotIn("json_client", result.db["clients"])
            entry = result.db["clients"]["sqlite_client"]
            self.assertEqual(entry["incoming"], 300)
            self.assertEqual(entry["history"]["2026-06-12"]["09"], {"incoming": 300, "outgoing": 400})

    def test_read_falls_back_to_json_when_sqlite_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "traffic.json"
            missing_db_path = Path(tmp_dir) / "missing.db"
            self.make_json_db(json_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True):
                result = traffic_repository.load_traffic_db_for_read_result(json_path, db_path=missing_db_path)

            self.assertEqual(result.source, "json")
            self.assertIn("json_client", result.db["clients"])
            self.assertFalse(missing_db_path.exists())

    def test_read_falls_back_to_json_when_sqlite_import_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "traffic.json"
            db_path = Path(tmp_dir) / "manager.db"
            self.make_json_db(json_path)
            self.make_sqlite_db(db_path, ready=False)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True):
                result = traffic_repository.load_traffic_db_for_read_result(json_path, db_path=db_path)

            self.assertEqual(result.source, "json")
            self.assertIn("json_client", result.db["clients"])


if __name__ == "__main__":
    unittest.main()
