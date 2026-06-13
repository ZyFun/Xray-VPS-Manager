from datetime import date, datetime, timezone
from types import SimpleNamespace
import unittest
from unittest import mock

from xray_vps_manager.telegram import notifications


class TelegramDailySummaryTests(unittest.TestCase):
    def make_context(self, db):
        client_db = {
            "clients": {
                "alice": {"enabled": True, "paymentType": "paid"},
                "bob": {"enabled": True, "paymentType": "paid"},
            }
        }
        traffic_db = {
            "clients": {
                "alice": {
                    "history": {"2026-06-12": {"08": {"incoming": 1024, "outgoing": 2048}}},
                    "lastOnline": "2026-06-13T07:59:00Z",
                },
                "bob": {
                    "history": {"2026-06-12": {"09": {"incoming": 512, "outgoing": 512}}},
                },
            }
        }

        return notifications.NotificationContext(
            load_db=lambda: db,
            save_db_sections=lambda _db, _sections: None,
            load_client_db=lambda: client_db,
            load_traffic_db=lambda: traffic_db,
            display_timezone=lambda: (timezone.utc, "UTC"),
            format_event_time=lambda value: value,
            format_access_until=lambda value: value,
            parse_time=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None,
            utc_now=lambda: datetime(2026, 6, 13, 8, 0, tzinfo=timezone.utc),
            utc_stamp=lambda: "2026-06-13T08:00:00Z",
            run_capture=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="active\n", stderr=""),
            send_chat_message=lambda *_args, **_kwargs: None,
            send_message=lambda *_args, **_kwargs: None,
            bot_name=lambda _db=None: "Vireika",
        )

    def test_daily_summary_includes_total_rent_amount_not_client_share(self) -> None:
        ctx = self.make_context({
            "paymentTotalAmount": "1123.12",
            "paymentCurrency": "₽",
            "paymentRoundingMode": "step",
            "paymentRoundingStep": "10",
        })

        with mock.patch.object(notifications, "disk_usage_label", return_value="ok"):
            text = notifications.build_daily_summary_message(ctx, date(2026, 6, 12))

        self.assertIn("Общая аренда сервера: 1123.12 ₽", text)
        self.assertNotIn("570 ₽", text)

    def test_daily_summary_marks_missing_total_rent_amount(self) -> None:
        ctx = self.make_context({"paymentTotalAmount": "", "paymentCurrency": "₽"})

        with mock.patch.object(notifications, "disk_usage_label", return_value="ok"):
            text = notifications.build_daily_summary_message(ctx, date(2026, 6, 12))

        self.assertIn("Общая аренда сервера: не указана", text)


if __name__ == "__main__":
    unittest.main()
