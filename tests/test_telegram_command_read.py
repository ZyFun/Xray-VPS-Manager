import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from xray_vps_manager.commands import telegram as telegram_command


class TelegramCommandReadTests(unittest.TestCase):
    def test_status_shows_paid_clients_sharing_the_rent(self) -> None:
        """VIREI-240: status shows active paid clients and the paid clients excluded from the share."""
        db = {"enabled": True, "paymentTotalAmount": "900", "paymentCurrency": "₽"}
        client_db = {
            "clients": {
                "alice": {"paymentType": "paid", "enabled": True, "expiresAt": "2099-01-01T00:00:00+03:00"},
                "bob": {"paymentType": "paid", "enabled": False, "disabledReason": "expired"},
                "carol": {"paymentType": "paid", "enabled": True, "expiresAt": "2020-01-01T00:00:00+03:00"},
                "dave": {"paymentType": "free", "enabled": True},
            }
        }
        output = io.StringIO()
        with mock.patch("xray_vps_manager.telegram.settings.load_db_sql", return_value=db), \
            mock.patch("xray_vps_manager.clients.repository.load_db_sql", return_value=client_db), \
            mock.patch("xray_vps_manager.xray.config.load_config", side_effect=OSError("no config")), \
            redirect_stdout(output):
            telegram_command.status()

        lines = [line.split(" : ", 1) for line in output.getvalue().splitlines()]
        values = {key.strip(): value for key, value in lines}
        self.assertEqual(values["Payment per paid client"], "900 ₽")
        self.assertEqual(values["Active paid clients"], "1")
        self.assertEqual(values["Paid clients excluded (disabled or expired)"], "2")

    def test_status_hides_excluded_paid_clients_row_when_none(self) -> None:
        db = {"paymentTotalAmount": "900", "paymentCurrency": "₽"}
        client_db = {"clients": {"alice": {"paymentType": "paid", "enabled": True}}}
        output = io.StringIO()
        with mock.patch("xray_vps_manager.telegram.settings.load_db_sql", return_value=db), \
            mock.patch("xray_vps_manager.clients.repository.load_db_sql", return_value=client_db), \
            mock.patch("xray_vps_manager.xray.config.load_config", side_effect=OSError("no config")), \
            redirect_stdout(output):
            telegram_command.status()

        self.assertIn("Active paid clients", output.getvalue())
        self.assertNotIn("Paid clients excluded", output.getvalue())

    def test_load_db_uses_runtime_read_layer(self) -> None:
        expected = {"enabled": True, "source": "sqlite"}
        with mock.patch.object(
            telegram_command.telegram_settings,
            "load_db_sql",
            return_value=expected,
        ) as load_db_sql:
            self.assertEqual(telegram_command.load_db(), expected)

        load_db_sql.assert_called_once_with()

    def test_load_traffic_db_uses_runtime_read_layer(self) -> None:
        expected = {"clients": {"alice": {"incoming": 1, "outgoing": 2}}}
        with mock.patch.object(
            telegram_command.traffic_repository,
            "load_traffic_db_for_read",
            return_value=expected,
        ) as load_traffic_for_read:
            self.assertEqual(telegram_command.load_traffic_db(), expected)

        load_traffic_for_read.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
