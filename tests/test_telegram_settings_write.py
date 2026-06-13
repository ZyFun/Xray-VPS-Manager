from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest import mock

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


class TelegramSettingsWriteSwitchTests(unittest.TestCase):
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

    def read_json_file(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def write_json_file(self, path: Path, db: dict) -> None:
        path.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n")

    def save_with_mocked_permissions(self, db: dict, json_path: Path, db_path: Path) -> None:
        with mock.patch.object(telegram_settings, "chown_xray"), mock.patch.object(telegram_settings.os, "chmod"):
            telegram_settings.save_db(db, json_path, db_path=db_path)

    def save_sections_with_mocked_permissions(
        self,
        db: dict,
        sections: list[str],
        json_path: Path,
        db_path: Path,
    ) -> None:
        with mock.patch.object(telegram_settings, "chown_xray"), mock.patch.object(telegram_settings.os, "chmod"):
            telegram_settings.save_db_sections(db, sections, json_path, db_path=db_path)

    def test_save_writes_json_only_when_sqlite_write_flag_is_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "telegram-bot.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                self.save_with_mocked_permissions(telegram_db(), json_path, db_path)

            self.assertEqual(self.read_json_file(json_path)["botName"], "JsonBot")
            connection = database.open_database(db_path)
            try:
                self.assertEqual(sqlite_telegram.get_setting(connection, "botName"), "OldBot")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentTotalAmount"), "1")
                self.assertEqual(len(sqlite_telegram.list_subscriptions(connection)), 1)
            finally:
                connection.close()

    def test_save_writes_telegram_settings_to_sqlite_when_write_flag_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "telegram-bot.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True):
                self.save_with_mocked_permissions(telegram_db(), json_path, db_path)

            connection = database.open_database(db_path)
            try:
                self.assertEqual(sqlite_telegram.get_setting(connection, "botName"), "JsonBot")
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

    def test_save_uses_sqlite_as_primary_when_read_and_write_flags_are_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "telegram-bot.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            self.write_json_file(json_path, {"botName": "RollbackBot", "clientSubscriptions": {}})

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                self.save_with_mocked_permissions(telegram_db(), json_path, db_path)

            connection = database.open_database(db_path)
            try:
                self.assertEqual(sqlite_telegram.get_setting(connection, "botName"), "JsonBot")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentTotalAmount"), "500")
                subscriptions = sqlite_telegram.list_subscriptions(connection)
            finally:
                connection.close()
            self.assertEqual({item["chatId"] for item in subscriptions}, {"111", "222"})
            self.assertEqual(self.read_json_file(json_path)["botName"], "RollbackBot")

    def test_save_fails_when_sqlite_write_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "telegram-bot.json"
            missing_db_path = root / "missing.db"

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "manager database is missing"):
                    self.save_with_mocked_permissions(telegram_db(), json_path, missing_db_path)

            self.assertFalse(json_path.exists())
            self.assertFalse(missing_db_path.exists())

    def test_save_fails_when_sqlite_primary_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "telegram-bot.json"
            missing_db_path = root / "missing.db"

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "manager database is missing"):
                    self.save_with_mocked_permissions(telegram_db(), json_path, missing_db_path)

            self.assertFalse(json_path.exists())
            self.assertFalse(missing_db_path.exists())

    def test_save_fails_when_sqlite_write_import_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "telegram-bot.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path, ready=False)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_WRITES": "1"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "JSON import is not marked ready"):
                    self.save_with_mocked_permissions(telegram_db(), json_path, db_path)

            self.assertFalse(json_path.exists())
            connection = database.open_database(db_path)
            try:
                self.assertEqual(sqlite_telegram.get_setting(connection, "botName"), "OldBot")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentTotalAmount"), "1")
                self.assertEqual(len(sqlite_telegram.list_subscriptions(connection)), 1)
            finally:
                connection.close()

    def test_save_fails_when_sqlite_primary_import_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "telegram-bot.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path, ready=False)

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "JSON import is not marked ready"):
                    self.save_with_mocked_permissions(telegram_db(), json_path, db_path)

            self.assertFalse(json_path.exists())
            connection = database.open_database(db_path)
            try:
                self.assertEqual(sqlite_telegram.get_setting(connection, "botName"), "OldBot")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentTotalAmount"), "1")
                self.assertEqual(len(sqlite_telegram.list_subscriptions(connection)), 1)
            finally:
                connection.close()

    def test_save_sections_uses_sqlite_read_source_when_primary_flags_are_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "telegram-bot.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)
            self.write_json_file(
                json_path,
                {
                    "botName": "RollbackBot",
                    "enabled": False,
                    "clientSubscriptions": {},
                },
            )

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                self.save_sections_with_mocked_permissions(
                    {"enabled": True},
                    ["enabled"],
                    json_path,
                    db_path,
                )

            connection = database.open_database(db_path)
            try:
                self.assertEqual(sqlite_telegram.get_setting(connection, "botName"), "OldBot")
                self.assertEqual(sqlite_telegram.get_setting(connection, "enabled"), "true")
                self.assertEqual(sqlite_settings.get_payment_setting(connection, "paymentTotalAmount"), "1")
                self.assertEqual(len(sqlite_telegram.list_subscriptions(connection)), 1)
            finally:
                connection.close()
            self.assertEqual(self.read_json_file(json_path)["botName"], "RollbackBot")

    def test_save_state_section_preserves_existing_expiry_reminders_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            json_path = root / "telegram-bot.json"
            db_path = root / "manager.db"
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

            stale_poller_state = {
                "clientSubscriptionState": {
                    "userUpdateOffset": 90,
                    "expiryReminders": {},
                    "lastUserPoll": "2026-06-13T07:01:00Z",
                    "lastExpiryReminderCheck": "2026-06-12T22:00:00Z",
                }
            }
            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                self.save_sections_with_mocked_permissions(
                    stale_poller_state,
                    ["clientSubscriptionState"],
                    json_path,
                    db_path,
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
            root = Path(tmp_dir)
            json_path = root / "telegram-bot.json"
            db_path = root / "manager.db"
            self.make_sqlite_db(db_path)

            with mock.patch.dict(
                os.environ,
                {"XRAY_MANAGER_SQLITE_READS": "1", "XRAY_MANAGER_SQLITE_WRITES": "1"},
                clear=True,
            ):
                self.save_sections_with_mocked_permissions(
                    {
                        "clientSubscriptions": {},
                        "clientSubscriptionState": {"userUpdateOffset": 11, "expiryReminders": {}},
                    },
                    ["clientSubscriptionState"],
                    json_path,
                    db_path,
                )

            connection = database.open_database(db_path)
            try:
                subscriptions = sqlite_telegram.list_subscriptions(connection)
            finally:
                connection.close()
            self.assertEqual(len(subscriptions), 1)
            self.assertEqual(subscriptions[0]["chatId"], "999")


if __name__ == "__main__":
    unittest.main()
