import unittest
import tempfile
from pathlib import Path
from unittest import mock

from xray_vps_manager.activity import sync as activity_sync


class ActivitySyncTests(unittest.TestCase):
    def test_known_clients_uses_client_read_switch(self) -> None:
        client_db = {
            "clients": {
                "sqlite_client": {
                    "connection": "sqlite-connection",
                    "client": {"email": "sqlite_client|created=2026-06-12T07:01:00Z"},
                }
            }
        }

        with mock.patch.object(activity_sync.repository, "load_json", return_value={}), \
            mock.patch.object(activity_sync.client_repository, "load_db_sql", return_value=client_db) as load_db:
            clients = activity_sync.known_clients()

        load_db.assert_called_once_with()
        self.assertEqual(
            clients,
            {
                "sqlite_client|created=2026-06-12T07:01:00Z": {
                    "client": "sqlite_client",
                    "email": "sqlite_client|created=2026-06-12T07:01:00Z",
                    "connection": "sqlite-connection",
                },
                "sqlite_client": {
                    "client": "sqlite_client",
                    "email": "sqlite_client|created=2026-06-12T07:01:00Z",
                    "connection": "sqlite-connection",
                }
            },
        )

    def test_sync_records_counters_and_alerts_when_detailed_mode_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            access_log = Path(tmp_dir) / "access.log"
            access_log.write_text(
                "2026/06/12 08:00:00 accepted tcp:example.ru:443 "
                "[vless-reality -> geoip-warning-RU] email: alice|created=2026-06-12T07:01:00Z\n"
            )
            stat = access_log.stat()
            db = {"accessLog": {"inode": stat.st_ino, "offset": 0}, "clients": {}}
            calls = []

            with (
                mock.patch.object(activity_sync, "ACCESS_LOG_PATH", access_log),
                mock.patch.object(
                    activity_sync,
                    "known_clients",
                    return_value={
                        "alice|created=2026-06-12T07:01:00Z": {
                            "client": "alice",
                            "email": "alice|created=2026-06-12T07:01:00Z",
                            "connection": "vless-reality",
                        }
                    },
                ),
                mock.patch.object(activity_sync.settings, "activity_enabled", return_value=False),
                mock.patch.object(activity_sync.settings, "alerts_enabled", return_value=True),
                mock.patch.object(activity_sync.settings, "retention_days", return_value=365),
                mock.patch.object(activity_sync.settings, "alert_retention_days", return_value=90),
                mock.patch.object(activity_sync.settings, "xray_error_event_retention_days", return_value=180),
                mock.patch.object(activity_sync.repository, "ensure_dirs"),
                mock.patch.object(
                    activity_sync.repository,
                    "detail_capture_status_for_read",
                    return_value={"mode": "off", "selectedClients": []},
                ),
                mock.patch.object(activity_sync.repository, "load_activity_db", return_value=db),
                mock.patch.object(activity_sync.repository, "save_activity_db"),
                mock.patch.object(activity_sync.repository, "prune_activity", return_value=0),
                mock.patch.object(activity_sync.repository, "prune_alerts_for_write", return_value=0),
                mock.patch.object(activity_sync.repository, "prune_xray_errors_for_write", return_value=0),
                mock.patch.object(activity_sync.activity_blocklist, "reconcile_xray_config", return_value=None),
                mock.patch.object(
                    activity_sync.repository,
                    "record_pipeline_event_for_write",
                    side_effect=lambda event, **kwargs: calls.append((event, kwargs))
                    or {"storedDetail": False, "storedAlerts": 1, "storedCounters": True},
                ),
            ):
                result = activity_sync.sync_activity(lambda _message: None)

            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 1)
            event, kwargs = calls[0]
            self.assertEqual(event["client"], "alice")
            self.assertEqual(event["risks"], ["xray-geoip:RU"])
            self.assertEqual(kwargs["detail_mode"], "off")
            self.assertTrue(kwargs["alerts_enabled"])
            self.assertEqual(db["accessLog"]["offset"], access_log.stat().st_size)
            self.assertFalse(db["enabled"])

    def test_sync_runs_maintenance_when_no_clients_are_known(self) -> None:
        messages: list[str] = []

        with (
            mock.patch.object(activity_sync, "known_clients", return_value={}),
            mock.patch.object(activity_sync.repository, "ensure_dirs"),
            mock.patch.object(activity_sync.settings, "alert_retention_days", return_value=90),
            mock.patch.object(activity_sync.settings, "xray_error_event_retention_days", return_value=180),
            mock.patch.object(activity_sync.repository, "prune_alerts_for_write", return_value=2) as prune_alerts,
            mock.patch.object(activity_sync.raw_logs, "sync_error_log", return_value=0) as sync_error_log,
            mock.patch.object(activity_sync.repository, "prune_xray_errors_for_write", return_value=3) as prune_errors,
            mock.patch.object(activity_sync.activity_blocklist, "reconcile_xray_config", return_value=None) as reconcile,
        ):
            result = activity_sync.sync_activity(messages.append)

        self.assertEqual(result, 0)
        prune_alerts.assert_called_once()
        sync_error_log.assert_called_once_with(messages.append)
        prune_errors.assert_called_once()
        reconcile.assert_called_once_with()
        self.assertTrue(any("No clients found" in message for message in messages))
        self.assertTrue(any("2 alerts pruned" in message for message in messages))
        self.assertTrue(any("3 errors pruned" in message for message in messages))

    def test_sync_runs_maintenance_when_access_log_is_missing(self) -> None:
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_access_log = Path(tmp_dir) / "access.log"

            with (
                mock.patch.object(activity_sync, "ACCESS_LOG_PATH", missing_access_log),
                mock.patch.object(activity_sync, "known_clients", return_value={"alice": {"client": "alice"}}),
                mock.patch.object(activity_sync.repository, "ensure_dirs"),
                mock.patch.object(activity_sync.settings, "alert_retention_days", return_value=90),
                mock.patch.object(activity_sync.settings, "xray_error_event_retention_days", return_value=180),
                mock.patch.object(activity_sync.repository, "prune_alerts_for_write", return_value=0) as prune_alerts,
                mock.patch.object(activity_sync.raw_logs, "sync_error_log", return_value=0) as sync_error_log,
                mock.patch.object(activity_sync.repository, "prune_xray_errors_for_write", return_value=0) as prune_errors,
                mock.patch.object(activity_sync.activity_blocklist, "reconcile_xray_config", return_value=None) as reconcile,
            ):
                result = activity_sync.sync_activity(messages.append)

        self.assertEqual(result, 0)
        prune_alerts.assert_called_once()
        sync_error_log.assert_called_once_with(messages.append)
        prune_errors.assert_called_once()
        reconcile.assert_called_once_with()
        self.assertTrue(any("Access log not found" in message for message in messages))

    def test_sync_does_not_advance_offset_when_pipeline_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            access_log = Path(tmp_dir) / "access.log"
            access_log.write_text(
                "2026/06/12 08:00:00 accepted tcp:example.ru:443 "
                "[vless-reality -> geoip-warning-RU] email: alice|created=2026-06-12T07:01:00Z\n"
            )
            stat = access_log.stat()
            db = {"accessLog": {"inode": stat.st_ino, "offset": 0}, "clients": {}}
            messages: list[str] = []

            with (
                mock.patch.object(activity_sync, "ACCESS_LOG_PATH", access_log),
                mock.patch.object(
                    activity_sync,
                    "known_clients",
                    return_value={
                        "alice|created=2026-06-12T07:01:00Z": {
                            "client": "alice",
                            "email": "alice|created=2026-06-12T07:01:00Z",
                            "connection": "vless-reality",
                        }
                    },
                ),
                mock.patch.object(activity_sync.settings, "activity_enabled", return_value=False),
                mock.patch.object(activity_sync.settings, "alerts_enabled", return_value=True),
                mock.patch.object(activity_sync.settings, "retention_days", return_value=365),
                mock.patch.object(activity_sync.repository, "ensure_dirs"),
                mock.patch.object(
                    activity_sync.repository,
                    "detail_capture_status_for_read",
                    return_value={"mode": "off", "selectedClients": []},
                ),
                mock.patch.object(activity_sync.repository, "load_activity_db", return_value=db),
                mock.patch.object(activity_sync.repository, "save_activity_db") as save_activity_db,
                mock.patch.object(
                    activity_sync.repository,
                    "record_pipeline_event_for_write",
                    side_effect=RuntimeError("sqlite write failed"),
                ),
            ):
                result = activity_sync.sync_activity(messages.append)

            self.assertEqual(result, 1)
            self.assertEqual(db["accessLog"]["offset"], 0)
            save_activity_db.assert_not_called()
            self.assertTrue(any("offset was not advanced" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
