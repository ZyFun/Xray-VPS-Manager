"""Сводка `activity.summary` в `manager_metadata` не ведётся и не пишется (VIREI-248).

Характеризация: вывод отчётов активности и `xray-activity status` на фикстуре access.log после
прогона sync с подробным журналом — ожидания не меняются после правки. Регрессия: прогон sync,
`save_activity_db` (его вызывают `controls`, `cleanup`, `sync`) не пишут строку `activity.summary`
и удаляют накопленную, повторный прогон её не создаёт.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.test_activity_sync_alert_windows import AlertWindowSyncCase

from xray_vps_manager.activity import cleanup as activity_cleanup
from xray_vps_manager.activity import client_reports
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import settings as activity_settings
from xray_vps_manager.activity import status as activity_status
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity

SUMMARY_KEY = sqlite_activity.ACTIVITY_SUMMARY_KEY
REPORT_DAY = date(2026, 6, 12)
OLD_SUMMARY = {"alice": {"totalEvents": 7, "days": {"2026-06-12": {"events": 7}}}}


class SummaryRemovalCase(AlertWindowSyncCase):
    def sync_patches(self, stack: ExitStack) -> dict:
        mocks = super().sync_patches(stack)
        # Подробный журнал включён: именно в этой ветке раньше обновлялась сводка.
        stack.enter_context(mock.patch.object(activity_settings, "activity_enabled", return_value=True))
        return mocks

    def seed_summary(self) -> None:
        def write(connection) -> None:
            sqlite_activity.set_metadata_json(connection, SUMMARY_KEY, OLD_SUMMARY)

        self.with_connection(write)

    def summary_row(self):
        return self.with_connection(
            lambda connection: connection.execute(
                "SELECT value FROM manager_metadata WHERE key = ?", (SUMMARY_KEY,)
            ).fetchone()
        )


class ActivityReportsCharacterizationTests(SummaryRemovalCase):
    """Текущее поведение отчётов; не меняется после VIREI-248."""

    def setUp(self) -> None:
        super().setUp()
        result, messages, _mocks = self.run_sync()
        self.assertEqual(result, 0)
        self.assertIn(
            "Activity sync saved: 6 parsed, 6 detailed, 6 counter updates, 3 alerts, 0 skipped, "
            "0 detailed pruned, 0 alerts pruned, 0 errors pruned.",
            messages,
        )

    def report_patches(self, stack: ExitStack) -> None:
        stack.enter_context(mock.patch.object(database, "MANAGER_DB_PATH", self.db_path))
        stack.enter_context(
            mock.patch.object(client_reports.activity_time, "date_range_from_days", return_value=(REPORT_DAY, REPORT_DAY))
        )
        stack.enter_context(mock.patch.object(client_reports.activity_exceptions, "exception_items_for_read", return_value=[]))
        stack.enter_context(
            mock.patch.object(client_reports, "known_credential_connections", return_value=["vless-reality"])
        )

    def test_client_report_for_day(self) -> None:
        with ExitStack() as stack:
            self.report_patches(stack)
            report = client_reports.client_report("alice", "1")

        self.assertEqual(report["totalEvents"], 5)
        self.assertEqual(
            report["rows"],
            [["2026-06-12", 5, 3, "443(3), 8443(1), 8080(1)", "direct(5)", "-", "-", "-",
              "one.example(3), two.example(1), three.example(1)"]],
        )
        self.assertEqual(
            report["credentialRows"],
            [
                ["vless-reality", 5, 3, "443(3), 8443(1), 8080(1)", "direct(5)", "-", "-", "-",
                 "one.example(3), two.example(1), three.example(1)"],
                ["TOTAL", 5, "-", "-", "-", "-", "-", "-", "-"],
            ],
        )

    def test_counter_history(self) -> None:
        for client, expected in (("alice", 5), ("bob", 1)):
            rows = self.read_hour_counters(client)
            self.assertEqual([(row["bucketStart"], row["totalEvents"]) for row in rows], [("2026-06-12T08:00:00+00:00", expected)])
        alice = self.read_hour_counters("alice")[0]
        self.assertEqual((alice["uniqueHosts"], alice["uniquePorts"]), (3, 3))

    def test_suspicious_report(self) -> None:
        with ExitStack() as stack:
            self.report_patches(stack)
            report = client_reports.suspicious_report("1")

        self.assertEqual([row[0] for row in report["rows"]], ["alice"])
        row = report["rows"][0]
        self.assertEqual(row[2], 9)
        self.assertEqual(
            row[1], "unique-ports(3), unique-hosts(3), burst(3)"
        )

    def test_status_rows(self) -> None:
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(database, "MANAGER_DB_PATH", self.db_path))
            stack.enter_context(mock.patch.object(activity_status, "MANAGER_DB_PATH", self.db_path))
            stack.enter_context(mock.patch.object(activity_status.settings, "activity_enabled", return_value=True))
            stack.enter_context(mock.patch.object(activity_status.repository, "load_json", return_value={}))
            stack.enter_context(mock.patch.object(activity_status.activity_exceptions, "exception_items_for_read", return_value=[]))
            stack.enter_context(mock.patch.object(activity_status.activity_blocklist, "block_items", return_value=[]))
            stack.enter_context(mock.patch.object(activity_status.activity_blocklist, "active_block_items", return_value=[]))
            stack.enter_context(mock.patch.object(activity_status.settings, "xray_geoip_warning_code", return_value=""))
            stack.enter_context(mock.patch.object(activity_status.activity_parser, "geoip_path", return_value=None))
            stack.enter_context(mock.patch.object(activity_status, "first_event_status", return_value="2026-06-12"))
            rows, warnings = activity_status.status_rows()

        by_name = {row[0]: row[1] for row in rows}
        self.assertEqual(warnings, [])
        self.assertEqual(by_name["Detailed mode"], "all")
        self.assertEqual(by_name["Detailed retention"], "365 days")
        self.assertEqual(by_name["Alert-log"], "enabled")
        self.assertEqual(by_name["First event"], "2026-06-12")
        self.assertRegex(by_name["Last sync"], r"^\d{4}-\d{2}-\d{2}T")


class SummaryNotWrittenTests(SummaryRemovalCase):
    def recorded_summary_writes(self, action) -> list[str]:
        statements: list[str] = []
        original = database.open_database

        def tracing_open(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch.object(database, "open_database", tracing_open):
            action()
        return [
            sql for sql in statements
            if SUMMARY_KEY in sql and sql.lstrip().upper().startswith(("INSERT", "UPDATE", "REPLACE"))
        ]

    def test_sync_with_events_does_not_write_summary(self) -> None:
        writes = self.recorded_summary_writes(lambda: self.assertEqual(self.run_sync()[0], 0))

        self.assertEqual(writes, [])
        self.assertIsNone(self.summary_row())

    def test_sync_removes_accumulated_summary_once_and_rerun_does_not_recreate_it(self) -> None:
        self.seed_summary()
        self.assertIsNotNone(self.summary_row())

        self.assertEqual(self.run_sync()[0], 0)
        self.assertIsNone(self.summary_row())

        writes = self.recorded_summary_writes(lambda: self.assertEqual(self.run_sync()[0], 0))
        self.assertEqual(writes, [])
        self.assertIsNone(self.summary_row())

    def test_save_activity_db_does_not_write_summary_and_removes_old_one(self) -> None:
        """`controls` (срок хранения, вкл/выкл журнала) и `cleanup` сохраняют состояние тем же вызовом."""
        self.seed_summary()
        db = {"version": 1, "enabled": True, "retentionDays": 90, "clients": OLD_SUMMARY, "accessLog": {"offset": 5}}

        writes = self.recorded_summary_writes(lambda: activity_repository.save_activity_db(db, db_path=self.db_path))

        self.assertEqual(writes, [])
        self.assertIsNone(self.summary_row())
        self.assertEqual(self.with_connection(sqlite_activity.get_access_log_state)["offset"], 5)

    def test_cleanup_retention_does_not_write_summary(self) -> None:
        self.seed_summary()

        with ExitStack() as stack:
            self.sync_patches(stack)
            writes = self.recorded_summary_writes(
                lambda: activity_cleanup._apply_retention(30, db_path=self.db_path)
            )

        self.assertEqual(writes, [])
        self.assertIsNone(self.summary_row())

    def test_load_activity_db_ignores_stored_summary(self) -> None:
        self.seed_summary()

        db = activity_repository.load_activity_db(30, True, db_path=self.db_path)

        self.assertEqual(db["clients"], {})


if __name__ == "__main__":
    unittest.main()
