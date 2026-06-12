from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings


def activity_event(client: str = "sqlite_client") -> dict:
    return {
        "time": "2026-06-12T08:00:00Z",
        "client": client,
        "email": f"{client}|created=2026-06-12T07:00:00Z",
        "connection": "vless-reality",
        "host": "example.com",
        "port": "443",
        "outbound": "cascade-upstream",
        "risks": ["xray-geoip:RU"],
    }


def exception_db() -> dict:
    return {
        "version": 1,
        "items": [
            {
                "value": "*.example.com",
                "kind": "mask",
                "source": "manual",
                "createdAt": "2026-06-12T08:01:00Z",
            }
        ],
    }


class ActivityRepositoryWriteSwitchTests(unittest.TestCase):
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
            sqlite_clients.upsert_client(
                connection,
                "sqlite_client",
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "created": "2026-06-12T07:01:00Z",
                    "enabled": True,
                    "connection": "vless-reality",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "email": "sqlite_client|created=2026-06-12T07:01:00Z",
                    },
                },
            )
            sqlite_activity.upsert_exception(
                connection,
                {
                    "value": "old.example.com",
                    "kind": "domain",
                    "source": "manual",
                    "createdAt": "2026-06-12T07:30:00Z",
                },
            )
            if ready:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def patch_activity_dirs(self, root: Path):
        return mock.patch.multiple(
            activity_repository,
            ACTIVITY_DIR=root / "activity",
            CLIENT_LOG_DIR=root / "activity" / "clients",
            EXPORT_DIR=root / "exports",
        )

    def test_append_event_writes_jsonl_only_when_sqlite_write_flag_is_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {}, clear=True), self.patch_activity_dirs(root):
                activity_repository.append_event(activity_event(), db_path=db_path)

            self.assertTrue((root / "activity" / "clients" / "sqlite_client.jsonl").exists())
            connection = database.open_database(db_path)
            try:
                events = list(sqlite_activity.iter_events(connection, client_name="sqlite_client"))
            finally:
                connection.close()
            self.assertEqual(events, [])

    def test_append_event_mirrors_event_to_ready_sqlite_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True), self.patch_activity_dirs(root):
                activity_repository.append_event(activity_event(), db_path=db_path)

            connection = database.open_database(db_path)
            try:
                events = list(sqlite_activity.iter_events(connection, client_name="sqlite_client"))
            finally:
                connection.close()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["host"], "example.com")
            self.assertEqual(events[0]["risks"], ["xray-geoip:RU"])

    def test_append_event_skips_sqlite_when_client_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True), self.patch_activity_dirs(root):
                activity_repository.append_event(activity_event("missing_client"), db_path=db_path)

            self.assertTrue((root / "activity" / "clients" / "missing_client.jsonl").exists())
            connection = database.open_database(db_path)
            try:
                events = list(sqlite_activity.iter_events(connection))
            finally:
                connection.close()
            self.assertEqual(events, [])

    def test_save_exceptions_mirrors_current_items_to_ready_sqlite_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            exceptions_path = root / "activity-exceptions.json"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True), self.patch_activity_dirs(root):
                activity_exceptions.save_activity_exceptions(exception_db(), exceptions_path, db_path=db_path)

            self.assertEqual(json.loads(exceptions_path.read_text())["items"][0]["value"], "*.example.com")
            connection = database.open_database(db_path)
            try:
                exceptions = sqlite_activity.list_exceptions(connection)
            finally:
                connection.close()
            self.assertEqual(
                exceptions,
                [{"value": "*.example.com", "kind": "mask", "source": "manual", "createdAt": "2026-06-12T08:01:00Z"}],
            )

    def test_activity_mirror_skips_missing_or_not_ready_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            missing_db_path = root / "missing.db"
            not_ready_db_path = root / "not-ready.db"
            exceptions_path = root / "activity-exceptions.json"
            self.make_sqlite_db(not_ready_db_path, ready=False)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True), self.patch_activity_dirs(root):
                activity_repository.append_event(activity_event(), db_path=missing_db_path)
                activity_exceptions.save_activity_exceptions(exception_db(), exceptions_path, db_path=not_ready_db_path)

            self.assertFalse(missing_db_path.exists())
            connection = database.open_database(not_ready_db_path)
            try:
                self.assertEqual(sqlite_activity.list_exceptions(connection)[0]["value"], "old.example.com")
                self.assertEqual(list(sqlite_activity.iter_events(connection)), [])
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
