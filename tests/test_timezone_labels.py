"""B1.1 / VIREI-107: явное изменение характеризационных ожиданий T1.

Семь адаптеров используют общий fallback на локальную зону с предупреждением.
Валидные настройки и прежние подписи пустой зоны сохранены. Sandbox задаёт
настоящую ZoneInfo, чтобы результаты не зависели от зоны машины.
"""

from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import os
import sys
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from xray_vps_manager.activity import client_reports
from xray_vps_manager.activity import raw_logs
from xray_vps_manager.clients import settings as client_settings
from xray_vps_manager.commands import client as client_command
from xray_vps_manager.commands import menu_status
from xray_vps_manager.commands import telegram as telegram_command
from xray_vps_manager.commands import test as test_command
from xray_vps_manager.commands import traffic_sync
from xray_vps_manager.core import time as core_time
from xray_vps_manager.core.server_env import read_server_env
from xray_vps_manager.telegram import traffic as telegram_traffic
from tests.support.sandbox import Sandbox

VALID = "Europe/Moscow"
INVALID = "Mars/Base"
SERVER_TZ = "Asia/Tokyo"
SERVER_OFFSET = timedelta(hours=9)
WARNING = "MANAGER_TIMEZONE invalid: Mars/Base (server local time)"
STRICT_MESSAGE = "MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time."


class TimezoneLabelCase(unittest.TestCase):
    """Временный server.env и локальная зона сервера; MANAGER_TIMEZONE из окружения процесса убран."""

    server_tz = SERVER_TZ

    def setUp(self) -> None:
        self.box = Sandbox(tz=self.server_tz)
        self.box.__enter__()
        self.addCleanup(self.box.__exit__, None, None, None)
        self.env_path = self.box.paths.server_env
        self.write_env("")
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("MANAGER_TIMEZONE", None)

    def write_env(self, zone: str, extra: str = "") -> None:
        self.env_path.write_text(f"MANAGER_TIMEZONE={zone}\n{extra}")

    def read_env(self, path=None, **kwargs):
        return read_server_env(self.env_path)


class MenuHeaderTimezoneTests(TimezoneLabelCase):
    """B1.1: шапка показывает неверную зону и продолжает работу."""

    def test_valid_zone_label_is_zone_name(self) -> None:
        self.write_env(VALID)

        self.assertEqual(menu_status.manager_timezone_label(), VALID)

    def test_empty_zone_uses_server_local_abbreviation(self) -> None:
        self.write_env("", "SECURITY_AUDIT_LAST_RUN=2026-10-01T09:00:00Z\n")

        self.assertEqual(menu_status.manager_updated_header_value("2026-06-13 11:58 UTC"), "2026-06-13 20:58 JST")
        self.assertEqual(menu_status.security_audit_header_value(), "2026-10-01 18:00 JST")
        self.assertEqual(menu_status.manager_timezone_label(), "server local time (JST)")

    def test_invalid_zone_preserves_every_header_value(self) -> None:
        """B1.1 / XR-REV-033: неверная зона больше не закрывает шапку меню."""
        self.write_env(INVALID, "SECURITY_AUDIT_LAST_RUN=2026-10-01T09:00:00Z\n")
        values = {
            "Manager Updated": lambda: menu_status.manager_updated_header_value("2026-06-13 11:58 UTC"),
            "Security Audit": menu_status.security_audit_header_value,
            "label": menu_status.manager_timezone_label,
        }

        self.assertEqual(values["Manager Updated"](), "2026-06-13 20:58 JST")
        self.assertEqual(values["Security Audit"](), "2026-10-01 18:00 JST")
        self.assertEqual(values["label"](), WARNING)

    def test_trailing_comment_makes_zone_invalid(self) -> None:
        """B1.1 исправляет XR-REV-033; парсер XR-REV-069 остаётся до B1.2."""
        self.write_env(f"{VALID} # основная зона")
        self.assertEqual(menu_status.manager_updated_header_value("2026-06-13 11:58 UTC"), "2026-06-13 20:58 JST")
        self.assertEqual(menu_status.manager_timezone_label(), f"MANAGER_TIMEZONE invalid: {VALID} # основная зона (server local time)")


class TelegramEventTimeTests(TimezoneLabelCase):
    """`xray-telegram`: `format_event_time` и подпись отчётов трафика бота (`display_timezone`)."""

    def test_event_time_label_for_valid_empty_and_invalid_zone(self) -> None:
        cases = {
            VALID: "2026-06-12 11:00:00 Europe/Moscow",
            "": "2026-06-12 17:00:00 server local time",
            INVALID: "2026-06-12 17:00:00 MANAGER_TIMEZONE invalid: Mars/Base (server local time)",
        }

        for zone, expected in cases.items():
            with self.subTest(zone=zone):
                self.write_env(zone)
                self.assertEqual(telegram_command.format_event_time("2026-06-12T08:00:00Z"), expected)

    def test_invalid_zone_falls_back_to_local(self) -> None:
        """B1.1 / XR-REV-038: вместо UTC используется локальная зона с общей пометкой."""
        self.write_env(INVALID)

        self.assertEqual(
            telegram_command.display_timezone(),
            (ZoneInfo(SERVER_TZ), "MANAGER_TIMEZONE invalid: Mars/Base (server local time)"),
        )

    def test_traffic_report_day_follows_display_timezone(self) -> None:
        """B1.1 / XR-REV-038: сутки отчёта при неверной зоне следуют локальному календарю."""
        now = datetime(2026, 6, 12, 20, 0, tzinfo=timezone.utc)
        cases = {
            VALID: (date(2026, 6, 12), "Europe/Moscow"),
            "": (date(2026, 6, 13), "server local time"),
            INVALID: (date(2026, 6, 13), "MANAGER_TIMEZONE invalid: Mars/Base (server local time)"),
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

    def test_display_timezone_for_valid_empty_and_invalid_zone(self) -> None:
        self.write_env(VALID)
        self.assertEqual(client_reports.activity_display_timezone(), (ZoneInfo(VALID), VALID))

        self.write_env("")
        tzinfo, label = client_reports.activity_display_timezone()
        self.assertEqual(label, "server local time (JST)")
        self.assertEqual(datetime(2026, 6, 12, tzinfo=tzinfo).utcoffset(), SERVER_OFFSET)

    def test_invalid_zone_falls_back_to_local(self) -> None:
        """B1.1 / XR-REV-038: activity использует локальную зону вместо UTC."""
        self.write_env(INVALID)

        self.assertEqual(
            client_reports.activity_display_timezone(),
            (ZoneInfo(SERVER_TZ), "MANAGER_TIMEZONE invalid: Mars/Base (server local time)"),
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
            INVALID: ("2026-06-12 17:05:00", "MANAGER_TIMEZONE invalid: Mars/Base (server local time)"),
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
    """B1.1: bucket `xray-traffic-sync` и журнал используют общую политику."""

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
        self.assertEqual(datetime(2026, 6, 12, tzinfo=tzinfo).utcoffset(), SERVER_OFFSET)
        self.assertEqual(bucket.utcoffset(), SERVER_OFFSET)
        self.assertEqual(output, "")

    def test_invalid_zone_falls_back_to_server_local_time_with_log_line(self) -> None:
        """B1.1 / XR-REV-038: bucket и отчёты согласованы; журнал содержит общую пометку."""
        self.write_env(INVALID)

        tzinfo, bucket, output = self.bucket_timezone(["xray-traffic-sync"])
        self.assertEqual(datetime(2026, 6, 12, tzinfo=tzinfo).utcoffset(), SERVER_OFFSET)
        self.assertEqual(bucket.utcoffset(), SERVER_OFFSET)
        self.assertEqual(output, (WARNING + "\n") * 2)

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
        self.assertEqual(diag.failures, ["Manager timezone: MANAGER_TIMEZONE invalid: Mars/Base (server local time)"])
        self.assertEqual(output.getvalue(), "FAIL: Manager timezone: MANAGER_TIMEZONE invalid: Mars/Base (server local time)\n")

    def test_raw_local_alias_still_fails_diagnostics_without_changing_timer(self) -> None:
        """107-R03: псевдоним допустим в CLI, но сырое значение опасно для OnCalendar."""
        for alias in ("local", "server", "system", "default", "сервер", "локально", "по умолчанию"):
            with self.subTest(alias=alias):
                diag = self.diagnostics(alias)
                before = self.env_path.read_bytes()
                output = StringIO()
                with redirect_stdout(output):
                    diag.check("Manager timezone", lambda: test_command.check_manager_timezone(diag))
                message = (f"MANAGER_TIMEZONE alias {alias!r} is written into timer OnCalendar; "
                           "use xray-client set-timezone server")
                self.assertEqual(diag.failures, ["Manager timezone: " + message])
                self.assertEqual(output.getvalue(), "FAIL: Manager timezone: " + message + "\n")
                self.assertEqual(test_command.manager_timezone(diag), ZoneInfo(SERVER_TZ))
                self.assertEqual(raw_logs.raw_log_on_calendar(), f"*-*-* 03:00:00 {alias}")
                self.assertEqual(self.env_path.read_bytes(), before)

    def test_traffic_checks_timezone_uses_local_for_invalid_zone(self) -> None:
        """B1.1 / XR-REV-038: диагностика трафика использует локальную зону без исключения."""
        self.assertEqual(test_command.manager_timezone(self.diagnostics(VALID)), ZoneInfo(VALID))
        self.assertEqual(datetime(2026, 6, 12, tzinfo=test_command.manager_timezone(self.diagnostics(""))).utcoffset(), SERVER_OFFSET)

        self.assertEqual(test_command.manager_timezone(self.diagnostics(INVALID)), ZoneInfo(SERVER_TZ))


class ClientTimezoneTests(TimezoneLabelCase):
    """B1.1: чтение клиентской зоны использует общий fallback."""

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
        self.assertEqual(datetime(2026, 6, 12, tzinfo=client_settings.manager_timezone()).utcoffset(), SERVER_OFFSET)
        self.assertEqual(client_settings.manager_timezone_label(), "server local time (JST)")

    def test_invalid_zone_shows_warning_without_exception(self) -> None:
        """B1.1 / XR-REV-038: клиентский read-путь показывает пометку без ValueError."""
        self.write_env(INVALID)

        self.assertEqual(client_settings.manager_timezone(), ZoneInfo(SERVER_TZ))
        self.assertEqual(client_settings.manager_timezone_label(), WARNING)
        self.assertEqual(self.run_timezone_command()[0], f"MANAGER_TIMEZONE: {WARNING}")

    def test_existing_local_aliases_remain_valid_for_client_and_menu(self) -> None:
        """B1.1 сохраняет локальные псевдонимы прежних строгих адаптеров."""
        for name in ("server", "local", "default", "system", "сервер", "локально", "по умолчанию"):
            with self.subTest(name=name):
                self.write_env(name)
                self.assertEqual(client_settings.configured_timezone_name(), "")
                self.assertEqual(menu_status.configured_timezone_name(), "")
                self.assertEqual(client_settings.manager_timezone_label(), "server local time (JST)")
                self.assertEqual(menu_status.manager_timezone_label(), "server local time (JST)")

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
        self.assertEqual(datetime(2026, 6, 12, tzinfo=tzinfo).utcoffset(), SERVER_OFFSET)

    def test_invalid_zone_labels_server_local_fallback(self) -> None:
        """B1.1 / XR-REV-038: совместимый tuple содержит предупреждение о неверной зоне."""
        self.write_env(INVALID)

        tzinfo, label = core_time.manager_timezone(self.env_path)

        self.assertEqual(label, WARNING)
        self.assertEqual(datetime(2026, 6, 12, tzinfo=tzinfo).utcoffset(), SERVER_OFFSET)

    def test_raw_log_next_rotation_label_and_on_calendar(self) -> None:
        """B1.1 / XR-REV-038: подпись ротации предупреждает; прежний OnCalendar сохранён."""
        now = datetime(2026, 6, 12, 4, 0, tzinfo=timezone.utc)
        cases = {
            VALID: ("2026-06-13 03:00:00 Europe/Moscow", "*-*-* 03:00:00 Europe/Moscow"),
            "": ("2026-06-13 03:00:00 server local time", "*-*-* 03:00:00"),
            INVALID: (f"2026-06-13 03:00:00 {WARNING}", "*-*-* 03:00:00 Mars/Base"),
        }

        for zone, (label, on_calendar) in cases.items():
            with self.subTest(zone=zone):
                self.write_env(zone)
                self.assertEqual(raw_logs.next_rotation_label(now), label)
                self.assertEqual(raw_logs.raw_log_on_calendar(), on_calendar)


class FixedOffsetServerTimeTests(TimezoneLabelCase):
    """Пустая зона на сервере с летним временем (Europe/Berlin)."""

    server_tz = "Europe/Berlin"

    def test_empty_zone_keeps_dst_transitions(self) -> None:
        """B1.1 / XR-REV-065: локальная зона сохраняет зимние и летние смещения."""
        self.write_env("")
        winter = datetime(2026, 1, 15, 12, 0)
        summer = datetime(2026, 7, 15, 12, 0)

        zones = {
            "core/time": core_time.manager_timezone(self.env_path)[0],
            "clients/settings": client_settings.manager_timezone(),
        }

        for name, tzinfo in zones.items():
            with self.subTest(name):
                self.assertIsInstance(tzinfo, ZoneInfo)
                self.assertEqual(tzinfo.utcoffset(winter), timedelta(hours=1))
                self.assertEqual(tzinfo.utcoffset(summer), timedelta(hours=2))
                self.assertEqual(tzinfo.tzname(winter), "CET")
                self.assertEqual(tzinfo.tzname(summer), "CEST")



if __name__ == "__main__":
    unittest.main()
