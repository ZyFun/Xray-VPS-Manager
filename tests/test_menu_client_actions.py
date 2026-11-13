from contextlib import redirect_stderr
from io import StringIO
import sys
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox
from xray_vps_manager.commands.menu import execute_action
from xray_vps_manager.commands import menu_client_actions


class MenuClientActionsTests(unittest.TestCase):
    def test_add_client_menu_rejects_existing_client_name(self) -> None:
        with mock.patch("builtins.input", return_value="alice 30"), \
            mock.patch.object(menu_client_actions, "client_exists_for_menu", return_value=True), \
            mock.patch.object(
                menu_client_actions.menu_reality_actions,
                "choose_connection",
            ) as choose_connection:
            self.assertEqual(menu_client_actions.ask_new_client_command(), [])

        choose_connection.assert_not_called()

    def test_existing_client_connection_menu_builds_add_command_without_payment_change(self) -> None:
        with mock.patch.object(
            menu_client_actions,
            "choose_client_row",
            return_value={"name": "alice", "connection": "vless-main"},
        ), mock.patch.object(
            menu_client_actions,
            "choose_available_connection_for_client",
            return_value="trojan-main",
        ):
            self.assertEqual(
                menu_client_actions.ask_existing_client_connection_command(),
                ["xray-client", "add", "alice", "--connection", "trojan-main"],
            )

    def test_client_route_selection_uses_numbered_cascade_table(self) -> None:
        config = {
            "outbounds": [
                {"tag": "direct", "protocol": "freedom"},
                {"tag": "cascade-de", "protocol": "vless"},
                {"tag": "cascade-us", "protocol": "vless"},
            ],
            "routing": {
                "rules": [
                    {"type": "field", "network": "tcp,udp", "outboundTag": "cascade-de"},
                ]
            },
        }
        db = {
            "cascadeRoutes": {
                "cascade-de": {"tag": "cascade-de", "country": "Германия"},
                "cascade-us": {"tag": "cascade-us", "country": "США"},
            },
            "clients": {
                "alice": {
                    "selectedCascadeTag": "cascade-de",
                },
            },
        }

        with mock.patch.object(menu_client_actions, "load_config", return_value=config), \
            mock.patch.object(menu_client_actions, "load_db_sql", return_value=db), \
            mock.patch("builtins.input", return_value="2"):
            self.assertEqual(menu_client_actions.choose_client_route("alice"), "cascade-us")

    def test_update_selected_client_route_uses_shared_adapter(self) -> None:
        with mock.patch.object(menu_client_actions, "choose_client", return_value="alice"), \
            mock.patch.object(menu_client_actions, "choose_client_route", return_value="cascade-us"), \
            mock.patch.object(menu_client_actions, "run_command") as update_route:
            menu_client_actions.update_selected_client_route()

        update_route.assert_called_once_with("client", argv=["route", "alice", "cascade-us"])

    def test_menu_access_preserves_cli_argument_validation(self) -> None:
        with Sandbox() as box:
            for value in ("--bogus", "-h"):
                with self.subTest(value=value):
                    cli = box.run_cli("client", "extend-days", "alice", value)
                    argv = sys.argv
                    stderr = StringIO()
                    with redirect_stderr(stderr), \
                            mock.patch.object(menu_client_actions, "choose_client", return_value="alice"), \
                            mock.patch("builtins.input", side_effect=["1", value]):
                        menu_client_actions.update_selected_client_days()
                    self.assertEqual(cli.exit_code, 2)
                    self.assertEqual(stderr.getvalue(), cli.stderr)
                    self.assertIs(sys.argv, argv)

    def test_menu_routes_cli_cancellation_to_menu_action_boundary(self) -> None:
        with Sandbox(), mock.patch.object(menu_client_actions, "choose_client", return_value="alice"), \
                mock.patch.object(menu_client_actions, "choose_client_route", return_value="cascade-us"), \
                mock.patch("xray_vps_manager.clients.service.set_route", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt) as error:
                execute_action("Изменить страну подключения", menu_client_actions.update_selected_client_route)
        self.assertTrue(error.exception.menu_cancel_reported)

    def test_available_connections_skip_client_existing_credentials(self) -> None:
        db = {
            "clients": {
                "alice": {
                    "credentials": {
                        "vless-main": {"connection": "vless-main"},
                    },
                },
            },
        }
        connection_rows = [
            {
                "tag": "vless-main",
                "name": "VLESS",
                "security": "reality",
                "port": 443,
                "sni": "a.example",
                "transport": "tcp",
                "fingerprint": "chrome",
            },
            {
                "tag": "trojan-main",
                "name": "Trojan",
                "security": "tls",
                "port": 443,
                "sni": "b.example",
                "transport": "ws",
                "fingerprint": "-",
            },
        ]
        with mock.patch.object(menu_client_actions, "load_db_sql", return_value=db), \
            mock.patch.object(
                menu_client_actions.menu_reality_actions,
                "connection_rows",
                return_value=connection_rows,
            ):
            self.assertEqual(
                menu_client_actions.choose_available_connection_for_client("alice"),
                "trojan-main",
            )

    def test_available_connections_reports_when_client_has_all_connections(self) -> None:
        db = {
            "clients": {
                "alice": {
                    "credentials": {
                        "vless-main": {"connection": "vless-main"},
                        "trojan-main": {"connection": "trojan-main"},
                    },
                },
            },
        }
        connection_rows = [
            {"tag": "vless-main", "name": "VLESS"},
            {"tag": "trojan-main", "name": "Trojan"},
        ]
        with mock.patch.object(menu_client_actions, "load_db_sql", return_value=db), \
            mock.patch.object(
                menu_client_actions.menu_reality_actions,
                "connection_rows",
                return_value=connection_rows,
            ), \
            mock.patch("builtins.print") as print_mock:
            self.assertEqual(
                menu_client_actions.choose_available_connection_for_client("alice"),
                "",
            )

        print_mock.assert_any_call("У клиента уже есть credentials во всех доступных подключениях.")


if __name__ == "__main__":
    unittest.main()
