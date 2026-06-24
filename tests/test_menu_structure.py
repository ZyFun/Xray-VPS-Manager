import unittest

from xray_vps_manager.commands import menu


class MenuStructureTests(unittest.TestCase):
    def test_main_menu_uses_task_oriented_sections(self) -> None:
        self.assertIn(("2", "Подключения и TLS"), menu.main_menu_actions())
        self.assertIn(("3", "Маршрутизация"), menu.main_menu_actions())
        self.assertIn(("4", "Трафик и активность"), menu.main_menu_actions())
        self.assertIn(("5", "Сервис и диагностика"), menu.main_menu_actions())
        self.assertIn(("9", "Обновления"), menu.main_menu_actions())
        self.assertNotIn(("2", "Настройки Xray"), menu.main_menu_actions())
        self.assertIn("9", menu.main_menu_handlers())

    def test_caddy_and_connection_rename_actions_are_exposed(self) -> None:
        self.assertIn(("2", "Подключения Trojan"), menu.connection_tls_menu_actions())
        self.assertIn("2", menu.connection_tls_menu_handlers())
        self.assertIn(("4", "Caddy / TLS"), menu.connection_tls_menu_actions())
        self.assertIn("4", menu.connection_tls_menu_handlers())
        self.assertIn(("2", "Создать Trojan TLS подключение"), menu.trojan_menu_actions())
        self.assertIn(("3", "Удалить Trojan-подключение"), menu.trojan_menu_actions())
        self.assertIn("2", menu.trojan_menu_handlers())
        self.assertIn("3", menu.trojan_menu_handlers())
        self.assertIn(("4", "Бэкапы"), menu.caddy_menu_actions())
        self.assertIn("4", menu.caddy_menu_handlers())
        self.assertIn(("5", "TLS randomizer"), menu.caddy_menu_actions())
        self.assertIn("5", menu.caddy_menu_handlers())
        self.assertIn(("2", "Включить для site"), menu.caddy_random_tls_menu_actions())
        self.assertIn(("4", "Переключить сейчас"), menu.caddy_random_tls_menu_actions())
        self.assertIn("2", menu.caddy_random_tls_menu_handlers())
        self.assertIn("4", menu.caddy_random_tls_menu_handlers())
        self.assertIn(("1", "Показать TLS site configs"), menu.caddy_sites_menu_actions())
        self.assertIn(("9", "Убрать дефолтный site :80"), menu.caddy_sites_menu_actions())
        self.assertIn(("1", "Создать backup Caddy config"), menu.caddy_backup_menu_actions())
        self.assertIn(("5", "Создать backup сайта"), menu.caddy_backup_menu_actions())
        self.assertIn(("7", "Восстановить сайт из backup"), menu.caddy_backup_menu_actions())
        self.assertIn("1", menu.caddy_backup_menu_handlers())
        self.assertIn("5", menu.caddy_backup_menu_handlers())
        self.assertIn("7", menu.caddy_backup_menu_handlers())
        self.assertIn(("8", "Расширенные XHTTP настройки"), menu.reality_menu_actions())
        self.assertIn("8", menu.reality_menu_handlers())
        self.assertIn(("10", "Переименовать подключение"), menu.reality_menu_actions())
        self.assertIn("10", menu.reality_menu_handlers())

    def test_client_connection_actions_are_exposed(self) -> None:
        self.assertIn(("3", "Добавить подключение к клиенту"), menu.client_menu_actions())
        self.assertIn("3", menu.client_menu_handlers())
        self.assertIn(("9", "Перенести клиента в другое подключение"), menu.client_menu_actions())
        self.assertIn("9", menu.client_menu_handlers())
        self.assertIn(("12", "Лимиты трафика"), menu.client_menu_actions())
        self.assertIn("12", menu.client_menu_handlers())
        self.assertIn(("13", "Изменить страну подключения"), menu.client_menu_actions())
        self.assertIn("13", menu.client_menu_handlers())

    def test_routing_activity_service_and_update_actions_are_exposed(self) -> None:
        self.assertIn(("3", "Торренты"), menu.routing_menu_actions())
        self.assertIn(("4", "GeoIP routing"), menu.routing_menu_actions())
        self.assertIn(("5", "Блокировки IP/доменов"), menu.routing_menu_actions())
        self.assertIn(("6", "SQLite: статус базы"), menu.service_diagnostics_menu_actions())
        self.assertIn(("8", "Изменить часовой пояс"), menu.service_diagnostics_menu_actions())
        self.assertIn(("2", "Geo assets"), menu.updates_menu_actions())
        self.assertIn(("3", "Менеджер"), menu.updates_menu_actions())
        self.assertNotIn(("6", "Обновить geoip/geosite из Xray release"), menu.update_menu_actions())
        self.assertIn(("6", "Настройки журнала активности"), menu.traffic_menu_actions())
        self.assertIn(("7", "Настройки суммарного трафика"), menu.traffic_menu_actions())
        self.assertIn(("2", "Включить строку с множителем"), menu.total_traffic_settings_menu_actions())
        self.assertIn(("3", "Отключить строку с множителем"), menu.total_traffic_settings_menu_actions())
        self.assertIn(("4", "Экспорт activity по клиенту"), menu.traffic_menu_actions())


if __name__ == "__main__":
    unittest.main()
