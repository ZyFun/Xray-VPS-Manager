import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from xray_vps_manager.commands import menu_telegram_actions


class MenuTelegramActionsTests(unittest.TestCase):
    def test_update_payment_details_phone_calls_independent_command(self) -> None:
        calls = []
        inputs = iter(["5", "+7 999 123-45-67", "2"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            menu_telegram_actions.update_payment_details(calls.append)

        self.assertEqual(calls[0], ["xray-telegram", "payment-details"])
        self.assertEqual(
            calls[1],
            ["xray-telegram", "payment-details", "phone", "+7 999 123-45-67", "Сбербанк"],
        )

    def test_update_payment_amount_does_not_configure_payment_details(self) -> None:
        calls = []
        inputs = iter(["", "1"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            menu_telegram_actions.update_payment_amount(calls.append)

        self.assertEqual(calls, [["xray-telegram", "payment-amount"]])


if __name__ == "__main__":
    unittest.main()
