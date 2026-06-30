import sqlite3
import unittest

from xray_vps_manager.activity import bypass as activity_bypass
from xray_vps_manager.activity import reports as activity_reports
from xray_vps_manager.db import schema
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.xray import bypass as bypass_config
from xray_vps_manager.xray import outbound_links


VLESS_LINK = "vless://11111111-1111-1111-1111-111111111111@example.com:443?encryption=none&security=none&type=tcp#Example"


def bypass_config_with_marker() -> dict:
    outbound, _label = outbound_links.parse_vless_outbound(VLESS_LINK, "bypass-ru")
    config = {
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "blocked", "protocol": "blackhole"},
        ],
        "routing": {"rules": []},
    }
    bypass_config.upsert_bypass_outbound(config, outbound)
    bypass_config.apply_bypass_route(config, "bypass-ru", "RU")
    return config


class ActivityBypassTests(unittest.TestCase):
    def open_db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        schema.ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO clients(name, uuid)
            VALUES ('alice', '00000000-0000-0000-0000-000000000001')
            """
        )
        return connection

    def test_append_bypass_risk_keeps_geoip_trigger_and_adds_report_marker(self) -> None:
        event = {
            "outbound": "geoip-warning-RU",
            "risks": ["xray-geoip:RU"],
        }

        activity_bypass.append_bypass_risk(event, config=bypass_config_with_marker())

        self.assertEqual(event["risks"], ["xray-geoip:RU", "xray-bypass:RU"])

    def test_bypass_risk_is_not_stored_as_alert(self) -> None:
        with self.open_db() as connection:
            ids = sqlite_activity.add_alerts_for_event(
                connection,
                {
                    "time": "2026-06-12T08:00:00Z",
                    "client": "alice",
                    "host": "example.ru",
                    "port": "443",
                    "outbound": "geoip-warning-RU",
                    "risks": ["xray-geoip:RU", "xray-bypass:RU"],
                },
            )
            rows = sqlite_activity.list_alert_events(connection, limit=10)

        self.assertEqual(len(ids), 1)
        self.assertEqual(rows[0]["risk"], "xray-geoip:RU")

    def test_bypass_risk_alone_is_not_suspicious_counter(self) -> None:
        with self.open_db() as connection:
            sqlite_activity.upsert_client_counter(
                connection,
                {
                    "time": "2026-06-12T08:00:00Z",
                    "client": "alice",
                    "connection": "vless-reality",
                    "host": "example.ru",
                    "port": "443",
                    "risks": ["xray-bypass:RU"],
                },
                bucket_type="day",
                bucket_start="2026-06-12",
            )
            rows = sqlite_activity.list_client_counters(connection, bucket_type="day", limit=10)

        self.assertEqual(rows[0]["suspiciousEvents"], 0)
        self.assertEqual(rows[0]["riskCounts"]["xray-bypass:RU"], 1)

    def test_bypass_risk_uses_separate_report_counter(self) -> None:
        aggregate = activity_reports.aggregate_events(
            [
                {
                    "time": "2026-06-12T08:00:00Z",
                    "host": "example.ru",
                    "port": "443",
                    "outbound": "geoip-warning-RU",
                    "risks": ["xray-geoip:RU", "xray-bypass:RU"],
                }
            ],
            exceptions=[],
        )

        self.assertEqual(aggregate["risks"], {"xray-geoip:RU": 1})
        self.assertEqual(aggregate["bypass"], {"xray-bypass:RU": 1})


if __name__ == "__main__":
    unittest.main()
