import unittest
from unittest import mock

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
