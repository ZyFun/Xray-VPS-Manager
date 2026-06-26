from datetime import datetime, timezone
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.telegram import notifications


class TelegramGeoIPNotificationSQLiteTests(unittest.TestCase):
    def make_sqlite_db(self, path: Path) -> None:
        connection = database.open_database(path)
        try:
            sqlite_connections.upsert_connection(
                connection,
                "vless-reality",
                {
                    "tag": "vless-reality",
                    "name": "default",
                    "port": 443,
                    "sni": "example.com",
                    "dest": "example.com:443",
                    "fingerprint": "chrome",
                },
            )
            sqlite_clients.upsert_client(
                connection,
                "alice",
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "created": "2026-06-12T08:00:00Z",
                    "enabled": True,
                    "connection": "vless-reality",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "email": "alice|created=2026-06-12T08:00:00Z",
                    },
                },
            )
            sqlite_clients.upsert_client(
                connection,
                "bob",
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "created": "2026-06-12T08:00:00Z",
                    "enabled": True,
                    "connection": "vless-reality",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000002",
                        "email": "bob|created=2026-06-12T08:00:00Z",
                    },
                },
            )
            sqlite_activity.add_event(
                connection,
                {
                    "time": "2026-06-12T07:59:00Z",
                    "client": "alice",
                    "host": "old.example.ru",
                    "port": "443",
                    "outbound": "geoip-warning-RU",
                    "risks": ["xray-geoip:RU"],
                },
            )
            sqlite_activity.add_event(
                connection,
                {
                    "time": "2026-06-12T08:01:00Z",
                    "client": "alice",
                    "host": "new.example.ru",
                    "port": "443",
                    "outbound": "geoip-warning-RU",
                    "risks": ["xray-geoip:RU"],
                },
            )
            sqlite_activity.add_event(
                connection,
                {
                    "time": "2026-06-12T08:01:30Z",
                    "client": "bob",
                    "host": "bob.example.ru",
                    "port": "443",
                    "outbound": "geoip-warning-RU",
                    "risks": ["xray-geoip:RU"],
                },
            )
            sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def make_context(
        self,
        db: dict,
        messages: list[str],
        manager_db_path: Path,
        client_messages=None,
        client_db=None,
        utc_stamp: str = "2026-06-12T08:02:00Z",
    ):
        def save_sections(updated_db, sections):
            db.update(updated_db)
            self.assertIn(sections, (("geoipState",), ("geoipState", "clientSubscriptionState")))

        if client_messages is None:
            client_messages = []
        if client_db is None:
            client_db = {"clients": {}}

        return notifications.NotificationContext(
            load_db=lambda: db,
            save_db_sections=save_sections,
            load_client_db=lambda: client_db,
            load_traffic_db=lambda: {"clients": {}},
            display_timezone=lambda: (timezone.utc, "UTC"),
            format_event_time=lambda value: value,
            format_access_until=lambda value: value,
            parse_time=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None,
            utc_now=lambda: datetime.fromisoformat(utc_stamp.replace("Z", "+00:00")),
            utc_stamp=lambda: utc_stamp,
            run_capture=lambda *args, **kwargs: None,
            send_chat_message=lambda _db, chat_id, text, **kwargs: client_messages.append(
                {
                    "chat_id": str(chat_id),
                    "text": text,
                    "reply_markup": kwargs.get("reply_markup"),
                }
            ),
            send_message=lambda _db, text, parse_mode=None: messages.append(text),
            bot_name=lambda _db=None: "Bot",
            manager_db_path=manager_db_path,
        )

    def test_notify_geoip_reads_new_events_from_sqlite_after_cutover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            telegram_db = {
                "enabled": True,
                "token": "token",
                "chatId": "1",
                "geoipState": {
                    "lastGeoipNotification": "2026-06-12T08:00:00Z",
                    "sentIds": [],
                },
            }
            sent_messages: list[str] = []
            ctx = self.make_context(telegram_db, sent_messages, db_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                result = notifications.notify_geoip(ctx)

            self.assertEqual(result, 0)
            self.assertEqual(len(sent_messages), 1)
            self.assertIn("new.example.ru", sent_messages[0])
            self.assertNotIn("old.example.ru", sent_messages[0])
            self.assertIn("bob.example.ru", sent_messages[0])
            self.assertEqual(telegram_db["geoipState"]["sqliteLastEventId"], 3)
            self.assertEqual(telegram_db["geoipState"]["lastGeoipNotification"], "2026-06-12T08:02:00Z")

    def test_notify_geoip_initializes_sqlite_offset_when_no_notification_anchor_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            telegram_db = {"enabled": True, "token": "token", "chatId": "1", "geoipState": {"sentIds": []}}
            sent_messages: list[str] = []
            ctx = self.make_context(telegram_db, sent_messages, db_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                result = notifications.notify_geoip(ctx)

            self.assertEqual(result, 0)
            self.assertEqual(sent_messages, [])
            self.assertEqual(telegram_db["geoipState"]["sqliteLastEventId"], 3)

    def test_notify_geoip_marks_alert_events_admin_notified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            connection = database.open_database(db_path)
            try:
                alert_id = sqlite_activity.add_alert_event(
                    connection,
                    {
                        "time": "2026-06-12T08:01:00Z",
                        "client": "alice",
                        "host": "alert.example.ru",
                        "port": "443",
                        "outbound": "geoip-warning-RU",
                        "risks": ["xray-geoip:RU"],
                    },
                    "xray-geoip:RU",
                )
            finally:
                connection.close()
            telegram_db = {
                "enabled": True,
                "token": "token",
                "chatId": "1",
                "geoipState": {
                    "lastGeoipNotification": "2026-06-12T08:00:00Z",
                    "sentIds": [],
                },
            }
            sent_messages: list[str] = []
            ctx = self.make_context(telegram_db, sent_messages, db_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                result = notifications.notify_geoip(ctx)

            connection = database.open_database(db_path)
            try:
                alerts = sqlite_activity.list_alert_events(connection, limit=10)
            finally:
                connection.close()
            self.assertEqual(result, 0)
            self.assertEqual(len(sent_messages), 1)
            self.assertEqual(telegram_db["geoipState"]["sqliteLastAlertId"], alert_id)
            self.assertEqual(alerts[0]["notified_admin_at"], "2026-06-12T08:02:00Z")

    def test_notify_geoip_resends_updated_alert_group_after_admin_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            connection = database.open_database(db_path)
            try:
                alert_id = sqlite_activity.add_alert_event(
                    connection,
                    {
                        "time": "2026-06-12T08:01:00Z",
                        "client": "alice",
                        "host": "alert.example.ru",
                        "port": "443",
                        "outbound": "geoip-warning-RU",
                        "risks": ["xray-geoip:RU"],
                    },
                    "xray-geoip:RU",
                )
            finally:
                connection.close()
            telegram_db = {
                "enabled": True,
                "token": "token",
                "chatId": "1",
                "geoipState": {
                    "lastGeoipNotification": "2026-06-12T08:00:00Z",
                    "sentIds": [],
                },
            }
            sent_messages: list[str] = []
            ctx = self.make_context(telegram_db, sent_messages, db_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(notifications.notify_geoip(ctx), 0)

            connection = database.open_database(db_path)
            try:
                sqlite_activity.add_alert_event(
                    connection,
                    {
                        "time": "2026-06-12T08:05:00Z",
                        "client": "alice",
                        "host": "alert.example.ru",
                        "port": "443",
                        "outbound": "geoip-warning-RU",
                        "risks": ["xray-geoip:RU"],
                    },
                    "xray-geoip:RU",
                )
            finally:
                connection.close()

            sent_messages.clear()
            ctx = self.make_context(telegram_db, sent_messages, db_path, utc_stamp="2026-06-12T08:06:00Z")
            with mock.patch.dict(os.environ, {}, clear=True):
                result = notifications.notify_geoip(ctx)

            connection = database.open_database(db_path)
            try:
                alerts = sqlite_activity.list_alert_events(connection, limit=10)
            finally:
                connection.close()
            self.assertEqual(result, 0)
            self.assertEqual(len(sent_messages), 1)
            self.assertIn("alert.example.ru", sent_messages[0])
            self.assertEqual(telegram_db["geoipState"]["sqliteLastAlertId"], alert_id)
            self.assertEqual(alerts[0]["event_count"], 2)
            self.assertEqual(alerts[0]["notified_admin_at"], "2026-06-12T08:06:00Z")

    def test_geoip_alert_reader_keeps_offset_at_last_returned_alert_when_limited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            connection = database.open_database(db_path)
            try:
                alert_ids = [
                    sqlite_activity.add_alert_event(
                        connection,
                        {
                            "time": f"2026-06-12T08:0{index}:00Z",
                            "client": "alice",
                            "host": f"limited-{index}.example.ru",
                            "port": "443",
                            "outbound": "geoip-warning-RU",
                            "risks": ["xray-geoip:RU"],
                        },
                        "xray-geoip:RU",
                    )
                    for index in range(1, 4)
                ]
            finally:
                connection.close()

            events, last_alert_id = notifications.activity_repository.geoip_alerts_after_for_read(
                after_time="2026-06-12T08:00:00Z",
                limit=2,
                db_path=db_path,
            )

            self.assertEqual([event["alertId"] for event in events], alert_ids[:2])
            self.assertEqual(last_alert_id, alert_ids[1])

    def test_notify_geoip_sends_client_activity_without_internal_client_name_or_owner_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            telegram_db = {
                "enabled": True,
                "token": "token",
                "chatId": "1",
                "geoipState": {
                    "lastGeoipNotification": "2026-06-12T08:00:00Z",
                    "sentIds": [],
                    "clientSentIds": {},
                },
                "clientSubscriptions": {
                    "1": {
                        "client": "bob",
                        "clientId": "00000000-0000-0000-0000-000000000002",
                        "enabled": True,
                        "activityNotificationsEnabled": True,
                    },
                    "2": {
                        "client": "alice",
                        "clientId": "00000000-0000-0000-0000-000000000001",
                        "enabled": True,
                        "activityNotificationsEnabled": True,
                    },
                    "3": {
                        "client": "bob",
                        "clientId": "00000000-0000-0000-0000-000000000002",
                        "enabled": True,
                        "activityNotificationsEnabled": False,
                    },
                },
            }
            admin_messages: list[str] = []
            client_messages: list[tuple[str, str]] = []
            ctx = self.make_context(
                telegram_db,
                admin_messages,
                db_path,
                client_messages=client_messages,
                client_db={
                    "clients": {
                        "alice": {"id": "00000000-0000-0000-0000-000000000001"},
                        "bob": {"id": "00000000-0000-0000-0000-000000000002"},
                    }
                },
            )

            with mock.patch.dict(os.environ, {}, clear=True):
                result = notifications.notify_geoip(ctx)

            self.assertEqual(result, 0)
            self.assertEqual(len(admin_messages), 1)
            self.assertIn("Клиент: alice", admin_messages[0])
            self.assertIn("Клиент: bob", admin_messages[0])
            self.assertEqual(len(client_messages), 1)
            client_message = client_messages[0]
            self.assertEqual(client_message["chat_id"], "2")
            client_text = client_message["text"]
            self.assertIn("new.example.ru", client_text)
            self.assertIn("Что это значит:", client_text)
            self.assertIn("split tunneling", client_text)
            self.assertNotIn("old.example.ru", client_text)
            self.assertNotIn("bob.example.ru", client_text)
            self.assertNotIn("alice", client_text)
            self.assertNotIn("Клиент:", client_text)
            self.assertNotIn("Outbound:", client_text)
            self.assertEqual(
                client_message["reply_markup"],
                {
                    "inline_keyboard": [
                        [{"text": "Добавить в исключения", "callback_data": "client:activity-exception:list"}],
                    ]
                },
            )
            candidates = telegram_db["clientSubscriptionState"]["activityExceptionCandidates"]["2"]["items"]
            self.assertEqual(candidates[0]["host"], "new.example.ru")
            self.assertIn("2", telegram_db["geoipState"]["clientSentIds"])
            self.assertNotIn("1", telegram_db["geoipState"]["clientSentIds"])
            self.assertNotIn("3", telegram_db["geoipState"]["clientSentIds"])

    def test_notify_geoip_skips_client_activity_personal_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            telegram_db = {
                "enabled": True,
                "token": "token",
                "chatId": "1",
                "geoipState": {
                    "lastGeoipNotification": "2026-06-12T08:00:00Z",
                    "sentIds": [],
                    "clientSentIds": {},
                },
                "clientSubscriptionState": {
                    "activityNotificationExceptions": {
                        "2": [
                            {
                                "host": "new.example.ru",
                                "port": "443",
                                "regions": "RU",
                                "clientId": "00000000-0000-0000-0000-000000000001",
                            }
                        ]
                    }
                },
                "clientSubscriptions": {
                    "2": {
                        "client": "alice",
                        "clientId": "00000000-0000-0000-0000-000000000001",
                        "enabled": True,
                        "activityNotificationsEnabled": True,
                    },
                },
            }
            admin_messages: list[str] = []
            client_messages: list[dict] = []
            ctx = self.make_context(
                telegram_db,
                admin_messages,
                db_path,
                client_messages=client_messages,
                client_db={"clients": {"alice": {"id": "00000000-0000-0000-0000-000000000001"}}},
            )

            with mock.patch.dict(os.environ, {}, clear=True):
                result = notifications.notify_geoip(ctx)

            self.assertEqual(result, 0)
            self.assertEqual(len(admin_messages), 1)
            self.assertIn("new.example.ru", admin_messages[0])
            self.assertEqual(client_messages, [])

    def test_notify_geoip_warns_without_exception_button_when_many_client_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            connection = database.open_database(db_path)
            try:
                for index in range(6):
                    sqlite_activity.add_event(
                        connection,
                        {
                            "time": f"2026-06-12T08:01:{40 + index:02d}Z",
                            "client": "alice",
                            "host": f"many-{index}.example.ru",
                            "port": "443",
                            "outbound": "geoip-warning-RU",
                            "risks": ["xray-geoip:RU"],
                        },
                    )
            finally:
                connection.close()
            telegram_db = {
                "enabled": True,
                "token": "token",
                "chatId": "1",
                "geoipState": {
                    "lastGeoipNotification": "2026-06-12T08:00:00Z",
                    "sentIds": [],
                    "clientSentIds": {},
                },
                "clientSubscriptions": {
                    "2": {
                        "client": "alice",
                        "clientId": "00000000-0000-0000-0000-000000000001",
                        "enabled": True,
                        "activityNotificationsEnabled": True,
                    },
                },
            }
            admin_messages: list[str] = []
            client_messages: list[dict] = []
            ctx = self.make_context(
                telegram_db,
                admin_messages,
                db_path,
                client_messages=client_messages,
                client_db={"clients": {"alice": {"id": "00000000-0000-0000-0000-000000000001"}}},
            )

            with mock.patch.dict(os.environ, {}, clear=True):
                result = notifications.notify_geoip(ctx)

            self.assertEqual(result, 0)
            self.assertEqual(len(client_messages), 1)
            self.assertIn("Предупреждений много.", client_messages[0]["text"])
            self.assertIn("geoip:RU", client_messages[0]["text"])
            self.assertIsNone(client_messages[0]["reply_markup"])
            self.assertEqual(
                telegram_db["clientSubscriptionState"]["activityExceptionCandidates"]["2"]["items"],
                [],
            )

    def test_client_geoip_message_mentions_bypass_without_claiming_issue_is_solved(self) -> None:
        ctx = self.make_context({}, [], Path("/tmp/manager.db"))

        text = notifications.build_client_geoip_message(
            ctx,
            {},
            [
                {
                    "time": "2026-06-12T08:01:00Z",
                    "host": "example.ru",
                    "port": "443",
                    "outbound": "geoip-warning-RU",
                    "risks": ["xray-geoip:RU", "xray-bypass:RU"],
                }
            ],
        )

        self.assertIn("GeoIP bypass", text)
        self.assertIn("Direct/Bypass", text)
        self.assertIn("split tunneling", text)
