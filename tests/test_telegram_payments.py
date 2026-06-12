import unittest

from xray_vps_manager.telegram import payments


class TelegramPaymentDetailsTests(unittest.TestCase):
    def test_apply_phone_payment_details_normalizes_number(self) -> None:
        db = {}

        payments.apply_payment_transfer(db, "phone", "+7 999 123-45-67", "Т-Банк (Тинькофф)")

        self.assertEqual(db["paymentTransferMethod"], "phone")
        self.assertEqual(db["paymentPhone"], "+79991234567")
        self.assertEqual(db["paymentBank"], "Т-Банк (Тинькофф)")
        self.assertEqual(payments.payment_transfer_label(db), "по номеру телефона +79991234567, банк: Т-Банк (Тинькофф)")
        self.assertEqual(
            payments.payment_transfer_message_lines(db),
            [
                "Перевод нужно выполнить по номеру телефона:",
                "+79991234567",
                "Банк: Т-Банк (Тинькофф)",
            ],
        )

    def test_apply_payment_details_for_card_and_bank_account(self) -> None:
        db = {}

        payments.apply_payment_transfer(db, "card", "2200 0000 0000 0000")
        self.assertEqual(payments.payment_transfer_label(db), "по номеру карты 2200 0000 0000 0000")

        payments.apply_payment_transfer(db, "bank-account", "40817810000000000000")
        self.assertEqual(payments.payment_transfer_label(db), "на банковский счёт 40817810000000000000")
        self.assertEqual(db["paymentCard"], "")

    def test_clear_payment_details_restores_default_message(self) -> None:
        db = {}

        payments.apply_payment_transfer(db, "phone", "89991234567", "Сбербанк")
        payments.apply_payment_transfer(db, "none")

        self.assertEqual(payments.payment_transfer_label(db), "не указаны")
        self.assertEqual(payments.payment_transfer_message_lines(db), [])
        self.assertEqual(db["paymentPhone"], "")
        self.assertEqual(db["paymentBank"], "")

    def test_phone_payment_details_require_valid_phone_and_bank(self) -> None:
        with self.assertRaisesRegex(ValueError, "слишком короткий"):
            payments.apply_payment_transfer({}, "phone", "123", "Сбербанк")

        with self.assertRaisesRegex(ValueError, "Банк не может быть пустым"):
            payments.apply_payment_transfer({}, "phone", "+79991234567", "")


if __name__ == "__main__":
    unittest.main()
