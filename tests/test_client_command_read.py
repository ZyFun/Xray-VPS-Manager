import unittest
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_client_characterization import ALICE_KEY, script_processes
from xray_vps_manager.commands import client_arguments
from xray_vps_manager.commands.client_connections import update_trojan_caddy_site
from xray_vps_manager.db import database
from xray_vps_manager.xray import caddy


def script_caddy(box):
    box.processes.executables["caddy"] = "/usr/bin/caddy"
    ok = box.processes.ok()
    box.processes.script({
        ("caddy", "validate", "--config", box.paths.caddyfile): ok,
        "systemctl enable --now caddy": ok,
        "systemctl reload caddy": ok,
    })


class ClientCommandReadTests(unittest.TestCase):
    def run_client(self, box, *args):
        result = box.run_cli("client", *args)
        self.assertEqual(result.exit_code, 0, result.stderr)
        return result

    def add_trojan(self, box, *options):
        script_processes(box)
        script_caddy(box)
        return self.run_client(
            box, "add-trojan-connection", "Trojan", "9443", "vpn.example.com", "--public-port", "1443", *options,
        )

    def test_parse_add_args_supports_protocol_sugar(self) -> None:
        parsed = client_arguments.parse_add_args(["alice", "30", "--protocol", "trojan", "--payment", "paid"])

        self.assertEqual(parsed, ("alice", 30, False, None, "trojan", "paid"))

    def test_parse_add_args_rejects_connection_and_protocol_together(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
            client_arguments.parse_add_args(["alice", "--connection", "trojan-tls", "--protocol", "trojan"])

    def test_cmd_add_prints_access_key_with_connection_link(self) -> None:
        with Sandbox() as box, running_xray(box):
            script_processes(box)
            setup = box.run_cli("client", "add-trojan-connection", "Trojan", "9443", "vpn.example.com", "--no-caddy")
            self.assertEqual(setup.exit_code, 0, setup.stderr)
            result = box.run_cli("client", "add", "alice", "30", "--connection", "trojan-tls")

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn(f"Access key: vpn-key:{ALICE_KEY}", result.stdout)
        self.assertRegex(result.stdout, r"trojan://[^@]+@vpn\.example\.com:443\?")
        self.assertNotIn("vpn-key:00000000-0000-4000-8000-000000000002", result.stdout)

    def test_trojan_password_check_prints_policy_rows(self) -> None:
        with Sandbox() as box, running_xray(box):
            self.add_trojan(box, "--no-caddy")
            self.run_client(box, "add", "alice", "--connection", "trojan-tls")
            output = self.run_client(box, "trojan-password-check").stdout

        self.assertIn("alice", output)
        self.assertIn("trojan-tls", output)
        self.assertIn("OK", output)

    def test_traffic_period_prints_credential_breakdown_for_multi_credential_client(self) -> None:
        with Sandbox() as box, running_xray(box):
            self.add_trojan(box, "--no-caddy")
            self.run_client(box, "add", "alice", "--connection", "trojan-tls")
            with database.write_session() as conn:
                conn.executemany(
                    "INSERT INTO credential_traffic_totals(client_name,connection_tag)"
                    " VALUES ('alice',?)",
                    [("vless-reality",), ("trojan-tls",)],
                )
                conn.execute("INSERT INTO traffic_totals(client_name) VALUES ('alice')")
                conn.executemany(
                    "INSERT INTO credential_traffic_history"
                    "(client_name,connection_tag,bucket_date,bucket_hour,incoming_bytes,outgoing_bytes)"
                    " VALUES ('alice',?,'2026-06-12',?,?,?)",
                    [("vless-reality", 8, 1024, 2048), ("trojan-tls", 9, 4096, 8192)],
                )
                conn.execute(
                    "INSERT INTO traffic_history(client_name,bucket_date,bucket_hour,incoming_bytes,outgoing_bytes)"
                    " VALUES ('alice','2026-06-12',8,5120,10240)"
                )
            output = self.run_client(box, "traffic-period", "alice", "2026-06-12", "2026-06-12").stdout

        self.assertIn("Credentials", output)
        self.assertIn("vless-reality", output)
        self.assertIn("trojan-tls", output)
        self.assertIn("15.00KB", output)

    def test_rotate_trojan_password_prints_reissued_link(self) -> None:
        with Sandbox() as box, running_xray(box):
            script_processes(box)
            setup = box.run_cli("client", "add-trojan-connection", "Trojan", "9443", "vpn.example.com", "--no-caddy")
            self.assertEqual(setup.exit_code, 0, setup.stderr)
            added = box.run_cli("client", "add", "alice", "--connection", "trojan-tls")
            self.assertEqual(added.exit_code, 0, added.stderr)
            before = box.run_cli("client", "link", "alice", "--connection", "trojan-tls")
            result = box.run_cli("client", "rotate-trojan-password", "alice", "--connection", "trojan-tls")
            after = box.run_cli("client", "link", "alice", "--connection", "trojan-tls")

        self.assertEqual(before.exit_code, 0, before.stderr)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(after.exit_code, 0, after.stderr)
        self.assertIn("Trojan password rotated.", result.stdout)
        self.assertIn("Client must reimport this Trojan link:", result.stdout)
        self.assertRegex(after.stdout, r"trojan://[^@]+@vpn\.example\.com:443\?")
        self.assertIn(after.stdout.strip(), result.stdout)
        self.assertNotEqual(before.stdout, after.stdout)

    def test_connection_list_shows_protocol_column(self) -> None:
        with Sandbox() as box, running_xray(box):
            self.add_trojan(box, "--no-caddy")
            output = self.run_client(box, "connection-list").stdout

        self.assertIn("PROTOCOL", output)
        self.assertIn("SECURITY", output)
        self.assertIn("vless", output)
        self.assertIn("trojan", output)
        self.assertIn("tls", output)

    def test_parse_trojan_caddy_connection_args(self) -> None:
        parsed = client_arguments.parse_trojan_connection_add_args(
            [
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
                "--install-caddy",
                "--tls-min-version",
                "tls1.2",
                "--tls-max-version",
                "tls1.3",
            ]
        )

        self.assertEqual(
            parsed,
            (
                "trojan-main",
                "10100",
                "vpn.example.com",
                "",
                "",
                "chrome",
                "ws",
                "/private-trojan",
                "443",
                True,
                "tls1.2",
                "tls1.3",
            ),
        )

    def test_parse_trojan_connection_defaults_to_caddy_websocket(self) -> None:
        parsed = client_arguments.parse_trojan_connection_add_args(
            [
                "trojan-main",
                "10100",
                "vpn.example.com",
            ]
        )

        self.assertEqual(
            parsed,
            (
                "trojan-main",
                "10100",
                "vpn.example.com",
                "",
                "",
                "chrome",
                "ws",
                "/trojan",
                "",
                True,
                "tls1.2",
                "tls1.3",
            ),
        )

    def test_parse_trojan_connection_can_disable_caddy_setup(self) -> None:
        parsed = client_arguments.parse_trojan_connection_add_args(
            [
                "trojan-main",
                "10100",
                "vpn.example.com",
                "--no-caddy",
            ]
        )

        self.assertEqual(parsed[6], "ws")
        self.assertFalse(parsed[9])

    def test_parse_trojan_legacy_direct_tls_args(self) -> None:
        parsed = client_arguments.parse_trojan_connection_add_args(
            [
                "trojan-direct",
                "8443",
                "vpn.example.com",
                "/etc/ssl/vpn/fullchain.pem",
                "/etc/ssl/vpn/privkey.pem",
                "firefox",
            ]
        )

        self.assertEqual(parsed[3], "/etc/ssl/vpn/fullchain.pem")
        self.assertEqual(parsed[4], "/etc/ssl/vpn/privkey.pem")
        self.assertEqual(parsed[5], "firefox")
        self.assertEqual(parsed[6], "tcp")
        self.assertFalse(parsed[9])

    def test_parse_trojan_connection_update_args(self) -> None:
        parsed = client_arguments.parse_trojan_connection_update_args(
            [
                "trojan-tls",
                "--domain",
                "vpn.example.com",
                "--local-port",
                "10101",
                "--public-port",
                "443",
                "--ws-path",
                "/trojan2",
                "--fingerprint",
                "firefox",
                "--tls-min-version",
                "tls1.2",
                "--tls-max-version",
                "tls1.3",
            ]
        )

        self.assertEqual(
            parsed,
            (
                "trojan-tls",
                "vpn.example.com",
                "10101",
                "443",
                "/trojan2",
                "firefox",
                "tls1.2",
                "tls1.3",
            ),
        )

    def test_cmd_trojan_connection_update_refreshes_caddy_site(self) -> None:
        with Sandbox() as box, running_xray(box):
            self.add_trojan(box)
            # Проверка bind через системный шов, подменённый в Sandbox.
            with mock.patch("socket.socket", return_value=mock.MagicMock()):
                result = self.run_client(
                    box, "update-trojan-connection", "trojan-tls",
                    "--domain", "vpn.example.com", "--local-port", "10101",
                    "--public-port", "1443", "--ws-path", "/trojan2", "--fingerprint", "firefox",
                    "--tls-min-version", "tls1.2", "--tls-max-version", "tls1.3",
                )
            site = box.paths.caddy_conf_dir / "vpn.example.com.caddy"
            self.assertIn("127.0.0.1:10101", site.read_text())
            self.assertIn("/trojan2", site.read_text())
            self.assertEqual(box.processes.calls.count(("systemctl", "reload", "caddy")), 2)

        self.assertIn("Trojan connection updated: Trojan", result.stdout)
        self.assertIn("Выведи клиентам новые Trojan-ссылки", result.stdout)

    def test_trojan_caddy_site_conflict_stops_before_config_save(self) -> None:
        with Sandbox() as box, running_xray(box):
            self.add_trojan(box)
            before = box.paths.config.read_bytes()
            db_before = box.db_rows("reality_connections")
            calls = len(box.processes.calls)
            result = box.run_cli("client", "add-trojan-connection", "New", "10100", "vpn.example.com",
                                 "--public-port", "1443")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("already exists", result.stderr)
            self.assertEqual(box.paths.config.read_bytes(), before)
            self.assertEqual(box.db_rows("reality_connections"), db_before)
            self.assertEqual(len(box.processes.calls), calls)


class TrojanCaddySiteBackupRotationTests(unittest.TestCase):
    """Смена домена сайта Caddy в `xray-client` оставляет не больше N копий (VIREI-241)."""

    def test_update_trojan_caddy_site_domain_change_prunes_copies(self) -> None:
        with Sandbox() as box:
            script_caddy(box)
            conf_dir = box.paths.caddy_conf_dir
            conf_dir.mkdir(parents=True)
            old_path = conf_dir / "old.example.com.caddy"
            new_path = conf_dir / "new.example.com.caddy"
            old_path.write_text(caddy.caddy_site_block("old.example.com", 10100))
            for path in (old_path, new_path):
                for index in range(7):
                    path.with_name(f"{path.name}.bak.2026010100000000{index:04d}").write_text("old\n")
            result = SimpleNamespace(
                caddy_enabled=True,
                public_host="new.example.com",
                previous_public_host="old.example.com",
                local_port=10100,
                tls_min_version="tls1.2",
                tls_max_version="tls1.3",
                ws_path="/trojan",
            )
            written = update_trojan_caddy_site(result)

            old_copies = sorted(conf_dir.glob("old.example.com.caddy.bak.*"))
            new_copies = sorted(conf_dir.glob("new.example.com.caddy.bak.*"))
            self.assertFalse(old_path.exists())
            self.assertTrue(new_path.exists())
            self.assertEqual(len(old_copies), caddy.SITE_BACKUP_KEEP)
            self.assertEqual(old_copies[-1], written.backup)
            self.assertEqual(len(new_copies), caddy.SITE_BACKUP_KEEP)

if __name__ == "__main__":
    unittest.main()
