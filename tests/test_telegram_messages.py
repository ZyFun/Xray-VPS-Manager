from datetime import datetime
from zoneinfo import ZoneInfo
import unittest

from xray_vps_manager.telegram import messages


def bot_name(_db=None) -> str:
    return "Vireika"


def payment_amount_label(_db=None) -> str:
    return "500 ₽"


class TelegramMessageTests(unittest.TestCase):
    def test_subscription_intro_uses_configured_bot_name(self) -> None:
        text = messages.subscription_intro_text({}, bot_name)

        self.assertIn("Привет. Я бот Vireika.", text)
        self.assertIn("VLESS Reality-ссылку", text)

    def test_client_help_text_uses_configured_bot_name(self) -> None:
        text = messages.client_help_text({}, bot_name)

        self.assertIn("Vireika: помощь", text)
        self.assertIn("Я рядом, чтобы было проще следить за доступом к VPN.", text)
        self.assertIn("• проверить статус подписки;", text)
        self.assertIn("• отписаться от бота, если уведомления больше не нужны.", text)
        self.assertIn("Обычно я напоминаю об оплате в 08:00", text)
        self.assertIn("Иногда я также присылаю технические уведомления", text)

    def test_expiry_reminder_includes_payment_amount_without_client_name(self) -> None:
        text = messages.build_expiry_reminder_message(
            {},
            {"name": "internal_client"},
            5,
            datetime(2026, 7, 4, 0, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            "Europe/Moscow",
            bot_name,
            payment_amount_label,
        )

        self.assertIn("Vireika: напоминание об оплате", text)
        self.assertIn("Через 5 дней заканчивается текущий период.", text)
        self.assertIn("Доступ до: 2026-07-04 00:00 Europe/Moscow", text)
        self.assertIn("Сумма оплаты: 500 ₽", text)
        self.assertNotIn("Перевод нужно выполнить", text)
        self.assertNotIn("internal_client", text)

    def test_expiry_reminder_includes_phone_payment_details(self) -> None:
        text = messages.build_expiry_reminder_message(
            {
                "paymentTransferMethod": "phone",
                "paymentPhone": "+79991234567",
                "paymentBank": "Т-Банк (Тинькофф)",
            },
            {},
            1,
            datetime(2026, 7, 4, 0, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            "Europe/Moscow",
            bot_name,
            payment_amount_label,
        )

        self.assertIn("Через 1 день заканчивается текущий период.", text)
        self.assertIn("Перевод нужно выполнить по номеру телефона:\n+79991234567", text)
        self.assertIn("Банк: Т-Банк (Тинькофф)", text)

    def test_access_updated_message_uses_friendly_payment_received_text(self) -> None:
        text = messages.build_access_updated_message(
            {},
            {"expiresAt": "2026-08-03T00:00:00+03:00"},
            bot_name,
            lambda value: "2026-08-03 00:00 Europe/Moscow" if value else "бессрочно",
        )

        self.assertEqual(
            text,
            "\n".join(
                [
                    "Vireika: всё готово",
                    "",
                    "Спасибо! Оплата за совместную аренду сервера получена.",
                    "",
                    "Доступ продлён до: 2026-08-03 00:00 Europe/Moscow",
                ]
            ),
        )

    def test_client_added_message_uses_payment_reminder_transfer_format(self) -> None:
        text = messages.build_client_added_message(
            {
                "botUsername": "ExampleVpnBot",
                "paymentTransferMethod": "phone",
                "paymentPhone": "+79991234567",
                "paymentBank": "Т-Банк (Тинькофф)",
            },
            "vless://example?type=tcp&security=reality#Xray",
            "2026-07-14 00:00 Europe/Moscow",
            "paid",
            "500 ₽",
            bot_name,
        )

        self.assertIn(
            "Ваш VPN-ключ:\n<pre><code>vless://example?type=tcp&amp;security=reality#Xray</code></pre>",
            text,
        )
        self.assertIn("По этому же ключу @ExampleVpnBot будет показывать статус подписки", text)
        self.assertIn("Не забудь открыть @ExampleVpnBot и подключить уведомления.", text)
        self.assertIn("Сумма оплаты: 500 ₽", text)
        self.assertIn("Перевод нужно выполнить по номеру телефона:\n+79991234567", text)
        self.assertIn("Банк: Т-Банк (Тинькофф)", text)
        self.assertNotIn("Когда будет удобно", text)
        self.assertNotIn("подтверждение", text)

    def test_maintenance_template_aliases_and_unknown_template(self) -> None:
        self.assertEqual(messages.normalize_maintenance_template_id(""), "start")
        self.assertEqual(messages.normalize_maintenance_template_id("2"), "done")
        self.assertIn("Vireika: плановые работы", messages.maintenance_notice_message({}, "start", bot_name))
        self.assertIn("Vireika: работы завершены", messages.maintenance_notice_message({}, "done", bot_name))

        with self.assertRaisesRegex(ValueError, "Неизвестный шаблон"):
            messages.maintenance_notice_message({}, "strange", bot_name)

    def test_truncate_telegram_text_keeps_short_text_and_marks_long_text(self) -> None:
        self.assertEqual(messages.truncate_telegram_text("short", limit=20), "short")

        truncated = messages.truncate_telegram_text("x" * 200, limit=100)
        self.assertLessEqual(len(truncated), 100)
        self.assertTrue(truncated.endswith("...вывод сокращён..."))


if __name__ == "__main__":
    unittest.main()
