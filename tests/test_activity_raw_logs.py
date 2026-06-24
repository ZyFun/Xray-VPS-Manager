import unittest
import tempfile
import gzip
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from xray_vps_manager.activity import backfill
from xray_vps_manager.activity import raw_logs
from xray_vps_manager.commands import activity as activity_command


class ActivityRawLogsTests(unittest.TestCase):
    def test_raw_log_timer_unit_uses_on_calendar_with_manager_timezone(self) -> None:
        env = {"XRAY_RAW_LOG_ROTATE_TIME": "03:00", "MANAGER_TIMEZONE": "Europe/Moscow"}

        unit = raw_logs.raw_log_timer_unit(env)

        self.assertIn("OnCalendar=*-*-* 03:00:00 Europe/Moscow", unit)
        self.assertIn("Persistent=true", unit)
        self.assertNotIn("OnUnitActiveSec=5min", unit)

    def test_raw_log_timer_unit_uses_server_local_time_when_timezone_is_empty(self) -> None:
        env = {"XRAY_RAW_LOG_ROTATE_TIME": "04:30", "MANAGER_TIMEZONE": ""}

        self.assertIn("OnCalendar=*-*-* 04:30:00", raw_logs.raw_log_timer_unit(env))
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

            result = raw_logs.sync_raw_log_timer(
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

    def test_rotate_raw_logs_aborts_before_rename_when_pre_sync_fails(self) -> None:
        messages: list[str] = []

        with (
            mock.patch.object(raw_logs, "rotation_due", return_value=(True, "2026-06-12", "UTC")),
            mock.patch.object(raw_logs, "drain_logs_before_rotation", return_value=1),
            mock.patch.object(raw_logs, "rotate_file") as rotate_file,
        ):
            result = raw_logs.rotate_raw_logs(only_if_due=True, log=messages.append)

        self.assertEqual(result, 1)
        rotate_file.assert_not_called()
        self.assertTrue(any("aborted before renaming" in message for message in messages))

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


if __name__ == "__main__":
    unittest.main()
