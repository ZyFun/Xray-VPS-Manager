import unittest
from datetime import datetime, timezone

from xray_vps_manager.telegram import payments


class TelegramPaymentDetailsTests(unittest.TestCase):
    def test_domain_annual_rent_is_added_to_monthly_payment_share(self) -> None:
        db = {
            "paymentTotalAmount": "1000",
            "paymentDomainAnnualAmount": "1200",
            "paymentCurrency": "₽",
            "paymentRoundingMode": "none",
            "paymentRoundingStep": "10",
        }
        client_db = {
            "clients": {
                "alice": {"paymentType": "paid"},
                "bob": {"paymentType": "paid"},
                "carol": {"paymentType": "free"},
            }
        }

        summary = payments.payment_summary(db, client_db)

        self.assertEqual(summary["serverMonthly"], "1000 ₽")
        self.assertEqual(summary["domainAnnual"], "1200 ₽")
        self.assertEqual(summary["domainMonthly"], "100 ₽")
        self.assertEqual(summary["total"], "1100 ₽")
        self.assertEqual(summary["share"], "550 ₽")
        self.assertEqual(payments.payment_amount_label(db, client_db), "550 ₽")

    def test_disabled_and_expired_paid_clients_are_excluded_from_share(self) -> None:
        """VIREI-240: the rent is split only between enabled paid clients with unexpired access."""
        now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        db = {"paymentTotalAmount": "900", "paymentCurrency": "₽"}
        client_db = {
            "clients": {
                "alice": {"paymentType": "paid", "enabled": True},
                "bob": {"paymentType": "paid", "enabled": True, "expiresAt": "2026-11-01T00:00:00+03:00"},
                "carol": {"paymentType": "paid", "enabled": True, "expiresAt": "2026-10-20T00:00:00+03:00"},
                "dave": {
                    "paymentType": "paid",
                    "enabled": False,
                    "disabledReason": "expired",
                    "expiresAt": "2026-10-18T00:00:00+03:00",
                },
                "erin": {"paymentType": "paid", "enabled": False, "disabledReason": "traffic-limit"},
                "frank": {"paymentType": "paid", "enabled": False},
                "grace": {"paymentType": "free", "enabled": True},
            }
        }

        self.assertEqual(payments.paid_client_count(client_db, now), 3)
        self.assertEqual(payments.excluded_paid_client_count(client_db, now), 3)
        self.assertEqual(payments.payment_amount_label(db, client_db, now), "300 ₽")
        summary = payments.payment_summary(db, client_db, now)
        self.assertEqual(summary["paidCount"], 3)
        self.assertEqual(summary["excludedPaidCount"], 3)
        self.assertEqual(summary["share"], "300 ₽")

    def test_paid_client_with_passed_expiry_is_excluded_before_timer_disables_it(self) -> None:
        now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        db = {"paymentTotalAmount": "1000", "paymentCurrency": "₽"}
        client_db = {
            "clients": {
                "alice": {"paymentType": "paid", "enabled": True},
                "bob": {"paymentType": "paid", "enabled": True, "expiresAt": "2026-10-19T14:59:00+03:00"},
            }
        }

        self.assertEqual(payments.paid_client_count(client_db, now), 1)
        self.assertEqual(payments.payment_amount_label(db, client_db, now), "1000 ₽")

    def test_renewed_paid_client_counts_again(self) -> None:
        now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        db = {"paymentTotalAmount": "1000", "paymentCurrency": "₽"}
        bob = {"paymentType": "paid", "enabled": False, "disabledReason": "expired", "expiresAt": "2026-10-18T00:00:00+03:00"}
        client_db = {"clients": {"alice": {"paymentType": "paid", "enabled": True}, "bob": bob}}
        self.assertEqual(payments.payment_amount_label(db, client_db, now), "1000 ₽")

        bob.update({"enabled": True, "expiresAt": "2026-11-18T00:00:00+03:00"})
        bob.pop("disabledReason")

        self.assertEqual(payments.payment_amount_label(db, client_db, now), "500 ₽")
        self.assertEqual(payments.excluded_paid_client_count(client_db, now), 0)

    def test_share_is_not_calculated_when_all_paid_clients_are_disabled(self) -> None:
        db = {"paymentTotalAmount": "1000", "paymentCurrency": "₽"}
        client_db = {"clients": {"alice": {"paymentType": "paid", "enabled": False, "disabledReason": "expired"}}}

        self.assertEqual(
            payments.payment_amount_label(db, client_db),
            "не рассчитана: нет платных клиентов (общая сумма: 1000 ₽)",
        )
        self.assertEqual(payments.payment_summary(db, client_db)["share"], "не рассчитана")

    def test_domain_annual_rent_monthly_part_rounds_up_to_cents(self) -> None:
        db = {
            "paymentTotalAmount": "1000",
            "paymentDomainAnnualAmount": "1000",
            "paymentCurrency": "$",
        }

        self.assertEqual(payments.domain_monthly_amount(db), "83.34")
        self.assertEqual(payments.effective_total_amount(db), "1083.34")

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
