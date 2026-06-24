from datetime import timezone
import unittest

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity, activity_blocklist, clients, connections, settings, telegram, traffic


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
                "transport": "grpc",
                "grpcServiceName": "vless-grpc",
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
                "transport": "grpc",
                "grpcServiceName": "vless-grpc",
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
        traffic.upsert_access_log_state(
            self.connection,
            {
                "path": "/var/log/xray/access.log",
                "inode": 3,
                "offset": 700,
                "updated": "2026-06-12T08:03:00Z",
            },
        )

        entry = traffic.get_traffic_entry(self.connection, "alice")

        self.assertEqual(entry["incoming"], 100)
        self.assertEqual(entry["outgoing"], 200)
        self.assertEqual(entry["history"]["2026-06-12"]["08"], {"incoming": 150, "outgoing": 270})
        self.assertEqual(
            traffic.get_access_log_state(self.connection),
            {
                "path": "/var/log/xray/access.log",
                "inode": 3,
                "offset": 700,
                "updated": "2026-06-12T08:03:00Z",
            },
        )

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
        self.assertEqual(activity.list_event_clients(self.connection), ["alice"])
        self.assertEqual(
            activity.list_exceptions(self.connection),
            [{"value": "*.example.com", "kind": "mask", "source": "manual", "createdAt": "2026-06-12T08:05:00Z"}],
        )

    def test_activity_alert_counters_capture_and_error_events(self) -> None:
        event = {
            "time": "2026-06-12T08:04:00Z",
            "client": "alice",
            "email": "alice|created=2026-06-12T08:01:00Z",
            "connection": "vless-reality",
            "host": "example.ru",
            "port": "443",
            "outbound": "geoip-warning-RU",
            "risks": ["xray-geoip:RU"],
        }

        self.assertEqual(activity.set_detail_mode(self.connection, "selected"), "selected")
        activity.set_detail_clients(self.connection, ["alice"])
        self.assertEqual(
            activity.detail_capture_status(self.connection),
            {"mode": "selected", "selectedClients": ["alice"]},
        )

        activity.upsert_client_counters(self.connection, event, timezone.utc)
        activity.upsert_client_counters(self.connection, event, timezone.utc)
        alert_id = activity.add_alert_event(self.connection, event, "xray-geoip:RU")
        activity.add_alert_event(self.connection, event, "xray-geoip:RU")

        counters = activity.list_client_counters(self.connection, bucket_type="hour")
        alerts = list(activity.iter_geoip_alerts_after(self.connection, after_id=0, after_time="2026-06-12T08:00:00Z"))

        self.assertEqual(counters[0]["totalEvents"], 2)
        self.assertEqual(counters[0]["geoipEvents"], 2)
        self.assertEqual(counters[0]["uniqueHosts"], 1)
        self.assertEqual(counters[0]["uniquePorts"], 1)
        self.assertEqual(counters[0]["riskCounts"], {"xray-geoip:RU": 2})
        self.assertEqual(alerts[0]["alertId"], alert_id)
        self.assertEqual(alerts[0]["event_count"], 2)

        error_id = activity.upsert_xray_error_event(
            self.connection,
            {
                "event_time": "2026-06-12T08:05:00Z",
                "level": "error",
                "source": "manager",
                "component": "xray-logrotate",
                "message": "try-restart xray.service failed",
            },
        )
        activity.upsert_xray_error_event(
            self.connection,
            {
                "event_time": "2026-06-12T08:06:00Z",
                "level": "error",
                "source": "manager",
                "component": "xray-logrotate",
                "message": "try-restart xray.service failed",
            },
        )
        errors = activity.list_xray_error_events(self.connection)
        self.assertEqual(errors[0]["id"], error_id)
        self.assertEqual(errors[0]["eventCount"], 2)
        self.assertEqual(errors[0]["lastSeen"], "2026-06-12T08:06:00Z")

    def test_activity_window_alerts_create_burst_and_unique_risk_rows(self) -> None:
        limits = {
            "burstEvents": 2,
            "burstWindowMinutes": 15,
            "uniqueHosts": 2,
            "uniquePorts": 2,
        }
        events = [
            {
                "time": "2026-06-12T08:00:00Z",
                "client": "alice",
                "connection": "vless-reality",
                "host": "one.example",
                "port": "443",
            },
            {
                "time": "2026-06-12T08:01:00Z",
                "client": "alice",
                "connection": "vless-reality",
                "host": "two.example",
                "port": "8443",
            },
        ]

        ids = []
        for event in events:
            ids.extend(activity.add_window_alerts_for_event(self.connection, event, limits, timezone.utc))

        alerts = activity.list_alert_events(self.connection, limit=10)
        risks = {row["risk"]: row for row in alerts}
        self.assertEqual(set(risks), {"burst", "unique-hosts", "unique-ports"})
        self.assertEqual(risks["burst"]["event_count"], 2)
        self.assertEqual(risks["unique-hosts"]["event_count"], 2)
        self.assertEqual(risks["unique-ports"]["event_count"], 2)
        self.assertEqual(len(ids), 3)

    def test_xray_error_events_can_filter_multiple_levels(self) -> None:
        for level in ("info", "warning", "error"):
            activity.upsert_xray_error_event(
                self.connection,
                {
                    "event_time": f"2026-06-12T08:0{len(level)}:00Z",
                    "level": level,
                    "source": "xray-error-log",
                    "component": "core",
                    "message": f"{level} message",
                },
            )

        rows = activity.list_xray_error_events(self.connection, level="warning,error")

        self.assertEqual({row["level"] for row in rows}, {"warning", "error"})

    def test_activity_blocklist_repository_roundtrip_and_stats(self) -> None:
        item = activity_blocklist.upsert_block(
            self.connection,
            {
                "value": "example.com",
                "kind": "domain",
                "sourceClient": "alice",
                "sourceEventId": None,
                "source": "geoip-menu",
                "comment": "test block",
                "createdAt": "2026-06-12T08:08:00Z",
                "expiresAt": "",
                "enabled": True,
            },
        )
        activity_blocklist.record_hit(self.connection, item["id"], "alice", "2026-06-12T08:09:00Z")
        activity_blocklist.record_hit(self.connection, item["id"], "alice", "2026-06-12T08:10:00Z")

        self.assertEqual(activity_blocklist.active_blocks(self.connection, "2026-06-12T08:11:00Z")[0]["value"], "example.com")
        stats = activity_blocklist.list_hit_stats(self.connection)

        self.assertEqual(stats[0]["totalHits"], 2)
        self.assertEqual(stats[0]["clients"], {"alice": 2})
        self.assertEqual(stats[0]["firstSeen"], "2026-06-12T08:09:00Z")
        self.assertEqual(stats[0]["lastSeen"], "2026-06-12T08:10:00Z")

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
                "activityNotificationsEnabled": True,
                "createdAt": "2026-06-12T08:06:00Z",
                "updatedAt": "2026-06-12T08:06:00Z",
            },
        )
        settings.set_metadata(self.connection, "schema-source", "test")
        settings.set_payment_setting(self.connection, "currency", "₽")

        self.assertEqual(telegram.get_setting(self.connection, "botName"), "Vireika")
        self.assertEqual(telegram.get_state(self.connection, "dailySummaryState"), {"lastSentDay": "2026-06-11"})
        subscription = telegram.list_subscriptions(self.connection, enabled_only=True)[0]
        self.assertEqual(subscription["clientName"], "alice")
        self.assertTrue(subscription["activityNotificationsEnabled"])
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
