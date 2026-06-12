from pathlib import Path
import tempfile
import unittest

from fixture_json_state import write_json_state_fixture
from xray_vps_manager.db import database, json_import
from xray_vps_manager.db.repositories import activity, clients, connections, settings, telegram, traffic


class JsonToSQLiteImportTests(unittest.TestCase):
    def test_import_json_state_imports_current_files_without_deleting_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = write_json_state_fixture(root)
            connection = database.open_database(":memory:")

            summary = json_import.import_json_state(connection, paths)

            self.assertEqual(summary.counts["connections"], 1)
            self.assertEqual(summary.counts["clients"], 1)
            self.assertEqual(summary.counts["traffic_clients"], 1)
            self.assertEqual(summary.counts["activity_events"], 1)
            self.assertEqual(summary.counts["activity_exceptions"], 1)
            self.assertEqual(summary.counts["telegram_subscriptions"], 1)
            self.assertEqual(summary.counts["skipped_traffic_clients"], 1)
            self.assertEqual(summary.counts["skipped_activity_events"], 1)
            self.assertTrue(paths.clients.exists())
            self.assertEqual(connections.get_connection(connection, "vless-reality")["publicKey"], "pub")
            self.assertEqual(clients.get_client(connection, "alice")["paymentType"], "paid")
            self.assertEqual(traffic.get_traffic_entry(connection, "alice")["history"]["2026-06-12"]["08"]["incoming"], 100)
            self.assertEqual(traffic.get_access_log_state(connection)["offset"], 123)
            self.assertEqual(list(activity.iter_events(connection, client_name="alice"))[0]["risks"], ["xray-geoip:RU"])
            self.assertEqual(activity.list_exceptions(connection)[0]["value"], "*.example.com")
            self.assertEqual(telegram.get_setting(connection, "botName"), "Vireika")
            self.assertEqual(telegram.get_state(connection, "dailySummaryState"), {"lastSentDay": "2026-06-11"})
            self.assertEqual(settings.get_payment_setting(connection, "paymentTotalAmount"), "500")

    def test_import_is_repeatable_with_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = write_json_state_fixture(root)
            connection = database.open_database(":memory:")

            json_import.import_json_state(connection, paths)
            summary = json_import.import_json_state(connection, paths)

            self.assertEqual(summary.counts["clients"], 1)
            self.assertEqual(json_import.table_count(connection, "clients"), 1)
            self.assertEqual(json_import.table_count(connection, "activity_events"), 1)
            self.assertEqual(json_import.table_count(connection, "telegram_subscriptions"), 1)

    def test_import_json_files_opens_database_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = write_json_state_fixture(root)
            db_path = root / "manager.db"

            summary = json_import.import_json_files(paths, db_path)

            self.assertEqual(summary.counts["clients"], 1)
            self.assertTrue(db_path.exists())
            connection = database.open_database(db_path)
            try:
                self.assertEqual(clients.get_client(connection, "alice")["id"], "00000000-0000-0000-0000-000000000001")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
