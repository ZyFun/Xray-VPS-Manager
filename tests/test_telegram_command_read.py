import unittest
from unittest import mock

from xray_vps_manager.commands import telegram as telegram_command


class TelegramCommandReadTests(unittest.TestCase):
    def test_load_db_uses_runtime_read_layer(self) -> None:
        expected = {"enabled": True, "source": "sqlite"}
        with mock.patch.object(
            telegram_command.telegram_settings,
            "load_db_sql",
            return_value=expected,
        ) as load_db_sql:
            self.assertEqual(telegram_command.load_db(), expected)

        load_db_sql.assert_called_once_with(telegram_command.TELEGRAM_DB_PATH)

    def test_load_traffic_db_uses_runtime_read_layer(self) -> None:
        expected = {"clients": {"alice": {"incoming": 1, "outgoing": 2}}}
        with mock.patch.object(
            telegram_command.traffic_repository,
            "load_traffic_db_for_read",
            return_value=expected,
        ) as load_traffic_for_read:
            self.assertEqual(telegram_command.load_traffic_db(), expected)

        load_traffic_for_read.assert_called_once_with(telegram_command.TRAFFIC_PATH)


if __name__ == "__main__":
    unittest.main()
