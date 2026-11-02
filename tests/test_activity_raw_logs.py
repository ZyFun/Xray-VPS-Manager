import unittest
import tempfile
import gzip
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from tests.support.sandbox import Sandbox, ProcessResult

from xray_vps_manager.system import units as system_units, unit_sync
from xray_vps_manager.activity import backfill
from xray_vps_manager.activity import raw_logs
from xray_vps_manager.activity import settings as activity_settings
from xray_vps_manager.activity import sync as activity_sync
from xray_vps_manager.commands import activity as activity_command


class ActivityRawLogsTests(unittest.TestCase):
    def test_raw_log_timer_unit_uses_on_calendar_with_manager_timezone(self) -> None:
        env = {"XRAY_RAW_LOG_ROTATE_TIME": "03:00", "MANAGER_TIMEZONE": "Europe/Moscow"}

        unit = unit_sync.raw_log_timer_unit(env)

        self.assertIn("OnCalendar=*-*-* 03:00:00 Europe/Moscow", unit)
        self.assertIn("Persistent=true", unit)
        self.assertNotIn("OnUnitActiveSec=5min", unit)

    def test_raw_log_timer_unit_uses_server_local_time_when_timezone_is_empty(self) -> None:
        env = {"XRAY_RAW_LOG_ROTATE_TIME": "04:30", "MANAGER_TIMEZONE": ""}

        self.assertIn("OnCalendar=*-*-* 04:30:00", unit_sync.raw_log_timer_unit(env))
        self.assertEqual(raw_logs.raw_log_on_calendar(env), "*-*-* 04:30:00")

    def test_next_rotation_label_uses_next_calendar_day_after_rotate_time(self) -> None:
        with (
            mock.patch.object(raw_logs.settings, "raw_log_rotate_time", return_value="03:00"),
            mock.patch.object(raw_logs, "manager_timezone", return_value=(timezone.utc, "UTC")),
        ):
            label = raw_logs.next_rotation_label(datetime(2026, 6, 12, 4, 0, tzinfo=timezone.utc))

        self.assertEqual(label, "2026-06-13 03:00:00 UTC")

    def test_sync_raw_log_timer_writes_generated_unit_without_systemctl(self) -> None:
        env = {"XRAY_RAW_LOG_ROTATE_TIME": "03:00", "MANAGER_TIMEZONE": "Europe/Moscow"}
        with tempfile.TemporaryDirectory() as tmp_dir:
            service_path = Path(tmp_dir) / "xray-raw-log-rotate.service"
            timer_path = Path(tmp_dir) / "xray-raw-log-rotate.timer"

            result = unit_sync.sync_raw_log_timer(
                run_systemctl=False,
                service_path=service_path,
                timer_path=timer_path,
                env=env,
            )

            service = service_path.read_text()
            unit = timer_path.read_text()
        self.assertEqual(result["path"], str(timer_path))
        self.assertEqual(result["servicePath"], str(service_path))
        self.assertEqual(result["timerPath"], str(timer_path))
        self.assertEqual(result["onCalendar"], "*-*-* 03:00:00 Europe/Moscow")
        self.assertEqual(result["systemctl"], "no")
        self.assertIn("ExecStart=/usr/local/sbin/xray-activity rotate-raw-logs --due", service)
        self.assertIn("OnCalendar=*-*-* 03:00:00 Europe/Moscow", unit)
        self.assertIn("Unit=xray-raw-log-rotate.service", unit)

    def test_rotate_raw_logs_rotates_with_warning_and_fails_when_pre_sync_fails(self) -> None:
        messages: list[str] = []
        restarted = subprocess.CompletedProcess(["systemctl", "try-restart", "xray.service"], 0, "", "")

        with (
            mock.patch.object(raw_logs, "_load_state", return_value={}),
            mock.patch.object(raw_logs, "rotation_due", return_value=(True, "2026-06-12", "UTC")),
            mock.patch.object(raw_logs, "drain_logs_before_rotation", return_value=1),
            mock.patch.object(
                raw_logs,
                "rotate_file",
                side_effect=lambda path, stamp: path.with_name(f"{path.name}.{stamp}.gz"),
            ) as rotate_file,
            mock.patch.object(raw_logs, "prune_rotated_logs", return_value=0),
            mock.patch.object(raw_logs, "restart_xray", return_value=restarted) as restart_xray,
            mock.patch.object(raw_logs, "_save_state") as save_state,
        ):
            result = raw_logs.rotate_raw_logs(only_if_due=True, log=messages.append)

        self.assertEqual(result, 1)
        self.assertEqual(
            [call.args[0] for call in rotate_file.call_args_list],
            [raw_logs.ACCESS_LOG_PATH, raw_logs.ERROR_LOG_PATH],
        )
        restart_xray.assert_called_once_with()
        self.assertEqual(save_state.call_args.args[0]["lastRunDate"], "2026-06-12")
        self.assertTrue(any(message.startswith("WARN:") and "rotating anyway" in message for message in messages))
        self.assertTrue(messages[-1].startswith("ERROR:"))
        self.assertIn("pre-sync failed", messages[-1])
        self.assertFalse(any("aborted before renaming" in message for message in messages))

    def test_drain_logs_before_rotation_records_one_pre_sync_error_with_rotation_note(self) -> None:
        cases = (
            ("non-zero result", {"return_value": 1}, "activity sync=1, error sync=0; rotation continues"),
            ("exception", {"side_effect": RuntimeError("database is locked")}, "database is locked; rotation continues"),
        )
        for name, sync_behavior, raw_line in cases:
            with self.subTest(name):
                messages: list[str] = []

                with (
                    mock.patch.object(activity_sync, "sync_activity", **sync_behavior),
                    mock.patch.object(raw_logs, "sync_error_log", return_value=0),
                    mock.patch.object(raw_logs, "record_rotation_error") as record_rotation_error,
                ):
                    result = raw_logs.drain_logs_before_rotation(messages.append)

                self.assertEqual(result, 1)
                record_rotation_error.assert_called_once_with("raw log rotation pre-sync failed", raw_line=raw_line)
                self.assertTrue(any(message.startswith("ERROR: raw log rotation pre-sync failed") for message in messages))

    def test_raw_log_archive_rows_lists_access_and_error_archives(self) -> None:
        old_access = raw_logs.ACCESS_LOG_PATH
        old_error = raw_logs.ERROR_LOG_PATH
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            raw_logs.ACCESS_LOG_PATH = tmp_path / "access.log"
            raw_logs.ERROR_LOG_PATH = tmp_path / "error.log"
            (tmp_path / "access.log.20260612-030000.gz").write_text("access")
            (tmp_path / "error.log.20260612-030000.gz").write_text("error")
            try:
                rows = raw_logs.raw_log_archive_rows()
            finally:
                raw_logs.ACCESS_LOG_PATH = old_access
                raw_logs.ERROR_LOG_PATH = old_error

        files = {row["file"]: row for row in rows}
        self.assertEqual(files["access.log.20260612-030000.gz"]["type"], "access")
        self.assertEqual(files["error.log.20260612-030000.gz"]["type"], "error")

    def test_raw_log_timestamp_range_reads_current_and_compressed_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            access_log = tmp_path / "access.log"
            access_log.write_text(
                "2026/06/13 08:00:00 accepted tcp:current.example:443 "
                "[vless-reality -> direct] email: alice\n"
            )
            with gzip.open(tmp_path / "access.log.20260612-030000.gz", "wt", encoding="utf-8") as handle:
                handle.write(
                    "2026/06/12 07:00:00 accepted tcp:archive.example:443 "
                    "[vless-reality -> direct] email: alice\n"
                )

            self.assertEqual(
                raw_logs.raw_log_timestamp_range(access_log),
                (
                    raw_logs.activity_time.access_time_to_iso("2026/06/12 07:00:00"),
                    raw_logs.activity_time.access_time_to_iso("2026/06/13 08:00:00"),
                ),
            )

    def test_parse_xray_error_line_extracts_level_component_and_message(self) -> None:
        event = raw_logs.parse_xray_error_line(
            "2026/06/12 08:00:00 [Warning] infra/conf/serial: Reading config failed"
        )

        self.assertIsNotNone(event)
        self.assertEqual(event["level"], "warning")
        self.assertEqual(event["source"], "xray-error-log")
        self.assertEqual(event["component"], "infra/conf/serial")
        self.assertEqual(event["message"], "Reading config failed")
        self.assertEqual(event["raw_line"], "2026/06/12 08:00:00 [Warning] infra/conf/serial: Reading config failed")

    def test_parse_xray_error_line_accepts_started_line(self) -> None:
        event = raw_logs.parse_xray_error_line("2026/06/12 08:00:00 Xray 26.3.27 started")

        self.assertIsNotNone(event)
        self.assertEqual(event["level"], "info")
        self.assertEqual(event["message"], "Xray 26.3.27 started")

    def test_backfill_iter_events_filters_client_and_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            access_log = Path(tmp_dir) / "access.log"
            access_log.write_text(
                "2026/06/12 08:00:00 accepted tcp:example.ru:443 "
                "[vless-reality -> geoip-warning-RU] email: alice|created=2026-06-12T07:01:00Z\n"
                "2026/06/13 08:00:00 accepted tcp:bob.example.ru:443 "
                "[vless-reality -> direct] email: bob|created=2026-06-12T07:01:00Z\n"
            )
            clients = {
                "alice|created=2026-06-12T07:01:00Z": {
                    "client": "alice",
                    "email": "alice|created=2026-06-12T07:01:00Z",
                    "connection": "vless-reality",
                },
                "bob|created=2026-06-12T07:01:00Z": {
                    "client": "bob",
                    "email": "bob|created=2026-06-12T07:01:00Z",
                    "connection": "vless-reality",
                },
            }
            scan_stats = []

            rows = list(
                backfill.iter_backfill_events(
                    [access_log],
                    clients,
                    client_name="alice",
                    start=date.fromisoformat("2026-06-12"),
                    end=date.fromisoformat("2026-06-12"),
                    scan_stats=scan_stats,
                )
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1]["client"], "alice")
        self.assertEqual(rows[0][1]["risks"], ["xray-geoip:RU"])
        self.assertEqual(scan_stats[0]["rawLines"], 2)
        self.assertEqual(scan_stats[0]["parsedEvents"], 2)
        self.assertEqual(scan_stats[0]["matchedEvents"], 1)

    def test_backfill_stats_output_includes_period_raw_lines_and_file_breakdown(self) -> None:
        output = StringIO()
        stats = {
            "target": "alice",
            "start": "2026-06-12",
            "end": "2026-06-13",
            "files": ["/var/log/xray/access.log"],
            "rawLines": 12,
            "parsedEvents": 10,
            "matched": 4,
            "inserted": 0,
            "duplicates": 1,
            "unknownClients": 0,
            "retentionSkipped": 0,
            "fileStats": [
                {
                    "file": "/var/log/xray/access.log",
                    "rawLines": 12,
                    "parsedEvents": 10,
                    "matchedEvents": 4,
                }
            ],
        }

        with redirect_stdout(output):
            activity_command.print_backfill_stats(stats, applied=False)

        text = output.getvalue()
        self.assertIn("Target: alice", text)
        self.assertIn("Period: 2026-06-12 .. 2026-06-13", text)
        self.assertIn("Raw lines scanned: 12", text)
        self.assertIn("Parsed events: 10", text)
        self.assertIn("access.log", text)

    def test_backfill_retention_guard_skips_events_before_detailed_retention(self) -> None:
        retention_start = date.fromisoformat("2026-06-10")

        self.assertTrue(
            backfill._event_before_retention(
                {"time": "2026-06-09T23:59:59Z"},
                retention_start,
            )
        )
        self.assertFalse(
            backfill._event_before_retention(
                {"time": "2026-06-10T00:00:00Z"},
                retention_start,
            )
        )


class RawLogTimerSyncCommandTests(unittest.TestCase):
    """VIREI-84 (T1): `xray-activity raw-log-timer-sync [--no-systemctl]` и итоговый OnCalendar до шага 1 (`units sync`)."""

    def setUp(self) -> None:
        self.sandbox = Sandbox().__enter__()
        self.addCleanup(self.sandbox.__exit__, None, None, None)
        self.env_path = self.sandbox.paths.server_env
        self.service_path = self.sandbox.paths.systemd_dir / "xray-raw-log-rotate.service"
        self.timer_path = self.sandbox.paths.systemd_dir / "xray-raw-log-rotate.timer"
        self.systemctl_calls = []
        self.enable_code = 0
        self.sandbox.processes.script({
            ("systemctl", "daemon-reload"): self.systemctl,
            ("systemctl", "enable", "--now", "xray-raw-log-rotate.timer"): self.systemctl,
            ("systemctl", "restart", "xray-raw-log-rotate.timer"): self.systemctl,
        })

    def systemctl(self, command, **kwargs):
        self.systemctl_calls.append(list(command))
        code = self.enable_code if command[1] == "enable" else 0
        return ProcessResult(returncode=code, stderr="Failed to enable unit" if code else "")

    def run_command(self, *args):
        result = self.sandbox.run_cli("activity", *args)
        return result.exit_code, result.stdout, result.stderr

    def test_no_systemctl_writes_units_and_prints_summary(self) -> None:
        self.env_path.write_text("MANAGER_TIMEZONE=Europe/Moscow\n")

        code, stdout, stderr = self.run_command("raw-log-timer-sync", "--no-systemctl")

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(
            stdout,
            f"Raw Xray log rotate service synced: {self.service_path}\n"
            f"Raw Xray log rotate timer synced: {self.timer_path}\n"
            "OnCalendar: *-*-* 03:00:00 Europe/Moscow\n"
            "systemctl: no\n",
        )
        self.assertEqual(self.service_path.read_text(), unit_sync.raw_log_service_unit())
        self.assertIn("OnCalendar=*-*-* 03:00:00 Europe/Moscow\n", self.timer_path.read_text())
        self.assertEqual(self.systemctl_calls, [])

    def test_with_systemctl_reloads_enables_and_restarts_timer(self) -> None:
        self.env_path.write_text("MANAGER_TIMEZONE=\nXRAY_RAW_LOG_ROTATE_TIME=04:30\n")

        code, stdout, _stderr = self.run_command("raw-log-timer-sync")

        self.assertEqual(code, 0)
        self.assertTrue(stdout.endswith("OnCalendar: *-*-* 04:30:00\nsystemctl: yes\n"), stdout)
        self.assertEqual(
            self.systemctl_calls,
            [
                ["systemctl", "daemon-reload"],
                ["systemctl", "enable", "--now", "xray-raw-log-rotate.timer"],
                ["systemctl", "restart", "xray-raw-log-rotate.timer"],
            ],
        )

    def test_systemctl_failure_exits_after_units_are_written(self) -> None:
        self.env_path.write_text("MANAGER_TIMEZONE=Europe/Moscow\n")
        self.enable_code = 1

        code, stdout, stderr = self.run_command("raw-log-timer-sync")

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "ERROR: Raw Xray log rotate timer sync failed: Failed to enable unit\n")
        self.assertTrue(self.service_path.exists())
        self.assertIn("OnCalendar=*-*-* 03:00:00 Europe/Moscow\n", self.timer_path.read_text())
        self.assertEqual([call[1] for call in self.systemctl_calls], ["daemon-reload", "enable"])

    def test_unknown_option_prints_usage_and_writes_nothing(self) -> None:
        code, stdout, stderr = self.run_command("raw-log-timer-sync", "--bogus")

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Usage:", stderr)
        self.assertFalse(self.timer_path.exists())
        self.assertEqual(self.systemctl_calls, [])

    def test_on_calendar_uses_valid_timezone_or_server_local_time(self) -> None:
        """VIREI-252: неверное имя даёт локальное расписание и одно предупреждение CLI."""
        local = "*-*-* 03:00:00"
        moscow = "*-*-* 03:00:00 Europe/Moscow"
        cases = (
            ("MANAGER_TIMEZONE=Europe/Moscow", moscow, ""),
            ("MANAGER_TIMEZONE=", local, ""),
            ("export manager_timezone = Europe/Berlin # DST\nXRAY_RAW_LOG_ROTATE_TIME=04:30",
             "*-*-* 04:30:00 Europe/Berlin", ""),
            ("MANAGER_TIMEZONE=../UTC", local, "MANAGER_TIMEZONE invalid: ../UTC (server local time)\n"),
            ("MANAGER_TIMEZONE=local", local, ""),
            ("MANAGER_TIMEZONE=Mars/Base", local, "MANAGER_TIMEZONE invalid: Mars/Base (server local time)\n"),
            ("MANAGER_TIMEZONE=Europe/Moscow # основная зона", moscow, ""),
            ("MANAGER_TIMEZONE=Europe/Moscow\nXRAY_RAW_LOG_ROTATE_TIME=25:00", moscow, ""),
        )

        for env_text, on_calendar, warning in cases:
            with self.subTest(env=env_text):
                self.env_path.write_text(env_text)
                code, stdout, stderr = self.run_command("raw-log-timer-sync", "--no-systemctl")

                self.assertEqual(self.env_path.read_text(), env_text)
                self.assertEqual(self.systemctl_calls, [])
                self.assertEqual(stderr, warning)
                self.assertEqual(code, 0)
                self.assertIn(f"OnCalendar: {on_calendar}\n", stdout)
                self.assertIn(f"OnCalendar={on_calendar}\n", self.timer_path.read_text())


if __name__ == "__main__":
    unittest.main()
