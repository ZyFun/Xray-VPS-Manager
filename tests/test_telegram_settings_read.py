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


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class TelegramSettingsReadSwitchTests(unittest.TestCase):
    def make_json_db(self, path: Path) -> None:
        write_json(
            path,
            {
                "version": 1,
                "enabled": False,
                "token": "json-token",
                "botName": "JsonBot",
                "chatId": "111",
                "chatLabel": "json-owner",
                "routeMode": "direct",
                "paymentTotalAmount": "100",
                "paymentCurrency": "₽",
                "paymentRoundingMode": "none",
                "paymentRoundingStep": "10",
                "paymentTransferMethod": "phone",
                "paymentPhone": "+79991112233",
                "paymentBank": "Сбербанк",
                "clientSubscriptions": {
                    "111": {
                        "client": "json_client",
                        "clientId": "00000000-0000-0000-0000-000000000001",
                        "connection": "json-connection",
                        "chatLabel": "json-owner",
                        "linkHash": "json-hash",
                        "subscribedAt": "2026-06-12T08:00:00Z",
                        "enabled": True,
                    }
                },
                "dailySummaryState": {"lastSentDay": "2026-06-11"},
            },
        )

    def make_sqlite_db(self, path: Path, *, ready: bool = True) -> None:
        connection = database.open_database(path)
        try:
            sqlite_connections.upsert_connection(
                connection,
                "sqlite-connection",
                {
                    "tag": "sqlite-connection",
                    "name": "sqlite",
                    "port": 8443,
                    "sni": "sqlite.example.com",
                    "dest": "sqlite.example.com:443",
                    "fingerprint": "safari",
                },
            )
            sqlite_clients.upsert_client(
                connection,
                "sqlite_client",
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "created": "2026-06-12T09:00:00Z",
                    "connection": "sqlite-connection",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000002",
                        "email": "sqlite_client|created=2026-06-12T09:00:00Z",
                    },
                },
            )
            for key, value in {
                "version": "1",
                "enabled": "true",
                "token": "sqlite-token",
                "botName": "SQLiteBot",
                "chatId": "222",
                "chatLabel": "sqlite-owner",
                "routeMode": "cascade",
            }.items():
                sqlite_telegram.set_setting(connection, key, value)
            for key, value in {
                "paymentTotalAmount": "500",
                "paymentCurrency": "$",
                "paymentRoundingMode": "step",
                "paymentRoundingStep": "50",
                "paymentTransferMethod": "card",
                "paymentCard": "2200 0000 0000 0000",
            }.items():
                sqlite_settings.set_payment_setting(connection, key, value)
            sqlite_telegram.set_state(connection, "dailySummaryState", {"lastSentDay": "2026-06-12"})
            sqlite_telegram.upsert_subscription(
                connection,
                {
                    "chatId": "222",
                    "chatLabel": "sqlite-owner",
                    "clientName": "sqlite_client",
                    "clientUuid": "00000000-0000-0000-0000-000000000002",
                    "connection": "sqlite-connection",
                    "linkSignature": {"linkHash": "sqlite-hash"},
                    "enabled": True,
                    "createdAt": "2026-06-12T09:10:00Z",
                    "updatedAt": "2026-06-12T09:11:00Z",
                },
            )
            if ready:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def test_read_uses_json_when_sqlite_flag_is_not_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "telegram-bot.json"
            db_path = Path(tmp_dir) / "manager.db"
            self.make_json_db(json_path)
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {}, clear=True):
                result = telegram_settings.load_db_sql_result(json_path, db_path=db_path)

            self.assertEqual(result.source, "json")
            self.assertEqual(result.db["botName"], "JsonBot")
            self.assertIn("111", result.db["clientSubscriptions"])

    def test_read_uses_sqlite_when_flag_is_enabled_and_database_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "telegram-bot.json"
            db_path = Path(tmp_dir) / "manager.db"
            self.make_json_db(json_path)
            self.make_sqlite_db(db_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True):
                result = telegram_settings.load_db_sql_result(json_path, db_path=db_path)

            self.assertEqual(result.source, "sqlite")
            self.assertTrue(result.db["enabled"])
            self.assertEqual(result.db["botName"], "SQLiteBot")
            self.assertEqual(result.db["paymentTotalAmount"], "500")
            self.assertEqual(result.db["paymentCurrency"], "$")
            self.assertEqual(result.db["paymentTransferMethod"], "card")
            self.assertEqual(result.db["paymentCard"], "2200 0000 0000 0000")
            self.assertEqual(result.db["dailySummaryState"], {"lastSentDay": "2026-06-12"})
            self.assertEqual(
                result.db["clientSubscriptions"]["222"],
                {
                    "client": "sqlite_client",
                    "clientId": "00000000-0000-0000-0000-000000000002",
                    "connection": "sqlite-connection",
                    "chatLabel": "sqlite-owner",
                    "linkHash": "sqlite-hash",
                    "subscribedAt": "2026-06-12T09:10:00Z",
                    "enabled": True,
                    "updatedAt": "2026-06-12T09:11:00Z",
                },
            )

    def test_read_falls_back_to_json_when_sqlite_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "telegram-bot.json"
            missing_db_path = Path(tmp_dir) / "missing.db"
            self.make_json_db(json_path)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True):
                result = telegram_settings.load_db_sql_result(json_path, db_path=missing_db_path)

            self.assertEqual(result.source, "json")
            self.assertEqual(result.db["botName"], "JsonBot")
            self.assertFalse(missing_db_path.exists())

    def test_read_falls_back_to_json_when_sqlite_import_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = Path(tmp_dir) / "telegram-bot.json"
            db_path = Path(tmp_dir) / "manager.db"
            self.make_json_db(json_path)
            self.make_sqlite_db(db_path, ready=False)

            with mock.patch.dict(os.environ, {"XRAY_MANAGER_SQLITE_READS": "1"}, clear=True):
                result = telegram_settings.load_db_sql_result(json_path, db_path=db_path)

            self.assertEqual(result.source, "json")
            self.assertEqual(result.db["botName"], "JsonBot")


if __name__ == "__main__":
    unittest.main()
