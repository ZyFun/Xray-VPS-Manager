import unittest

from xray_vps_manager.telegram import keyboards


class TelegramKeyboardTests(unittest.TestCase):
    def test_subscribed_client_menu_includes_user_actions(self) -> None:
        buttons = [button for row in keyboards.client_menu_keyboard(subscribed=True) for button in row]

        self.assertIn({"text": "Статистика трафика", "callback_data": "client:traffic"}, buttons)
        self.assertIn({"text": "Статус подписки", "callback_data": "client:status"}, buttons)
        self.assertIn({"text": "Получить VLESS-ссылку", "callback_data": "client:link"}, buttons)

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

    def test_admin_menu_client_menu_button_returns_to_client_menu(self) -> None:
        rows = keyboards.admin_menu_keyboard()["inline_keyboard"]
        buttons = [button for row in rows for button in row]

        self.assertIn({"text": "Клиентское меню", "callback_data": "client:menu"}, buttons)


if __name__ == "__main__":
    unittest.main()
