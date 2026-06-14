from pathlib import Path
import tempfile
import unittest

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.repositories import telegram as sqlite_telegram
from xray_vps_manager.telegram import settings as telegram_settings


def telegram_db() -> dict:
    return {
        "version": 1,
        "enabled": True,
        "token": "json-token",
        "botName": "JsonBot",
        "botUsername": "JsonVpnBot",
        "chatId": "111",
        "chatLabel": "owner",
        "routeMode": "cascade",
        "paymentTotalAmount": "500",
        "paymentCurrency": "$",
        "paymentRoundingMode": "step",
        "paymentRoundingStep": "50",
        "paymentTransferMethod": "phone",
        "paymentPhone": "+79991234567",
        "paymentBank": "Т-Банк (Тинькофф)",
        "paymentCard": "",
        "paymentBankAccount": "",
        "geoipState": {"sentIds": ["geo-1"]},
        "clientSubscriptionState": {"userUpdateOffset": 10, "expiryReminders": {"111": "sent"}},
        "dailySummaryState": {"lastSentDay": "2026-06-12"},
        "adminState": {"mode": "idle"},
        "clientSubscriptions": {
            "111": {
                "client": "sqlite_client",
                "clientId": "00000000-0000-0000-0000-000000000001",
                "connection": "vless-reality",
                "chatLabel": "owner",
                "linkHash": "hash-1",
                "subscribedAt": "2026-06-12T08:00:00Z",
                "enabled": True,
            },
            "222": {
                "client": "missing_client",
                "clientId": "00000000-0000-0000-0000-000000000002",
                "connection": "vless-reality",
                "chatLabel": "guest",
                "linkHash": "hash-2",
                "subscribedAt": "2026-06-12T09:00:00Z",
                "enabled": False,
            },
        },
    }


class TelegramSettingsWriteTests(unittest.TestCase):
    def make_sqlite_db(self, path: Path, *, ready: bool = True) -> None:
        connection = database.open_database(path)
        try:
            sqlite_connections.upsert_connection(
                connection,
                "vless-reality",
                {
                    "tag": "vless-reality",
                    "name": "default",
                    "created": "2026-06-12T07:00:00Z",
                    "port": 443,
                    "sni": "example.com",
                    "dest": "example.com:443",
                    "fingerprint": "chrome",
                },
            )
            sqlite_clients.upsert_client(
                connection,
                "sqlite_client",
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "created": "2026-06-12T07:01:00Z",
                    "enabled": True,
                    "connection": "vless-reality",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "email": "sqlite_client|created=2026-06-12T07:01:00Z",
                    },
                },
            )
            sqlite_telegram.set_setting(connection, "botName", "OldBot")
            sqlite_settings.set_payment_setting(connection, "paymentTotalAmount", "1")
            sqlite_settings.set_payment_setting(connection, "paymentTransferMethod", "none")
            sqlite_telegram.set_state(connection, "dailySummaryState", {"lastSentDay": "old"})
            sqlite_telegram.upsert_subscription(
                connection,
                {
                    "chatId": "999",
                    "chatLabel": "old",
                    "clientName": "sqlite_client",
                    "clientUuid": "00000000-0000-0000-0000-000000000009",
                    "connection": "vless-reality",
                    "linkSignature": {"linkHash": "old"},
                    "enabled": True,
                    "createdAt": "2026-06-12T07:30:00Z",
                },
            )
            if ready:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def test_save_writes_telegram_settings_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            json_path = Path(tmp_dir) / "telegram-bot.json"
            self.make_sqlite_db(db_path)

            telegram_settings.save_db(telegram_db(), json_path, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                self.assertEqual(sqlite_telegram.get_setting(connection, "botName"), "JsonBot")
                self.assertEqual(sqlite_telegram.get_setting(connection, "botUsername"), "JsonVpnBot")
                self.assertEqual(sqlite_telegram.get_setting(connection, "enabled"), "true")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentTotalAmount"), "500")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentCurrency"), "$")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentTransferMethod"), "phone")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentPhone"), "+79991234567")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentBank"), "Т-Банк (Тинькофф)")
                self.assertEqual(sqlite_telegram.get_state(connection, "dailySummaryState"), {"lastSentDay": "2026-06-12"})
                subscriptions = sqlite_telegram.list_subscriptions(connection)
            finally:
                connection.close()
            self.assertEqual({item["chatId"] for item in subscriptions}, {"111", "222"})
            by_chat = {item["chatId"]: item for item in subscriptions}
            self.assertEqual(by_chat["111"]["clientName"], "sqlite_client")
            self.assertEqual(by_chat["111"]["linkSignature"], {"linkHash": "hash-1"})
            self.assertEqual(by_chat["222"]["clientName"], "")
            self.assertFalse(by_chat["222"]["enabled"])
            self.assertFalse(json_path.exists())

    def test_save_fails_when_sqlite_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "missing.db"
            json_path = Path(tmp_dir) / "telegram-bot.json"

            with self.assertRaisesRegex(RuntimeError, "manager database is missing"):
                telegram_settings.save_db(telegram_db(), json_path, db_path=db_path)

            self.assertFalse(json_path.exists())
            self.assertFalse(db_path.exists())

    def test_save_fails_when_sqlite_database_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            json_path = Path(tmp_dir) / "telegram-bot.json"
            self.make_sqlite_db(db_path, ready=False)

            with self.assertRaisesRegex(RuntimeError, "database is not marked ready"):
                telegram_settings.save_db(telegram_db(), json_path, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                self.assertEqual(sqlite_telegram.get_setting(connection, "botName"), "OldBot")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentTotalAmount"), "1")
                self.assertEqual(len(sqlite_telegram.list_subscriptions(connection)), 1)
            finally:
                connection.close()
            self.assertFalse(json_path.exists())

    def test_save_sections_updates_only_requested_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            json_path = Path(tmp_dir) / "telegram-bot.json"
            self.make_sqlite_db(db_path)

            telegram_settings.save_db_sections({"enabled": True}, ["enabled"], json_path, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                self.assertEqual(sqlite_telegram.get_setting(connection, "botName"), "OldBot")
                self.assertEqual(sqlite_telegram.get_setting(connection, "enabled"), "true")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentTotalAmount"), "1")
                self.assertEqual(len(sqlite_telegram.list_subscriptions(connection)), 1)
            finally:
                connection.close()
            self.assertFalse(json_path.exists())

    def test_save_state_section_preserves_existing_expiry_reminders_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            json_path = Path(tmp_dir) / "telegram-bot.json"
            self.make_sqlite_db(db_path)
            connection = database.open_database(db_path)
            try:
                sqlite_telegram.set_state(
                    connection,
                    "clientSubscriptionState",
                    {
                        "userUpdateOffset": 100,
                        "expiryReminders": {"sent-key": "2026-06-13T07:00:00Z"},
                        "lastExpiryReminderCheck": "2026-06-13T07:00:00Z",
                    },
                )
            finally:
                connection.close()

            telegram_settings.save_db_sections(
                {
                    "clientSubscriptionState": {
                        "userUpdateOffset": 90,
                        "expiryReminders": {},
                        "lastUserPoll": "2026-06-13T07:01:00Z",
                        "lastExpiryReminderCheck": "2026-06-12T22:00:00Z",
                    }
                },
                ["clientSubscriptionState"],
                json_path,
                db_path=db_path,
            )

            connection = database.open_database(db_path)
            try:
                state = sqlite_telegram.get_state(connection, "clientSubscriptionState")
                self.assertEqual(state["userUpdateOffset"], 100)
                self.assertEqual(state["expiryReminders"], {"sent-key": "2026-06-13T07:00:00Z"})
                self.assertEqual(state["lastExpiryReminderCheck"], "2026-06-13T07:00:00Z")
                self.assertEqual(state["lastUserPoll"], "2026-06-13T07:01:00Z")
            finally:
                connection.close()
            self.assertFalse(json_path.exists())

    def test_save_state_section_does_not_rewrite_sqlite_subscriptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            json_path = Path(tmp_dir) / "telegram-bot.json"
            self.make_sqlite_db(db_path)

            telegram_settings.save_db_sections(
                {
                    "clientSubscriptions": {},
                    "clientSubscriptionState": {"userUpdateOffset": 11, "expiryReminders": {}},
                },
                ["clientSubscriptionState"],
                json_path,
                db_path=db_path,
            )

            connection = database.open_database(db_path)
            try:
                subscriptions = sqlite_telegram.list_subscriptions(connection)
            finally:
                connection.close()
            self.assertEqual(len(subscriptions), 1)
            self.assertEqual(subscriptions[0]["chatId"], "999")
            self.assertFalse(json_path.exists())


if __name__ == "__main__":
    unittest.main()
