from pathlib import Path
import tempfile
import unittest

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.repositories import traffic as sqlite_traffic
from xray_vps_manager.traffic import repository as traffic_repository


def traffic_db() -> dict:
    return {
        "accessLog": {
            "path": "/var/log/xray/access.log",
            "inode": 123,
            "offset": 456,
            "updated": "2026-06-12T08:02:00Z",
        },
        "clients": {
            "alice": {
                "email": "alice|created=2026-06-12T08:00:00Z",
                "incoming": 100,
                "outgoing": 200,
                "last": {"uplink": 10, "downlink": 20},
                "lastOnline": "2026-06-12T08:01:00Z",
                "history": {"2026-06-12": {"08": {"incoming": 100, "outgoing": 200}}},
            },
            "unknown_client": {
                "email": "unknown_client",
                "incoming": 1,
                "outgoing": 1,
            },
        },
    }


class TrafficRepositoryWriteTests(unittest.TestCase):
    def make_sqlite_db(self, path: Path, *, ready: bool = True) -> None:
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
            for name, uuid_suffix in (("alice", "001"), ("old_client", "009")):
                sqlite_clients.upsert_client(
                    connection,
                    name,
                    {
                        "id": f"00000000-0000-0000-0000-000000000{uuid_suffix}",
                        "created": "2026-06-12T07:01:00Z",
                        "enabled": True,
                        "connection": "vless-reality",
                        "client": {
                            "id": f"00000000-0000-0000-0000-000000000{uuid_suffix}",
                            "email": f"{name}|created=2026-06-12T07:01:00Z",
                        },
                    },
                )
            sqlite_traffic.upsert_traffic_entry(
                connection,
                "old_client",
                {
                    "email": "old_client|created=2026-06-12T07:01:00Z",
                    "incoming": 300,
                    "outgoing": 400,
                    "history": {"2026-06-12": {"07": {"incoming": 300, "outgoing": 400}}},
                },
            )
            if ready:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def test_save_writes_traffic_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            json_path = Path(tmp_dir) / "traffic.json"
            self.make_sqlite_db(db_path)

            traffic_repository.save_traffic_db(traffic_db(), json_path, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                entries = sqlite_traffic.list_traffic_entries(connection)
                access_log_state = sqlite_traffic.get_access_log_state(connection)
            finally:
                connection.close()
            self.assertEqual(set(entries), {"alice"})
            self.assertEqual(entries["alice"]["incoming"], 100)
            self.assertEqual(entries["alice"]["outgoing"], 200)
            self.assertEqual(entries["alice"]["history"]["2026-06-12"]["08"], {"incoming": 100, "outgoing": 200})
            self.assertEqual(access_log_state["offset"], 456)
            self.assertFalse(json_path.exists())

    def test_remove_traffic_clients_removes_from_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            json_path = Path(tmp_dir) / "traffic.json"
            self.make_sqlite_db(db_path)

            removed = traffic_repository.remove_traffic_clients(["old_client"], json_path, db_path=db_path)

            self.assertTrue(removed)
            connection = database.open_database(db_path)
            try:
                entries = sqlite_traffic.list_traffic_entries(connection)
            finally:
                connection.close()
            self.assertEqual(entries, {})
            self.assertFalse(json_path.exists())

    def test_save_fails_when_sqlite_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "missing.db"
            json_path = Path(tmp_dir) / "traffic.json"

            with self.assertRaisesRegex(RuntimeError, "manager database is missing"):
                traffic_repository.save_traffic_db(traffic_db(), json_path, db_path=db_path)

            self.assertFalse(json_path.exists())
            self.assertFalse(db_path.exists())

    def test_save_fails_when_sqlite_database_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            json_path = Path(tmp_dir) / "traffic.json"
            self.make_sqlite_db(db_path, ready=False)

            with self.assertRaisesRegex(RuntimeError, "database is not marked ready"):
                traffic_repository.save_traffic_db(traffic_db(), json_path, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                entries = sqlite_traffic.list_traffic_entries(connection)
            finally:
                connection.close()
            self.assertEqual(set(entries), {"old_client"})
            self.assertFalse(json_path.exists())


if __name__ == "__main__":
    unittest.main()
