import unittest

from tests.support.sandbox import Sandbox
from xray_vps_manager.clients import routes as client_route_service
from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.core.errors import ExternalCommandError
from xray_vps_manager.xray import client_route_runtime, client_routes


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

        self.assertTrue(client_route_service.sync_routes_from_config(config, db))

        self.assertEqual(db["cascadeRoutes"]["cascade-de"]["country"], "Германия")
        self.assertEqual(db["cascadeRoutes"]["cascade-us"]["country"], "США")
        self.assertEqual(db["clients"]["alice"]["selectedCascadeTag"], "cascade-de")

    def test_existing_country_is_not_overwritten(self) -> None:
        config = route_config()
        db = {
            "cascadeRoutes": {"cascade-de": {"tag": "cascade-de", "country": "Deutschland"}},
            "clients": {},
        }

        client_route_service.sync_routes_from_config(config, db)

        self.assertEqual(db["cascadeRoutes"]["cascade-de"]["country"], "Deutschland")

    def test_ensure_client_route_config_adds_routing_service_balancer_and_user_rule(self) -> None:
        config = route_config()
        entry = {
            "id": "00000000-0000-0000-0000-000000000001",
            "selectedCascadeTag": "cascade-us",
            "client": {"email": "alice|created=2026-06-12T08:00:00Z"},
        }

        self.assertTrue(client_route_service.ensure_client_route_config(config, "alice", entry))

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

        self.assertTrue(client_route_service.ensure_client_route_config(config, "alice", entry))

        self.assertEqual(config["routing"]["balancers"], [{"tag": balancer_tag, "selector": ["cascade-us"]}])

    def test_runtime_override_uses_short_server_flag_if_long_flag_is_unsupported(self):
        with Sandbox(db=None) as box:
            base = (str(box.paths.xray_bin), "api", "bo")
            args = ("-b", "balancer", "cascade-de")
            primary = (*base, "--server=127.0.0.1:10085", *args)
            fallback = (*base, "-s", "127.0.0.1:10085", *args)
            box.processes.script({
                primary: box.processes.result(2, stderr="flag provided but not defined: -server"),
                fallback: box.processes.ok(),
            })

            client_route_runtime.set_override("balancer", "cascade-de")

            self.assertEqual(box.processes.calls, [primary, fallback])
            self.assertEqual([kwargs["timeout"] for _, kwargs in box.processes.requests], [8, 8])

    def test_ensure_all_client_route_config_removes_stale_client_balancers(self) -> None:
        config = route_config()
        config["routing"]["rules"].insert(0, {"type": "field", "user": ["old"], "balancerTag": "client-route-old"})
        config["routing"]["balancers"] = [{"tag": "client-route-old", "selector": ["cascade-de"], "fallbackTag": "cascade-de"}]
        db = {"clients": {}, "cascadeRoutes": {}}

        self.assertTrue(client_route_service.ensure_all_client_route_config(config, db))

        self.assertFalse(any(rule.get("balancerTag") == "client-route-old" for rule in config["routing"]["rules"]))
        self.assertFalse(any(balancer.get("tag") == "client-route-old" for balancer in config["routing"]["balancers"]))

    def test_remove_all_client_route_config_removes_client_rules_and_balancers(self) -> None:
        config = route_config()
        config["routing"]["rules"].insert(0, {"type": "field", "user": ["alice"], "balancerTag": "client-route-alice"})
        config["routing"]["balancers"] = [{"tag": "client-route-alice", "selector": ["cascade-de"]}]

        self.assertTrue(client_routes.remove_all_client_route_config(config))

        self.assertFalse(any(rule.get("balancerTag") == "client-route-alice" for rule in config["routing"]["rules"]))
        self.assertEqual(config["routing"]["balancers"], [])

    def test_remove_unavailable_client_route_config_removes_balancer_without_configured_outbound(self) -> None:
        config = route_config()
        config["outbounds"] = [item for item in config["outbounds"] if item["tag"] != "cascade-us"]
        config["routing"]["rules"].insert(0, {"type": "field", "user": ["alice"], "balancerTag": "client-route-alice"})
        config["routing"]["balancers"] = [{"tag": "client-route-alice", "selector": ["cascade-us"]}]

        self.assertTrue(client_routes.remove_unavailable_client_route_config(config))

        self.assertFalse(any(rule.get("balancerTag") == "client-route-alice" for rule in config["routing"]["rules"]))
        self.assertEqual(config["routing"]["balancers"], [])


class ClientRouteBoundaryTests(unittest.TestCase):
    def test_credential_emails_keep_sorted_order_deduplication_and_fallbacks(self):
        entry = {"id": "uuid", "created": "entry-stamp", "credentials": {
            "z": {"connection": "z", "client": {"email": " shared "}},
            "a": {"connection": "a", "created": "credential-stamp", "client": {}},
            "b": {"connection": "b", "client": {"email": "shared"}},
        }}
        self.assertEqual(client_credentials.client_emails("alice", entry), [
            "alice|created=credential-stamp|connection=a", "shared",
        ])
        self.assertEqual(entry["connection"], "a")
        self.assertEqual(client_credentials.client_email("alice", {"id": "uuid", "created": "stamp"}),
                         "alice|created=stamp")
        with self.assertRaisesRegex(ValueError, "Client has no UUID in database: alice"):
            client_credentials.client_emails("alice", {})

    def test_pure_config_uses_supplied_emails_without_normalizing_credentials(self):
        config = route_config()
        entry = {"id": "uuid", "credentials": "not-a-dict"}
        emails = ["alice|custom=value", "alice|connection=second"]
        self.assertTrue(client_routes.ensure_client_route_config(config, "alice", entry, emails=emails))
        rule = next(rule for rule in config["routing"]["rules"] if "user" in rule)
        self.assertEqual(rule["user"], emails)
        self.assertEqual(entry, {"id": "uuid", "credentials": "not-a-dict", "selectedCascadeTag": "cascade-de"})

    def test_skipped_routes_do_not_resolve_credentials(self):
        for selected, catchall in (("missing", "cascade-de"), ("cascade-de", "warp-out")):
            with self.subTest(selected=selected, catchall=catchall):
                config = route_config()
                config["routing"]["rules"][-1]["outboundTag"] = catchall
                entry = {"selectedCascadeTag": selected}
                self.assertFalse(client_route_service.ensure_client_route_config(config, "alice", entry))
                self.assertEqual(entry, {"selectedCascadeTag": selected})

    def test_credential_failure_preserves_preparation_order(self):
        config = route_config()
        entry = {"selectedCascadeTag": "cascade-us"}
        tag = client_routes.client_balancer_tag("alice", entry)
        config["routing"]["rules"].insert(0, {"user": ["old"], "balancerTag": tag})
        with self.assertRaisesRegex(ValueError, "Client has no UUID in database: alice"):
            client_route_service.ensure_client_route_config(config, "alice", entry)
        self.assertIn("RoutingService", config["api"]["services"])
        self.assertEqual(config["routing"]["balancers"], [{"tag": tag, "selector": ["cascade-us"]}])
        self.assertFalse(any(rule.get("balancerTag") == tag for rule in config["routing"]["rules"]))
        self.assertEqual(entry["selectedCascadeTag"], "cascade-us")

    def test_snapshot_operations_do_not_open_or_create_database(self):
        with Sandbox(db=None) as box:
            box.paths.manager_db.unlink()
            config = route_config()
            db = {"clients": {"alice": {"id": "uuid", "created": "stamp"}}}
            self.assertTrue(client_route_service.ensure_all_client_route_config(config, db))
            self.assertEqual(client_route_service.selected_route_label(db, db["clients"]["alice"]), "Германия")
            self.assertEqual(len(client_route_service.route_options(db, config)), 2)
            self.assertFalse(box.paths.manager_db.exists())
            self.assertEqual(box.processes.calls, [])

    def test_runtime_override_reports_xray_error(self):
        with Sandbox(db=None) as box:
            command = (str(box.paths.xray_bin), "api", "bo", "--server=127.0.0.1:10085",
                       "-b", "balancer", "cascade-us")
            box.processes.script({command: box.processes.result(2, stderr="cannot find tag")})

            with self.assertRaises(ExternalCommandError) as error:
                client_route_runtime.set_override("balancer", "cascade-us")

            self.assertEqual(error.exception.detail, "cannot find tag")
            self.assertEqual(box.processes.calls, [command])

    def test_runtime_override_restores_target_that_looks_like_an_option(self):
        with Sandbox(db=None) as box:
            command = (str(box.paths.xray_bin), "api", "bo", "--server=127.0.0.1:10085",
                       "-b", "balancer", "--", "-r")
            box.processes.script({command: box.processes.ok()})

            client_route_runtime.set_override("balancer", "-r")

            self.assertEqual(box.processes.calls, [command])


if __name__ == "__main__":
    unittest.main()
