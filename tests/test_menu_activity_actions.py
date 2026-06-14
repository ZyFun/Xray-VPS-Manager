import unittest

from xray_vps_manager.commands import menu_activity_actions


class MenuActivityActionsTests(unittest.TestCase):
    def test_geoip_warning_is_inserted_before_client_route_rules(self) -> None:
        config = {
            "outbounds": [
                {"tag": "direct", "protocol": "freedom"},
                {"tag": "cascade-de", "protocol": "vless"},
            ],
            "routing": {
                "rules": [
                    {"type": "field", "ip": ["geoip:private"], "outboundTag": "blocked"},
                    {"type": "field", "user": ["alice|created=2026-06-12T08:00:00Z"], "balancerTag": "client-route-alice"},
                    {"type": "field", "network": "tcp,udp", "outboundTag": "cascade-de"},
                ]
            },
        }

        menu_activity_actions.apply_xray_geoip_warning_config(config, "RU")

        rules = config["routing"]["rules"]
        warning_index = next(index for index, rule in enumerate(rules) if rule.get("outboundTag") == "geoip-warning-RU")
        client_index = next(index for index, rule in enumerate(rules) if rule.get("balancerTag") == "client-route-alice")
        self.assertLess(warning_index, client_index)
        self.assertEqual(rules[warning_index]["ip"], ["geoip:ru"])


if __name__ == "__main__":
    unittest.main()
