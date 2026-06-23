import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from xray_vps_manager.commands import menu_caddy_actions, menu_reality_actions
from xray_vps_manager.xray import caddy


class MenuRealityActionsReadTests(unittest.TestCase):
    def test_update_connection_db_uses_runtime_read_layer(self) -> None:
        db = {"connections": {}}
        with mock.patch.object(menu_reality_actions, "load_db_sql", return_value=db) as load_db_sql, \
            mock.patch.object(menu_reality_actions, "save_db") as save_db:
            menu_reality_actions.update_connection_db(
                "vless-reality",
                port=443,
                sni="example.com",
                dest="example.com:443",
                fingerprint="chrome",
            )

        load_db_sql.assert_called_once_with()
        save_db.assert_called_once_with(db)
        self.assertEqual(
            db["connections"]["vless-reality"],
            {
                "tag": "vless-reality",
                "name": "default",
                "port": 443,
                "sni": "example.com",
                "dest": "example.com:443",
                "fingerprint": "chrome",
            },
        )

    def test_choose_xhttp_mode_selects_mode_by_number(self) -> None:
        inputs = iter(["3"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            mode = menu_reality_actions.choose_xhttp_mode("auto")

        self.assertEqual(mode, "stream-up")

    def test_choose_transport_uses_xhttp_mode_list(self) -> None:
        inputs = iter(["3", "/private-xhttp", "4", "n"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            settings = menu_reality_actions.choose_transport("tcp")

        self.assertEqual(
            settings,
            {
                "transport": "xhttp",
                "xhttp_path": "/private-xhttp",
                "xhttp_mode": "stream-one",
            },
        )

    def test_create_trojan_connection_builds_cli_command(self) -> None:
        inputs = iter(["trojan-main", "", "vpn.example.com", "", "/private-trojan", "1", "", "y"])
        calls = []

        with mock.patch.object(menu_reality_actions, "current_fingerprint", return_value="chrome"), \
            mock.patch.object(menu_reality_actions, "load_config", return_value={"inbounds": []}), \
            mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), \
            redirect_stdout(StringIO()):
            menu_reality_actions.create_trojan_connection(lambda command: calls.append(command))

        self.assertEqual(
            calls,
            [
                [
                    "xray-client",
                    "add-trojan-connection",
                    "trojan-main",
                    "10100",
                    "vpn.example.com",
                    "chrome",
                    "--transport",
                    "ws",
                    "--ws-path",
                    "/private-trojan",
                    "--public-port",
                    "443",
                    "--tls-min-version",
                    "tls1.2",
                    "--tls-max-version",
                    "tls1.3",
                    "--install-caddy",
                ]
            ],
        )

    def test_choose_transport_can_collect_xhttp_advanced_defaults(self) -> None:
        inputs = iter(["3", "/private-xhttp", "1", "y", "", "", "n", "n", "n", "", "", "", "", "", "", "n"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            settings = menu_reality_actions.choose_transport("tcp")

        self.assertEqual(settings["transport"], "xhttp")
        self.assertEqual(settings["xhttp_extra"]["xPaddingBytes"], "100-1000")
        self.assertEqual(settings["xhttp_extra"]["scStreamUpServerSecs"], "20-80")
        self.assertEqual(settings["xhttp_extra"]["xmux"]["maxConcurrency"], "16-32")
        self.assertEqual(settings["xhttp_extra"]["xmux"]["hMaxRequestTimes"], "600-900")

    def test_prompt_xhttp_download_settings_collects_tls_profile(self) -> None:
        inputs = iter(["down.example.com", "", "", "", "", "", "", "1", "", "n", "n"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            settings = menu_reality_actions.prompt_xhttp_download_settings("/private-xhttp", "auto")

        self.assertEqual(
            settings,
            {
                "address": "down.example.com",
                "port": 443,
                "network": "xhttp",
                "security": "tls",
                "tlsSettings": {
                    "serverName": "down.example.com",
                    "fingerprint": "chrome",
                    "alpn": ["h2"],
                },
                "xhttpSettings": {
                    "path": "/private-xhttp",
                    "mode": "auto",
                },
            },
        )

    def test_prompt_xhttp_advanced_settings_keeps_download_settings_when_not_editing(self) -> None:
        download_settings = {
            "address": "down.example.com",
            "port": 443,
            "network": "xhttp",
            "security": "tls",
            "tlsSettings": {
                "serverName": "down.example.com",
                "fingerprint": "chrome",
                "alpn": ["h2"],
            },
            "xhttpSettings": {
                "path": "/private-xhttp",
                "mode": "auto",
            },
        }
        current = {
            "xPaddingBytes": "100-1000",
            "scStreamUpServerSecs": "20-80",
            "xmux": {
                "maxConcurrency": "16-32",
                "maxConnections": 0,
                "cMaxReuseTimes": 0,
                "hMaxRequestTimes": "600-900",
                "hMaxReusableSecs": "1800-3000",
                "hKeepAlivePeriod": 0,
            },
            "downloadSettings": download_settings,
        }
        inputs = iter(["", "", "n", "n", "n", "", "", "", "", "", "", "n"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            settings = menu_reality_actions.prompt_xhttp_advanced_settings(
                current,
                default_xhttp_path="/private-xhttp",
                default_xhttp_mode="auto",
            )

        self.assertEqual(settings["downloadSettings"], download_settings)

    def test_choose_tls_versions_selects_profile_by_number(self) -> None:
        inputs = iter(["3"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            tls_min, tls_max = menu_reality_actions.choose_tls_versions("tls1.2", "tls1.2")

        self.assertEqual((tls_min, tls_max), ("tls1.2", "tls1.3"))

    def test_caddy_tls_prompt_uses_same_profile_list(self) -> None:
        inputs = iter(["4"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            tls_min, tls_max = menu_caddy_actions.prompt_tls_versions("tls1.2", "tls1.2")

        self.assertEqual((tls_min, tls_max), ("tls1.3", "tls1.3"))

    def test_update_site_tls_supports_static_site_default_profile(self) -> None:
        inputs = iter(["4"])
        site = caddy.SiteConfig(
            path=Path("/etc/caddy/conf.d/site.example.com.caddy"),
            domain="site.example.com",
            local_port=None,
            tls_min_version="default",
            tls_max_version="default",
        )

        with mock.patch.object(menu_caddy_actions, "site_rows", return_value=[site]), \
            mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), \
            mock.patch.object(menu_caddy_actions, "apply_site_tls_write") as apply_tls, \
            redirect_stdout(StringIO()):
            menu_caddy_actions.update_site_tls()

        apply_tls.assert_called_once_with(site, "tls1.3", "tls1.3")


if __name__ == "__main__":
    unittest.main()
