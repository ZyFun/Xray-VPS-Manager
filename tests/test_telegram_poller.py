import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from tests.support.sandbox import Sandbox
from xray_vps_manager.commands import telegram as telegram_command
from xray_vps_manager.db import database
from xray_vps_manager.telegram import admin, poller, settings
from xray_vps_manager.xray import cascade


class TelegramPollerTests(unittest.TestCase):
    def make_context(self, db, updates, events, client_db=None, run_capture=None, send_response=None):
        if client_db is None:
            client_db = {"clients": {}}
        if callable(client_db):
            load_client_db = client_db
        else:
            load_client_db = lambda: client_db
        if run_capture is None:
            run_capture = lambda *args, **kwargs: None

        def load_country_routes():
            snapshot = load_client_db()
            return snapshot, {"outbounds": [{"tag": tag} for tag in snapshot.get("cascadeRoutes", {})]}

        def save_db_sections(updated_db, sections):
            state = updated_db.get("clientSubscriptionState", {})
            events.append(("save", tuple(sections), state.get("userUpdateOffset")))

        def send_chat_message(_db, chat_id, text, reply_markup=None, parse_mode=None):
            events.append(("send", str(chat_id), text))
            if callable(send_response):
                return send_response(_db, chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
            return send_response

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
            load_country_routes=load_country_routes,
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

    def test_owner_can_send_news_notice_from_admin_panel(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 40, "expiryReminders": {}},
            "clientSubscriptions": {
                "222": {"client": "alice", "clientId": "alice-id", "enabled": True},
            },
            "adminState": {},
        }
        updates = [
            {
                "update_id": 41,
                "callback_query": {
                    "id": "callback-news",
                    "data": "admin:notice:news",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
            {
                "update_id": 42,
                "message": {
                    "text": "Добавили выбор страны подключения.",
                    "chat": {"id": "111", "type": "private", "username": "owner"},
                },
            },
            {
                "update_id": 43,
                "callback_query": {
                    "id": "callback-send-news",
                    "data": "admin:notice-send:news",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
        ]
        events = []

        def send_notice(_ctx, _db, message, dry_run=False, yes=False, label="message"):
            events.append(("notice", message, dry_run, yes, label))
            return 0

        ctx = self.make_context(db, updates, events)

        with mock.patch.object(admin, "send_notice_message", side_effect=send_notice):
            self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        preview = [event for event in events if event[0] == "send" and "Предпросмотр: Новости" in event[2]][0]
        self.assertIn("Получателей: 1", preview[2])
        self.assertIn("Vireika: объявление\n\nДобавили выбор страны подключения.", preview[2])
        self.assertIn(
            ("notice", "Vireika: объявление\n\nДобавили выбор страны подключения.", False, True, "Новости"),
            events,
        )
        self.assertNotIn("newsNoticeText", db.get("adminState", {}))
        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Уведомление отправлено подписанным клиентам.", sent[2])

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

    def test_owner_can_add_paid_client_from_admin_panel(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "botUsername": "ExampleVpnBot",
            "paymentTotalAmount": "500",
            "paymentCurrency": "₽",
            "paymentTransferMethod": "phone",
            "paymentPhone": "+79991234567",
            "paymentBank": "Т-Банк (Тинькофф)",
            "clientSubscriptionState": {"userUpdateOffset": 130, "expiryReminders": {}},
            "clientSubscriptions": {},
            "adminState": {},
        }
        updates = [
            {
                "update_id": 131,
                "callback_query": {
                    "id": "callback-add-menu",
                    "data": "admin:client-add",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
            {
                "update_id": 132,
                "message": {
                    "text": "alice 30",
                    "chat": {"id": "111", "type": "private", "username": "owner"},
                },
            },
            {
                "update_id": 133,
                "callback_query": {
                    "id": "callback-add-paid",
                    "data": "admin:client-add-payment:paid",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
        ]
        events = []
        client_db = {
            "connections": {
                "vless-reality": {"tag": "vless-reality", "name": "main", "port": 443},
            },
            "clients": {},
        }

        def run_capture(command, timeout=20, **_kwargs):
            events.append(("run", command, timeout))
            client_db["clients"]["alice"] = {
                "id": "00000000-0000-0000-0000-000000000001",
                "expiresAt": "2099-07-14T00:00:00+03:00",
                "paymentType": "paid",
                "connection": "vless-reality",
            }
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "Added client: alice\n"
                    "Access key: vpn-key:00000000-0000-0000-0000-000000000001\n"
                    "Amount per paid client: 500 ₽\n"
                    "vless://alice-key@example.com:443?type=tcp#Xray"
                ),
                stderr="",
            )

        ctx = self.make_context(db, updates, events, client_db=client_db, run_capture=run_capture)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertIn(
            (
                "run",
                ["/usr/local/sbin/xray-client", "add", "alice", "30", "--connection", "vless-reality", "--payment", "paid"],
                120,
            ),
            events,
        )
        self.assertNotIn("111", db.get("adminState", {}))
        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Клиент добавлен.", sent[2])
        self.assertIn("Ссылка подключения:\n<pre><code>vless://alice-key@example.com:443?type=tcp#Xray</code></pre>", sent[2])
        self.assertIn("Ключ доступа для бота:\n<pre><code>vpn-key:00000000-0000-0000-0000-000000000001</code></pre>", sent[2])
        self.assertIn("По ключу доступа @ExampleVpnBot будет показывать статус подписки", sent[2])
        self.assertIn("Не забудь открыть @ExampleVpnBot и подключить уведомления.", sent[2])
        self.assertIn("Доступ до: 2099-07-14T00:00:00+03:00", sent[2])
        self.assertIn("Сумма оплаты: 500 ₽", sent[2])
        self.assertIn("Перевод нужно выполнить по номеру телефона:\n+79991234567", sent[2])
        self.assertIn("Банк: Т-Банк (Тинькофф)", sent[2])
        self.assertNotIn("Added client: alice", sent[2])

    def test_owner_add_client_selects_saved_connection_from_admin_panel(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 135, "expiryReminders": {}},
            "clientSubscriptions": {},
            "adminState": {},
        }
        updates = [
            {
                "update_id": 136,
                "callback_query": {
                    "id": "callback-add-menu",
                    "data": "admin:client-add",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
            {
                "update_id": 137,
                "message": {
                    "text": "bob",
                    "chat": {"id": "111", "type": "private", "username": "owner"},
                },
            },
            {
                "update_id": 138,
                "callback_query": {
                    "id": "callback-add-free",
                    "data": "admin:client-add-payment:free",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
            {
                "update_id": 139,
                "callback_query": {
                    "id": "callback-add-connection",
                    "data": "admin:client-add-connection:1",
                    "message": {"chat": {"id": "111", "type": "private"}},
                },
            },
        ]
        events = []
        client_db = {
            "connections": {
                "vless-reality": {"tag": "vless-reality", "name": "main", "port": 443},
                "vless-reality-2": {"tag": "vless-reality-2", "name": "second", "port": 8443},
            },
            "clients": {},
        }

        def run_capture(command, timeout=20, **_kwargs):
            events.append(("run", command, timeout))
            client_db["clients"]["bob"] = {
                "id": "00000000-0000-0000-0000-000000000002",
                "expiresAt": "",
                "paymentType": "free",
                "connection": "vless-reality-2",
            }
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "Access key: vpn-key:00000000-0000-0000-0000-000000000002\n"
                    "vless://bob-key@example.com:8443?type=tcp#Xray"
                ),
                stderr="",
            )

        ctx = self.make_context(db, updates, events, client_db=client_db, run_capture=run_capture)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertIn(
            (
                "run",
                ["/usr/local/sbin/xray-client", "add", "bob", "0", "--connection", "vless-reality-2", "--payment", "free"],
                120,
            ),
            events,
        )
        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Оплата: бесплатный клиент", sent[2])
        self.assertNotIn("Сумма оплаты:", sent[2])

    def test_owner_add_client_rejects_invalid_access_days(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 140, "expiryReminders": {}},
            "clientSubscriptions": {},
            "adminState": {
                "111": {
                    "action": "add-client-input",
                    "startedAt": "2026-06-12T22:00:00Z",
                }
            },
        }
        updates = [
            {
                "update_id": 141,
                "message": {
                    "text": "alice месяц",
                    "chat": {"id": "111", "type": "private", "username": "owner"},
                },
            }
        ]
        events = []

        def run_capture(command, timeout=20, **_kwargs):
            events.append(("run", command, timeout))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = self.make_context(db, updates, events, run_capture=run_capture)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertFalse(any(event[0] == "run" for event in events))
        self.assertEqual(db["adminState"]["111"]["action"], "add-client-input")
        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Срок доступа должен быть числом дней.", sent[2])

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

    def test_client_can_enable_activity_notifications_from_menu(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 41, "expiryReminders": {}},
            "clientSubscriptions": {
                "222": {
                    "client": "internal_alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                    "enabled": True,
                    "activityNotificationsEnabled": False,
                }
            },
        }
        updates = [
            {
                "update_id": 42,
                "callback_query": {
                    "id": "callback-activity-on",
                    "data": "client:activity:on",
                    "message": {"chat": {"id": "222", "type": "private"}},
                },
            }
        ]
        events = []
        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"internal_alice": {"id": "00000000-0000-0000-0000-000000000001"}}},
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertTrue(db["clientSubscriptions"]["222"]["activityNotificationsEnabled"])
        self.assertIn(("save", ("clientSubscriptions",), 43), events)
        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Клиентская рассылка: включена.", sent[2])
        self.assertNotIn("internal_alice", sent[2])

    def test_client_can_add_activity_exception_from_notification_candidate(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {
                "userUpdateOffset": 41,
                "expiryReminders": {},
                "callbackGuards": {"222": {"activeMessageId": 90, "consumedMessageIds": []}},
                "activityExceptionCandidates": {
                    "222": {
                        "items": [
                            {
                                "host": "video.example.ru",
                                "port": "443",
                                "regions": "RU",
                                "clientId": "00000000-0000-0000-0000-000000000001",
                            }
                        ],
                        "updatedAt": "2026-06-12T22:00:00Z",
                    }
                },
            },
            "clientSubscriptions": {
                "222": {
                    "client": "internal_alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                    "enabled": True,
                    "activityNotificationsEnabled": True,
                }
            },
        }
        updates = [
            {
                "update_id": 42,
                "callback_query": {
                    "id": "callback-exception-list",
                    "data": "client:activity-exception:list",
                    "message": {"message_id": 120, "chat": {"id": "222", "type": "private"}},
                },
            },
            {
                "update_id": 43,
                "callback_query": {
                    "id": "callback-exception-add",
                    "data": "client:activity-exception:add:0",
                    "message": {"message_id": 121, "chat": {"id": "222", "type": "private"}},
                },
            },
        ]
        events = []
        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"internal_alice": {"id": "00000000-0000-0000-0000-000000000001"}}},
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        exceptions = db["clientSubscriptionState"]["activityNotificationExceptions"]["222"]
        self.assertEqual(exceptions[0]["host"], "video.example.ru")
        self.assertEqual(exceptions[0]["port"], "443")
        self.assertEqual(exceptions[0]["regions"], "RU")
        sent = [event for event in events if event[0] == "send"]
        self.assertIn("Что добавить в исключения?", sent[0][2])
        self.assertIn("Больше не буду присылать личные предупреждения по video.example.ru:443 (RU).", sent[-1][2])
        self.assertNotIn("internal_alice", sent[-1][2])

    def test_client_can_remove_activity_exception_from_activity_menu(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {
                "userUpdateOffset": 44,
                "expiryReminders": {},
                "activityNotificationExceptions": {
                    "222": [
                        {
                            "host": "video.example.ru",
                            "port": "443",
                            "regions": "RU",
                            "clientId": "00000000-0000-0000-0000-000000000001",
                        },
                        {
                            "host": "cdn.example.ru",
                            "port": "443",
                            "regions": "RU",
                            "clientId": "00000000-0000-0000-0000-000000000001",
                        },
                    ]
                },
            },
            "clientSubscriptions": {
                "222": {
                    "client": "internal_alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                    "enabled": True,
                    "activityNotificationsEnabled": True,
                }
            },
        }
        updates = [
            {
                "update_id": 45,
                "callback_query": {
                    "id": "callback-exceptions",
                    "data": "client:activity-exceptions",
                    "message": {"message_id": 122, "chat": {"id": "222", "type": "private"}},
                },
            },
            {
                "update_id": 46,
                "callback_query": {
                    "id": "callback-exception-delete",
                    "data": "client:activity-exception:delete:0",
                    "message": {"message_id": 123, "chat": {"id": "222", "type": "private"}},
                },
            },
        ]
        events = []
        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"internal_alice": {"id": "00000000-0000-0000-0000-000000000001"}}},
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        remaining = db["clientSubscriptionState"]["activityNotificationExceptions"]["222"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["host"], "cdn.example.ru")
        sent = [event for event in events if event[0] == "send"]
        self.assertIn("Личные исключения активности", sent[0][2])
        self.assertIn("Исключение удалено: video.example.ru:443 (RU).", sent[-1][2])
        self.assertNotIn("internal_alice", sent[-1][2])

    def test_duplicate_client_callback_from_same_message_runs_once(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 42, "expiryReminders": {}},
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
                "update_id": 43,
                "callback_query": {
                    "id": "callback-help-1",
                    "data": "client:help",
                    "message": {"message_id": 77, "chat": {"id": "222", "type": "private"}},
                },
            },
            {
                "update_id": 44,
                "callback_query": {
                    "id": "callback-help-2",
                    "data": "client:help",
                    "message": {"message_id": 77, "chat": {"id": "222", "type": "private"}},
                },
            },
        ]
        events = []
        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={"clients": {"alice": {"id": "00000000-0000-0000-0000-000000000001"}}},
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"]
        self.assertEqual(len(sent), 1)
        self.assertIn("Vireika: помощь", sent[0][2])
        guard = db["clientSubscriptionState"]["callbackGuards"]["222"]
        self.assertEqual(guard["consumedMessageIds"], [77])

    def test_old_client_callback_is_blocked_after_new_client_message(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {
                "userUpdateOffset": 44,
                "expiryReminders": {},
                "callbackGuards": {"222": {"activeMessageId": 90, "consumedMessageIds": []}},
            },
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
                    "id": "callback-old-help",
                    "data": "client:help",
                    "message": {"message_id": 89, "chat": {"id": "222", "type": "private"}},
                },
            }
        ]
        events = []
        ctx = self.make_context(db, updates, events)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertFalse(any(event[0] == "send" for event in events))
        guard = db["clientSubscriptionState"]["callbackGuards"]["222"]
        self.assertEqual(guard["activeMessageId"], 90)
        self.assertEqual(guard["consumedMessageIds"], [])

    def test_client_menu_callback_can_open_from_admin_message(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {
                "userUpdateOffset": 46,
                "expiryReminders": {},
                "callbackGuards": {"111": {"activeMessageId": 90, "consumedMessageIds": []}},
            },
            "clientSubscriptions": {},
        }
        updates = [
            {
                "update_id": 47,
                "callback_query": {
                    "id": "callback-client-menu",
                    "data": "client:menu",
                    "message": {"message_id": 50, "chat": {"id": "111", "type": "private"}},
                },
            }
        ]
        events = []
        ctx = self.make_context(db, updates, events)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"]
        self.assertEqual(len(sent), 1)
        self.assertIn("Привет. Я бот Vireika.", sent[0][2])
        guard = db["clientSubscriptionState"]["callbackGuards"]["111"]
        self.assertEqual(guard["consumedMessageIds"], [50])

    def test_client_menu_send_registers_latest_callback_message(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 48, "expiryReminders": {}},
            "clientSubscriptions": {},
        }
        updates = [
            {
                "update_id": 49,
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
            send_response={"ok": True, "result": {"message_id": 91}},
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        guard = db["clientSubscriptionState"]["callbackGuards"]["222"]
        self.assertEqual(guard["activeMessageId"], 91)

    def test_access_key_subscription_sets_subscribed_command_menu(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 45, "expiryReminders": {}},
            "clientSubscriptions": {},
        }
        client_uuid = "00000000-0000-0000-0000-000000000001"
        updates = [
            {
                "update_id": 46,
                "message": {
                    "text": f"vpn-key:{client_uuid}",
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
                "clients": {
                    "alice": {
                        "id": client_uuid,
                        "connection": "vless-reality",
                    }
                },
            },
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        api_calls = [event for event in events if event[0] == "api"]
        self.assertEqual(api_calls[0][1], "setMyCommands")
        self.assertEqual(api_calls[0][2]["scope"], {"type": "chat", "chat_id": "222"})
        self.assertIn({"command": "unsubscribe", "description": "Отписаться от бота"}, api_calls[0][2]["commands"])
        sent = [event[2] for event in events if event[0] == "send"]
        self.assertTrue(any("Подписка подключена." in text for text in sent))
        self.assertTrue(
            any("не позднее чем за 5 дней и ещё раз за 1 день до окончания доступа" in text for text in sent)
        )

    def test_protocol_link_subscription_is_rejected_with_access_key_hint(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 46, "expiryReminders": {}},
            "clientSubscriptions": {},
        }
        updates = [
            {
                "update_id": 47,
                "message": {
                    "text": "vless://00000000-0000-0000-0000-000000000001@vpn.example:443?security=reality",
                    "chat": {"id": "222", "type": "private", "username": "client"},
                },
            }
        ]
        events = []
        ctx = self.make_context(db, updates, events)

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Протокольные ссылки больше не используются", sent[2])
        self.assertIn("vpn-key:00000000-0000-0000-0000-000000000000", sent[2])
        self.assertEqual(db.get("clientSubscriptions"), {})

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
        self.assertNotIn("Я не нашёл ключ доступа", sent[2])

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
        self.assertIn("• получить актуальную VPN-ссылку;", sent[2])
        self.assertIn("• сменить страну подключения;", sent[2])
        self.assertIn("После смены страны переподключи VPN", sent[2])
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
        self.assertIn("После смены страны переподключи VPN", sent[2])
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

    def test_country_menu_shows_current_country_for_subscribed_client(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 86, "expiryReminders": {}},
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
                "update_id": 87,
                "callback_query": {
                    "id": "callback-country",
                    "data": "client:country",
                    "message": {"chat": {"id": "222", "type": "private"}},
                },
            }
        ]
        events = []
        ctx = self.make_context(
            db,
            updates,
            events,
            client_db={
                "cascadeRoutes": {
                    "cascade-de": {"country": "Германия"},
                    "cascade-us": {"country": "США"},
                },
                "clients": {
                    "alice": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "selectedCascadeTag": "cascade-de",
                    }
                },
            },
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Текущая страна: Германия", sent[2])

    def test_selecting_default_country_persists_choice_after_catchall_changes(self) -> None:
        """117-R01: явное нажатие закрепляет страну, которую /status до этого только вычислял."""
        with Sandbox() as box:
            config = box.config()
            config["outbounds"].extend({"tag": tag, "protocol": "vless"} for tag in ("cascade-de", "cascade-us"))
            cascade.activate_cascade_route(config, "cascade-de")
            box.paths.config.write_text(json.dumps(config))
            db = {"enabled": True, "token": "test-token", "chatId": "111",
                  "clientSubscriptions": {"222": {"client": "alice", "enabled": True}}}
            update = {"update_id": 1, "callback_query": {
                "id": "select-de", "data": "client:country:cascade-de",
                "message": {"chat": {"id": 222, "type": "private"}},
            }}

            def run_route(command, timeout):
                result = box.run_cli("client", *command[1:])
                return SimpleNamespace(returncode=result.exit_code, stdout=result.stdout, stderr=result.stderr)

            ctx = replace(
                telegram_command.poller_context(),
                load_db=lambda: db,
                curl_json=mock.Mock(return_value={"ok": True, "result": [update]}),
                send_chat_message=mock.Mock(return_value=None),
                answer_callback_query=mock.Mock(),
                run_capture=mock.Mock(side_effect=run_route),
            )
            with database.session(box.paths.manager_db) as conn:
                self.assertFalse(conn.execute("SELECT selected_cascade_tag FROM clients WHERE name = 'alice'").fetchone()[0])
            self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)
            ctx.run_capture.assert_called_once_with(
                [str(ctx.xray_client), "route", "alice", "cascade-de"], timeout=poller.CLIENT_ROUTE_TIMEOUT,
            )
            cascade.activate_cascade_route(config, "cascade-us")
            box.paths.config.write_text(json.dumps(config))
            self.assertEqual(ctx.load_client_db()["clients"]["alice"]["selectedCascadeTag"], "cascade-de")
            with database.session(box.paths.manager_db) as conn:
                stored = conn.execute("SELECT selected_cascade_tag FROM clients WHERE name = 'alice'").fetchone()[0]
                self.assertEqual(stored, "cascade-de")

    def test_country_menu_and_old_callback_ignore_removed_outbound(self) -> None:
        with Sandbox() as box:
            config = box.config()
            config["outbounds"].append({"tag": "cascade-de", "protocol": "vless"})
            cascade.activate_cascade_route(config, "cascade-de")
            box.paths.config.write_text(json.dumps(config))
            with database.write_session(box.paths.manager_db) as conn:
                conn.execute("UPDATE clients SET selected_cascade_tag = 'cascade-old' WHERE name = 'alice'")
                conn.execute("INSERT INTO cascade_routes(tag, country) VALUES ('cascade-old', 'Архив')")
            before = box.db_rows("clients", "cascade_routes")
            db = {"clientSubscriptions": {"222": {"client": "alice"}}}
            ctx = replace(telegram_command.poller_context(), send_chat_message=mock.Mock(return_value=None),
                          answer_callback_query=mock.Mock(), run_capture=mock.Mock())
            poller.send_client_country_menu(ctx, db, "222")
            buttons = ctx.send_chat_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
            offered = [button["callback_data"] for row in buttons for button in row]
            self.assertIn("client:country:cascade-de", offered)
            self.assertNotIn("client:country:cascade-old", offered)

            update = {"callback_query": {"id": "old", "data": "client:country:cascade-old",
                                         "message": {"chat": {"type": "private", "id": 222}}}}
            self.assertTrue(poller.handle_callback_query(ctx, db, update))
            ctx.answer_callback_query.assert_called_once_with(db, "old", "Страна недоступна")
            ctx.run_capture.assert_not_called()
            self.assertEqual(box.db_rows("clients", "cascade_routes"), before)

    def test_country_unavailable_when_config_cannot_be_read(self) -> None:
        with Sandbox() as box:
            with database.write_session(box.paths.manager_db) as conn:
                conn.execute("UPDATE clients SET selected_cascade_tag = 'cascade-de' WHERE name = 'alice'")
                conn.execute("INSERT INTO cascade_routes(tag, country) VALUES ('cascade-de', 'Германия')")
            box.paths.config.unlink()
            db = {"clientSubscriptions": {"222": {"client": "alice"}}}
            ctx = replace(
                telegram_command.poller_context(),
                send_chat_message=mock.Mock(return_value=None),
                answer_callback_query=mock.Mock(),
                run_capture=mock.Mock(),
            )
            for action in ("client:country", "client:country:cascade-de"):
                with self.subTest(action=action):
                    ctx.send_chat_message.reset_mock()
                    update = {"callback_query": {
                        "id": "choice", "data": action, "message": {"chat": {"type": "private", "id": 222}},
                    }}
                    self.assertTrue(poller.handle_callback_query(ctx, db, update))
                    ctx.send_chat_message.assert_called_once()
                    self.assertEqual(ctx.send_chat_message.call_args.args[2],
                                     "Сейчас нет доступных для выбора стран. Попробуй позже или обратись к администратору.")
            ctx.run_capture.assert_not_called()

    def test_selecting_current_country_does_not_call_route_command(self) -> None:
        db = {
            "enabled": True,
            "token": "token",
            "chatId": "111",
            "clientSubscriptionState": {"userUpdateOffset": 88, "expiryReminders": {}},
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
                "update_id": 89,
                "callback_query": {
                    "id": "callback-country-de",
                    "data": "client:country:cascade-de",
                    "message": {"chat": {"id": "222", "type": "private"}},
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
            client_db={
                "cascadeRoutes": {
                    "cascade-de": {"country": "Германия"},
                    "cascade-us": {"country": "США"},
                },
                "clients": {
                    "alice": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "selectedCascadeTag": "cascade-de",
                    }
                },
            },
            run_capture=run_capture,
        )

        self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)

        self.assertFalse(any(event[0] == "run" for event in events))
        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Эта страна уже выбрана: Германия.", sent[2])

    def test_selecting_new_country_mentions_vpn_reconnect(self) -> None:
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
                "update_id": 91,
                "callback_query": {
                    "id": "callback-country-us",
                    "data": "client:country:cascade-us",
                    "message": {"chat": {"id": "222", "type": "private"}},
                },
            }
        ]
        events = []
        snapshots = [
            {
                "cascadeRoutes": {
                    "cascade-de": {"country": "Германия"},
                    "cascade-us": {"country": "США"},
                },
                "clients": {
                    "alice": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "selectedCascadeTag": "cascade-de",
                    }
                },
            },
            {
                "cascadeRoutes": {
                    "cascade-de": {"country": "Германия"},
                    "cascade-us": {"country": "США"},
                },
                "clients": {
                    "alice": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "selectedCascadeTag": "cascade-us",
                    }
                },
            },
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

        self.assertIn(
            ("run", ["/usr/local/sbin/xray-client", "route", "alice", "cascade-us"], poller.CLIENT_ROUTE_TIMEOUT),
            events,
        )
        sent = [event for event in events if event[0] == "send"][-1]
        self.assertIn("Страна подключения изменена: США.", sent[2])
        self.assertIn("Переподключи VPN", sent[2])

    def test_conflicting_stop_does_not_acknowledge_or_roll_back_another_chat(self) -> None:
        events = []
        stored_at_menu_reset = []
        updates = [
            {"update_id": 42, "message": {"chat": {"id": 42, "type": "private"}, "text": "/stop"}},
            {"update_id": 43, "callback_query": {
                "id": "stop-43", "data": "client:unsubscribe", "message": {"chat": {"id": 43, "type": "private"}},
            }},
        ]
        with Sandbox():
            settings.save_db({
                "enabled": True, "token": "fixture", "chatId": "999",
                "clientSubscriptions": {chat: {"clientId": "first"} for chat in ("42", "43")},
            })
            ctx = self.make_context({}, updates, events)

            def transport(db, method, payload=None, timeout=30):
                if method == "getUpdates":
                    with database.write_session() as conn:
                        settings.save_subscription(conn, "42", {"clientId": "next"})
                elif method == "deleteMyCommands":
                    stored_at_menu_reset.append((payload["scope"]["chat_id"], set(settings.load_db()["clientSubscriptions"])))
                return {"ok": True, "result": updates}

            ctx = replace(ctx, load_db=settings.load_db, save_db_sections=settings.save_db_sections, curl_json=transport)
            self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)
            saved = settings.load_db()
        self.assertEqual(stored_at_menu_reset, [("43", {"42"})])
        self.assertEqual(saved["clientSubscriptionState"]["userUpdateOffset"], 44)
        self.assertEqual(set(saved["clientSubscriptions"]), {"42"})
        self.assertEqual(saved["clientSubscriptions"]["42"]["clientId"], "next")
        messages = {event[1]: event[2] for event in events if event[0] == "send"}
        self.assertIn("Не удалось обработать действие", messages["42"])
        self.assertIn("Подписка на бота отключена", messages["43"])
        self.assertNotIn("Подписка на бота отключена", messages["42"])

    def test_conflicting_subscription_button_answers_callback_once(self) -> None:
        for action in ("client:unsubscribe", "client:activity:on"):
            with self.subTest(action), Sandbox():
                settings.save_db({
                    "enabled": True, "token": "fixture", "chatId": "999",
                    "clientSubscriptions": {"42": {"clientId": "first"}},
                })
                update = {"update_id": 1, "callback_query": {
                    "id": "button", "data": action, "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}},
                }}
                events = []
                answers = []
                ctx = self.make_context({}, [update], events)

                def transport(db, method, payload=None, timeout=30):
                    if method == "getUpdates":
                        with database.write_session() as conn:
                            settings.save_subscription(conn, "42", {"clientId": "next"})
                    return {"ok": True, "result": [update]}

                ctx = replace(ctx, load_db=settings.load_db, save_db_sections=settings.save_db_sections,
                              curl_json=transport, answer_callback_query=lambda db, uid, text: answers.append((uid, text)))
                self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)
                self.assertEqual(answers, [("button", "Не удалось сохранить подписку")])
                self.assertTrue(any(e[0] == "send" and "Не удалось обработать действие" in e[2] for e in events))
                self.assertEqual(settings.load_db()["clientSubscriptions"]["42"]["clientId"], "next")

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
