import unittest
from types import SimpleNamespace

from xray_vps_manager.xray import client_routes


def route_config():
    return {
        "api": {"tag": "api", "services": ["StatsService"]},
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "blocked", "protocol": "blackhole"},
            {"tag": "cascade-de", "protocol": "vless"},
            {"tag": "cascade-us", "protocol": "vless"},
        ],
        "routing": {
            "rules": [
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "blocked"},
                {"type": "field", "network": "tcp,udp", "outboundTag": "cascade-de"},
            ]
        },
    }


class ClientRouteTests(unittest.TestCase):
    def test_sync_assigns_default_countries_and_active_route_to_clients(self) -> None:
        config = route_config()
        db = {
            "clients": {
                "alice": {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "client": {"email": "alice|created=2026-06-12T08:00:00Z"},
                }
            }
        }

        self.assertTrue(client_routes.sync_routes_from_config(config, db))

        self.assertEqual(db["cascadeRoutes"]["cascade-de"]["country"], "Германия")
        self.assertEqual(db["cascadeRoutes"]["cascade-us"]["country"], "США")
        self.assertEqual(db["clients"]["alice"]["selectedCascadeTag"], "cascade-de")

    def test_existing_country_is_not_overwritten(self) -> None:
        config = route_config()
        db = {
            "cascadeRoutes": {"cascade-de": {"tag": "cascade-de", "country": "Deutschland"}},
            "clients": {},
        }

        client_routes.sync_routes_from_config(config, db)

        self.assertEqual(db["cascadeRoutes"]["cascade-de"]["country"], "Deutschland")

    def test_ensure_client_route_config_adds_routing_service_balancer_and_user_rule(self) -> None:
        config = route_config()
        entry = {
            "id": "00000000-0000-0000-0000-000000000001",
            "selectedCascadeTag": "cascade-us",
            "client": {"email": "alice|created=2026-06-12T08:00:00Z"},
        }

        self.assertTrue(client_routes.ensure_client_route_config(config, "alice", entry))

        self.assertIn("RoutingService", config["api"]["services"])
        balancer_tag = client_routes.client_balancer_tag("alice", entry)
        self.assertIn(
            {"tag": balancer_tag, "selector": ["cascade-us"]},
            config["routing"]["balancers"],
        )
        self.assertIn(
            {"type": "field", "user": ["alice|created=2026-06-12T08:00:00Z"], "balancerTag": balancer_tag},
            config["routing"]["rules"],
        )

    def test_ensure_client_route_config_removes_unsafe_balancer_fields(self) -> None:
        config = route_config()
        entry = {
            "id": "00000000-0000-0000-0000-000000000001",
            "selectedCascadeTag": "cascade-us",
            "client": {"email": "alice|created=2026-06-12T08:00:00Z"},
        }
        balancer_tag = client_routes.client_balancer_tag("alice", entry)
        config["routing"]["balancers"] = [
            {
                "tag": balancer_tag,
                "selector": ["cascade-de", "cascade-us"],
                "fallbackTag": "cascade-us",
                "strategy": {"type": "random"},
            }
        ]

        self.assertTrue(client_routes.ensure_client_route_config(config, "alice", entry))

        self.assertEqual(config["routing"]["balancers"], [{"tag": balancer_tag, "selector": ["cascade-us"]}])

    def test_runtime_override_tries_api_commands_until_one_succeeds(self) -> None:
        calls = []

        def run_capture(command, timeout=8):
            calls.append(command)
            return SimpleNamespace(returncode=0 if len(calls) == 2 else 2, stdout="", stderr="bad flags")

        ok, detail = client_routes.apply_runtime_override(
            "alice",
            {"id": "00000000-0000-0000-0000-000000000001"},
            "cascade-de",
            run_capture,
        )

        self.assertTrue(ok)
        self.assertEqual(detail, "")
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0][-3:],
            ["-b", client_routes.client_balancer_tag("alice", {"id": "00000000-0000-0000-0000-000000000001"}), "cascade-de"],
        )

    def test_ensure_all_client_route_config_removes_stale_client_balancers(self) -> None:
        config = route_config()
        config["routing"]["rules"].insert(0, {"type": "field", "user": ["old"], "balancerTag": "client-route-old"})
        config["routing"]["balancers"] = [{"tag": "client-route-old", "selector": ["cascade-de"], "fallbackTag": "cascade-de"}]
        db = {"clients": {}, "cascadeRoutes": {}}

        self.assertTrue(client_routes.ensure_all_client_route_config(config, db))

        self.assertFalse(any(rule.get("balancerTag") == "client-route-old" for rule in config["routing"]["rules"]))
        self.assertFalse(any(balancer.get("tag") == "client-route-old" for balancer in config["routing"]["balancers"]))


if __name__ == "__main__":
    unittest.main()
