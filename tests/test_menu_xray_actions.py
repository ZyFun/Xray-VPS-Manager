from contextlib import redirect_stdout
from io import StringIO
import unittest

from xray_vps_manager.commands import menu_xray_actions


class MenuXrayActionsTests(unittest.TestCase):
    def test_sqlite_status_calls_manager_command(self) -> None:
        calls = []

        menu_xray_actions.sqlite_status(calls.append)

        self.assertEqual(calls, [["xray-vps-manager", "sqlite", "status"]])

    def test_sqlite_validate_cutover_calls_manager_command(self) -> None:
        calls = []

        menu_xray_actions.sqlite_validate_cutover(calls.append)

        self.assertEqual(calls, [["xray-vps-manager", "sqlite", "validate-cutover"]])

    def test_sqlite_cleanup_legacy_requires_confirmation(self) -> None:
        calls = []
        stdout = StringIO()

        with redirect_stdout(stdout):
            menu_xray_actions.sqlite_cleanup_legacy(calls.append, lambda _message: False)

        self.assertEqual(calls, [])
        self.assertIn("Очистка legacy SQLite отменена.", stdout.getvalue())

    def test_sqlite_cleanup_legacy_calls_manager_command_after_confirmation(self) -> None:
        calls = []
        stdout = StringIO()

        with redirect_stdout(stdout):
            menu_xray_actions.sqlite_cleanup_legacy(calls.append, lambda _message: True)

        self.assertEqual(calls, [["xray-vps-manager", "sqlite", "cleanup-legacy", "--yes"]])


if __name__ == "__main__":
    unittest.main()
