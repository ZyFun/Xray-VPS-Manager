import unittest
from unittest import mock

from xray_vps_manager.commands import menu_status


class MenuStatusTests(unittest.TestCase):
    def test_manager_updated_uses_manager_timezone(self) -> None:
        with mock.patch.object(menu_status, "read_server_env", return_value={"MANAGER_TIMEZONE": "Europe/Moscow"}):
            self.assertEqual(
                menu_status.manager_updated_header_value("2026-06-13 11:58 UTC"),
                "2026-06-13 14:58 MSK",
            )

    def test_manager_updated_keeps_unparseable_value(self) -> None:
        with mock.patch.object(menu_status, "read_server_env", return_value={"MANAGER_TIMEZONE": "Europe/Moscow"}):
            self.assertEqual(
                menu_status.manager_updated_header_value("manual-build"),
                "manual-build",
            )


if __name__ == "__main__":
    unittest.main()
