import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from xray_vps_manager.commands import menu, menu_client_actions
from xray_vps_manager.db.storage import SQLiteReadUnavailable

ROWS = [
    {
        "name": "alice",
        "connectionName": "VLESS",
        "status": "enabled",
        "paymentType": "free",
        "expiresAt": "",
        "created": "2026-01-01",
        "credentialsTotal": 2,
    },
]
TRAFFIC_DB = {
    "clients": {"alice": {"incoming": 1024**3, "outgoing": 512 * 1024**2}},
    "credentials": {},
}


class RemoveClientFromMenuTests(unittest.TestCase):
    def run_remove(self, *, choice="1", confirmed=False, traffic=TRAFFIC_DB):
        call = mock.Mock()
        confirm = mock.Mock(return_value=confirmed)
        traffic_patch = (
            {"side_effect": traffic} if isinstance(traffic, Exception) else {"return_value": traffic}
        )
        output = io.StringIO()
        with mock.patch.object(menu_client_actions, "client_rows_for_selection", return_value=ROWS), \
            mock.patch("xray_vps_manager.traffic.repository.load_traffic_db_for_read", **traffic_patch), \
            mock.patch("builtins.input", return_value=choice), \
            redirect_stdout(output):
            menu_client_actions.remove_client_from_menu(call, confirm)
        return call, confirm, output.getvalue()

    def test_declined_confirmation_does_not_remove_client(self) -> None:
        call, confirm, output = self.run_remove(confirmed=False)

        confirm.assert_called_once()
        call.assert_not_called()
        self.assertIn("Действие отменено.", output)

    def test_confirmed_removal_calls_remove_command(self) -> None:
        call, confirm, _output = self.run_remove(confirmed=True)

        confirm.assert_called_once()
        call.assert_called_once_with(["xray-client", "remove", "alice"])

    def test_summary_shows_name_credentials_and_total_traffic_before_question(self) -> None:
        call, confirm, output = self.run_remove(confirmed=False)

        self.assertIn("alice", output)
        self.assertIn("Подключений (credentials): 2", output)
        self.assertIn("Трафик за всё время: 1.50GB", output)
        self.assertIn("alice", confirm.call_args.args[0])

    def test_unreadable_traffic_is_shown_as_unknown(self) -> None:
        call, confirm, output = self.run_remove(
            confirmed=False,
            traffic=SQLiteReadUnavailable("SQLite manager database is missing."),
        )

        self.assertIn("Трафик за всё время: неизвестно", output)
        confirm.assert_called_once()
        call.assert_not_called()

    def test_back_on_client_selection_skips_question_and_command(self) -> None:
        call, confirm, output = self.run_remove(choice="0")

        confirm.assert_not_called()
        call.assert_not_called()
        self.assertIn("Действие отменено.", output)

    def test_client_menu_item_uses_confirming_adapter(self) -> None:
        self.assertIn(("10", "Удалить клиента"), menu.client_menu_actions())
        title, handler = menu.client_menu_handlers()["10"]
        self.assertEqual(title, "Удалить клиента")
        with mock.patch.object(menu_client_actions, "remove_client_from_menu") as adapter, \
            mock.patch.object(menu_client_actions, "call_client_command") as generic:
            handler()

        adapter.assert_called_once_with(menu.call, menu.confirm)
        generic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
