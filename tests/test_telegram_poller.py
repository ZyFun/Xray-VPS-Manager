import unittest
from pathlib import Path

from xray_vps_manager.telegram import admin, poller


class TelegramPollerTests(unittest.TestCase):
    def make_context(self, db, updates, events):
        def save_db_sections(updated_db, sections):
            state = updated_db.get("clientSubscriptionState", {})
            events.append(("save", tuple(sections), state.get("userUpdateOffset")))

        def send_chat_message(_db, chat_id, text, reply_markup=None, parse_mode=None):
            events.append(("send", str(chat_id), text))

        admin_context = admin.AdminContext(
            load_client_db=lambda: {"clients": {}},
            save_db_sections=save_db_sections,
            format_access_until=lambda value: value or "бессрочно",
            run_capture=lambda *args, **kwargs: None,
            send_chat_message=send_chat_message,
            bot_name=lambda current_db=None: "Vireika",
            notification_context=None,
        )
        return poller.PollerContext(
            load_db=lambda: db,
            save_db_sections=save_db_sections,
            load_client_db=lambda: {"clients": {}},
            format_access_until=lambda value: value or "бессрочно",
            run_capture=lambda *args, **kwargs: None,
            send_chat_message=send_chat_message,
            answer_callback_query=lambda *args, **kwargs: events.append(("answer",)),
            curl_json=lambda _db, method, payload=None, timeout=30: {"ok": True, "result": updates},
            bot_name=lambda current_db=None: "Vireika",
            server_name_fragment=lambda: "Xray",
            utc_stamp=lambda: "2026-06-12T22:00:00Z",
            admin_context=admin_context,
            xray_client=Path("/usr/local/sbin/xray-client"),
        )

    def test_poll_saves_update_offset_before_sending_admin_menu(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 10, "expiryReminders": {}},
            "clientSubscriptions": {},
        }
        updates = [
            {
                "update_id": 15,
                "callback_query": {
                    "id": "callback-1",
                    "data": "admin:menu",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            }
        ]
        events = []
        ctx = self.make_context(db, updates, events)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertEqual(events[0], ("save", ("clientSubscriptionState",), 16))
        self.assertIn(("answer",), events)
        self.assertTrue(any(event[0] == "send" for event in events))

    def test_poll_saves_update_offset_before_replying_to_text_message(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 20, "expiryReminders": {}},
            "clientSubscriptions": {},
        }
        updates = [
            {
                "update_id": 25,
                "message": {
                    "text": "/status",
                    "chat": {"id": "222", "type": "private", "username": "client"},
                },
            }
        ]
        events = []
        ctx = self.make_context(db, updates, events)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertEqual(events[0], ("save", ("clientSubscriptionState",), 26))
        self.assertTrue(any(event[0] == "send" for event in events))


if __name__ == "__main__":
    unittest.main()
