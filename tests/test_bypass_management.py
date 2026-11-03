import io
import json
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from xray_vps_manager.commands import set_bypass
from xray_vps_manager.db import database
from tests.support.sandbox import Sandbox
from xray_vps_manager.db.repositories import bypass as sqlite_bypass
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

    def test_test_inbounds_include_forced_and_routing_checks(self) -> None:
        config = base_config()

        set_bypass.add_test_inbounds(config, "bypass-ru")

        inbounds = {item["tag"]: item for item in config["inbounds"]}
        self.assertEqual(inbounds[set_bypass.TEST_INBOUND_TAG]["port"], set_bypass.TEST_SOCKS_PORT)
        self.assertEqual(inbounds[set_bypass.ROUTE_TEST_INBOUND_TAG]["port"], set_bypass.ROUTE_TEST_SOCKS_PORT)
        forced_rules = [
            rule
            for rule in config["routing"]["rules"]
            if set_bypass.TEST_INBOUND_TAG in bypass_config.rule_values(rule, "inboundTag")
        ]
        route_rules = [
            rule
            for rule in config["routing"]["rules"]
            if set_bypass.ROUTE_TEST_INBOUND_TAG in bypass_config.rule_values(rule, "inboundTag")
        ]
        self.assertEqual(forced_rules[0]["outboundTag"], "bypass-ru")
        self.assertEqual(route_rules, [])

    def test_test_inbounds_reject_existing_port_conflict(self) -> None:
        config = base_config()
        config["inbounds"] = [
            {
                "tag": "existing-socks",
                "listen": "127.0.0.1",
                "port": set_bypass.ROUTE_TEST_SOCKS_PORT,
                "protocol": "socks",
            }
        ]

        with mock.patch.object(set_bypass.sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                set_bypass.add_test_inbounds(config, "bypass-ru")

    def test_route_log_outbound_parser(self) -> None:
        line = "2026/06/26 14:22:33 from tcp:127.0.0.1:35454 accepted tcp:ya.ru:443 [bypass-route-test-socks -> geoip-warning-RU]"

        self.assertEqual(set_bypass.route_outbound_from_access_line(line), "geoip-warning-RU")

    def test_route_split_uses_configured_target_for_region(self) -> None:
        calls = []

        def curl_probe(_proxy: str, url: str, *, capture_body: bool = False):
            calls.append(url)
            return SimpleNamespace(returncode=0, stdout="200", stderr="")

        def log_line(_offset: int, host: str, port: int, timeout: float = 2.0) -> str:
            if host == "bank.kz" and port == 443:
                return "from tcp:127.0.0.1:35454 accepted tcp:bank.kz:443 [bypass-route-test-socks -> geoip-warning-KZ]"
            if host == "example.com" and port == 443:
                return "from tcp:127.0.0.1:35454 accepted tcp:example.com:443 [bypass-route-test-socks -> cascade-main]"
            return ""

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(set_bypass, "curl_probe", side_effect=curl_probe))
            stack.enter_context(mock.patch.object(set_bypass, "access_log_offset", return_value=0))
            stack.enter_context(mock.patch.object(set_bypass, "access_log_line_for_host", side_effect=log_line))

            self.assertTrue(set_bypass.test_route_split("KZ", "https://bank.kz/"))

        self.assertIn("https://bank.kz/", calls)
        self.assertIn(set_bypass.DEFAULT_FOREIGN_ROUTE_TEST_URL, calls)
        self.assertNotIn("https://ya.ru/", calls)

    def test_normalize_test_target_accepts_domain_ip_and_url(self) -> None:
        self.assertEqual(set_bypass.normalize_test_target("ya.ru"), "https://ya.ru/")
        self.assertEqual(set_bypass.normalize_test_target("http://192.0.2.10:8080/check"), "http://192.0.2.10:8080/check")
        with self.assertRaises(ValueError):
            set_bypass.normalize_test_target("example.com:bad")

    def configure_sandbox(self, box, config):
        box.paths.config.write_text(json.dumps(config))
        box.processes.script({
            (str(box.paths.xray_bin), "run", "-test", "-config", str(box.paths.config)): box.processes.ok(),
            "systemctl stop xray": box.processes.ok(),
            "systemctl reset-failed xray": box.processes.ok(),
            "systemctl restart xray": box.processes.ok(),
            "systemctl show --property=ActiveState,SubState,NRestarts xray": box.processes.ok(
                "ActiveState=active\nSubState=running\nNRestarts=0\n"
            ),
        })

    def test_add_reads_vless_link_before_interactive_region_selection(self) -> None:
        events = []

        class Terminal(io.StringIO):
            def isatty(self):
                return True

            def readline(self, *args):
                events.append("link")
                return super().readline(*args)

        def answer(prompt):
            if prompt == "GeoIP region: ":
                events.append("region")
                return "1"
            self.assertIn("test target", prompt)
            events.append("target")
            return "ya.ru"

        with Sandbox() as box:
            self.configure_sandbox(box, base_config())
            with mock.patch.object(sys, "stdin", Terminal(VLESS_LINK)), mock.patch("builtins.input", side_effect=answer):
                set_bypass.cmd_add(["ru"])
            with database.session() as connection:
                record = sqlite_bypass.get_route(connection, "bypass-ru")
        self.assertEqual(events, ["link", "region", "target"])
        self.assertEqual(record["testTarget"], "https://ya.ru/")

    def test_enable_outbound_only_repairs_missing_metadata(self) -> None:
        with Sandbox() as box:
            config = base_config()
            bypass_config.upsert_bypass_outbound(config, bypass_outbound("ru"))
            self.configure_sandbox(box, config)
            set_bypass.cmd_enable(["ru", "--region", "RU", "--test-target", "ya.ru"])
            config = json.loads(box.paths.config.read_text())
            with database.session() as connection:
                record = sqlite_bypass.get_route(connection, "bypass-ru")
        self.assertIsNotNone(record)
        self.assertEqual(record["regionCode"], "RU")
        self.assertEqual(record["regionLabel"], "Россия")
        self.assertEqual(record["testTarget"], "https://ya.ru/")
        self.assertTrue(record["enabled"])
        self.assertTrue(
            bypass_config.outbounds_equivalent(
                bypass_config.outbound_by_tag(config, "geoip-warning-RU"),
                bypass_config.outbound_by_tag(config, "bypass-ru"),
            )
        )
        self.assertTrue(
            any(
                rule.get("outboundTag") == "geoip-warning-RU" and rule.get("ip") == ["geoip:ru"]
                for rule in config["routing"]["rules"]
            )
        )


if __name__ == "__main__":
    unittest.main()
