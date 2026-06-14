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
                "botUsername": "SQLiteVpnBot",
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

    def test_read_uses_sqlite_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)

            result = telegram_settings.load_db_sql_result(db_path=db_path)

            self.assertEqual(result.source, "sqlite")
            self.assertTrue(result.db["enabled"])
            self.assertEqual(result.db["botName"], "SQLiteBot")
            self.assertEqual(result.db["botUsername"], "SQLiteVpnBot")
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

    def test_read_fails_when_sqlite_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_db_path = Path(tmp_dir) / "missing.db"

            with self.assertRaisesRegex(SQLiteReadUnavailable, "manager database is missing"):
                telegram_settings.load_db_sql_result(db_path=missing_db_path)

            self.assertFalse(missing_db_path.exists())

    def test_read_fails_when_sqlite_database_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path, ready=False)

            with self.assertRaisesRegex(SQLiteReadUnavailable, "database is not marked ready"):
                telegram_settings.load_db_sql_result(db_path=db_path)


if __name__ == "__main__":
    unittest.main()
