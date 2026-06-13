from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest

from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings


def activity_event(client: str = "alice") -> dict:
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


class ActivityRepositoryWriteTests(unittest.TestCase):
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
                "alice",
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "created": "2026-06-12T07:01:00Z",
                    "enabled": True,
                    "connection": "vless-reality",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "email": "alice|created=2026-06-12T07:01:00Z",
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
            sqlite_activity.set_summary(connection, {"alice": {"totalEvents": 1}})
            sqlite_activity.set_source_metadata(
                connection,
                {
                    "version": 1,
                    "enabled": True,
                    "retentionDays": 365,
                    "lastSync": "2026-06-12T07:40:00Z",
                    "lastPrune": "2026-06-12T07:40:00Z",
                },
            )
            sqlite_activity.upsert_access_log_state(
                connection,
                {
                    "path": "/var/log/xray/access.log",
                    "inode": 10,
                    "offset": 1000,
                    "updated": "2026-06-12T07:40:00Z",
                },
            )
            if ready:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def test_append_event_writes_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)

            activity_repository.append_event(activity_event(), db_path=db_path)

            connection = database.open_database(db_path)
            try:
                events = list(sqlite_activity.iter_events(connection, client_name="alice"))
            finally:
                connection.close()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["host"], "example.com")
            self.assertEqual(events[0]["risks"], ["xray-geoip:RU"])

    def test_append_event_fails_when_client_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)

            with self.assertRaisesRegex(RuntimeError, "client is missing"):
                activity_repository.append_event(activity_event("missing_client"), db_path=db_path)

            connection = database.open_database(db_path)
            try:
                self.assertEqual(list(sqlite_activity.iter_events(connection)), [])
            finally:
                connection.close()

    def test_save_and_load_exceptions_use_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)

            activity_exceptions.save_activity_exceptions(exception_db(), db_path=db_path)
            loaded = activity_exceptions.load_activity_exceptions(db_path=db_path)

            self.assertEqual(
                loaded["items"],
                [{"value": "*.example.com", "kind": "mask", "source": "manual", "createdAt": "2026-06-12T08:01:00Z"}],
            )

    def test_load_activity_db_reads_summary_and_offset_from_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)

            db = activity_repository.load_activity_db(30, True, db_path=db_path)

            self.assertEqual(db["clients"]["alice"]["totalEvents"], 1)
            self.assertEqual(db["accessLog"]["offset"], 1000)
            self.assertEqual(db["retentionDays"], 30)
            self.assertEqual(db["enabled"], True)
            self.assertEqual(db["lastSync"], "2026-06-12T07:40:00Z")

    def test_save_activity_db_writes_summary_and_offset_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)
            db = {
                "version": 1,
                "enabled": True,
                "retentionDays": 90,
                "lastSync": "2026-06-12T08:10:00Z",
                "lastPrune": "2026-06-12T08:11:00Z",
                "accessLog": {
                    "path": "/var/log/xray/access.log",
                    "inode": 20,
                    "offset": 2000,
                    "updated": "2026-06-12T08:10:00Z",
                },
                "clients": {"alice": {"totalEvents": 2}},
            }

            activity_repository.save_activity_db(db, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                self.assertEqual(sqlite_activity.get_summary(connection)["alice"]["totalEvents"], 2)
                self.assertEqual(sqlite_activity.get_access_log_state(connection)["offset"], 2000)
                self.assertEqual(sqlite_activity.get_source_metadata(connection)["lastSync"], "2026-06-12T08:10:00Z")
            finally:
                connection.close()

    def test_prune_activity_removes_old_sqlite_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)
            connection = database.open_database(db_path)
            try:
                sqlite_activity.add_event(connection, {**activity_event(), "time": "2026-06-01T08:00:00Z"})
                sqlite_activity.add_event(connection, {**activity_event(), "time": "2026-06-12T08:00:00Z"})
            finally:
                connection.close()
            db = {
                "clients": {
                    "alice": {
                        "days": {
                            "2026-06-01": {"events": 1},
                            "2026-06-12": {"events": 1},
                        }
                    }
                }
            }

            removed = activity_repository.prune_activity(
                db,
                7,
                date(2026, 6, 12),
                datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc),
                force=True,
                db_path=db_path,
            )

            connection = database.open_database(db_path)
            try:
                events = list(sqlite_activity.iter_events(connection, client_name="alice"))
            finally:
                connection.close()
            self.assertEqual(removed, 1)
            self.assertEqual([event["time"] for event in events], ["2026-06-12T08:00:00Z"])
            self.assertNotIn("2026-06-01", db["clients"]["alice"]["days"])

    def test_activity_write_fails_for_missing_or_not_ready_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_db_path = Path(tmp_dir) / "missing.db"
            not_ready_db_path = Path(tmp_dir) / "not-ready.db"
            self.make_sqlite_db(not_ready_db_path, ready=False)

            with self.assertRaisesRegex(RuntimeError, "manager database is missing"):
                activity_repository.append_event(activity_event(), db_path=missing_db_path)
            with self.assertRaisesRegex(RuntimeError, "database is not marked ready"):
                activity_exceptions.save_activity_exceptions(exception_db(), db_path=not_ready_db_path)

            self.assertFalse(missing_db_path.exists())


if __name__ == "__main__":
    unittest.main()
