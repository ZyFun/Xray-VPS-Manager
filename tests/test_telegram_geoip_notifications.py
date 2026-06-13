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
            sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def make_context(self, db: dict, messages: list[str], manager_db_path: Path):
        def save_sections(updated_db, sections):
            db.update(updated_db)
            self.assertEqual(sections, ("geoipState",))

        return notifications.NotificationContext(
            load_db=lambda: db,
            save_db_sections=save_sections,
            load_client_db=lambda: {"clients": {}},
            load_traffic_db=lambda: {"clients": {}},
            display_timezone=lambda: (timezone.utc, "UTC"),
            format_event_time=lambda value: value,
            format_access_until=lambda value: value,
            parse_time=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None,
            utc_now=lambda: datetime(2026, 6, 12, 8, 2, tzinfo=timezone.utc),
            utc_stamp=lambda: "2026-06-12T08:02:00Z",
            run_capture=lambda *args, **kwargs: None,
            send_chat_message=lambda *args, **kwargs: None,
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
            self.assertEqual(telegram_db["geoipState"]["sqliteLastEventId"], 2)
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
            self.assertEqual(telegram_db["geoipState"]["sqliteLastEventId"], 2)
