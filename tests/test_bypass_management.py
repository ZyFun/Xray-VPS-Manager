import sqlite3
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xray_vps_manager.commands import set_bypass
from xray_vps_manager.db import schema
from xray_vps_manager.xray import bypass as bypass_config
from xray_vps_manager.xray import outbound_links


VLESS_LINK = "vless://11111111-1111-1111-1111-111111111111@example.com:443?encryption=none&security=none&type=tcp#Example"


def base_config() -> dict:
    return {
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "blocked", "protocol": "blackhole"},
            {"tag": "cascade-main", "protocol": "vless", "settings": {"vnext": [{"address": "cascade.example", "port": 443}]}},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "protocol": ["bittorrent"], "outboundTag": "blocked"},
                {"type": "field", "user": ["alice|created=2026-06-12T08:00:00Z"], "balancerTag": "client-route-alice"},
                {"type": "field", "network": "tcp,udp", "outboundTag": "cascade-main"},
            ],
        },
    }


def bypass_outbound(name: str) -> dict:
    outbound, _label = outbound_links.parse_vless_outbound(VLESS_LINK, bypass_config.bypass_tag(name))
    return outbound


class BypassManagementTests(unittest.TestCase):
    def test_apply_bypass_uses_notification_marker_before_client_routes(self) -> None:
        config = base_config()
        outbound = bypass_outbound("ru")
        bypass_config.upsert_bypass_outbound(config, outbound)

        bypass_config.apply_bypass_route(config, "bypass-ru", "RU")

        rules = config["routing"]["rules"]
        marker_index = next(index for index, rule in enumerate(rules) if rule.get("outboundTag") == "geoip-warning-RU")
        client_index = next(index for index, rule in enumerate(rules) if rule.get("balancerTag") == "client-route-alice")
        torrent_index = next(index for index, rule in enumerate(rules) if "bittorrent" in rule.get("protocol", []))
        self.assertLess(torrent_index, marker_index)
        self.assertLess(marker_index, client_index)
        self.assertEqual(rules[marker_index]["ip"], ["geoip:ru"])
        self.assertEqual(config["routing"]["domainStrategy"], "IPOnDemand")
        self.assertTrue(
            bypass_config.outbounds_equivalent(
                bypass_config.outbound_by_tag(config, "geoip-warning-RU"),
                bypass_config.outbound_by_tag(config, "bypass-ru"),
            )
        )
        self.assertFalse(any(rule.get("outboundTag") == "bypass-ru" for rule in rules))

    def test_disable_restores_previous_domain_strategy_when_no_geoip_rules_remain(self) -> None:
        config = base_config()
        outbound = bypass_outbound("ru")
        env = {}
        bypass_config.upsert_bypass_outbound(config, outbound)
        bypass_config.ensure_geoip_domain_strategy(config, env)
        bypass_config.apply_bypass_route(config, "bypass-ru", "RU")

        bypass_config.disable_bypass_route(config, "bypass-ru", "RU", env_values=env)

        self.assertEqual(config["routing"]["domainStrategy"], "IPIfNonMatch")
        self.assertFalse(any(rule.get("outboundTag") == "geoip-warning-RU" for rule in config["routing"]["rules"]))
        self.assertIsNone(bypass_config.outbound_by_tag(config, "geoip-warning-RU"))
        self.assertIsNotNone(bypass_config.outbound_by_tag(config, "bypass-ru"))

    def test_sync_geoip_warning_outbounds_preserves_bypass_marker(self) -> None:
        config = base_config()
        bypass_config.upsert_bypass_outbound(config, bypass_outbound("ru"))
        bypass_config.apply_bypass_route(config, "bypass-ru", "RU")
        config["routing"]["rules"][-1]["outboundTag"] = "direct"

        bypass_config.sync_geoip_warning_outbounds(config)

        self.assertTrue(
            bypass_config.outbounds_equivalent(
                bypass_config.outbound_by_tag(config, "geoip-warning-RU"),
                bypass_config.outbound_by_tag(config, "bypass-ru"),
            )
        )

    def test_add_reads_vless_link_before_interactive_region_selection(self) -> None:
        events = []
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        schema.ensure_schema(connection)

        def read_link() -> str:
            events.append("link")
            return VLESS_LINK

        def read_region(_value: str = "") -> tuple[str, str]:
            events.append("region")
            return "RU", "Россия"

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(set_bypass.sys, "stdin", SimpleNamespace(isatty=lambda: True)))
            stack.enter_context(mock.patch.object(set_bypass, "read_vless_link", side_effect=read_link))
            stack.enter_context(mock.patch.object(set_bypass, "read_region", side_effect=read_region))
            stack.enter_context(mock.patch.object(set_bypass, "load_config", return_value=base_config()))
            stack.enter_context(mock.patch.object(set_bypass, "read_server_env", return_value={}))
            stack.enter_context(mock.patch.object(set_bypass, "open_db", return_value=connection))
            stack.enter_context(mock.patch.object(set_bypass, "apply_config", return_value=Path("/tmp/config.json.bak")))
            stack.enter_context(mock.patch.object(set_bypass, "write_server_env_values"))
            stack.enter_context(mock.patch.object(set_bypass, "notify_config_event"))
            set_bypass.cmd_add(["ru"])

        self.assertEqual(events, ["link", "region"])


if __name__ == "__main__":
    unittest.main()
