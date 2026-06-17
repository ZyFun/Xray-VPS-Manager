import unittest

from xray_vps_manager.commands import menu_xray_actions


class MenuXrayActionsTests(unittest.TestCase):
    def test_sqlite_status_calls_manager_command(self) -> None:
        calls = []

        menu_xray_actions.sqlite_status(calls.append)

        self.assertEqual(calls, [["xray-vps-manager", "sqlite", "status"]])

    def test_manager_update_actions_call_manager_update_command(self) -> None:
        calls = []

        menu_xray_actions.check_manager_update(calls.append)
        menu_xray_actions.update_manager(calls.append)
        menu_xray_actions.show_manager_update_backups(calls.append)
        menu_xray_actions.rollback_manager(calls.append, lambda _message: True)

        self.assertEqual(
            calls,
            [
                ["xray-manager-update", "--check"],
                ["xray-manager-update", "--update"],
                ["xray-manager-update", "--backups"],
                ["xray-manager-update", "--rollback"],
            ],
        )

    def test_cascade_menu_actions_call_named_cascade_commands(self) -> None:
        calls = []

        menu_xray_actions.show_cascades(calls.append)
        menu_xray_actions.select_cascade(calls.append)
        menu_xray_actions.test_selected_cascade(calls.append)
        menu_xray_actions.remove_cascade(calls.append)

        self.assertEqual(
            calls,
            [
                ["xray-set-cascade", "list"],
                ["xray-set-cascade", "use"],
                ["xray-set-cascade", "test-select"],
                ["xray-set-cascade", "remove"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
