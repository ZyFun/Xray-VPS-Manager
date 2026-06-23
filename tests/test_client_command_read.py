import unittest
from unittest import mock

from xray_vps_manager.commands import client as client_command


class ClientCommandReadTests(unittest.TestCase):
    def test_load_db_uses_runtime_read_layer(self) -> None:
        expected = {"clients": {"alice": {}}}
        with mock.patch.object(
            client_command.client_repository,
            "load_db_sql",
            return_value=expected,
        ) as load_db_sql:
            self.assertEqual(client_command.load_db(), expected)

        load_db_sql.assert_called_once_with()

    def test_load_traffic_db_uses_runtime_read_layer(self) -> None:
        expected = {"clients": {"alice": {"incoming": 1, "outgoing": 2}}}
        with mock.patch.object(
            client_command.traffic_repository,
            "load_traffic_db_for_read",
            return_value=expected,
        ) as load_traffic_for_read:
            self.assertEqual(client_command.load_traffic_db(), expected)

        load_traffic_for_read.assert_called_once_with()

    def test_parse_trojan_caddy_connection_args(self) -> None:
        parsed = client_command.parse_trojan_connection_add_args(
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
        parsed = client_command.parse_trojan_connection_add_args(
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
        parsed = client_command.parse_trojan_connection_add_args(
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
        parsed = client_command.parse_trojan_connection_add_args(
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


if __name__ == "__main__":
    unittest.main()
