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
        self.assertNotIn("internal_client", text)

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
