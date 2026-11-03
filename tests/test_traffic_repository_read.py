from pathlib import Path
import tempfile
import unittest

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import traffic as sqlite_traffic
from xray_vps_manager.db.storage import SQLiteReadUnavailable
from xray_vps_manager.traffic import repository as traffic_repository


class TrafficRepositoryReadTests(unittest.TestCase):
    def make_sqlite_db(self, path: Path) -> None:
        connection = database.initialize(path)
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
            sqlite_traffic.upsert_access_log_state(
                connection,
                {
                    "path": "/var/log/xray/access.log",
                    "inode": 9,
                    "offset": 900,
                    "updated": "2026-06-12T09:02:00Z",
                },
            )
        finally:
            connection.close()

    def test_read_uses_sqlite_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)

            result = traffic_repository.load_traffic_db_for_read(db_path=db_path)

            entry = result["clients"]["sqlite_client"]
            self.assertEqual(entry["incoming"], 300)
            self.assertEqual(entry["history"]["2026-06-12"]["09"], {"incoming": 300, "outgoing": 400})
            self.assertEqual(result["accessLog"]["offset"], 900)

    def test_read_fails_when_sqlite_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_db_path = Path(tmp_dir) / "missing.db"

            with self.assertRaisesRegex(SQLiteReadUnavailable, "manager database is missing"):
                traffic_repository.load_traffic_db_for_read(db_path=missing_db_path)

            self.assertFalse(missing_db_path.exists())

    def test_read_fails_when_sqlite_database_is_uninitialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            db_path.touch()

            with self.assertRaisesRegex(SQLiteReadUnavailable, "database is not initialized"):
                traffic_repository.load_traffic_db_for_read(db_path=db_path)


if __name__ == "__main__":
    unittest.main()
