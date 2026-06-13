from datetime import date
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.storage import SQLiteReadUnavailable


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events)
    )


class ActivityRepositoryReadSwitchTests(unittest.TestCase):
    def make_json_activity(self, root: Path) -> tuple[Path, Path]:
        log_dir = root / "activity" / "clients"
        write_jsonl(
            log_dir / "json_client.jsonl",
            [
                {
                    "time": "2026-06-12T08:00:00Z",
                    "client": "json_client",
                    "host": "json.example.com",
                    "port": "443",
                    "outbound": "direct",
                    "risks": ["xray-geoip:RU"],
                }
            ],
        )
        exceptions_path = root / "activity-exceptions.json"
        write_json(
            exceptions_path,
            {
                "version": 1,
                "items": [
                    {
                        "value": "*.json.example.com",
                        "kind": "mask",
                        "source": "manual",
                        "createdAt": "2026-06-12T08:01:00Z",
                    }
                ],
            },
        )
        return log_dir, exceptions_path

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
            sqlite_activity.add_event(
                connection,
                {
                    "time": "2026-06-12T09:00:00Z",
                    "client": "sqlite_client",
                    "host": "sqlite.example.com",
                    "port": "443",
                    "outbound": "cascade-upstream",
                    "risks": ["xray-geoip:RU"],
                },
            )
            sqlite_activity.upsert_exception(
                connection,
                {
                    "value": "*.sqlite.example.com",
                    "kind": "mask",
                    "source": "manual",
                    "createdAt": "2026-06-12T09:01:00Z",
                },
            )
            if ready:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def read_events(self, client: str, db_path: Path) -> list[dict]:
        return list(
            activity_repository.iter_events_for_read(
                client,
                date(2026, 6, 12),
                date(2026, 6, 12),
                activity_time.parse_time,
                db_path=db_path,
            )
        )

    def test_read_uses_json_when_sqlite_flag_is_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            log_dir, exceptions_path = self.make_json_activity(root)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(activity_repository, "CLIENT_LOG_DIR", log_dir):
                events = self.read_events("json_client", db_path)
                clients = activity_repository.event_client_names_for_read(date(2026, 6, 12), date(2026, 6, 12), db_path=db_path)
                exceptions = activity_exceptions.exception_items_for_read(exceptions_path, db_path=db_path)

            self.assertEqual([event["host"] for event in events], ["json.example.com"])
            self.assertIsNone(clients)
            self.assertEqual(exceptions[0]["value"], "*.json.example.com")

    def test_read_uses_sqlite_when_flag_is_enabled_and_database_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            log_dir, exceptions_path = self.make_json_activity(root)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True), mock.patch.object(activity_repository, "CLIENT_LOG_DIR", log_dir):
                events = self.read_events("sqlite_client", db_path)
                clients = activity_repository.event_client_names_for_read(date(2026, 6, 12), date(2026, 6, 12), db_path=db_path)
                exceptions = activity_exceptions.exception_items_for_read(exceptions_path, db_path=db_path)

            self.assertEqual([event["host"] for event in events], ["sqlite.example.com"])
            self.assertEqual(clients, ["sqlite_client"])
            self.assertEqual(exceptions[0]["value"], "*.sqlite.example.com")

    def test_read_fails_when_sqlite_database_is_missing_and_sqlite_reads_are_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            log_dir, exceptions_path = self.make_json_activity(root)
            missing_db_path = root / "missing.db"

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True), mock.patch.object(activity_repository, "CLIENT_LOG_DIR", log_dir):
                with self.assertRaisesRegex(SQLiteReadUnavailable, "manager database is missing"):
                    self.read_events("json_client", missing_db_path)
                with self.assertRaisesRegex(SQLiteReadUnavailable, "manager database is missing"):
                    activity_repository.event_client_names_for_read(date(2026, 6, 12), date(2026, 6, 12), db_path=missing_db_path)
                with self.assertRaisesRegex(SQLiteReadUnavailable, "manager database is missing"):
                    activity_exceptions.exception_items_for_read(exceptions_path, db_path=missing_db_path)

            self.assertFalse(missing_db_path.exists())

    def test_read_fails_when_sqlite_import_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            log_dir, exceptions_path = self.make_json_activity(root)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path, ready=False)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True), mock.patch.object(activity_repository, "CLIENT_LOG_DIR", log_dir):
                with self.assertRaisesRegex(SQLiteReadUnavailable, "JSON import is not marked ready"):
                    self.read_events("json_client", db_path)
                with self.assertRaisesRegex(SQLiteReadUnavailable, "JSON import is not marked ready"):
                    activity_repository.event_client_names_for_read(date(2026, 6, 12), date(2026, 6, 12), db_path=db_path)
                with self.assertRaisesRegex(SQLiteReadUnavailable, "JSON import is not marked ready"):
                    activity_exceptions.exception_items_for_read(exceptions_path, db_path=db_path)


if __name__ == "__main__":
    unittest.main()
