"""Регрессия XR-REV-056 и XR-REV-018: клиент только в config.json не останавливает activity sync и ротацию raw-логов."""

from contextlib import ExitStack, redirect_stderr
from datetime import datetime, timedelta, timezone
import gzip
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.activity import backfill as activity_backfill
from xray_vps_manager.activity import bypass as activity_bypass
from xray_vps_manager.activity import raw_logs
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import settings as activity_settings
from xray_vps_manager.activity import sync as activity_sync
from xray_vps_manager.commands import activity as activity_command
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.storage import SQLiteReadUnavailable

TAG = "vless-reality"
ALICE = "alice|created=2026-06-12T07:01:00Z"
BOB = "bob|created=2026-06-12T07:02:00Z"
CAROL = "carol|created=2026-06-12T07:03:00Z"


def access_line(email: str, host: str) -> str:
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y/%m/%d %H:%M:%S")
    return f"{stamp} accepted tcp:{host}:443 [{TAG} -> direct] email: {email}\n"


class ConfigOnlyClientTests(unittest.TestCase):
    """alice есть в config.json и SQLite, bob и carol — только в config.json."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.access_log = self.root / "access.log"
        self.error_log = self.root / "error.log"
        self.config_path = self.root / "config.json"
        self.db_path = self.root / "manager.db"
        self.config_path.write_text(json.dumps(self.config()))
        self.make_manager_db()

        stack = ExitStack()
        self.addCleanup(stack.close)
        for target, name, value in (
            (database, "MANAGER_DB_PATH", self.db_path),
            (activity_sync, "ACCESS_LOG_PATH", self.access_log),
            (activity_sync, "CONFIG_PATH", self.config_path),
            (activity_bypass, "CONFIG_PATH", self.config_path),
            (raw_logs, "ACCESS_LOG_PATH", self.access_log),
            (raw_logs, "ERROR_LOG_PATH", self.error_log),
            (activity_settings, "SERVER_ENV_PATH", self.root / "server.env"),
        ):
            stack.enter_context(mock.patch.object(target, name, value))
        stack.enter_context(mock.patch.object(activity_repository, "ensure_dirs"))
        stack.enter_context(
            mock.patch.object(activity_repository, "manager_timezone", return_value=(timezone.utc, "UTC"))
        )
        stack.enter_context(
            mock.patch.object(activity_sync.activity_blocklist, "reconcile_xray_config", return_value=None)
        )

    @staticmethod
    def config() -> dict:
        return {
            "inbounds": [
                {
                    "tag": TAG,
                    "protocol": "vless",
                    "settings": {"clients": [{"email": ALICE}, {"email": BOB}, {"email": CAROL}]},
                }
            ],
            "outbounds": [{"tag": "direct", "protocol": "freedom"}],
            "routing": {"rules": []},
        }

    def make_manager_db(self) -> None:
        connection = database.open_database(self.db_path)
        try:
            sqlite_connections.upsert_connection(
                connection,
                TAG,
                {
                    "tag": TAG,
                    "name": "default",
                    "created": "2026-06-12T07:00:00Z",
                    "port": 443,
                    "sni": "example.com",
                    "dest": "example.com:443",
                    "fingerprint": "chrome",
                },
            )
            sqlite_clients.upsert_client(
                connection,
                "alice",
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "created": "2026-06-12T07:01:00Z",
                    "enabled": True,
                    "connection": TAG,
                    "client": {"id": "00000000-0000-0000-0000-000000000001", "email": ALICE},
                },
            )
            sqlite_activity.set_detail_mode(connection, "all")
            sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def write_log(self, *lines: str) -> None:
        self.access_log.write_text("".join(lines))

    def mark_sqlite_not_ready(self) -> None:
        connection = database.open_database(self.db_path)
        try:
            sqlite_settings.set_metadata(connection, "jsonImport.completed", "false")
        finally:
            connection.close()

    def query(self, func, *args, **kwargs):
        connection = database.open_database(self.db_path)
        try:
            return func(connection, *args, **kwargs)
        finally:
            connection.close()

    def counter_totals(self) -> dict:
        rows = self.query(
            lambda connection: connection.execute(
                "SELECT client_name, SUM(total_events) AS total FROM activity_client_counters "
                "WHERE bucket_type = 'day' GROUP BY client_name"
            ).fetchall()
        )
        return {row["client_name"]: row["total"] for row in rows}

    def detailed_hosts(self) -> list:
        events = self.query(lambda connection: list(sqlite_activity.iter_events(connection)))
        return sorted(event["host"] for event in events)

    def access_offset(self) -> int:
        return int(self.query(sqlite_activity.get_access_log_state).get("offset") or 0)

    @staticmethod
    def summary(messages: list) -> str:
        return next(message for message in messages if message.startswith("Activity sync saved:"))

    @staticmethod
    def warnings(messages: list) -> list:
        return [message for message in messages if message.startswith("WARN:")]

    def test_sync_skips_config_only_client_and_records_other_events_once(self) -> None:
        self.write_log(access_line(ALICE, "a1.example"), access_line(BOB, "b1.example"), access_line(ALICE, "a2.example"))
        messages: list = []

        result = activity_sync.sync_activity(messages.append)

        self.assertEqual(result, 0)
        self.assertEqual(self.access_offset(), self.access_log.stat().st_size)
        self.assertEqual(self.counter_totals(), {"alice": 2})
        self.assertEqual(self.detailed_hosts(), ["a1.example", "a2.example"])
        self.assertIn("2 parsed", self.summary(messages))
        self.assertIn("1 skipped", self.summary(messages))

        messages = []
        result = activity_sync.sync_activity(messages.append)

        self.assertEqual(result, 0)
        self.assertIn("0 parsed", self.summary(messages))
        self.assertEqual(self.counter_totals(), {"alice": 2})
        self.assertEqual(self.detailed_hosts(), ["a1.example", "a2.example"])

    def test_sync_warns_once_per_run_about_several_foreign_lines(self) -> None:
        self.write_log(
            access_line(BOB, "b1.example"),
            access_line(CAROL, "c1.example"),
            access_line(BOB, "b2.example"),
            access_line(ALICE, "a1.example"),
            "not an access log line\n",
        )
        messages: list = []

        result = activity_sync.sync_activity(messages.append)

        self.assertEqual(result, 0)
        warnings = self.warnings(messages)
        self.assertEqual(len(warnings), 1)
        self.assertIn("3 access log line(s)", warnings[0])
        self.assertIn("bob, carol", warnings[0])
        self.assertIn("4 skipped", self.summary(messages))
        self.assertEqual(self.counter_totals(), {"alice": 1})

    def test_sync_skips_event_of_client_removed_from_sqlite_after_client_lookup(self) -> None:
        self.write_log(access_line(ALICE, "a1.example"), access_line(BOB, "b1.example"))
        stale_clients = {
            ALICE: {"client": "alice", "email": ALICE, "connection": TAG},
            BOB: {"client": "bob", "email": BOB, "connection": TAG},
        }
        messages: list = []

        with mock.patch.object(activity_sync, "known_clients", return_value=stale_clients):
            result = activity_sync.sync_activity(messages.append)

        self.assertEqual(result, 0)
        self.assertEqual(self.access_offset(), self.access_log.stat().st_size)
        self.assertEqual(self.counter_totals(), {"alice": 1})
        self.assertIn("1 parsed", self.summary(messages))
        self.assertIn("1 skipped", self.summary(messages))
        self.assertEqual(len(self.warnings(messages)), 1)
        self.assertIn("bob", self.warnings(messages)[0])

    def test_sync_skips_config_only_clients_when_sqlite_has_no_clients(self) -> None:
        self.query(sqlite_clients.delete_client, "alice")
        self.write_log(access_line(ALICE, "a1.example"), access_line(BOB, "b1.example"))
        messages: list = []

        result = activity_sync.sync_activity(messages.append)

        self.assertEqual(result, 0)
        self.assertEqual(self.access_offset(), self.access_log.stat().st_size)
        self.assertIn("0 parsed", self.summary(messages))
        self.assertIn("2 skipped", self.summary(messages))
        warnings = self.warnings(messages)
        self.assertEqual(len(warnings), 1)
        self.assertIn("alice, bob", warnings[0])
        self.assertFalse(any("No clients found" in message for message in messages))

        messages = []
        result = activity_sync.sync_activity(messages.append)

        self.assertEqual(result, 0)
        self.assertIn("0 skipped", self.summary(messages))
        self.assertEqual(self.warnings(messages), [])
        self.assertEqual(self.counter_totals(), {})
        self.assertEqual(self.detailed_hosts(), [])

    def test_sync_fails_without_advancing_offset_when_sqlite_is_not_ready(self) -> None:
        self.write_log(access_line(ALICE, "a1.example"), access_line(BOB, "b1.example"))
        self.mark_sqlite_not_ready()

        with self.assertRaises(SQLiteReadUnavailable):
            activity_sync.sync_activity(lambda _message: None)

        self.assertEqual(self.access_offset(), 0)
        self.assertEqual(self.counter_totals(), {})

    def test_rotate_raw_logs_due_rotates_when_log_has_config_only_client(self) -> None:
        self.write_log(access_line(ALICE, "a1.example"), access_line(BOB, "b1.example"), access_line(ALICE, "a2.example"))
        messages: list = []
        restarted = subprocess.CompletedProcess(["systemctl", "try-restart", "xray.service"], 0, "", "")

        with mock.patch.object(raw_logs, "rotation_due", return_value=(True, "2026-10-14", "UTC")), \
            mock.patch.object(raw_logs, "restart_xray", return_value=restarted) as restart_xray:
            result = raw_logs.rotate_raw_logs(only_if_due=True, log=messages.append)

        self.assertEqual(result, 0)
        restart_xray.assert_called_once_with()
        archives = sorted(self.root.glob("access.log.*.gz"))
        self.assertEqual(len(archives), 1)
        with gzip.open(archives[0], "rt", encoding="utf-8") as handle:
            archived = handle.read()
        self.assertIn(f"email: {BOB}", archived)
        self.assertEqual(self.access_log.read_text(), "")
        self.assertEqual(self.counter_totals(), {"alice": 2})
        self.assertFalse(any("pre-sync failed" in message for message in messages))
        state = self.query(sqlite_activity.get_metadata_json, raw_logs.RAW_LOG_ROTATION_STATE_KEY)
        self.assertEqual(state.get("lastRunDate"), "2026-10-14")

    def test_rotate_raw_logs_due_rotates_and_fails_when_pre_sync_cannot_read_sqlite(self) -> None:
        self.write_log(access_line(ALICE, "a1.example"), access_line(BOB, "b1.example"))
        self.mark_sqlite_not_ready()
        messages: list = []
        restarted = subprocess.CompletedProcess(["systemctl", "try-restart", "xray.service"], 0, "", "")

        with mock.patch.object(raw_logs, "rotation_due", return_value=(True, "2026-10-14", "UTC")), \
            mock.patch.object(raw_logs, "restart_xray", return_value=restarted):
            result = raw_logs.rotate_raw_logs(only_if_due=True, log=messages.append)

        self.assertEqual(result, 1)
        archives = sorted(self.root.glob("access.log.*.gz"))
        self.assertEqual(len(archives), 1)
        with gzip.open(archives[0], "rt", encoding="utf-8") as handle:
            archived = handle.read()
        self.assertIn(f"email: {ALICE}", archived)
        self.assertEqual(self.access_log.read_text(), "")
        self.assertTrue(any(message.startswith("WARN:") and "rotating anyway" in message for message in messages))
        self.assertIn("pre-sync failed", messages[-1])

    def backfill_period(self) -> tuple:
        today = datetime.now(timezone.utc).date()
        return today - timedelta(days=2), today + timedelta(days=1)

    def test_backfill_all_ignores_lines_of_config_only_client(self) -> None:
        self.write_log(access_line(ALICE, "a1.example"), access_line(BOB, "b1.example"), access_line(ALICE, "a2.example"))
        start, end = self.backfill_period()

        clients = activity_sync.known_clients()
        with mock.patch.object(activity_backfill, "access_log_files", return_value=[self.access_log]):
            stats = activity_backfill.run_backfill(clients, client_name="all", start=start, end=end, apply=True)

        self.assertEqual(stats["rawLines"], 3)
        self.assertEqual(stats["parsedEvents"], 2)
        self.assertEqual(stats["clients"], {"alice": 2})
        self.assertEqual(stats["unknownClients"], 0)
        self.assertEqual(stats["inserted"], 2)
        self.assertEqual(self.detailed_hosts(), ["a1.example", "a2.example"])

    def test_backfill_command_rejects_config_only_client(self) -> None:
        self.write_log(access_line(BOB, "b1.example"))
        start, end = self.backfill_period()
        stderr = StringIO()

        with mock.patch.object(activity_backfill, "access_log_files", return_value=[self.access_log]), \
            redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            activity_command.backfill_activity("bob", start.isoformat(), end.isoformat(), ["--dry-run"])

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Unknown client in active config/client db: bob", stderr.getvalue())
        self.assertEqual(self.detailed_hosts(), [])


if __name__ == "__main__":
    unittest.main()
