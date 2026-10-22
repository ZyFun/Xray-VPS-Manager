"""VIREI-84 (T1): подписи и зоны MANAGER_TIMEZONE в семи местах до шага 1 (единый `core/clock.py`).

Семь модулей с `ZoneInfo(` (перепроверка XR-REV-038): шапка меню (`commands/menu_status`), Telegram
(`commands/telegram.format_event_time`), отчёты активности (`activity/client_reports`), bucket `xray-traffic-sync`,
`xray-test`, `xray-client timezone` (`clients/settings`) и `core/time.manager_timezone` (raw-log ротация, activity sync,
меню трафика и клиентов). Каждое место проверяется при валидной, пустой и невалидной зоне; локальная зона сервера
задаётся через `TZ` (Asia/Tokyo — без перехода на летнее время), чтобы подписи не зависели от машины.
"""

from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.activity import client_reports
from xray_vps_manager.activity import raw_logs
from xray_vps_manager.activity import settings as activity_settings
from xray_vps_manager.clients import settings as client_settings
from xray_vps_manager.commands import client as client_command
from xray_vps_manager.commands import menu_status
from xray_vps_manager.commands import telegram as telegram_command
from xray_vps_manager.commands import test as test_command
from xray_vps_manager.commands import traffic_sync
from xray_vps_manager.core import time as core_time
from xray_vps_manager.core.server_env import read_server_env
from xray_vps_manager.telegram import traffic as telegram_traffic

VALID = "Europe/Moscow"
INVALID = "Mars/Base"
SERVER_TZ = "Asia/Tokyo"
SERVER_OFFSET = timedelta(hours=9)
STRICT_MESSAGE = "MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time."


class TimezoneLabelCase(unittest.TestCase):
    """Временный server.env и локальная зона сервера; MANAGER_TIMEZONE из окружения процесса убран."""

    server_tz = SERVER_TZ

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env_path = Path(tmp.name) / "server.env"
        self.write_env("")
        environ = mock.patch.dict(os.environ, {"TZ": self.server_tz})
        self.addCleanup(time.tzset)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("MANAGER_TIMEZONE", None)
        time.tzset()

    def write_env(self, zone: str, extra: str = "") -> None:
        self.env_path.write_text(f"MANAGER_TIMEZONE={zone}\n{extra}")

    def read_env(self, path=None, **kwargs):
        return read_server_env(self.env_path)


class MenuHeaderTimezoneTests(TimezoneLabelCase):
    """Шапка `xray-menu`: `menu_status.normalize_timezone` строгий, ошибка — `die()` = SystemExit."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(menu_status, "SERVER_ENV_PATH", self.env_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_zone_label_is_zone_name(self) -> None:
        self.write_env(VALID)

        self.assertEqual(menu_status.manager_timezone_label(), VALID)

    def test_empty_zone_uses_server_local_abbreviation(self) -> None:
        self.write_env("", "SECURITY_AUDIT_LAST_RUN=2026-10-01T09:00:00Z\n")

        self.assertEqual(menu_status.manager_updated_header_value("2026-06-13 11:58 UTC"), "2026-06-13 20:58 JST")
        self.assertEqual(menu_status.security_audit_header_value(), "2026-10-01 18:00 JST")
        self.assertEqual(menu_status.manager_timezone_label(), "server local time (JST)")

    def test_invalid_zone_exits_from_every_header_value(self) -> None:
        """Текущее поведение, баг XR-REV-033: невалидная зона закрывает меню при отрисовке шапки."""
        self.write_env(INVALID, "SECURITY_AUDIT_LAST_RUN=2026-10-01T09:00:00Z\n")
        values = {
            "Manager Updated": lambda: menu_status.manager_updated_header_value("2026-06-13 11:58 UTC"),
            "Security Audit": menu_status.security_audit_header_value,
            "label": menu_status.manager_timezone_label,
        }

        for name, value in values.items():
            with self.subTest(name):
                with self.assertRaises(SystemExit) as caught:
                    value()
                self.assertEqual(caught.exception.code, STRICT_MESSAGE)

    def test_trailing_comment_makes_zone_invalid(self) -> None:
        """Текущее поведение, баги XR-REV-069 и XR-REV-033: хвостовой комментарий в server.env закрывает меню."""
        self.write_env(f"{VALID} # основная зона")

        with self.assertRaises(SystemExit) as caught:
            menu_status.manager_updated_header_value("2026-06-13 11:58 UTC")

        self.assertEqual(caught.exception.code, STRICT_MESSAGE)


class TelegramEventTimeTests(TimezoneLabelCase):
    """`xray-telegram`: `format_event_time` и подпись отчётов трафика бота (`display_timezone`)."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(telegram_command, "SERVER_ENV_PATH", self.env_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_event_time_label_for_valid_empty_and_invalid_zone(self) -> None:
        cases = {
            VALID: "2026-06-12 11:00:00 Europe/Moscow",
            "": "2026-06-12 17:00:00 server local time",
            INVALID: "2026-06-12 08:00:00 UTC (invalid MANAGER_TIMEZONE: Mars/Base)",
        }

        for zone, expected in cases.items():
            with self.subTest(zone=zone):
                self.write_env(zone)
                self.assertEqual(telegram_command.format_event_time("2026-06-12T08:00:00Z"), expected)

    def test_invalid_zone_falls_back_to_utc(self) -> None:
        """Текущее поведение, баг XR-REV-038: невалидная зона — UTC с пометкой, а не локальное время сервера."""
        self.write_env(INVALID)

        self.assertEqual(
            telegram_command.display_timezone(),
            (timezone.utc, "UTC (invalid MANAGER_TIMEZONE: Mars/Base)"),
        )

    def test_traffic_report_day_follows_display_timezone(self) -> None:
        """Текущее поведение, баг XR-REV-038: при невалидной зоне сутки отчёта бота — по UTC, при пустой — по зоне сервера."""
        now = datetime(2026, 6, 12, 20, 0, tzinfo=timezone.utc)
        cases = {
            VALID: (date(2026, 6, 12), "Europe/Moscow"),
            "": (date(2026, 6, 13), "server local time"),
            INVALID: (date(2026, 6, 12), "UTC (invalid MANAGER_TIMEZONE: Mars/Base)"),
        }

        for zone, expected in cases.items():
            with self.subTest(zone=zone):
                self.write_env(zone)
                today, label = telegram_traffic.local_today(telegram_command.display_timezone, now=now)
                self.assertEqual((today, label), expected)
                self.assertEqual(
                    telegram_traffic.report_title("day", today, label),
                    f"Статистика трафика за сутки\nДата: {expected[0].isoformat()} {expected[1]}",
                )


class ActivityReportTimezoneTests(TimezoneLabelCase):
    """Отчёты активности: `activity_display_timezone` и `timezoneLabel` в `geoip_risk_details`."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(activity_settings, "SERVER_ENV_PATH", self.env_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_display_timezone_for_valid_empty_and_invalid_zone(self) -> None:
        self.write_env(VALID)
        self.assertEqual(client_reports.activity_display_timezone(), (ZoneInfo(VALID), VALID))

        self.write_env("")
        tzinfo, label = client_reports.activity_display_timezone()
        self.assertEqual(label, "server local time (JST)")
        self.assertEqual(tzinfo.utcoffset(None), SERVER_OFFSET)

    def test_invalid_zone_falls_back_to_utc(self) -> None:
        """Текущее поведение, баг XR-REV-038: невалидная зона — UTC с пометкой, а не локальное время сервера."""
        self.write_env(INVALID)

        self.assertEqual(
            client_reports.activity_display_timezone(),
            (timezone.utc, "UTC (invalid MANAGER_TIMEZONE: Mars/Base)"),
        )

    def test_geoip_risk_details_rows_and_label_follow_display_timezone(self) -> None:
        alert = {
            "time": "2026-06-12T08:00:00Z",
            "last_seen_at": "2026-06-12T08:05:00Z",
            "client": "alice",
            "host": "example.ru",
            "port": "443",
            "outbound": "geoip-warning-RU",
            "risk": "xray-geoip:RU",
            "risks": ["xray-geoip:RU"],
        }
        cases = {
            VALID: ("2026-06-12 11:05:00", "Europe/Moscow"),
            "": ("2026-06-12 17:05:00", "server local time (JST)"),
            INVALID: ("2026-06-12 08:05:00", "UTC (invalid MANAGER_TIMEZONE: Mars/Base)"),
        }

        for zone, (row_time, label) in cases.items():
            with self.subTest(zone=zone):
                self.write_env(zone)
                with mock.patch.object(
                    client_reports.activity_time,
                    "date_range_from_days",
                    return_value=(date(2026, 6, 12), date(2026, 6, 12)),
                ), mock.patch.object(
                    client_reports.activity_exceptions, "exception_items_for_read", return_value=[]
                ), mock.patch.object(
                    client_reports.activity_repository, "alert_events_for_read", return_value=[alert]
                ):
                    report = client_reports.geoip_risk_details("1")

                self.assertEqual(report["timezoneLabel"], label)
                self.assertEqual(report["clients"][0]["rows"][0][0], row_time)


class TrafficSyncBucketTimezoneTests(TimezoneLabelCase):
    """Bucket `xray-traffic-sync`: своя копия `manager_timezone` с записью в лог при невалидной зоне."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(traffic_sync, "SERVER_ENV_PATH", self.env_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def bucket_timezone(self, argv):
        output = StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(output):
            tzinfo = traffic_sync.manager_timezone()
            bucket = traffic_sync.local_bucket_time()
        return tzinfo, bucket, output.getvalue()

    def test_valid_and_empty_zone_choose_bucket_timezone_silently(self) -> None:
        self.write_env(VALID)
        tzinfo, bucket, output = self.bucket_timezone(["xray-traffic-sync"])
        self.assertEqual(tzinfo, ZoneInfo(VALID))
        self.assertEqual(bucket.tzinfo, ZoneInfo(VALID))
        self.assertEqual(output, "")

        self.write_env("")
        tzinfo, bucket, output = self.bucket_timezone(["xray-traffic-sync"])
        self.assertEqual(tzinfo.utcoffset(None), SERVER_OFFSET)
        self.assertEqual(bucket.utcoffset(), SERVER_OFFSET)
        self.assertEqual(output, "")

    def test_invalid_zone_falls_back_to_server_local_time_with_log_line(self) -> None:
        """Текущее поведение, баг XR-REV-038: bucket — по локальному времени сервера, Telegram и отчёты — по UTC."""
        self.write_env(INVALID)

        tzinfo, bucket, output = self.bucket_timezone(["xray-traffic-sync"])
        self.assertEqual(tzinfo.utcoffset(None), SERVER_OFFSET)
        self.assertEqual(bucket.utcoffset(), SERVER_OFFSET)
        self.assertEqual(output, "Invalid MANAGER_TIMEZONE='Mars/Base'. Falling back to server local time.\n" * 2)

        _tzinfo, _bucket, quiet_output = self.bucket_timezone(["xray-traffic-sync", "--quiet"])
        self.assertEqual(quiet_output, "")


class XrayTestTimezoneTests(TimezoneLabelCase):
    """`xray-test`: проверка «Manager timezone» и зона проверок трафика по `server_env` из контекста."""

    def diagnostics(self, zone: str):
        self.write_env(zone)
        diag = test_command.Diagnostics()
        diag.context["server_env"] = read_server_env(self.env_path)
        return diag

    def test_manager_timezone_check_for_valid_empty_and_invalid_zone(self) -> None:
        self.assertEqual(test_command.check_manager_timezone(self.diagnostics(VALID)), "MANAGER_TIMEZONE=Europe/Moscow")
        self.assertEqual(
            test_command.check_manager_timezone(self.diagnostics("")),
            "MANAGER_TIMEZONE uses server local time",
        )

        diag = self.diagnostics(INVALID)
        output = StringIO()
        with redirect_stdout(output):
            diag.check("Manager timezone", lambda: test_command.check_manager_timezone(diag))
        self.assertEqual(diag.failures, ["Manager timezone: invalid MANAGER_TIMEZONE: Mars/Base"])
        self.assertEqual(output.getvalue(), "FAIL: Manager timezone: invalid MANAGER_TIMEZONE: Mars/Base\n")

    def test_traffic_checks_timezone_raises_for_invalid_zone(self) -> None:
        """Текущее поведение, баг XR-REV-038: зона проверок трафика `xray-test` не имеет fallback и падает исключением."""
        self.assertEqual(test_command.manager_timezone(self.diagnostics(VALID)), ZoneInfo(VALID))
        self.assertEqual(test_command.manager_timezone(self.diagnostics("")).utcoffset(None), SERVER_OFFSET)

        with self.assertRaises(ZoneInfoNotFoundError):
            test_command.manager_timezone(self.diagnostics(INVALID))


class ClientTimezoneTests(TimezoneLabelCase):
    """`xray-client timezone` и `clients/settings`: строгая зона, ошибка — ValueError."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(client_settings, "read_server_env", self.read_env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_timezone_command(self) -> list:
        output = StringIO()
        with mock.patch.object(sys, "argv", ["xray-client", "timezone"]), mock.patch("os.geteuid", return_value=0), \
                redirect_stdout(output):
            client_command.main()
        return output.getvalue().splitlines()

    def test_timezone_command_for_valid_and_empty_zone(self) -> None:
        cases = {VALID: ("MANAGER_TIMEZONE: Europe/Moscow", "MSK"), "": ("MANAGER_TIMEZONE: server local time", "JST")}

        for zone, (first_line, abbreviation) in cases.items():
            with self.subTest(zone=zone):
                self.write_env(zone)
                lines = self.run_timezone_command()
                self.assertEqual(len(lines), 2)
                self.assertEqual(lines[0], first_line)
                self.assertRegex(lines[1], rf"^Current time: \d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}} {abbreviation}$")

    def test_settings_label_for_valid_and_empty_zone(self) -> None:
        self.write_env(VALID)
        self.assertEqual(client_settings.manager_timezone(), ZoneInfo(VALID))
        self.assertEqual(client_settings.manager_timezone_label(), VALID)

        self.write_env("")
        self.assertEqual(client_settings.manager_timezone().utcoffset(None), SERVER_OFFSET)
        self.assertEqual(client_settings.manager_timezone_label(), "server local time (JST)")

    def test_invalid_zone_raises_value_error_without_die(self) -> None:
        """Текущее поведение, баг XR-REV-038: `xray-client timezone` падает необработанным ValueError (traceback)."""
        self.write_env(INVALID)

        for name, call in {
            "manager_timezone": client_settings.manager_timezone,
            "manager_timezone_label": client_settings.manager_timezone_label,
            "xray-client timezone": self.run_timezone_command,
        }.items():
            with self.subTest(name):
                with self.assertRaises(ValueError) as caught:
                    call()
                self.assertEqual(str(caught.exception), STRICT_MESSAGE)

    def test_empty_file_value_falls_back_to_process_environment(self) -> None:
        """Пустой MANAGER_TIMEZONE в server.env: `clients/settings` берёт зону из окружения процесса, `core/time` — нет."""
        self.write_env("")

        with mock.patch.dict(os.environ, {"MANAGER_TIMEZONE": VALID}):
            self.assertEqual(client_settings.manager_timezone_label(), VALID)
            self.assertEqual(core_time.manager_timezone(self.env_path)[1], "server local time")


class CoreTimeTimezoneTests(TimezoneLabelCase):
    """`core/time.manager_timezone` и его подписи в raw-log ротации."""

    def test_manager_timezone_for_valid_and_empty_zone(self) -> None:
        self.write_env(VALID)
        self.assertEqual(core_time.manager_timezone(self.env_path), (ZoneInfo(VALID), VALID))

        self.write_env("")
        tzinfo, label = core_time.manager_timezone(self.env_path)
        self.assertEqual(label, "server local time")
        self.assertEqual(tzinfo.utcoffset(None), SERVER_OFFSET)

    def test_invalid_zone_silently_falls_back_to_server_local_time(self) -> None:
        """Текущее поведение, баг XR-REV-038: невалидная зона молча заменяется локальным временем, без пометки."""
        self.write_env(INVALID)

        tzinfo, label = core_time.manager_timezone(self.env_path)

        self.assertEqual(label, "server local time")
        self.assertEqual(tzinfo.utcoffset(None), SERVER_OFFSET)

    def test_raw_log_next_rotation_label_and_on_calendar(self) -> None:
        """Текущее поведение, баг XR-REV-038: при невалидной зоне подпись — «server local time», а OnCalendar — с этой зоной."""
        now = datetime(2026, 6, 12, 4, 0, tzinfo=timezone.utc)
        cases = {
            VALID: ("2026-06-13 03:00:00 Europe/Moscow", "*-*-* 03:00:00 Europe/Moscow"),
            "": ("2026-06-13 03:00:00 server local time", "*-*-* 03:00:00"),
            INVALID: ("2026-06-13 03:00:00 server local time", "*-*-* 03:00:00 Mars/Base"),
        }

        for zone, (label, on_calendar) in cases.items():
            with self.subTest(zone=zone):
                self.write_env(zone)
                with mock.patch.object(core_time, "read_server_env", self.read_env), \
                        mock.patch.object(activity_settings, "SERVER_ENV_PATH", self.env_path):
                    self.assertEqual(raw_logs.next_rotation_label(now), label)
                    self.assertEqual(raw_logs.raw_log_on_calendar(), on_calendar)


class FixedOffsetServerTimeTests(TimezoneLabelCase):
    """Пустая зона на сервере с летним временем (Europe/Berlin)."""

    server_tz = "Europe/Berlin"

    def test_empty_zone_is_fixed_offset_snapshot(self) -> None:
        """Текущее поведение, баг XR-REV-065: «server local time» — смещение момента запуска, без перехода на летнее время."""
        self.write_env("")
        winter = datetime(2026, 1, 15, 12, 0)
        summer = datetime(2026, 7, 15, 12, 0)

        with mock.patch.object(client_settings, "read_server_env", self.read_env):
            zones = {
                "core/time": core_time.manager_timezone(self.env_path)[0],
                "clients/settings": client_settings.manager_timezone(),
            }

        for name, tzinfo in zones.items():
            with self.subTest(name):
                self.assertIsInstance(tzinfo, timezone)
                self.assertEqual(tzinfo.utcoffset(winter), tzinfo.utcoffset(summer))
                self.assertIn(tzinfo.utcoffset(winter), (timedelta(hours=1), timedelta(hours=2)))
                self.assertRegex(tzinfo.tzname(None), r"^CES?T$")


if __name__ == "__main__":
    unittest.main()
