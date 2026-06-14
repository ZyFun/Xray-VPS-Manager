import unittest
from pathlib import Path

from xray_vps_manager.telegram import admin


class TelegramAdminTests(unittest.TestCase):
    def make_context(self, events, client_db=None, send_response=None):
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
            return send_response

        return admin.AdminContext(
            load_client_db=lambda: client_db,
            save_db_sections=lambda _db, sections: events.append({"save": tuple(sections)}),
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

    def test_admin_menu_registers_latest_callback_message(self) -> None:
        db = {"adminState": {}}
        events = []
        ctx = self.make_context(events, send_response={"ok": True, "result": {"message_id": 42}})

        admin.send_admin_menu(ctx, db, "111")

        guard = db["adminState"]["callbackGuards"]["111"]
        self.assertEqual(guard["activeMessageId"], 42)

    def test_admin_callback_guard_blocks_duplicate_message_click(self) -> None:
        db = {"adminState": {}}
        events = []
        ctx = self.make_context(events)

        self.assertTrue(admin.accept_admin_callback(ctx, db, "111", "admin:backup", 42))
        self.assertFalse(admin.accept_admin_callback(ctx, db, "111", "admin:backup", 42))

    def test_admin_callback_guard_blocks_old_message_after_new_admin_message(self) -> None:
        db = {"adminState": {}}
        events = []
        ctx = self.make_context(events)

        self.assertTrue(admin.accept_admin_callback(ctx, db, "111", "admin:backup", 42))
        admin.register_admin_message(ctx, db, "111", {"ok": True, "result": {"message_id": 43}})

        self.assertFalse(admin.accept_admin_callback(ctx, db, "111", "admin:backup", 42))
        self.assertTrue(admin.accept_admin_callback(ctx, db, "111", "admin:backup", 43))

    def test_admin_menu_callback_from_client_menu_can_open_admin_panel(self) -> None:
        db = {"adminState": {}}
        events = []
        ctx = self.make_context(events)
        admin.register_admin_message(ctx, db, "111", {"ok": True, "result": {"message_id": 43}})

        self.assertTrue(admin.accept_admin_callback(ctx, db, "111", "admin:menu", 42))


if __name__ == "__main__":
    unittest.main()
