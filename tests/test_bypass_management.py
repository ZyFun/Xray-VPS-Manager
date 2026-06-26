import io
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xray_vps_manager.commands import set_bypass
from xray_vps_manager.db import database
from xray_vps_manager.db import schema
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

    def test_add_reads_vless_link_before_interactive_region_selection(self) -> None:
        events = []
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        db_path = Path(tmp_dir.name) / "manager.db"
        connection = database.open_database(db_path)

        def read_link() -> str:
            events.append("link")
            return VLESS_LINK

        def read_region(_value: str = "") -> tuple[str, str]:
            events.append("region")
            return "RU", "Россия"

        def read_target(_value: str = "", _default: str = "") -> str:
            events.append("target")
            return "https://ya.ru/"

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(set_bypass.sys, "stdin", SimpleNamespace(isatty=lambda: True)))
            stack.enter_context(mock.patch.object(set_bypass, "read_vless_link", side_effect=read_link))
            stack.enter_context(mock.patch.object(set_bypass, "read_region", side_effect=read_region))
            stack.enter_context(mock.patch.object(set_bypass, "read_test_target", side_effect=read_target))
            stack.enter_context(mock.patch.object(set_bypass, "load_config", return_value=base_config()))
            stack.enter_context(mock.patch.object(set_bypass, "read_server_env", return_value={}))
            stack.enter_context(mock.patch.object(set_bypass, "open_db", return_value=connection))
            stack.enter_context(mock.patch.object(set_bypass, "apply_config", return_value=Path("/tmp/config.json.bak")))
            stack.enter_context(mock.patch.object(set_bypass, "write_server_env_values"))
            stack.enter_context(mock.patch.object(set_bypass, "notify_config_event"))
            set_bypass.cmd_add(["ru"])

        read_connection = database.open_database(db_path)
        try:
            record = sqlite_bypass.get_route(read_connection, "bypass-ru")
        finally:
            read_connection.close()
        self.assertEqual(events, ["link", "region", "target"])
        self.assertEqual(record["testTarget"], "https://ya.ru/")

    def test_enable_outbound_only_repairs_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            connection = database.open_database(db_path)
            config = base_config()
            bypass_config.upsert_bypass_outbound(config, bypass_outbound("ru"))

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(set_bypass, "load_config", return_value=config))
                stack.enter_context(mock.patch.object(set_bypass, "read_server_env", return_value={}))
                stack.enter_context(mock.patch.object(set_bypass, "open_db", return_value=connection))
                stack.enter_context(mock.patch.object(set_bypass, "apply_config", return_value=Path("/tmp/config.json.bak")))
                stack.enter_context(mock.patch.object(set_bypass, "write_server_env_values"))
                stack.enter_context(mock.patch.object(set_bypass, "notify_config_event"))
                set_bypass.cmd_enable(["ru", "--region", "RU", "--test-target", "ya.ru"])

            read_connection = database.open_database(db_path)
            try:
                record = sqlite_bypass.get_route(read_connection, "bypass-ru")
            finally:
                read_connection.close()
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
