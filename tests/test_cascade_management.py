import unittest

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
        self.assertEqual(cascade_config.active_cascade_tag(config), "cascade-backup")

        set_cascade.set_active_cascade(config, "cascade-main")

        self.assertEqual(cascade_config.active_cascade_tag(config), "cascade-main")
        self.assertEqual(cascade_config.cascade_tags(config)[0], "cascade-main")

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


if __name__ == "__main__":
    unittest.main()
