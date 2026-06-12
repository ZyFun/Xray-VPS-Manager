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


def traffic_db() -> dict:
    return {
        "accessLog": {
            "path": "/var/log/xray/access.log",
            "inode": 123,
            "offset": 456,
            "updated": "2026-06-12T08:02:00Z",
        },
        "clients": {
            "json_client": {
                "email": "json_client|created=2026-06-12T08:00:00Z",
                "incoming": 100,
                "outgoing": 200,
                "last": {"uplink": 10, "downlink": 20},
                "lastOnline": "2026-06-12T08:01:00Z",
                "history": {"2026-06-12": {"08": {"incoming": 100, "outgoing": 200}}},
            },
            "stale_json_client": {
                "email": "stale_json_client",
                "incoming": 1,
                "outgoing": 1,
            },
        }
    }


class TrafficRepositoryWriteSwitchTests(unittest.TestCase):
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
            for name, uuid_suffix in (("json_client", "001"), ("old_client", "009")):
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

    def read_json_file(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def write_json_file(self, path: Path, db: dict) -> None:
        path.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n")

    def test_save_writes_json_only_when_sqlite_write_flag_is_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "traffic.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                traffic_repository.save_traffic_db(traffic_db(), json_path, db_path=db_path)

            self.assertIn("json_client", self.read_json_file(json_path)["clients"])
            connection = database.open_database(db_path)
            try:
                entries = sqlite_traffic.list_traffic_entries(connection)
            finally:
                connection.close()
            self.assertEqual(set(entries), {"old_client"})

    def test_save_mirrors_traffic_to_ready_sqlite_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "traffic.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True):
                traffic_repository.save_traffic_db(traffic_db(), json_path, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                entries = sqlite_traffic.list_traffic_entries(connection)
                access_log_state = sqlite_traffic.get_access_log_state(connection)
            finally:
                connection.close()
            self.assertEqual(set(entries), {"json_client"})
            self.assertEqual(entries["json_client"]["incoming"], 100)
            self.assertEqual(entries["json_client"]["outgoing"], 200)
            self.assertEqual(entries["json_client"]["history"]["2026-06-12"]["08"], {"incoming": 100, "outgoing": 200})
            self.assertEqual(
                access_log_state,
                {
                    "path": "/var/log/xray/access.log",
                    "inode": 123,
                    "offset": 456,
                    "updated": "2026-06-12T08:02:00Z",
                },
            )
            self.assertIn("json_client", self.read_json_file(json_path)["clients"])

    def test_save_uses_sqlite_as_primary_when_read_and_write_flags_are_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "traffic.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            self.write_json_file(
                json_path,
                {
                    "clients": {
                        "rollback_client": {
                            "email": "rollback_client",
                            "incoming": 1,
                            "outgoing": 2,
                        }
                    }
                },
            )

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                traffic_repository.save_traffic_db(traffic_db(), json_path, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                entries = sqlite_traffic.list_traffic_entries(connection)
                access_log_state = sqlite_traffic.get_access_log_state(connection)
            finally:
                connection.close()
            self.assertEqual(set(entries), {"json_client"})
            self.assertEqual(entries["json_client"]["incoming"], 100)
            self.assertEqual(entries["json_client"]["outgoing"], 200)
            self.assertEqual(access_log_state["offset"], 456)
            self.assertEqual(set(self.read_json_file(json_path)["clients"]), {"rollback_client"})

    def test_save_does_not_create_missing_sqlite_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "traffic.json"
            missing_db_path = root / "missing.db"

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True):
                traffic_repository.save_traffic_db(traffic_db(), json_path, db_path=missing_db_path)

            self.assertIn("json_client", self.read_json_file(json_path)["clients"])
            self.assertFalse(missing_db_path.exists())

    def test_save_fails_when_sqlite_primary_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "traffic.json"
            missing_db_path = root / "missing.db"

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "manager database is missing"):
                    traffic_repository.save_traffic_db(traffic_db(), json_path, db_path=missing_db_path)

            self.assertFalse(json_path.exists())
            self.assertFalse(missing_db_path.exists())

    def test_save_skips_sqlite_mirror_when_import_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "traffic.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path, ready=False)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True):
                traffic_repository.save_traffic_db(traffic_db(), json_path, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                entries = sqlite_traffic.list_traffic_entries(connection)
            finally:
                connection.close()
            self.assertEqual(set(entries), {"old_client"})

    def test_save_fails_when_sqlite_primary_import_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "traffic.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path, ready=False)

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "JSON import is not marked ready"):
                    traffic_repository.save_traffic_db(traffic_db(), json_path, db_path=db_path)

            self.assertFalse(json_path.exists())
            connection = database.open_database(db_path)
            try:
                entries = sqlite_traffic.list_traffic_entries(connection)
            finally:
                connection.close()
            self.assertEqual(set(entries), {"old_client"})

    def test_remove_traffic_clients_uses_json_when_sqlite_primary_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "traffic.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            self.write_json_file(json_path, traffic_db())

            with mock.patch.dict(os.environ, {}, clear=True):
                removed = traffic_repository.remove_traffic_clients(["json_client"], json_path, db_path=db_path)

            self.assertTrue(removed)
            self.assertNotIn("json_client", self.read_json_file(json_path)["clients"])
            connection = database.open_database(db_path)
            try:
                entries = sqlite_traffic.list_traffic_entries(connection)
            finally:
                connection.close()
            self.assertEqual(set(entries), {"old_client"})

    def test_remove_traffic_clients_uses_sqlite_as_primary_when_flags_are_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "traffic.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            self.write_json_file(json_path, traffic_db())

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                removed = traffic_repository.remove_traffic_clients(["old_client"], json_path, db_path=db_path)

            self.assertTrue(removed)
            self.assertIn("json_client", self.read_json_file(json_path)["clients"])
            connection = database.open_database(db_path)
            try:
                entries = sqlite_traffic.list_traffic_entries(connection)
            finally:
                connection.close()
            self.assertEqual(entries, {})

    def test_remove_traffic_clients_fails_when_sqlite_primary_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "traffic.json"
            missing_db_path = root / "missing.db"
            self.write_json_file(json_path, traffic_db())

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "manager database is missing"):
                    traffic_repository.remove_traffic_clients(["json_client"], json_path, db_path=missing_db_path)

            self.assertIn("json_client", self.read_json_file(json_path)["clients"])
            self.assertFalse(missing_db_path.exists())


if __name__ == "__main__":
    unittest.main()
