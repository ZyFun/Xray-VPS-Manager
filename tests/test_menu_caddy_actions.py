import functools
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from xray_vps_manager.commands import menu_caddy_actions
from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.xray import caddy


class MenuCaddyActionsTests(unittest.TestCase):
    def test_format_timer_eta_uses_minutes_and_hours(self) -> None:
        self.assertEqual(menu_caddy_actions.format_timer_eta(0), "сейчас")
        self.assertEqual(menu_caddy_actions.format_timer_eta(30), "через 1 мин")
        self.assertEqual(menu_caddy_actions.format_timer_eta(900), "через 15 мин")
        self.assertEqual(menu_caddy_actions.format_timer_eta(3600), "через 1 ч")
        self.assertEqual(menu_caddy_actions.format_timer_eta(4500), "через 1 ч 15 мин")

    def test_timer_line_next_and_left_parses_systemd_list_timers(self) -> None:
        next_run, left = menu_caddy_actions.timer_line_next_and_left(
            "Mon 2026-06-22 01:49:41 MSK 43min "
            "Mon 2026-06-22 01:01:45 MSK 4min 49s ago "
            "xray-caddy-random-tls@api.example.com.timer xray-caddy-random-tls@api.example.com.service"
        )

        self.assertEqual(next_run, "Mon 2026-06-22 01:49:41 MSK")
        self.assertEqual(left, "43min")
        self.assertEqual(menu_caddy_actions.format_systemd_left(left), "через 43 мин")

    def test_timer_line_next_and_left_parses_multi_part_left(self) -> None:
        next_run, left = menu_caddy_actions.timer_line_next_and_left(
            "Mon 2026-06-22 02:19:41 MSK 1h 13min n/a n/a "
            "xray-caddy-random-tls@api.example.com.timer xray-caddy-random-tls@api.example.com.service"
        )

        self.assertEqual(next_run, "Mon 2026-06-22 02:19:41 MSK")
        self.assertEqual(left, "1h 13min")
        self.assertEqual(menu_caddy_actions.format_systemd_left(left), "через 1 ч 13 мин")

    def test_tls_connection_options_include_only_caddy_compatible_transports(self) -> None:
        config = {
            "inbounds": [
                client_connections.make_tls_xhttp_inbound("vless-tls", 10000, xhttp_path="/vless"),
                client_connections.make_trojan_ws_inbound("trojan-tls", 10100, "/trojan"),
                client_connections.make_trojan_tls_inbound(
                    "trojan-tls-2",
                    8443,
                    "/etc/ssl/fullchain.pem",
                    "/etc/ssl/privkey.pem",
                ),
            ]
        }
        db = {
            "connections": {
                "vless-tls": {
                    "tag": "vless-tls",
                    "name": "api",
                    "security": "tls",
                    "transport": "xhttp",
                    "publicHost": "api.example.com",
                    "publicPort": 443,
                    "localPort": 10000,
                    "xhttpPath": "/vless",
                },
                "trojan-tls": {
                    "tag": "trojan-tls",
                    "name": "trojan",
                    "protocol": "trojan",
                    "security": "tls",
                    "transport": "ws",
                    "publicHost": "vpn.example.com",
                    "publicPort": 443,
                    "localPort": 10100,
                    "wsPath": "/trojan",
                },
                "trojan-tls-2": {
                    "tag": "trojan-tls-2",
                    "name": "legacy",
                    "protocol": "trojan",
                    "security": "tls",
                    "transport": "tcp",
                    "sni": "legacy.example.com",
                    "port": 8443,
                },
            },
            "clients": {},
        }

        with mock.patch.object(menu_caddy_actions, "load_xray_config", return_value=config), \
            mock.patch.object(menu_caddy_actions, "load_db_sql", return_value=db):
            options = menu_caddy_actions.tls_connection_options()

        self.assertEqual([item["tag"] for item in options], ["vless-tls", "trojan-tls"])
        self.assertEqual(options[0]["routePath"], "/vless")
        self.assertEqual(options[1]["upstreamTransport"], "http")
        self.assertEqual(options[1]["routePath"], "/trojan")


class MenuCaddyBackupRotationTests(unittest.TestCase):
    """Действия меню Caddy с копией файла оставляют не больше N копий (VIREI-241)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conf_dir = Path(tmp.name) / "conf.d"
        self.conf_dir.mkdir()
        self.caddyfile = Path(tmp.name) / "Caddyfile"
        self.caddyfile.write_text(f"import {self.conf_dir}/*.caddy\n")
        conf_dir = self.conf_dir
        list_site_configs = caddy.list_site_configs
        self.reload = mock.Mock()
        patches = [
            mock.patch.object(caddy, "CADDYFILE_PATH", self.caddyfile),
            mock.patch.object(
                caddy, "site_config_path", lambda domain, conf_dir_arg=None: conf_dir / caddy.site_filename(domain)
            ),
            mock.patch.object(
                caddy,
                "write_site_config",
                functools.partial(caddy.write_site_config, conf_dir=conf_dir, caddyfile_path=self.caddyfile),
            ),
            mock.patch.object(caddy, "list_site_configs", lambda: list_site_configs(conf_dir)),
            mock.patch.object(
                caddy,
                "remove_site_block_from_caddyfile",
                functools.partial(caddy.remove_site_block_from_caddyfile, caddyfile_path=self.caddyfile),
            ),
            mock.patch.object(caddy, "validate_and_reload_caddy", self.reload),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def write_site(self, domain: str) -> Path:
        path = self.conf_dir / caddy.site_filename(domain)
        path.write_text(caddy.caddy_site_block(domain, 10300))
        return path

    def make_copies(self, path: Path, count: int) -> list:
        copies = []
        for index in range(count):
            copy = path.with_name(f"{path.name}.bak.2026010100000000{index:04d}")
            copy.write_text(f"old {index}\n")
            copies.append(copy)
        return copies

    def copies_of(self, path: Path) -> list:
        prefix = f"{path.name}.bak."
        return sorted(
            item for item in path.parent.iterdir() if item.name.startswith(prefix) and item.name[len(prefix):].isdigit()
        )

    def test_update_site_domain_prunes_copies_of_old_site(self) -> None:
        old_path = self.write_site("old.example.com")
        old = self.make_copies(old_path, 7)
        new_path = self.conf_dir / caddy.site_filename("new.example.com")
        new_old = self.make_copies(new_path, 7)

        with mock.patch("builtins.input", return_value="new.example.com"), redirect_stdout(StringIO()):
            menu_caddy_actions.update_site_domain()

        self.assertFalse(old_path.exists())
        self.assertTrue(new_path.exists())
        old_copies = self.copies_of(old_path)
        self.assertEqual(len(old_copies), caddy.SITE_BACKUP_KEEP)
        self.assertEqual(old_copies[:-1], old[-4:])
        self.assertEqual(self.copies_of(new_path), new_old[-5:])

    def test_delete_site_prunes_copies_and_keeps_reported_backup(self) -> None:
        path = self.write_site("api.example.com")
        old = self.make_copies(path, 7)
        output = StringIO()

        with mock.patch("builtins.input", return_value="1"), redirect_stdout(output):
            menu_caddy_actions.delete_site(lambda message: True)

        copies = self.copies_of(path)
        self.assertFalse(path.exists())
        self.assertEqual(copies[:-1], old[-4:])
        self.assertIn(f"Backup: {copies[-1]}", output.getvalue())

    def test_delete_site_failure_restores_file_and_keeps_copies(self) -> None:
        path = self.write_site("api.example.com")
        self.make_copies(path, 7)
        self.reload.side_effect = subprocess.CalledProcessError(1, ["caddy", "validate"])

        with mock.patch("builtins.input", return_value="1"), redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit):
                menu_caddy_actions.delete_site(lambda message: True)

        self.assertTrue(path.exists())
        self.assertEqual(len(self.copies_of(path)), 8)

    def test_remove_default_http_site_prunes_caddyfile_copies(self) -> None:
        self.caddyfile.write_text(f":80 {{\n    file_server\n}}\n\nimport {self.conf_dir}/*.caddy\n")
        old = self.make_copies(self.caddyfile, 7)
        output = StringIO()

        with redirect_stdout(output):
            menu_caddy_actions.remove_default_http_site(lambda message: True)

        copies = self.copies_of(self.caddyfile)
        self.assertNotIn(":80 {", self.caddyfile.read_text())
        self.assertEqual(copies[:-1], old[-4:])
        self.assertIn(f"Backup: {copies[-1]}", output.getvalue())


if __name__ == "__main__":
    unittest.main()
