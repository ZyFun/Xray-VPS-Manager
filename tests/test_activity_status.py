import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xray_vps_manager.activity import status as activity_status


class ActivityStatusTests(unittest.TestCase):
    def test_manager_db_status_reports_sqlite_database_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            db_path.write_bytes(b"x" * 1536)

            with mock.patch.object(activity_status, "MANAGER_DB_PATH", db_path):
                self.assertEqual(activity_status.manager_db_status(), f"{db_path}, 1.50KB")

    def test_manager_db_status_reports_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"

            with mock.patch.object(activity_status, "MANAGER_DB_PATH", db_path):
                self.assertEqual(activity_status.manager_db_status(), f"{db_path}, missing")

    def test_status_rows_does_not_scan_geoip_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            db_path.write_text("db")
            geoip_path = Path(tmp_dir) / "geoip.dat"
            geoip_path.write_text("geoip")

            with (
                mock.patch.object(activity_status, "MANAGER_DB_PATH", db_path),
                mock.patch.object(activity_status.repository, "load_activity_db", return_value={"lastSync": "now"}),
                mock.patch.object(activity_status.repository, "load_json", return_value={"log": {"access": "/var/log/xray/access.log"}}),
                mock.patch.object(activity_status.activity_exceptions, "exception_items_for_read", return_value=[]),
                mock.patch.object(activity_status.activity_blocklist, "block_items", return_value=[]),
                mock.patch.object(activity_status.activity_blocklist, "active_block_items", return_value=[]),
                mock.patch.object(activity_status.settings, "activity_enabled", return_value=True),
                mock.patch.object(activity_status.settings, "retention_days", return_value=60),
                mock.patch.object(
                    activity_status.settings,
                    "risk_limits",
                    return_value={"burstEvents": 100, "burstWindowMinutes": 10, "uniqueHosts": 50, "uniquePorts": 20},
                ),
                mock.patch.object(activity_status.settings, "xray_geoip_warning_code", return_value="RU"),
                mock.patch.object(activity_status.activity_parser, "geoip_path", return_value=geoip_path),
                mock.patch.object(
                    activity_status.activity_parser,
                    "available_geoip_codes",
                    side_effect=AssertionError("status must not scan geoip.dat"),
                ),
            ):
                rows, warnings = activity_status.status_rows()

            self.assertEqual(warnings, [])
            self.assertIn(["Xray route GeoIP warnings", "RU"], rows)
            self.assertIn(["GeoIP data", str(geoip_path)], rows)


if __name__ == "__main__":
    unittest.main()
