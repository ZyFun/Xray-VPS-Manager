import unittest

from xray_vps_manager.commands import menu_xray_actions


class MenuXrayActionsTests(unittest.TestCase):
    def test_sqlite_status_calls_manager_command(self) -> None:
        calls = []

        menu_xray_actions.sqlite_status(calls.append)

        self.assertEqual(calls, [["xray-vps-manager", "sqlite", "status"]])


if __name__ == "__main__":
    unittest.main()
