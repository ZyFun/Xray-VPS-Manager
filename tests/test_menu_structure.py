import unittest

from xray_vps_manager.commands import menu


class MenuStructureTests(unittest.TestCase):
    def test_manager_update_is_root_menu_action(self) -> None:
        self.assertIn(("6", "Обновление менеджера"), menu.main_menu_actions())
        self.assertNotIn(("16", "Обновление менеджера"), menu.xray_settings_menu_actions())
        self.assertIn("6", menu.main_menu_handlers())
        self.assertNotIn("16", menu.xray_settings_menu_handlers())


if __name__ == "__main__":
    unittest.main()
