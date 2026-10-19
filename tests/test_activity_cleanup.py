from datetime import date
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.activity import cleanup
from xray_vps_manager.core.errors import StateError
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

    def test_cleanup_truncates_wal_after_vacuum(self) -> None:
        """VIREI-245: в WAL VACUUM пишет копию базы в `-wal`; после cleanup он нулевого размера."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            # Открытое соединение другого процесса: без него последний close сам удалил бы `-wal`.
            reader = sqlite3.connect(str(db_path))
            try:
                reader.execute("PRAGMA journal_mode = WAL")
                reader.execute("SELECT COUNT(*) FROM activity_events").fetchall()

                with mock.patch.object(cleanup.activity_time, "today_utc_date", return_value=date(2026, 6, 12)):
                    cleanup.cleanup_activity_data(
                        retention_days=7,
                        db_path=db_path,
                        backup_dir=root / "backups",
                        run_systemctl=False,
                    )

                wal_path = Path(f"{db_path}-wal")
                self.assertTrue(wal_path.exists())
                self.assertEqual(wal_path.stat().st_size, 0)
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0], 1)
            finally:
                reader.close()

    def test_cleanup_fails_when_reader_blocks_wal_truncate_and_restarts_units(self) -> None:
        """245-R01: занятый checkpoint возвращает busy=1 без исключения; cleanup не должен сообщать успех."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            calls = []
            active = {"xray-traffic-sync.timer", "xray-telegram-poller.service"}

            def runner(cmd, **_kwargs):
                calls.append(cmd)
                if cmd[:3] == ["systemctl", "is-active", "--quiet"]:
                    return subprocess.CompletedProcess(cmd, 0 if cmd[3] in active else 3, "", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            # Читатель вне writer units (ручной sqlite3, xray-test) держит снимок чтения.
            reader = sqlite3.connect(str(db_path))
            try:
                reader.execute("PRAGMA journal_mode = WAL")
                reader.execute("BEGIN")
                reader.execute("SELECT COUNT(*) FROM activity_events").fetchall()

                with mock.patch.object(cleanup.activity_time, "today_utc_date", return_value=date(2026, 6, 12)), mock.patch.object(
                    cleanup, "VACUUM_BUSY_TIMEOUT_SECONDS", 0.1
                ):
                    with self.assertRaisesRegex(RuntimeError, "manager.db-wal was not truncated"):
                        cleanup.cleanup_activity_data(
                            retention_days=7,
                            db_path=db_path,
                            backup_dir=root / "backups",
                            runner=runner,
                        )
            finally:
                reader.rollback()
                reader.close()

            self.assertIn(["systemctl", "stop", *cleanup.WRITER_STOP_UNITS], calls)
            self.assertEqual(calls[-1], ["systemctl", "start", "xray-traffic-sync.timer", "xray-telegram-poller.service"])

    def test_cleanup_without_free_space_fails_before_any_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            backup_dir = root / "backups"
            self.make_sqlite_db(db_path)
            required = 3 * database.database_disk_bytes(db_path)
            calls = []

            def runner(cmd, **_kwargs):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(cleanup.activity_time, "today_utc_date", return_value=date(2026, 6, 12)), mock.patch(
                "shutil.disk_usage", return_value=mock.Mock(free=required - 1)
            ):
                with self.assertRaises(StateError) as caught:
                    cleanup.cleanup_activity_data(
                        retention_days=7,
                        db_path=db_path,
                        backup_dir=backup_dir,
                        runner=runner,
                    )

            self.assertIn("Not enough free disk space", caught.exception.message)
            self.assertEqual(calls, [])
            self.assertFalse(backup_dir.exists())
            self.assertEqual(cleanup.database_stats(db_path, retention_days=7).activity_events, 2)


if __name__ == "__main__":
    unittest.main()
