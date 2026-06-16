import unittest
from unittest import mock

from xray_vps_manager.commands import set_cascade
from xray_vps_manager.commands import warp
from xray_vps_manager.telegram import setup as telegram_setup
from xray_vps_manager.xray import cascade as cascade_config


VLESS_LINK = "vless://11111111-1111-1111-1111-111111111111@example.com:443?encryption=none&security=none&type=tcp#Example"


def base_config() -> dict:
    return {
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "blocked", "protocol": "blackhole"},
        ],
        "routing": {
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
            ]
        },
    }


def cascade_outbound(name: str) -> dict:
    outbound, _label = set_cascade.parse_vless(VLESS_LINK, cascade_config.cascade_tag(name))
    return outbound


def add_client_route(config: dict, selector: str = "cascade-main") -> None:
    config.setdefault("routing", {}).setdefault("rules", []).insert(
        0,
        {"type": "field", "user": ["alice|created=2026-06-12T08:00:00Z"], "balancerTag": "client-route-alice"},
    )
    config.setdefault("routing", {}).setdefault("balancers", []).append(
        {"tag": "client-route-alice", "selector": [selector]},
    )


class CascadeManagementTests(unittest.TestCase):
    def test_configure_named_cascade_uses_cascade_name_tag(self) -> None:
        config = base_config()

        set_cascade.configure_cascade(config, cascade_outbound("upstream"))

        self.assertIn("cascade-upstream", cascade_config.cascade_tags(config))
        self.assertEqual(cascade_config.active_cascade_tag(config), "cascade-upstream")
        catchalls = [
            rule.get("outboundTag")
            for rule in cascade_config.routing_rules(config)
            if cascade_config.is_catchall_rule(rule)
        ]
        self.assertEqual(catchalls, ["cascade-upstream"])

    def test_multiple_cascades_are_kept_and_active_can_switch(self) -> None:
        config = base_config()
        set_cascade.configure_cascade(config, cascade_outbound("main"))
        set_cascade.configure_cascade(config, cascade_outbound("backup"))

        self.assertEqual(set(cascade_config.cascade_tags(config)), {"cascade-main", "cascade-backup"})
        self.assertEqual(cascade_config.active_cascade_tag(config), "cascade-main")
        self.assertEqual(cascade_config.cascade_tags(config), ["cascade-main", "cascade-backup"])

        set_cascade.set_active_cascade(config, "cascade-backup")

        self.assertEqual(cascade_config.active_cascade_tag(config), "cascade-backup")
        self.assertEqual(cascade_config.cascade_tags(config)[0], "cascade-backup")

    def test_adding_later_cascade_keeps_disabled_cascade_route_disabled(self) -> None:
        config = base_config()
        set_cascade.configure_cascade(config, cascade_outbound("main"))
        set_cascade.disable_cascade(config)

        set_cascade.configure_cascade(config, cascade_outbound("backup"))

        self.assertEqual(set(cascade_config.cascade_tags(config)), {"cascade-main", "cascade-backup"})
        self.assertEqual(cascade_config.active_cascade_tag(config), "")
        self.assertEqual(cascade_config.current_catchall_tag(config), "")

    def test_selecting_cascade_updates_telegram_cascade_route(self) -> None:
        config = base_config()
        set_cascade.configure_cascade(config, cascade_outbound("main"))
        set_cascade.configure_cascade(config, cascade_outbound("backup"))
        telegram_setup.ensure_telegram_proxy_config(config)

        set_cascade.set_active_cascade(config, "cascade-main")

        telegram_rules = [
            rule
            for rule in cascade_config.routing_rules(config)
            if cascade_config.TELEGRAM_SOCKS_TAG in cascade_config.rule_values(rule, "inboundTag")
        ]
        self.assertEqual(len(telegram_rules), 1)
        self.assertEqual(telegram_rules[0]["outboundTag"], "cascade-main")

    def test_warp_disable_restores_first_named_cascade(self) -> None:
        config = base_config()
        set_cascade.configure_cascade(config, cascade_outbound("main"))
        config["outbounds"].append({"tag": "warp-out", "protocol": "wireguard", "settings": {}})
        warp.remove_managed_catchall_routes(config)
        warp.append_catchall_route(config, "warp-out")

        warp.disable_warp_route(config)

        self.assertEqual(cascade_config.active_cascade_tag(config), "cascade-main")

    def test_warp_enable_removes_per_client_cascade_routes(self) -> None:
        config = base_config()
        set_cascade.configure_cascade(config, cascade_outbound("main"))
        add_client_route(config)

        def add_warp_outbound(config, endpoint_override=None):
            config.setdefault("outbounds", []).append({"tag": "warp-out", "protocol": "wireguard", "settings": {}})

        with mock.patch.object(warp, "upsert_warp_outbound", side_effect=add_warp_outbound):
            warp.enable_warp_route(config)

        self.assertEqual(cascade_config.current_catchall_tag(config), "warp-out")
        self.assertFalse(any(rule.get("balancerTag") == "client-route-alice" for rule in config["routing"]["rules"]))
        self.assertEqual(config["routing"].get("balancers", []), [])

    def test_warp_enable_removes_stale_warp_test_routes(self) -> None:
        config = base_config()
        config["inbounds"] = [{"tag": "warp-test-socks", "protocol": "socks"}]
        config["routing"]["rules"].insert(
            0,
            {"type": "field", "inboundTag": ["warp-test-socks"], "outboundTag": "warp-out"},
        )

        def add_warp_outbound(config, endpoint_override=None):
            config.setdefault("outbounds", []).append({"tag": "warp-out", "protocol": "wireguard", "settings": {}})

        with mock.patch.object(warp, "upsert_warp_outbound", side_effect=add_warp_outbound):
            warp.enable_warp_route(config)

        self.assertFalse(any(inbound.get("tag") == "warp-test-socks" for inbound in config.get("inbounds", [])))
        self.assertFalse(any("warp-test-socks" in cascade_config.rule_values(rule, "inboundTag") for rule in config["routing"]["rules"]))

    def test_disable_cascade_removes_per_client_cascade_routes(self) -> None:
        config = base_config()
        set_cascade.configure_cascade(config, cascade_outbound("main"))
        add_client_route(config)

        set_cascade.disable_cascade(config)

        self.assertEqual(cascade_config.current_catchall_tag(config), "")
        self.assertFalse(any(rule.get("balancerTag") == "client-route-alice" for rule in config["routing"]["rules"]))
        self.assertEqual(config["routing"].get("balancers", []), [])

    def test_remove_cascade_removes_stale_client_balancer(self) -> None:
        config = base_config()
        set_cascade.configure_cascade(config, cascade_outbound("main"))
        add_client_route(config)

        set_cascade.remove_cascade(config, "cascade-main")

        self.assertFalse(cascade_config.cascade_tags(config))
        self.assertFalse(any(rule.get("balancerTag") == "client-route-alice" for rule in config["routing"]["rules"]))
        self.assertEqual(config["routing"].get("balancers", []), [])


if __name__ == "__main__":
    unittest.main()
