import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from xray_vps_manager.telegram import admin, poller


class TelegramPollerTests(unittest.TestCase):
    def make_context(self, db, updates, events, client_db=None, run_capture=None):
        if client_db is None:
            client_db = {"clients": {}}
        if callable(client_db):
            load_client_db = client_db
        else:
            load_client_db = lambda: client_db
        if run_capture is None:
            run_capture = lambda *args, **kwargs: None

        def save_db_sections(updated_db, sections):
            state = updated_db.get("clientSubscriptionState", {})
            events.append(("save", tuple(sections), state.get("userUpdateOffset")))

        def send_chat_message(_db, chat_id, text, reply_markup=None, parse_mode=None):
            events.append(("send", str(chat_id), text))

        def curl_json(_db, method, payload=None, timeout=30):
            if method != "getUpdates":
                events.append(("api", method, payload))
            return {"ok": True, "result": updates}

        admin_context = admin.AdminContext(
            load_client_db=load_client_db,
            save_db_sections=save_db_sections,
            format_access_until=lambda value: value or "бессрочно",
            run_capture=run_capture,
            send_chat_message=send_chat_message,
            bot_name=lambda current_db=None: "Vireika",
            notification_context=None,
        )
        return poller.PollerContext(
            load_db=lambda: db,
            save_db_sections=save_db_sections,
            load_client_db=load_client_db,
            load_traffic_db=lambda: {"clients": {}},
            display_timezone=lambda: (ZoneInfo("Europe/Moscow"), "Europe/Moscow"),
            format_access_until=lambda value: value or "бессрочно",
            run_capture=run_capture,
            send_chat_message=send_chat_message,
            answer_callback_query=lambda *args, **kwargs: events.append(("answer",)),
            curl_json=curl_json,
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

    def test_duplicate_admin_callback_from_same_message_runs_once(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 20, "expiryReminders": {}},
            "clientSubscriptions": {},
            "adminState": {},
        }
        updates = [
            {
                "update_id": 21,
                "callback_query": {
                    "id": "callback-payments-1",
                    "data": "admin:payments",
                    "message": {"message_id": 77, "chat": {"id": "111", "type": "private"}},
                },
            },
            {
                "update_id": 22,
                "callback_query": {
                    "id": "callback-payments-2",
                    "data": "admin:payments",
                    "message": {"message_id": 77, "chat": {"id": "111", "type": "private"}},
                },
            },
        ]
        events = []
        ctx = self.make_context(db, updates, events)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"]
        self.assertEqual(len(sent), 1)
        self.assertIn("Xray VPS Manager: платежи", sent[0][2])
        guard = db["adminState"]["callbackGuards"]["111"]
        self.assertEqual(guard["consumedMessageIds"], [77])

    def test_admin_panel_callback_is_owner_only(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 30, "expiryReminders": {}},
            "clientSubscriptions": {},
            "adminState": {},
        }
        updates = [
            {
                "update_id": 31,
                "callback_query": {
                    "id": "callback-admin-menu",
                    "data": "admin:menu",
                    "message": {"message_id": 88, "chat": {"id": "222", "type": "private"}},
                },
            }
        ]
        events = []
        ctx = self.make_context(db, updates, events)

        with mock.patch.object(admin, "handle_callback", side_effect=AssertionError("admin callback must be owner-only")):
            self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertIn(("answer",), events)
        sent = [event for event in events if event[0] == "send"]
        self.assertEqual(len(sent), 1)
        self.assertNotIn("Xray VPS Manager: админ-панель", sent[0][2])
        self.assertNotIn("callbackGuards", db.get("adminState", {}))

    def test_owner_can_extend_client_subscription_from_admin_panel(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 100, "expiryReminders": {}},
            "clientSubscriptions": {},
            "adminState": {},
        }
        updates = [
            {
                "update_id": 101,
                "callback_query": {
                    "id": "callback-extend-menu",
                    "data": "admin:client-extend",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
            {
                "update_id": 102,
                "callback_query": {
                    "id": "callback-extend-alice",
                    "data": "admin:client-extend:0",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
            {
                "update_id": 103,
                "message": {
                    "text": "14",
                    "chat": {"id": "111", "type": "private", "username": "owner"},
                },
            },
        ]
        events = []

        def run_capture(command, timeout=20, **_kwargs):
            events.append(("run", command, timeout))
            return SimpleNamespace(
                returncode=0,
                stdout="Client: alice\nAccess until: 2026-07-01 00:00",
                stderr="",
            )

        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"alice": {"expiresAt": "2026-07-01T00:00:00+03:00"}}},
            run_capture=run_capture,
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertIn(("run", ["/usr/local/sbin/xray-client", "extend-days", "alice", "14"], 120), events)
        self.assertNotIn("111", db.get("adminState", {}))
        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Подписка продлена для alice на 14 дн.", sent[2])
        self.assertIn("Доступ до: 2026-07-01T00:00:00+03:00", sent[2])

    def test_owner_extend_selection_uses_saved_button_list_when_clients_change(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 105, "expiryReminders": {}},
            "clientSubscriptions": {},
            "adminState": {},
        }
        updates = [
            {
                "update_id": 106,
                "callback_query": {
                    "id": "callback-extend-menu",
                    "data": "admin:client-extend",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
            {
                "update_id": 107,
                "callback_query": {
                    "id": "callback-extend-first",
                    "data": "admin:client-extend:0",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
            {
                "update_id": 108,
                "message": {
                    "text": "7",
                    "chat": {"id": "111", "type": "private", "username": "owner"},
                },
            },
        ]
        events = []
        snapshots = [
            {"clients": {"alice": {"expiresAt": "2026-07-01T00:00:00+03:00"}, "bob": {}}},
            {"clients": {"aaron": {}, "alice": {"expiresAt": "2026-07-08T00:00:00+03:00"}, "bob": {}}},
        ]

        def load_client_db():
            if len(snapshots) > 1:
                return snapshots.pop(0)
            return snapshots[0]

        def run_capture(command, timeout=20, **_kwargs):
            events.append(("run", command, timeout))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = self.make_context(db, updates, events, client_db=load_client_db, run_capture=run_capture)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertIn(("run", ["/usr/local/sbin/xray-client", "extend-days", "alice", "7"], 120), events)
        self.assertNotIn(("run", ["/usr/local/sbin/xray-client", "extend-days", "aaron", "7"], 120), events)

    def test_owner_extend_subscription_rejects_non_numeric_days(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 110, "expiryReminders": {}},
            "clientSubscriptions": {},
            "adminState": {
                "111": {
                    "action": "extend-subscription-days",
                    "client": "alice",
                    "startedAt": "2026-06-12T22:00:00Z",
                }
            },
        }
        updates = [
            {
                "update_id": 111,
                "message": {
                    "text": "две недели",
                    "chat": {"id": "111", "type": "private", "username": "owner"},
                },
            }
        ]
        events = []

        def run_capture(command, timeout=20, **_kwargs):
            events.append(("run", command, timeout))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"alice": {"expiresAt": "2026-07-01T00:00:00+03:00"}}},
            run_capture=run_capture,
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertFalse(any(event[0] == "run" for event in events))
        self.assertEqual(db["adminState"]["111"]["action"], "extend-subscription-days")
        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Нужно отправить положительное целое число дней.", sent[2])

    def test_owner_extend_subscription_back_clears_pending_state(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 120, "expiryReminders": {}},
            "clientSubscriptions": {},
            "adminState": {
                "111": {
                    "action": "extend-subscription-days",
                    "client": "alice",
                    "startedAt": "2026-06-12T22:00:00Z",
                }
            },
        }
        updates = [
            {
                "update_id": 121,
                "callback_query": {
                    "id": "callback-back",
                    "data": "admin:clients",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
            {
                "update_id": 122,
                "message": {
                    "text": "14",
                    "chat": {"id": "111", "type": "private", "username": "owner"},
                },
            },
        ]
        events = []

        def run_capture(command, timeout=20, **_kwargs):
            events.append(("run", command, timeout))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"alice": {"expiresAt": "2026-07-01T00:00:00+03:00"}}},
            run_capture=run_capture,
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertFalse(any(event[0] == "run" for event in events))
        self.assertNotIn("111", db.get("adminState", {}))

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

    def test_vless_subscription_sets_subscribed_command_menu(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 45, "expiryReminders": {}},
            "clientSubscriptions": {},
        }
        client_uuid = "00000000-0000-0000-0000-000000000001"
        link = (
            f"vless://{client_uuid}@vpn.example:443?"
            "security=reality&encryption=none&pbk=public-key&fp=chrome&type=tcp"
            "&flow=xtls-rprx-vision&sni=example.com&sid=abcd"
        )
        updates = [
            {
                "update_id": 46,
                "message": {
                    "text": link,
                    "chat": {"id": "222", "type": "private", "username": "client"},
                },
            }
        ]
        events = []
        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={
                "connections": {
                    "vless-reality": {
                        "port": 443,
                        "publicKey": "public-key",
                        "sni": "example.com",
                        "shortId": "abcd",
                        "fingerprint": "chrome",
                    }
                },
                "clients": {
                    "alice": {
                        "id": client_uuid,
                        "connection": "vless-reality",
                        "client": {"id": client_uuid, "flow": "xtls-rprx-vision"},
                    }
                },
            },
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        api_calls = [event for event in events if event[0] == "api"]
        self.assertEqual(api_calls[0][1], "setMyCommands")
        self.assertEqual(api_calls[0][2]["scope"], {"type": "chat", "chat_id": "222"})
        self.assertIn({"command": "unsubscribe", "description": "Отписаться от бота"}, api_calls[0][2]["commands"])

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

    def test_unsubscribe_callback_resets_chat_command_menu(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 90, "expiryReminders": {}},
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
                "update_id": 95,
                "callback_query": {
                    "id": "callback-unsubscribe",
                    "data": "client:unsubscribe",
                    "message": {"chat": {"id": "222", "type": "private"}},
                },
            }
        ]
        events = []
        ctx = self.make_context(db, updates, events)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        api_calls = [event for event in events if event[0] == "api"]
        self.assertEqual(api_calls[0][1], "deleteMyCommands")
        self.assertEqual(api_calls[0][2], {"scope": {"type": "chat", "chat_id": "222"}})
        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Подписка на бота отключена.", sent[2])

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
