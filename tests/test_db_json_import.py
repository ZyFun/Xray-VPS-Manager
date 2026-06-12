import json
from pathlib import Path
import tempfile
import unittest

from xray_vps_manager.db import database, json_import
from xray_vps_manager.db.repositories import activity, clients, connections, settings, telegram, traffic


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class JsonToSQLiteImportTests(unittest.TestCase):
    def make_paths(self, root: Path) -> json_import.JsonStatePaths:
        return json_import.JsonStatePaths(
            clients=root / "clients.json",
            traffic=root / "traffic.json",
            activity=root / "activity.json",
            activity_exceptions=root / "activity-exceptions.json",
            activity_dir=root / "activity",
            client_activity_dir=root / "activity" / "clients",
            telegram=root / "telegram-bot.json",
        )

    def write_fixture(self, root: Path) -> json_import.JsonStatePaths:
        paths = self.make_paths(root)
        write_json(
            paths.clients,
            {
                "connections": {
                    "vless-reality": {
                        "tag": "vless-reality",
                        "name": "default",
                        "created": "2026-06-12T08:00:00Z",
                        "port": 443,
                        "sni": "example.com",
                        "dest": "example.com:443",
                        "fingerprint": "chrome",
                        "publicKey": "pub",
                        "shortId": "abcd",
                    }
                },
                "clients": {
                    "alice": {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "created": "2026-06-12T08:01:00Z",
                        "enabled": True,
                        "connection": "vless-reality",
                        "paymentType": "paid",
                        "trafficLimit": {
                            "period": "daily",
                            "bytes": 1073741824,
                            "setAt": "2026-06-12T08:02:00Z",
                        },
                        "client": {
                            "id": "00000000-0000-0000-0000-000000000001",
                            "flow": "xtls-rprx-vision",
                            "email": "alice|created=2026-06-12T08:01:00Z",
                        },
                    }
                },
            },
        )
        write_json(
            paths.traffic,
            {
                "version": 2,
                "historyRetentionMonths": 6,
                "updated": "2026-06-12T08:03:00Z",
                "accessLog": {
                    "path": "/var/log/xray/access.log",
                    "inode": 1,
                    "offset": 123,
                    "updated": "2026-06-12T08:03:00Z",
                },
                "clients": {
                    "alice": {
                        "email": "alice|created=2026-06-12T08:01:00Z",
                        "incoming": 100,
                        "outgoing": 200,
                        "last": {"uplink": 100, "downlink": 200},
                        "history": {"2026-06-12": {"08": {"incoming": 100, "outgoing": 200}}},
                    },
                    "stale": {"incoming": 1, "outgoing": 1},
                },
            },
        )
        write_json(
            paths.activity,
            {
                "version": 1,
                "enabled": True,
                "retentionDays": 365,
                "lastSync": "2026-06-12T08:04:00Z",
                "clients": {"alice": {"totalEvents": 1}},
                "accessLog": {
                    "path": "/var/log/xray/access.log",
                    "inode": 2,
                    "offset": 456,
                    "updated": "2026-06-12T08:04:00Z",
                },
            },
        )
        write_json(
            paths.activity_exceptions,
            {
                "version": 1,
                "items": [
                    {"value": "*.example.com", "kind": "mask", "source": "manual", "createdAt": "2026-06-12T08:05:00Z"}
                ],
            },
        )
        paths.client_activity_dir.mkdir(parents=True, exist_ok=True)
        (paths.client_activity_dir / "alice.jsonl").write_text(
            json.dumps(
                {
                    "time": "2026-06-12T08:06:00Z",
                    "client": "alice",
                    "email": "alice|created=2026-06-12T08:01:00Z",
                    "connection": "vless-reality",
                    "host": "example.com",
                    "port": "443",
                    "outbound": "cascade-upstream",
                    "risks": ["xray-geoip:RU"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
            + json.dumps({"time": "2026-06-12T08:07:00Z", "client": "stale"}, separators=(",", ":"))
            + "\n"
        )
        write_json(
            paths.telegram,
            {
                "version": 1,
                "enabled": True,
                "token": "secret",
                "botName": "Vireika",
                "chatId": "123",
                "chatLabel": "owner",
                "routeMode": "direct",
                "paymentTotalAmount": "500",
                "paymentCurrency": "₽",
                "paymentRoundingMode": "none",
                "paymentRoundingStep": "10",
                "geoipState": {"sentIds": []},
                "clientSubscriptionState": {"userUpdateOffset": 10},
                "dailySummaryState": {"lastSentDay": "2026-06-11"},
                "adminState": {"mode": "idle"},
                "clientSubscriptions": {
                    "123": {
                        "client": "alice",
                        "clientId": "00000000-0000-0000-0000-000000000001",
                        "connection": "vless-reality",
                        "chatLabel": "owner",
                        "linkHash": "hash",
                        "subscribedAt": "2026-06-12T08:08:00Z",
                        "enabled": True,
                    }
                },
            },
        )
        return paths

    def test_import_json_state_imports_current_files_without_deleting_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = self.write_fixture(root)
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
            self.assertEqual(list(activity.iter_events(connection, client_name="alice"))[0]["risks"], ["xray-geoip:RU"])
            self.assertEqual(activity.list_exceptions(connection)[0]["value"], "*.example.com")
            self.assertEqual(telegram.get_setting(connection, "botName"), "Vireika")
            self.assertEqual(telegram.get_state(connection, "dailySummaryState"), {"lastSentDay": "2026-06-11"})
            self.assertEqual(settings.get_payment_setting(connection, "paymentTotalAmount"), "500")

    def test_import_is_repeatable_with_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = self.write_fixture(root)
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
            paths = self.write_fixture(root)
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
