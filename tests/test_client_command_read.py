import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
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

    def test_connection_list_shows_protocol_column(self) -> None:
        config = {
            "inbounds": [
                client_command.client_connections.make_reality_inbound(
                    "vless-reality",
                    443,
                    "example.com",
                    "private-key",
                    "abcd",
                )
            ],
            "outbounds": [],
        }
        db = {"connections": {}, "clients": {}}
        with mock.patch.object(client_command.client_connections, "server_env_values", return_value={}):
            client_command.client_connections.add_trojan_caddy_connection(
                config,
                db,
                "trojan-web",
                "vpn.example.com",
                local_port=10100,
                public_port=443,
                fingerprint_value="firefox",
                ws_path="/trojan",
            )

        read_result = client_command.client_repository.ClientDbReadResult(db, "sqlite")
        output = StringIO()
        with mock.patch.object(client_command, "load_config", return_value=config), \
            mock.patch.object(client_command, "load_db_readonly", return_value=read_result), \
            mock.patch.object(client_command.client_connections, "server_env_values", return_value={}), \
            mock.patch.object(client_command.client_connections, "fingerprint", return_value="chrome"), \
            redirect_stdout(output):
            client_command.cmd_connection_list()

        text = output.getvalue()
        self.assertIn("PROTOCOL", text)
        self.assertIn("SECURITY", text)
        self.assertIn("vless", text)
        self.assertIn("trojan", text)
        self.assertIn("tls", text)

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

    def test_trojan_caddy_site_conflict_stops_before_config_save(self) -> None:
        with mock.patch.object(client_command, "load_config", return_value={"inbounds": []}), \
            mock.patch.object(client_command, "load_db", return_value={"connections": {}, "clients": {}}), \
            mock.patch.object(
                client_command.xray_caddy,
                "require_site_config_absent",
                side_effect=FileExistsError("existing site"),
            ) as require_absent, \
            mock.patch.object(client_command.client_connections, "add_trojan_caddy_connection") as add_connection, \
            mock.patch.object(client_command, "save_config_restart_xray_and_db") as save_config:
            with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
                client_command.cmd_trojan_connection_add(
                    "trojan-main",
                    "10100",
                    "vpn.example.com",
                    fingerprint_value="chrome",
                )

        require_absent.assert_called_once_with("vpn.example.com")
        add_connection.assert_not_called()
        save_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
