from datetime import datetime, timezone
import unittest

from xray_vps_manager.clients import limits
from xray_vps_manager.clients.status import enforce_traffic_limits


def client_item(name: str) -> dict:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "email": f"{name}|created=2026-01-01T00:00:00Z",
        "flow": "xtls-rprx-vision",
        "level": 0,
    }


def reality_config(items: list[dict]) -> dict:
    return {
        "inbounds": [
            {
                "tag": "vless-reality",
                "protocol": "vless",
                "settings": {"clients": items},
                "streamSettings": {"security": "reality"},
            }
        ]
    }


class ClientLimitTests(unittest.TestCase):
    def test_parse_limit_gb_accepts_numbers_and_unlimited_aliases(self) -> None:
        self.assertIsNone(limits.parse_limit_gb(""))
        self.assertIsNone(limits.parse_limit_gb("0"))
        self.assertIsNone(limits.parse_limit_gb("без лимита"))
        self.assertEqual(limits.parse_limit_gb("1.5gb"), int(1.5 * limits.BYTES_IN_GB))
        self.assertEqual(limits.parse_limit_gb("2,25"), int(2.25 * limits.BYTES_IN_GB))

    def test_parse_limit_gb_rejects_invalid_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "number in GB"):
            limits.parse_limit_gb("many")
        with self.assertRaisesRegex(ValueError, "too large"):
            limits.parse_limit_gb("1048577")

    def test_traffic_limit_status_uses_current_day_history(self) -> None:
        now = datetime(2026, 6, 12, 12, 30, tzinfo=timezone.utc)
        db_entry = {"trafficLimit": {"period": "daily", "bytes": 100}}
        traffic_entry = {
            "history": {
                "2026-06-11": {"23": {"incoming": 1000, "outgoing": 1000}},
                "2026-06-12": {
                    "10": {"incoming": 40, "outgoing": 35},
                    "11": {"incoming": 10, "outgoing": 15},
                },
            }
        }

        status = limits.traffic_limit_status(db_entry, traffic_entry, now)

        self.assertEqual(status["period"], "daily")
        self.assertEqual(status["periodKey"], "2026-06-12")
        self.assertEqual(status["usedBytes"], 100)
        self.assertEqual(status["remainingBytes"], 0)
        self.assertTrue(status["exceeded"])
        self.assertEqual(status["resetAt"], "2026-06-13T00:00+00:00")

    def test_enforce_traffic_limits_disables_active_client(self) -> None:
        item = client_item("alice")
        config = reality_config([dict(item)])
        db = {
            "clients": {
                "alice": {
                    "id": item["id"],
                    "created": "2026-01-01T00:00:00Z",
                    "enabled": True,
                    "connection": "vless-reality",
                    "client": dict(item),
                    "trafficLimit": {"period": "daily", "bytes": 100},
                }
            }
        }
        traffic_db = {
            "clients": {
                "alice": {
                    "history": {"2026-06-12": {"10": {"incoming": 60, "outgoing": 40}}}
                }
            }
        }

        result = enforce_traffic_limits(
            config,
            db,
            traffic_db,
            now=datetime(2026, 6, 12, 12, tzinfo=timezone.utc),
            stamp="2026-06-12T09:00:00Z",
        )

        self.assertEqual(result.due_names, ["alice"])
        self.assertEqual(config["inbounds"][0]["settings"]["clients"], [])
        self.assertFalse(db["clients"]["alice"]["enabled"])
        self.assertEqual(db["clients"]["alice"]["disabledReason"], "traffic-limit")
        self.assertEqual(db["clients"]["alice"]["trafficLimitExceededPeriod"], "2026-06-12")

    def test_enforce_traffic_limits_reactivates_after_period_reset(self) -> None:
        item = client_item("alice")
        config = reality_config([])
        db = {
            "clients": {
                "alice": {
                    "id": item["id"],
                    "created": "2026-01-01T00:00:00Z",
                    "enabled": False,
                    "connection": "vless-reality",
                    "client": dict(item),
                    "disabledReason": "traffic-limit",
                    "trafficLimitExceededPeriod": "2026-06-11",
                    "trafficLimit": {"period": "daily", "bytes": 100},
                }
            }
        }
        traffic_db = {"clients": {"alice": {"history": {"2026-06-12": {}}}}}

        result = enforce_traffic_limits(
            config,
            db,
            traffic_db,
            now=datetime(2026, 6, 12, 12, tzinfo=timezone.utc),
            stamp="2026-06-12T09:00:00Z",
        )

        self.assertEqual(result.reactivated_names, ["alice"])
        self.assertTrue(db["clients"]["alice"]["enabled"])
        self.assertNotIn("disabledReason", db["clients"]["alice"])
        self.assertEqual(config["inbounds"][0]["settings"]["clients"][0]["email"], item["email"])


if __name__ == "__main__":
    unittest.main()
