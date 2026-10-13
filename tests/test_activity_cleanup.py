from datetime import date
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.activity import cleanup
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings


def activity_event(moment: str) -> dict:
    return {
        "time": moment,
        "client": "alice",
        "email": "alice|created=2026-06-12T07:00:00Z",
        "connection": "vless-reality",
        "host": "example.com",
        "port": "443",
        "outbound": "direct",
        "risks": ["xray-geoip:RU"],
    }


class ActivityCleanupTests(unittest.TestCase):
    def make_sqlite_db(self, path: Path) -> None:
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
            sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
            sqlite_activity.add_event(connection, activity_event("2026-06-01T08:00:00Z"))
            sqlite_activity.add_event(connection, activity_event("2026-06-12T08:00:00Z"))
        finally:
            connection.close()

    def test_cleanup_prunes_old_events_backs_up_vacuums_and_restarts_active_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            backup_dir = root / "backups"
            self.make_sqlite_db(db_path)
            calls = []
            active = {"xray-traffic-sync.timer", "xray-telegram-poller.service"}

            def runner(cmd, **_kwargs):
                calls.append(cmd)
                if cmd[:3] == ["systemctl", "is-active", "--quiet"]:
                    return subprocess.CompletedProcess(cmd, 0 if cmd[3] in active else 3, "", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(cleanup.activity_time, "today_utc_date", return_value=date(2026, 6, 12)):
                result = cleanup.cleanup_activity_data(
                    retention_days=7,
                    db_path=db_path,
                    backup_dir=backup_dir,
                    runner=runner,
                )

            self.assertEqual(result.removed_events, 1)
            self.assertEqual(result.after.activity_events, 1)
            self.assertEqual(result.after.old_activity_events, 0)
            self.assertEqual(result.quick_check, "ok")
            self.assertTrue(result.backup_path and result.backup_path.exists())
            self.assertIn(["systemctl", "stop", *cleanup.WRITER_STOP_UNITS], calls)
            self.assertIn(["systemctl", "start", "xray-traffic-sync.timer", "xray-telegram-poller.service"], calls)


if __name__ == "__main__":
    unittest.main()
