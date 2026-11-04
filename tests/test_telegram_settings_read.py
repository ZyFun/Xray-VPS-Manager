from pathlib import Path
import tempfile
import unittest

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.repositories import telegram as sqlite_telegram
from xray_vps_manager.db.storage import SQLiteReadUnavailable
from xray_vps_manager.telegram import settings as telegram_settings


class TelegramSettingsReadTests(unittest.TestCase):
    def make_sqlite_db(self, path: Path) -> None:
        connection = database.initialize(path)
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
                "botUsername": "SQLiteVpnBot",
                "chatId": "222",
                "chatLabel": "sqlite-owner",
                "routeMode": "cascade",
            }.items():
                sqlite_telegram.set_setting(connection, key, value)
            for key, value in {
                "paymentTotalAmount": "500",
                "paymentDomainAnnualAmount": "1200",
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
                    "activityNotificationsEnabled": True,
                    "createdAt": "2026-06-12T09:10:00Z",
                    "updatedAt": "2026-06-12T09:11:00Z",
                },
            )
        finally:
            connection.close()

    def test_read_uses_sqlite_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)

            result = telegram_settings.load_db_sql(db_path=db_path)

            self.assertTrue(result["enabled"])
            self.assertEqual(result["botName"], "SQLiteBot")
            self.assertEqual(result["botUsername"], "SQLiteVpnBot")
            self.assertEqual(result["paymentTotalAmount"], "500")
            self.assertEqual(result["paymentDomainAnnualAmount"], "1200")
            self.assertEqual(result["paymentCurrency"], "$")
            self.assertEqual(result["paymentTransferMethod"], "card")
            self.assertEqual(result["paymentCard"], "2200 0000 0000 0000")
            self.assertEqual(result["dailySummaryState"], {"lastSentDay": "2026-06-12"})
            self.assertEqual(
                result["clientSubscriptions"]["222"],
                {
                    "client": "sqlite_client",
                    "clientId": "00000000-0000-0000-0000-000000000002",
                    "connection": "sqlite-connection",
                    "chatLabel": "sqlite-owner",
                    "linkHash": "sqlite-hash",
                    "subscribedAt": "2026-06-12T09:10:00Z",
                    "enabled": True,
                    "activityNotificationsEnabled": True,
                    "updatedAt": "2026-06-12T09:11:00Z",
                },
            )

    def test_read_fails_when_sqlite_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_db_path = Path(tmp_dir) / "missing.db"

            with self.assertRaisesRegex(SQLiteReadUnavailable, "manager database is missing"):
                telegram_settings.load_db_sql(db_path=missing_db_path)

            self.assertFalse(missing_db_path.exists())

    def test_read_fails_when_sqlite_database_is_uninitialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            db_path.touch()

            with self.assertRaisesRegex(SQLiteReadUnavailable, "database is not initialized"):
                telegram_settings.load_db_sql(db_path=db_path)


if __name__ == "__main__":
    unittest.main()
