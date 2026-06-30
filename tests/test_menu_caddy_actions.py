import unittest
from unittest import mock

from xray_vps_manager.commands import menu_caddy_actions
from xray_vps_manager.clients import connections as client_connections


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


if __name__ == "__main__":
    unittest.main()
