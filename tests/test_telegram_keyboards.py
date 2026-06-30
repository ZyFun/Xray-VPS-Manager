import unittest

from xray_vps_manager.telegram import keyboards


class TelegramKeyboardTests(unittest.TestCase):
    def test_subscribed_client_menu_includes_user_actions(self) -> None:
        buttons = [button for row in keyboards.client_menu_keyboard(subscribed=True) for button in row]

        self.assertIn({"text": "Статистика трафика", "callback_data": "client:traffic"}, buttons)
        self.assertIn({"text": "Статус подписки", "callback_data": "client:status"}, buttons)
        self.assertIn({"text": "Получить VPN-ссылку", "callback_data": "client:link"}, buttons)
        self.assertIn({"text": "Страна подключения", "callback_data": "client:country"}, buttons)
        self.assertIn({"text": "Уведомления активности", "callback_data": "client:activity"}, buttons)

    def test_subscribed_client_menu_uses_unsubscribe_button_label_and_places_it_last(self) -> None:
        buttons = [button for row in keyboards.client_menu_keyboard(subscribed=True) for button in row]

        self.assertIn({"text": "Отписаться от бота", "callback_data": "client:unsubscribe"}, buttons)
        self.assertNotIn({"text": "Отключить уведомления", "callback_data": "client:unsubscribe"}, buttons)
        self.assertEqual(buttons[-1], {"text": "Отписаться от бота", "callback_data": "client:unsubscribe"})

    def test_unsubscribed_client_menu_only_shows_subscribe_and_help(self) -> None:
        buttons = [button for row in keyboards.client_menu_keyboard(subscribed=False) for button in row]

        self.assertEqual(
            buttons,
            [
                {"text": "Подключить уведомления", "callback_data": "client:subscribe"},
                {"text": "Помощь", "callback_data": "client:help"},
            ],
        )

    def test_client_keyboard_hides_subscribe_button_for_subscribed_chat(self) -> None:
        markup = keyboards.client_keyboard_for_chat(
            {"clientSubscriptions": {"222": {"client": "alice"}}},
            "222",
        )
        buttons = [button for row in markup["inline_keyboard"] for button in row]

        self.assertNotIn({"text": "Подключить уведомления", "callback_data": "client:subscribe"}, buttons)
        self.assertIn({"text": "Статус подписки", "callback_data": "client:status"}, buttons)
        self.assertIn({"text": "Страна подключения", "callback_data": "client:country"}, buttons)
        self.assertIn({"text": "Уведомления активности", "callback_data": "client:activity"}, buttons)
        self.assertIn({"text": "Отписаться от бота", "callback_data": "client:unsubscribe"}, buttons)
        self.assertEqual(buttons[-1], {"text": "Отписаться от бота", "callback_data": "client:unsubscribe"})

    def test_client_keyboard_hides_user_actions_for_new_chat(self) -> None:
        markup = keyboards.client_keyboard_for_chat({"clientSubscriptions": {}}, "222")
        buttons = [button for row in markup["inline_keyboard"] for button in row]

        self.assertEqual(
            buttons,
            [
                {"text": "Подключить уведомления", "callback_data": "client:subscribe"},
                {"text": "Помощь", "callback_data": "client:help"},
            ],
        )

    def test_client_traffic_keyboard_contains_expected_reports(self) -> None:
        rows = keyboards.client_traffic_keyboard()["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertEqual(
            buttons[:3],
            [
                {"text": "За сутки", "callback_data": "client:traffic:day"},
                {"text": "За сутки по часам", "callback_data": "client:traffic:day-hours"},
                {"text": "За неделю по дням", "callback_data": "client:traffic:week-days"},
            ],
        )
        self.assertEqual(buttons[-1], {"text": "Назад", "callback_data": "client:menu"})

    def test_client_activity_keyboard_toggles_current_state(self) -> None:
        disabled_buttons = [button for row in keyboards.client_activity_keyboard(False)["inline_keyboard"] for button in row]
        enabled_buttons = [button for row in keyboards.client_activity_keyboard(True)["inline_keyboard"] for button in row]

        self.assertEqual(disabled_buttons[0], {"text": "Включить", "callback_data": "client:activity:on"})
        self.assertEqual(enabled_buttons[0], {"text": "Отключить", "callback_data": "client:activity:off"})
        self.assertIn({"text": "Исключения", "callback_data": "client:activity-exceptions"}, disabled_buttons)
        self.assertEqual(disabled_buttons[-1], {"text": "Назад", "callback_data": "client:menu"})

    def test_client_activity_exceptions_manage_keyboard_deletes_selected_item(self) -> None:
        buttons = [
            button
            for row in keyboards.client_activity_exceptions_manage_keyboard(
                [{"host": "video.example.ru", "port": "443", "regions": "RU"}]
            )["inline_keyboard"]
            for button in row
        ]

        self.assertEqual(
            buttons[0],
            {"text": "Удалить video.example.ru:443 · RU", "callback_data": "client:activity-exception:delete:0"},
        )
        self.assertEqual(buttons[-1], {"text": "Назад", "callback_data": "client:activity"})

    def test_client_country_keyboard_marks_current_route(self) -> None:
        rows = keyboards.client_country_keyboard(
            [
                {"tag": "cascade-de", "display": "Германия"},
                {"tag": "cascade-us", "display": "США"},
            ],
            current_tag="cascade-de",
        )["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertEqual(buttons[0], {"text": "Германия (выбрана)", "callback_data": "client:country:cascade-de"})
        self.assertEqual(buttons[1], {"text": "США", "callback_data": "client:country:cascade-us"})
        self.assertEqual(buttons[-1], {"text": "Назад", "callback_data": "client:menu"})

    def test_admin_menu_client_menu_button_returns_to_client_menu(self) -> None:
        rows = keyboards.admin_menu_keyboard()["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertIn({"text": "Клиентское меню", "callback_data": "client:menu"}, buttons)

    def test_admin_menu_groups_admin_sections(self) -> None:
        rows = keyboards.admin_menu_keyboard()["inline_keyboard"]

        self.assertEqual(
            rows,
            [
                [
                    {"text": "Статус", "callback_data": "admin:status-menu"},
                    {"text": "Клиенты", "callback_data": "admin:clients"},
                ],
                [
                    {"text": "Платежи", "callback_data": "admin:payments"},
                    {"text": "Уведомления", "callback_data": "admin:notices"},
                ],
                [
                    {"text": "Бэкапы", "callback_data": "admin:backups"},
                    {"text": "Активность", "callback_data": "admin:activity"},
                ],
                [
                    {"text": "Настройки сервера", "callback_data": "admin:server-settings"},
                    {"text": "Настройки бота", "callback_data": "admin:settings"},
                ],
                [{"text": "Клиентское меню", "callback_data": "client:menu"}],
            ],
        )

    def test_admin_status_keyboard_contains_status_actions(self) -> None:
        rows = keyboards.admin_status_keyboard()["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertEqual(
            buttons,
            [
                {"text": "Статус бота", "callback_data": "admin:status"},
                {"text": "Сводка сервера", "callback_data": "admin:daily-summary"},
                {"text": "Проверка сервера", "callback_data": "admin:test"},
                {"text": "Проверить напоминания", "callback_data": "admin:expiry"},
                {"text": "Назад", "callback_data": "admin:menu"},
            ],
        )

    def test_admin_clients_keyboard_contains_client_actions(self) -> None:
        rows = keyboards.admin_clients_keyboard()["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertEqual(buttons[0], {"text": "Добавить клиента", "callback_data": "admin:client-add"})
        self.assertEqual(buttons[1], {"text": "Получить VPN-ссылку", "callback_data": "admin:client-link"})
        self.assertEqual(buttons[2], {"text": "Получить ключ доступа", "callback_data": "admin:client-key"})
        self.assertEqual(buttons[3], {"text": "Подписки клиентов", "callback_data": "admin:subscribers"})
        self.assertEqual(buttons[4], {"text": "Продлить подписку", "callback_data": "admin:client-extend"})
        self.assertEqual(buttons[-1], {"text": "Назад", "callback_data": "admin:menu"})

    def test_admin_client_link_keyboard_uses_client_indexes(self) -> None:
        rows = keyboards.admin_client_link_keyboard(["alice", "bob"])["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertEqual(buttons[0], {"text": "alice", "callback_data": "admin:client-link:0"})
        self.assertEqual(buttons[1], {"text": "bob", "callback_data": "admin:client-link:1"})
        self.assertEqual(buttons[-1], {"text": "Назад", "callback_data": "admin:clients"})

    def test_link_credential_keyboards_use_connection_indexes(self) -> None:
        options = [
            {"label": "VLESS · main · reality/tcp"},
            {"label": "TROJAN · caddy · tls/ws"},
        ]

        client_buttons = [
            button for row in keyboards.client_link_credential_keyboard(options)["inline_keyboard"] for button in row
        ]
        admin_buttons = [
            button for row in keyboards.admin_client_link_credential_keyboard(options)["inline_keyboard"] for button in row
        ]

        self.assertEqual(client_buttons[0], {"text": "VLESS · main · reality/tcp", "callback_data": "client:link-credential:0"})
        self.assertEqual(client_buttons[1], {"text": "TROJAN · caddy · tls/ws", "callback_data": "client:link-credential:1"})
        self.assertEqual(admin_buttons[0], {"text": "VLESS · main · reality/tcp", "callback_data": "admin:client-link-credential:0"})
        self.assertEqual(admin_buttons[1], {"text": "TROJAN · caddy · tls/ws", "callback_data": "admin:client-link-credential:1"})

    def test_admin_client_key_keyboard_uses_client_indexes(self) -> None:
        rows = keyboards.admin_client_key_keyboard(["alice", "bob"])["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertEqual(buttons[0], {"text": "alice", "callback_data": "admin:client-key:0"})
        self.assertEqual(buttons[1], {"text": "bob", "callback_data": "admin:client-key:1"})
        self.assertEqual(buttons[-1], {"text": "Назад", "callback_data": "admin:clients"})

    def test_admin_payments_keyboard_contains_read_only_payment_sections(self) -> None:
        rows = keyboards.admin_payments_keyboard()["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertEqual(
            buttons,
            [
                {"text": "Текущая сумма", "callback_data": "admin:payment-total"},
                {"text": "Сумма на клиента", "callback_data": "admin:payment-share"},
                {"text": "Округление", "callback_data": "admin:payment-rounding"},
                {"text": "Назад", "callback_data": "admin:menu"},
            ],
        )

    def test_admin_notices_keyboard_contains_news_template(self) -> None:
        rows = keyboards.admin_notices_keyboard()["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertEqual(
            buttons,
            [
                {"text": "Плановые работы", "callback_data": "admin:notice:start"},
                {"text": "Работы завершены", "callback_data": "admin:notice:done"},
                {"text": "Новости", "callback_data": "admin:notice:news"},
                {"text": "Своё сообщение", "callback_data": "admin:notice:custom"},
                {"text": "Назад", "callback_data": "admin:menu"},
            ],
        )

    def test_admin_service_keyboards_keep_existing_actions_in_submenus(self) -> None:
        backup_buttons = [button for row in keyboards.admin_backups_keyboard()["inline_keyboard"] for button in row]
        activity_buttons = [button for row in keyboards.admin_activity_keyboard()["inline_keyboard"] for button in row]
        settings_buttons = [button for row in keyboards.admin_settings_keyboard()["inline_keyboard"] for button in row]
        server_settings_buttons = [button for row in keyboards.admin_server_settings_keyboard()["inline_keyboard"] for button in row]

        self.assertIn({"text": "Создать backup", "callback_data": "admin:backup"}, backup_buttons)
        self.assertIn({"text": "Проверить GeoIP", "callback_data": "admin:geoip"}, activity_buttons)
        self.assertIn({"text": "Статус бота", "callback_data": "admin:settings-status"}, settings_buttons)
        self.assertIn({"text": "TLS", "callback_data": "admin:server-tls"}, server_settings_buttons)

    def test_admin_server_tls_keyboard_lists_sites_and_profiles(self) -> None:
        site_rows = keyboards.admin_server_tls_sites_keyboard(
            [{"domain": "api.example.com", "tlsLabel": "TLS 1.2"}]
        )["inline_keyboard"]
        site_buttons = [button for row in site_rows for button in row]

        self.assertEqual(site_buttons[0], {"text": "api.example.com · TLS 1.2", "callback_data": "admin:server-tls-site:0"})

        profile_rows = keyboards.admin_server_tls_site_keyboard(0, "tls12_13")["inline_keyboard"]
        profile_buttons = [button for row in profile_rows for button in row]

        self.assertIn({"text": "TLS 1.2 + TLS 1.3 (выбрано)", "callback_data": "admin:server-tls-set:0:tls12_13"}, profile_buttons)
        self.assertEqual(profile_buttons[-1], {"text": "Назад", "callback_data": "admin:server-tls"})

    def test_admin_client_extend_keyboard_uses_client_indexes(self) -> None:
        rows = keyboards.admin_client_extend_keyboard(["alice", "bob"])["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertEqual(buttons[0], {"text": "alice", "callback_data": "admin:client-extend:0"})
        self.assertEqual(buttons[1], {"text": "bob", "callback_data": "admin:client-extend:1"})
        self.assertEqual(buttons[-1], {"text": "Назад", "callback_data": "admin:clients"})

    def test_admin_client_add_connection_keyboard_uses_connection_indexes(self) -> None:
        rows = keyboards.admin_client_add_connection_keyboard(
            [
                {"name": "main", "tag": "vless-reality", "port": 443},
                {"name": "backup", "tag": "vless-reality-2", "port": 8443},
            ]
        )["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertEqual(buttons[0], {"text": "main · 443", "callback_data": "admin:client-add-connection:0"})
        self.assertEqual(buttons[1], {"text": "backup · 8443", "callback_data": "admin:client-add-connection:1"})
        self.assertIn({"text": "Отмена", "callback_data": "admin:client-add-cancel"}, buttons)


if __name__ == "__main__":
    unittest.main()
