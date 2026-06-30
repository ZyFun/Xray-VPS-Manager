import unittest
from unittest import mock

from xray_vps_manager.activity import blocklist as activity_blocklist
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity_blocklist as sqlite_blocklist
from xray_vps_manager.db.repositories import clients, connections, settings
from xray_vps_manager.xray import blocklist as xray_blocklist


class ActivityBlocklistTests(unittest.TestCase):
    def test_xray_blocklist_rules_are_inserted_before_geoip_warning(self) -> None:
        config = {
            "outbounds": [
                {"tag": "direct", "protocol": "freedom"},
                {"tag": "blocked", "protocol": "blackhole"},
                {"tag": "geoip-warning-RU", "protocol": "freedom"},
            ],
            "routing": {
                "rules": [
                    {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                    {"type": "field", "ip": ["geoip:ru"], "outboundTag": "geoip-warning-RU"},
                    {"type": "field", "network": "tcp,udp", "outboundTag": "direct"},
                ]
            },
        }
        items = [
            {"value": "example.com", "kind": "domain"},
            {"value": "203.0.113.10", "kind": "ip"},
        ]

        changed = xray_blocklist.sync_blocklist_rules(config, items, known_items=items)

        self.assertTrue(changed)
        rules = config["routing"]["rules"]
        domain_index = next(index for index, rule in enumerate(rules) if "domain:example.com" in rule.get("domain", []))
        ip_index = next(index for index, rule in enumerate(rules) if "203.0.113.10" in rule.get("ip", []))
        geoip_index = next(index for index, rule in enumerate(rules) if rule.get("outboundTag") == "geoip-warning-RU")
        self.assertLess(domain_index, geoip_index)
        self.assertLess(ip_index, geoip_index)

    def test_xray_blocklist_sync_removes_known_inactive_values(self) -> None:
        config = {
            "outbounds": [{"tag": "blocked", "protocol": "blackhole"}],
            "routing": {
                "rules": [
                    {"type": "field", "domain": ["domain:example.com"], "outboundTag": "blocked"},
                    {"type": "field", "ip": ["geoip:ru"], "outboundTag": "geoip-warning-RU"},
                ]
            },
        }
        known = [{"value": "example.com", "kind": "domain"}]

        changed = xray_blocklist.sync_blocklist_rules(config, [], known_items=known)

        self.assertTrue(changed)
        self.assertFalse(any("domain" in rule and rule.get("outboundTag") == "blocked" for rule in config["routing"]["rules"]))

    def test_blocked_event_records_hit_for_matching_blocklist_entry(self) -> None:
        connection = database.open_database(":memory:")
        try:
            connections.upsert_connection(
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
            clients.upsert_client(
                connection,
                "alice",
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "connection": "vless-reality",
                    "client": {"email": "alice|created=2026-06-12T08:00:00Z"},
                },
            )
            settings.set_metadata(connection, "jsonImport.completed", "true")
            item = sqlite_blocklist.upsert_block(
                connection,
                {
                    "value": "example.com",
                    "kind": "domain",
                    "sourceClient": "alice",
                    "source": "geoip-menu",
                    "comment": "test",
                    "createdAt": "2026-06-12T08:01:00Z",
                    "expiresAt": "",
                    "enabled": True,
                },
            )

            matched = activity_blocklist.record_blocked_event_hit(
                connection,
                {
                    "time": "2026-06-12T08:02:00Z",
                    "client": "alice",
                    "host": "api.example.com",
                    "outbound": "blocked",
                    "risks": ["blocked"],
                },
            )

            self.assertEqual(matched["id"], item["id"])
            stats = sqlite_blocklist.list_hit_stats(connection)
            self.assertEqual(stats[0]["clients"], {"alice": 1})
        finally:
            connection.close()

    def test_candidates_skip_only_active_blocklist_values(self) -> None:
        event = {
            "id": 1,
            "time": "2026-06-12T08:02:00Z",
            "client": "alice",
            "host": "example.com",
            "port": "443",
            "target": "tcp:example.com:443",
            "outbound": "geoip-warning-RU",
            "risks": ["xray-geoip:RU"],
        }

        with mock.patch.object(activity_blocklist, "active_block_items", return_value=[{"value": "example.com"}]), \
            mock.patch.object(activity_blocklist.activity_repository, "iter_events_for_read", return_value=[event]):
            rows = activity_blocklist.block_candidate_rows("alice", "7", "RU")

        self.assertEqual(rows, [])

        with mock.patch.object(activity_blocklist, "active_block_items", return_value=[]), \
            mock.patch.object(activity_blocklist.activity_repository, "iter_events_for_read", return_value=[event]):
            rows = activity_blocklist.block_candidate_rows("alice", "7", "RU")

        self.assertEqual([row["value"] for row in rows], ["example.com"])


if __name__ == "__main__":
    unittest.main()
