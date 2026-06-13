import unittest

from xray_vps_manager.telegram import keyboards


class TelegramKeyboardTests(unittest.TestCase):
    def test_client_menu_includes_traffic_button(self) -> None:
        buttons = [button for row in keyboards.client_menu_keyboard() for button in row]

        self.assertIn({"text": "Статистика трафика", "callback_data": "client:traffic"}, buttons)

    def test_client_menu_uses_unsubscribe_button_label(self) -> None:
        buttons = [button for row in keyboards.client_menu_keyboard() for button in row]

        self.assertIn({"text": "Отписаться от бота", "callback_data": "client:unsubscribe"}, buttons)
        self.assertNotIn({"text": "Отключить уведомления", "callback_data": "client:unsubscribe"}, buttons)

    def test_client_keyboard_hides_subscribe_button_for_subscribed_chat(self) -> None:
        markup = keyboards.client_keyboard_for_chat(
            {"clientSubscriptions": {"222": {"client": "alice"}}},
            "222",
        )
        buttons = [button for row in markup["inline_keyboard"] for button in row]

        self.assertNotIn({"text": "Подключить уведомления", "callback_data": "client:subscribe"}, buttons)
        self.assertIn({"text": "Статус подписки", "callback_data": "client:status"}, buttons)
        self.assertIn({"text": "Отписаться от бота", "callback_data": "client:unsubscribe"}, buttons)

    def test_client_keyboard_keeps_subscribe_button_for_new_chat(self) -> None:
        markup = keyboards.client_keyboard_for_chat({"clientSubscriptions": {}}, "222")
        buttons = [button for row in markup["inline_keyboard"] for button in row]

        self.assertIn({"text": "Подключить уведомления", "callback_data": "client:subscribe"}, buttons)

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
        self.assertEqual(buttons[-1], {"text": "Назад", "callback_data": "client:help"})


if __name__ == "__main__":
    unittest.main()
