import unittest

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity, clients, connections, settings, telegram, traffic


class SQLiteRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = database.open_database(":memory:")
        connections.upsert_connection(
            self.connection,
            "vless-reality",
            {
                "tag": "vless-reality",
                "name": "default",
                "created": "2026-06-12T08:00:00Z",
                "port": 443,
                "sni": "example.com",
                "dest": "example.com:443",
                "fingerprint": "chrome",
                "publicKey": "pub",
                "shortId": "abcd",
            },
        )
        clients.upsert_client(
            self.connection,
            "alice",
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "created": "2026-06-12T08:01:00Z",
                "enabled": True,
                "connection": "vless-reality",
                "paymentType": "paid",
                "expiresAt": "2026-07-01T00:00:00+03:00",
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
            },
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_connection_repository_roundtrip(self) -> None:
        self.assertEqual(
            connections.get_connection(self.connection, "vless-reality"),
            {
                "tag": "vless-reality",
                "name": "default",
                "created": "2026-06-12T08:00:00Z",
                "port": 443,
                "sni": "example.com",
                "dest": "example.com:443",
                "fingerprint": "chrome",
                "publicKey": "pub",
                "shortId": "abcd",
            },
        )

    def test_client_repository_roundtrip_with_limit(self) -> None:
        entry = clients.get_client(self.connection, "alice")

        self.assertEqual(entry["id"], "00000000-0000-0000-0000-000000000001")
        self.assertEqual(entry["connection"], "vless-reality")
        self.assertEqual(entry["paymentType"], "paid")
        self.assertEqual(entry["expiresAt"], "2026-07-01T00:00:00+03:00")
        self.assertEqual(
            entry["trafficLimit"],
            {
                "period": "daily",
                "bytes": 1073741824,
                "setAt": "2026-06-12T08:02:00Z",
            },
        )

    def test_traffic_repository_roundtrip_and_delta(self) -> None:
        traffic.upsert_traffic_entry(
            self.connection,
            "alice",
            {
                "email": "alice|created=2026-06-12T08:01:00Z",
                "incoming": 100,
                "outgoing": 200,
                "last": {"uplink": 100, "downlink": 200},
                "lastOnline": "2026-06-12T08:03:00Z",
                "lastOnlineSource": "traffic",
                "updated": "2026-06-12T08:03:00Z",
                "history": {"2026-06-12": {"08": {"incoming": 100, "outgoing": 200}}},
            },
        )
        traffic.add_history_delta(self.connection, "alice", "2026-06-12", 8, 50, 70)

        entry = traffic.get_traffic_entry(self.connection, "alice")

        self.assertEqual(entry["incoming"], 100)
        self.assertEqual(entry["outgoing"], 200)
        self.assertEqual(entry["history"]["2026-06-12"]["08"], {"incoming": 150, "outgoing": 270})

    def test_activity_repository_events_and_exceptions(self) -> None:
        event_id = activity.add_event(
            self.connection,
            {
                "time": "2026-06-12T08:04:00Z",
                "client": "alice",
                "email": "alice|created=2026-06-12T08:01:00Z",
                "connection": "vless-reality",
                "host": "example.com",
                "port": "443",
                "outbound": "cascade-upstream",
                "risks": ["xray-geoip:RU"],
            },
        )
        activity.upsert_exception(
            self.connection,
            {"value": "*.example.com", "kind": "mask", "source": "manual", "createdAt": "2026-06-12T08:05:00Z"},
        )

        events = list(activity.iter_events(self.connection, client_name="alice"))

        self.assertEqual(events[0]["id"], event_id)
        self.assertEqual(events[0]["risks"], ["xray-geoip:RU"])
        self.assertEqual(
            activity.list_exceptions(self.connection),
            [{"value": "*.example.com", "kind": "mask", "source": "manual", "createdAt": "2026-06-12T08:05:00Z"}],
        )

    def test_telegram_and_settings_repositories(self) -> None:
        telegram.set_setting(self.connection, "botName", "Vireika")
        telegram.set_state(self.connection, "dailySummaryState", {"lastSentDay": "2026-06-11"})
        telegram.upsert_subscription(
            self.connection,
            {
                "chatId": "123",
                "chatLabel": "owner",
                "clientName": "alice",
                "clientUuid": "00000000-0000-0000-0000-000000000001",
                "connection": "vless-reality",
                "linkSignature": {"sni": "example.com"},
                "enabled": True,
                "createdAt": "2026-06-12T08:06:00Z",
                "updatedAt": "2026-06-12T08:06:00Z",
            },
        )
        settings.set_metadata(self.connection, "schema-source", "test")
        settings.set_payment_setting(self.connection, "currency", "₽")

        self.assertEqual(telegram.get_setting(self.connection, "botName"), "Vireika")
        self.assertEqual(telegram.get_state(self.connection, "dailySummaryState"), {"lastSentDay": "2026-06-11"})
        self.assertEqual(telegram.list_subscriptions(self.connection, enabled_only=True)[0]["clientName"], "alice")
        self.assertEqual(settings.get_metadata(self.connection, "schema-source"), "test")
        self.assertEqual(settings.get_payment_setting(self.connection, "currency"), "₽")

    def test_deleting_client_removes_dependent_rows(self) -> None:
        traffic.upsert_traffic_entry(self.connection, "alice", {"incoming": 1, "outgoing": 2})
        activity.add_event(self.connection, {"time": "2026-06-12T08:07:00Z", "client": "alice"})

        self.assertTrue(clients.delete_client(self.connection, "alice"))

        self.assertIsNone(clients.get_client(self.connection, "alice"))
        self.assertEqual(traffic.get_traffic_entry(self.connection, "alice"), {})
        self.assertEqual(list(activity.iter_events(self.connection, client_name="alice")), [])


if __name__ == "__main__":
    unittest.main()
