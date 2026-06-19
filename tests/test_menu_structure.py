import unittest

from xray_vps_manager.commands import menu


class MenuStructureTests(unittest.TestCase):
    def test_manager_update_is_root_menu_action(self) -> None:
        self.assertIn(("6", "Обновление менеджера"), menu.main_menu_actions())
        self.assertNotIn(("16", "Обновление менеджера"), menu.xray_settings_menu_actions())
        self.assertIn("6", menu.main_menu_handlers())
        self.assertNotEqual(menu.xray_settings_menu_handlers()["16"][0], "Обновление менеджера")

    def test_caddy_and_connection_rename_actions_are_exposed(self) -> None:
        self.assertIn(("16", "Caddy / TLS"), menu.xray_settings_menu_actions())
        self.assertIn("16", menu.xray_settings_menu_handlers())
        self.assertIn(("18", "Бэкапы"), menu.caddy_menu_actions())
        self.assertIn("18", menu.caddy_menu_handlers())
        self.assertIn(("1", "Создать backup Caddy config"), menu.caddy_backup_menu_actions())
        self.assertIn(("5", "Создать backup сайта"), menu.caddy_backup_menu_actions())
        self.assertIn(("7", "Восстановить сайт из backup"), menu.caddy_backup_menu_actions())
        self.assertIn("1", menu.caddy_backup_menu_handlers())
        self.assertIn("5", menu.caddy_backup_menu_handlers())
        self.assertIn("7", menu.caddy_backup_menu_handlers())
        self.assertIn(("9", "Переименовать подключение"), menu.reality_menu_actions())
        self.assertIn("9", menu.reality_menu_handlers())

    def test_client_move_connection_action_is_exposed(self) -> None:
        self.assertIn(("12", "Перенести клиента в другое подключение"), menu.client_menu_actions())
        self.assertIn("12", menu.client_menu_handlers())


if __name__ == "__main__":
    unittest.main()
