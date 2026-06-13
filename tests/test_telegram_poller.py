import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from xray_vps_manager.telegram import admin, poller


class TelegramPollerTests(unittest.TestCase):
    def make_context(self, db, updates, events, client_db=None):
        if client_db is None:
            client_db = {"clients": {}}

        def save_db_sections(updated_db, sections):
            state = updated_db.get("clientSubscriptionState", {})
            events.append(("save", tuple(sections), state.get("userUpdateOffset")))

        def send_chat_message(_db, chat_id, text, reply_markup=None, parse_mode=None):
            events.append(("send", str(chat_id), text))

        admin_context = admin.AdminContext(
            load_client_db=lambda: client_db,
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
            load_client_db=lambda: client_db,
            load_traffic_db=lambda: {"clients": {}},
            display_timezone=lambda: (ZoneInfo("Europe/Moscow"), "Europe/Moscow"),
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

    def test_start_shows_status_for_subscribed_client(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 30, "expiryReminders": {}},
            "clientSubscriptions": {
                "222": {
                    "client": "alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                    "enabled": True,
                }
            },
        }
        updates = [
            {
                "update_id": 35,
                "message": {
                    "text": "/start",
                    "chat": {"id": "222", "type": "private", "username": "client"},
                },
            }
        ]
        events = []
        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"alice": {"id": "00000000-0000-0000-0000-000000000001"}}},
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Текущая подписка:", sent[2])
        self.assertIn("Статус: включён", sent[2])
        self.assertNotIn("Отправь свою VLESS", sent[2])

    def test_subscribe_button_does_not_reask_link_for_subscribed_client(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 40, "expiryReminders": {}},
            "clientSubscriptions": {
                "222": {
                    "client": "alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                    "enabled": True,
                }
            },
        }
        updates = [
            {
                "update_id": 45,
                "callback_query": {
                    "id": "callback-subscribe",
                    "data": "client:subscribe",
                    "message": {"chat": {"id": "222", "type": "private"}},
                },
            }
        ]
        events = []
        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"alice": {"id": "00000000-0000-0000-0000-000000000001"}}},
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Уведомления уже подключены.", sent[2])
        self.assertIn("Статус: включён", sent[2])
        self.assertNotIn("Отправь сюда свою VLESS", sent[2])

    def test_plain_text_from_subscribed_client_shows_status(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 50, "expiryReminders": {}},
            "clientSubscriptions": {
                "222": {
                    "client": "alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                    "enabled": True,
                }
            },
        }
        updates = [
            {
                "update_id": 55,
                "message": {
                    "text": "привет",
                    "chat": {"id": "222", "type": "private", "username": "client"},
                },
            }
        ]
        events = []
        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"alice": {"id": "00000000-0000-0000-0000-000000000001"}}},
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Текущая подписка:", sent[2])
        self.assertNotIn("Я не нашёл VLESS-ссылку", sent[2])

    def test_help_command_sends_client_help_text(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 60, "expiryReminders": {}},
            "clientSubscriptions": {},
        }
        updates = [
            {
                "update_id": 65,
                "message": {
                    "text": "/help",
                    "chat": {"id": "222", "type": "private", "username": "client"},
                },
            }
        ]
        events = []
        ctx = self.make_context(db, updates, events)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Vireika: помощь", sent[2])
        self.assertIn("• получить актуальную VLESS-ссылку;", sent[2])
        self.assertNotIn("Привет. Я бот Vireika.", sent[2])

    def test_help_callback_sends_client_help_text(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 70, "expiryReminders": {}},
            "clientSubscriptions": {
                "222": {
                    "client": "alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                    "enabled": True,
                }
            },
        }
        updates = [
            {
                "update_id": 75,
                "callback_query": {
                    "id": "callback-help",
                    "data": "client:help",
                    "message": {"chat": {"id": "222", "type": "private"}},
                },
            }
        ]
        events = []
        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"alice": {"id": "00000000-0000-0000-0000-000000000001"}}},
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Vireika: помощь", sent[2])
        self.assertIn("• отписаться от бота, если уведомления больше не нужны.", sent[2])
        self.assertNotIn("Текущая подписка:", sent[2])

    def test_client_menu_callback_returns_home_instead_of_help(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 80, "expiryReminders": {}},
            "clientSubscriptions": {
                "222": {
                    "client": "alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                    "enabled": True,
                }
            },
        }
        updates = [
            {
                "update_id": 85,
                "callback_query": {
                    "id": "callback-menu",
                    "data": "client:menu",
                    "message": {"chat": {"id": "222", "type": "private"}},
                },
            }
        ]
        events = []
        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"alice": {"id": "00000000-0000-0000-0000-000000000001"}}},
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Текущая подписка:", sent[2])
        self.assertNotIn("Vireika: помощь", sent[2])

    def test_traffic_callback_sends_report_for_subscribed_client(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 30, "expiryReminders": {}},
            "clientSubscriptions": {
                "222": {
                    "client": "alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                    "enabled": True,
                }
            },
        }
        updates = [
            {
                "update_id": 35,
                "callback_query": {
                    "id": "callback-traffic",
                    "data": "client:traffic:day",
                    "message": {"chat": {"id": "222", "type": "private"}},
                },
            }
        ]
        events = []
        ctx = self.make_context(db, updates, events)
        today = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
        ctx = poller.PollerContext(
            **{
                **ctx.__dict__,
                "load_client_db": lambda: {
                    "clients": {
                        "alice": {"id": "00000000-0000-0000-0000-000000000001"},
                        "bob": {"id": "00000000-0000-0000-0000-000000000002"},
                    }
                },
                "load_traffic_db": lambda: {
                    "clients": {
                        "alice": {
                            "history": {
                                today: {
                                    "00": {"incoming": 1024, "outgoing": 2048},
                                }
                            }
                        },
                        "bob": {
                            "history": {
                                today: {
                                    "00": {"incoming": 999999, "outgoing": 999999},
                                }
                            }
                        },
                    }
                },
            }
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Статистика трафика за сутки", sent[2])
        self.assertIn("1.00KB", sent[2])
        self.assertIn("2.00KB", sent[2])
        self.assertNotIn("alice", sent[2])
        self.assertNotIn("bob", sent[2])


if __name__ == "__main__":
    unittest.main()
