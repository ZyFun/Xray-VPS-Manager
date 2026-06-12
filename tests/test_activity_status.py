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


if __name__ == "__main__":
    unittest.main()
