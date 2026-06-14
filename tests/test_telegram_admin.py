import unittest
from pathlib import Path

from xray_vps_manager.telegram import admin


class TelegramAdminTests(unittest.TestCase):
    def make_context(self, events, client_db=None):
        if client_db is None:
            client_db = {"clients": {}}

        def send_chat_message(_db, chat_id, text, reply_markup=None, parse_mode=None):
            events.append(
                {
                    "chat_id": str(chat_id),
                    "text": text,
                    "reply_markup": reply_markup,
                    "parse_mode": parse_mode,
                }
            )

        return admin.AdminContext(
            load_client_db=lambda: client_db,
            save_db_sections=lambda *_args, **_kwargs: None,
            format_access_until=lambda value: value or "бессрочно",
            run_capture=lambda *_args, **_kwargs: None,
            send_chat_message=send_chat_message,
            bot_name=lambda current_db=None: "Vireika",
            notification_context=None,
            xray_client=Path("/usr/local/sbin/xray-client"),
        )

    def test_payment_share_callback_sends_payments_submenu(self) -> None:
        db = {
            "paymentTotalAmount": "500",
            "paymentCurrency": "₽",
            "paymentRoundingMode": "none",
            "paymentRoundingStep": "10",
        }
        client_db = {
            "clients": {
                "alice": {"paymentType": "paid"},
                "bob": {"paymentType": "paid"},
                "carol": {"paymentType": "free"},
            }
        }
        events = []
        ctx = self.make_context(events, client_db=client_db)

        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:payment-share"))

        self.assertEqual(len(events), 1)
        self.assertIn("Сумма на клиента: 250 ₽", events[0]["text"])
        buttons = [button for row in events[0]["reply_markup"]["inline_keyboard"] for button in row]
        self.assertIn({"text": "Текущая сумма", "callback_data": "admin:payment-total"}, buttons)
        self.assertIn({"text": "Назад", "callback_data": "admin:menu"}, buttons)

    def test_settings_status_callback_sends_settings_submenu(self) -> None:
        db = {
            "enabled": True,
            "chatId": "111",
            "chatLabel": "owner",
            "routeMode": "cascade",
        }
        events = []
        ctx = self.make_context(events)

        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:settings-status"))

        self.assertEqual(len(events), 1)
        self.assertIn("Имя бота: Vireika", events[0]["text"])
        self.assertIn("Маршрут Telegram: cascade", events[0]["text"])
        buttons = [button for row in events[0]["reply_markup"]["inline_keyboard"] for button in row]
        self.assertIn({"text": "Статус бота", "callback_data": "admin:settings-status"}, buttons)


if __name__ == "__main__":
    unittest.main()
