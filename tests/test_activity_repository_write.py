from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import settings as activity_settings
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
            # Устаревшая сводка, которую менеджер больше не ведёт (VIREI-248): load её игнорирует, save удаляет.
            sqlite_activity.set_metadata_json(
                connection, sqlite_activity.ACTIVITY_SUMMARY_KEY, {"alice": {"totalEvents": 1}}
            )
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

    def test_strict_pipeline_write_skips_client_missing_from_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)

            result = activity_repository.record_pipeline_event_for_write(
                activity_event("missing_client"),
                detail_mode="all",
                selected_clients=[],
                alerts_enabled=True,
                db_path=db_path,
                strict=True,
            )

            self.assertEqual(result, {"storedDetail": False, "storedAlerts": 0, "storedCounters": False})
            connection = database.open_database(db_path)
            try:
                self.assertEqual(list(sqlite_activity.iter_events(connection)), [])
                for table in ("activity_client_counters", "activity_alert_events"):
                    self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            finally:
                connection.close()

    def test_strict_pipeline_write_fails_for_missing_or_not_ready_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_db_path = Path(tmp_dir) / "missing.db"
            not_ready_db_path = Path(tmp_dir) / "not-ready.db"
            self.make_sqlite_db(not_ready_db_path, ready=False)

            for db_path, error in (
                (missing_db_path, "manager database is missing"),
                (not_ready_db_path, "database is not marked ready"),
            ):
                with self.subTest(db_path=db_path.name), self.assertRaisesRegex(RuntimeError, error):
                    activity_repository.record_pipeline_event_for_write(
                        activity_event(),
                        detail_mode="all",
                        selected_clients=[],
                        alerts_enabled=True,
                        db_path=db_path,
                        strict=True,
                    )

            self.assertFalse(missing_db_path.exists())

    def test_pipeline_write_uses_window_state_limits_and_timezone_from_caller(self) -> None:
        """VIREI-246: sync передаёт состояние окон, лимиты и часовой пояс, прочитанные один раз за прогон."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)
            window_state: dict = {}
            limits = {"burstEvents": 1, "burstWindowMinutes": 15, "uniqueHosts": 100, "uniquePorts": 100}

            with mock.patch.object(activity_repository, "manager_timezone", side_effect=AssertionError("timezone read per event")), \
                    mock.patch.object(activity_settings, "risk_limits", side_effect=AssertionError("limits read per event")):
                result = activity_repository.record_pipeline_event_for_write(
                    activity_event(),
                    detail_mode="off",
                    selected_clients=[],
                    alerts_enabled=True,
                    window_state=window_state,
                    risk_limits=limits,
                    display_tz=timezone.utc,
                    db_path=db_path,
                    strict=True,
                )

            self.assertEqual(result, {"storedDetail": False, "storedAlerts": 2, "storedCounters": True})
            self.assertEqual(window_state["clients"]["alice"]["eventTimes"], ["2026-06-12T08:00:00Z"])
            connection = database.open_database(db_path)
            try:
                self.assertEqual(
                    {row["risk"] for row in sqlite_activity.list_alert_events(connection, limit=10)},
                    {"xray-geoip:RU", "burst"},
                )
                self.assertEqual(
                    sqlite_activity.get_metadata_json(connection, sqlite_activity.ACTIVITY_ALERT_WINDOW_STATE_KEY),
                    {},
                )
            finally:
                connection.close()

    def test_save_activity_db_writes_alert_windows_only_when_loaded(self) -> None:
        """VIREI-246: `alertWindows` сохраняется вместе с summary и offset; снимок без ключа строку не трогает."""
        key = sqlite_activity.ACTIVITY_ALERT_WINDOW_STATE_KEY
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)
            connection = database.open_database(db_path)
            try:
                sqlite_activity.set_metadata_json(
                    connection, key, {"clients": {"alice": {"eventTimes": ["2026-06-12T07:30:00Z"]}}}
                )
            finally:
                connection.close()

            plain = activity_repository.load_activity_db(30, True, db_path=db_path)
            db = activity_repository.load_activity_db(30, True, db_path=db_path, with_alert_windows=True)

            self.assertNotIn("alertWindows", plain)
            self.assertEqual(db["alertWindows"], {"clients": {"alice": {"eventTimes": ["2026-06-12T07:30:00Z"]}}})

            db["alertWindows"]["clients"]["alice"]["eventTimes"].append("2026-06-12T08:00:00Z")
            db["accessLog"]["offset"] = 2000
            activity_repository.save_activity_db(db, db_path=db_path)
            plain["accessLog"]["offset"] = 3000
            activity_repository.save_activity_db(plain, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                self.assertEqual(
                    sqlite_activity.get_metadata_json(connection, key)["clients"]["alice"]["eventTimes"],
                    ["2026-06-12T07:30:00Z", "2026-06-12T08:00:00Z"],
                )
                self.assertEqual(sqlite_activity.get_access_log_state(connection)["offset"], 3000)
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

    def test_load_activity_db_reads_offset_from_sqlite_and_ignores_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)

            db = activity_repository.load_activity_db(30, True, db_path=db_path)

            # Изменённое ожидание VIREI-248: сводка по клиентам больше не читается.
            self.assertEqual(db["clients"], {})
            self.assertEqual(db["accessLog"]["offset"], 1000)
            self.assertEqual(db["retentionDays"], 30)
            self.assertEqual(db["enabled"], True)
            self.assertEqual(db["lastSync"], "2026-06-12T07:40:00Z")

    def test_save_activity_db_writes_offset_and_removes_summary(self) -> None:
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
                # Изменённое ожидание VIREI-248: вместо записи сводки сохранение удаляет накопленную.
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM manager_metadata WHERE key = ?", (sqlite_activity.ACTIVITY_SUMMARY_KEY,)
                    ).fetchone()
                )
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
            db = {}

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
            # Изменённое ожидание VIREI-248: обрезки сводки в памяти больше нет, только lastPrune.
            self.assertIn("lastPrune", db)

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
