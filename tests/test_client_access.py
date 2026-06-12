from datetime import datetime, timezone
import unittest

from xray_vps_manager.clients import access
from xray_vps_manager.clients.status import expire_due_clients, reconcile_client_access_status


def client_item(name: str) -> dict:
    return {
        "id": "22222222-2222-2222-2222-222222222222",
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


class ClientAccessTests(unittest.TestCase):
    def test_parse_access_days_accepts_unlimited_aliases_and_positive_days(self) -> None:
        self.assertIsNone(access.parse_access_days(""))
        self.assertIsNone(access.parse_access_days("0"))
        self.assertIsNone(access.parse_access_days("бессрочно"))
        self.assertEqual(access.parse_access_days("30"), 30)

    def test_parse_access_days_rejects_invalid_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive number"):
            access.parse_access_days("1.5")
        with self.assertRaisesRegex(ValueError, "too large"):
            access.parse_access_days("36501")

    def test_parse_extend_days_requires_positive_number(self) -> None:
        self.assertEqual(access.parse_extend_days("+5"), 5)
        with self.assertRaisesRegex(ValueError, "positive number"):
            access.parse_extend_days("0")

    def test_access_expired_compares_in_current_timezone(self) -> None:
        entry = {"expiresAt": "2026-06-12T00:00:00+00:00"}

        self.assertFalse(access.access_expired(entry, datetime(2026, 6, 11, 23, 59, tzinfo=timezone.utc)))
        self.assertTrue(access.access_expired(entry, datetime(2026, 6, 12, 0, 0, tzinfo=timezone.utc)))

    def test_expire_due_clients_disables_and_removes_active_client(self) -> None:
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
                    "expiresAt": "2026-06-12T00:00:00+00:00",
                }
            }
        }

        result = expire_due_clients(
            config,
            db,
            now=datetime(2026, 6, 12, 1, tzinfo=timezone.utc),
            stamp="2026-06-12T01:00:00Z",
        )

        self.assertEqual(result.due_names, ["alice"])
        self.assertEqual(config["inbounds"][0]["settings"]["clients"], [])
        self.assertFalse(db["clients"]["alice"]["enabled"])
        self.assertEqual(db["clients"]["alice"]["disabledReason"], "expired")
        self.assertEqual(db["clients"]["alice"]["expiredAt"], "2026-06-12T01:00:00Z")

    def test_traffic_limit_takes_priority_over_expired_access(self) -> None:
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
                    "expiresAt": "2026-06-12T00:00:00+00:00",
                    "trafficLimit": {"period": "daily", "bytes": 100},
                }
            }
        }
        traffic_db = {
            "clients": {
                "alice": {
                    "history": {"2026-06-12": {"10": {"incoming": 100, "outgoing": 0}}}
                }
            }
        }

        entry, changed, status, traffic_status = reconcile_client_access_status(
            config,
            db,
            traffic_db,
            "alice",
            db["clients"]["alice"],
            now=datetime(2026, 6, 12, 12, tzinfo=timezone.utc),
        )

        self.assertTrue(changed)
        self.assertEqual(status, "disabled-traffic-limit")
        self.assertEqual(entry["disabledReason"], "traffic-limit")
        self.assertNotIn("expiredAt", entry)
        self.assertTrue(traffic_status["exceeded"])


if __name__ == "__main__":
    unittest.main()
