from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.telegram import notifications


class TelegramDailySummaryTests(unittest.TestCase):
    def make_context(self, db, manager_db_path=None):
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
            manager_db_path=manager_db_path,
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

        self.assertIn("Месячная аренда сервера: 1123.12 ₽", text)
        self.assertIn("Общая месячная аренда: 1123.12 ₽", text)
        self.assertNotIn("570 ₽", text)

    def test_daily_summary_adds_domain_annual_rent_to_monthly_total(self) -> None:
        ctx = self.make_context({
            "paymentTotalAmount": "1000",
            "paymentDomainAnnualAmount": "1200",
            "paymentCurrency": "₽",
        })

        with mock.patch.object(notifications, "disk_usage_label", return_value="ok"):
            text = notifications.build_daily_summary_message(ctx, date(2026, 6, 12))

        self.assertIn("Годовая аренда домена: 1200 ₽ (в месяц: 100 ₽)", text)
        self.assertIn("Общая месячная аренда: 1100 ₽", text)

    def test_daily_summary_marks_missing_total_rent_amount(self) -> None:
        ctx = self.make_context({"paymentTotalAmount": "", "paymentCurrency": "₽"})

        with mock.patch.object(notifications, "disk_usage_label", return_value="ok"):
            text = notifications.build_daily_summary_message(ctx, date(2026, 6, 12))

        self.assertIn("Общая месячная аренда: не указана", text)

    def test_daily_summary_includes_sqlite_database_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            db_path.write_bytes(b"x" * 2048)
            Path(f"{db_path}-wal").write_bytes(b"x" * 1024)
            ctx = self.make_context({"paymentTotalAmount": "500", "paymentCurrency": "₽"}, db_path)

            with mock.patch.object(notifications, "disk_usage_label", return_value="ok"):
                text = notifications.build_daily_summary_message(ctx, date(2026, 6, 12))

        self.assertIn("База данных: 3.00KB", text)
        self.assertIn("manager.db 2.00KB", text)
        self.assertIn("manager.db-wal 1.00KB", text)

    def test_daily_summary_reports_no_reboot_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            required_path = Path(tmp_dir) / "reboot-required"
            packages_path = Path(tmp_dir) / "reboot-required.pkgs"
            ctx = self.make_context({"paymentTotalAmount": "500", "paymentCurrency": "₽"})

            with (
                mock.patch.object(notifications, "disk_usage_label", return_value="ok"),
                mock.patch.object(notifications, "REBOOT_REQUIRED_PATH", required_path),
                mock.patch.object(notifications, "REBOOT_REQUIRED_PACKAGES_PATH", packages_path),
            ):
                text = notifications.build_daily_summary_message(ctx, date(2026, 6, 12))

        self.assertIn("Система: перезагрузка не требуется", text)

    def test_daily_summary_reports_kernel_reboot_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            required_path = Path(tmp_dir) / "reboot-required"
            packages_path = Path(tmp_dir) / "reboot-required.pkgs"
            required_path.write_text("*** System restart required ***\n", encoding="utf-8")
            packages_path.write_text("linux-image-6.8.0-124-generic\nlinux-base\n", encoding="utf-8")
            ctx = self.make_context({"paymentTotalAmount": "500", "paymentCurrency": "₽"})

            with (
                mock.patch.object(notifications, "disk_usage_label", return_value="ok"),
                mock.patch.object(notifications, "REBOOT_REQUIRED_PATH", required_path),
                mock.patch.object(notifications, "REBOOT_REQUIRED_PACKAGES_PATH", packages_path),
            ):
                text = notifications.build_daily_summary_message(ctx, date(2026, 6, 12))

        self.assertIn("Система: требуется перезагрузка после обновления ядра", text)
        self.assertIn("linux-image-6.8.0-124-generic", text)
        self.assertIn("linux-base", text)


if __name__ == "__main__":
    unittest.main()
