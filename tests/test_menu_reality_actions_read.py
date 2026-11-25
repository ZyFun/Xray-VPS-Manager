import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from xray_vps_manager.commands import menu_caddy_actions, menu_reality_actions
from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_client_characterization import write_config
from tests.test_connection_menu_characterization import script_menu_processes
from xray_vps_manager.caddy import sites
from xray_vps_manager.clients import repository


class MenuRealityActionsReadTests(unittest.TestCase):
    def test_fingerprint_edit_creates_metadata_for_config_only_connection(self) -> None:
        with Sandbox(db=None) as box:
            script_menu_processes(box)
            config = box.config()
            inbound = next(row for row in config["inbounds"] if row["tag"] == "vless-reality")
            reality = inbound["streamSettings"]["realitySettings"]
            reality.update(serverNames=["example.com"], dest="example.com:443")
            write_config(box, config)
            before = box.paths.config.read_bytes()

            result = box.run_menu(["2", "1", "6", "1", "0", "0", "0"])

            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            records = repository.load_db_sql()["connections"]
            self.assertEqual(list(records), ["vless-reality"])
            expected = {
                "tag": "vless-reality",
                "name": "default",
                "port": 443,
                "sni": "example.com",
                "dest": "example.com:443",
                "fingerprint": "chrome",
            }
            self.assertEqual({key: records["vless-reality"][key] for key in expected}, expected)
            self.assertEqual(box.paths.config.read_bytes(), before)
            box.assert_restarted(times=0)

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

    def test_create_trojan_connection_applies_selected_settings(self) -> None:
        answers = ["2", "2", "2", "trojan-main", "", "vpn.example.com", "/private-trojan",
                   "1", "", "y", "0", "0", "0"]
        with Sandbox() as box, running_xray(box):
            script_menu_processes(box)
            config = box.config()
            next(item for item in config["inbounds"] if item["tag"] == "vless-reality")["port"] = 5443
            write_config(box, config)
            result = box.run_menu(answers)

            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertIn("Added Trojan connection: trojan-main", result.stdout)
            self.assertIn("PUBLIC_PORT: 443", result.stdout)
            self.assertIn("FINGERPRINT: chrome", result.stdout)
            inbound = next(item for item in box.config()["inbounds"] if item["tag"] == "trojan-tls")
            self.assertEqual((inbound["protocol"], inbound["listen"], inbound["port"]), ("trojan", "127.0.0.1", 10100))
            self.assertEqual(inbound["streamSettings"]["wsSettings"]["path"], "/private-trojan")
            site = (box.paths.caddy_conf_dir / "vpn.example.com.caddy").read_text()
            self.assertIn("protocols tls1.2 tls1.3", site)
            self.assertFalse(any(call[:2] == ("xray-client", "add-trojan-connection") for call in box.processes.calls))

    def test_create_trojan_connection_refuses_existing_caddy_site(self) -> None:
        answers = ["2", "2", "2", "trojan-main", "", "vpn.example.com", "/private-trojan",
                   "1", "", "y", "0", "0", "0"]
        with Sandbox() as box:
            script_menu_processes(box)
            config = box.config()
            next(item for item in config["inbounds"] if item["tag"] == "vless-reality")["port"] = 5443
            write_config(box, config)
            path = sites.write_site_config("vpn.example.com", 10100)
            before = path.read_bytes()
            rows = box.db_rows("reality_connections")
            result = box.run_menu(answers)

            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertIn("existing site", result.stdout)
            self.assertIn("Caddy / TLS -> Site configs", result.stdout)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(box.db_rows("reality_connections"), rows)
            box.assert_restarted(times=0)

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
        with Sandbox() as box:
            script_menu_processes(box)
            box.paths.caddy_conf_dir.mkdir(parents=True)
            path = box.paths.caddy_conf_dir / "site.example.com.caddy"
            path.write_text("site.example.com {\n    file_server\n}\n")

            result = box.run_menu(["2", "4", "2", "5", "4", "0", "0", "0", "0"])

            self.assertEqual(result.exit_code, 0)
            self.assertIn("Caddy site TLS updated:", result.stdout)
            self.assertIn("protocols tls1.3 tls1.3", path.read_text())
            self.assertIn("file_server", path.read_text())


if __name__ == "__main__":
    unittest.main()
