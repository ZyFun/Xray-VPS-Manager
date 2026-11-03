"""Характеризационные тесты CLI перед переносом слоя данных (T2)."""

from contextlib import closing
import sqlite3
import unittest

from tests.support.sandbox import Sandbox


class DataCliTests(unittest.TestCase):
    def test_client_list_on_one_client_fixture(self):
        """Текущее поведение, баг XR-REV-036: ANSI-метка free в выводе без TTY и при NO_COLOR=1."""
        with Sandbox() as box:
            stats = ("/usr/local/bin/xray", "api", "statsquery", "--server=127.0.0.1:10085", "-pattern", "user>>>")
            box.processes.script({stats: box.processes.ok('{"stat": []}')})
            before = box.db_rows("clients", "client_credentials", "traffic_totals")

            result = box.run_cli("client", "list")

            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertEqual(box.processes.calls, [stats])
            self.assertEqual(box.db_rows("clients", "client_credentials", "traffic_totals"), before)
            box.assert_golden("client/list", {"stdout.txt": result.stdout})

    def test_telegram_status_on_one_paid_client_fixture(self):
        with Sandbox() as box:
            with closing(sqlite3.connect(box.paths.manager_db)) as connection, connection:
                connection.execute("UPDATE clients SET payment_type = 'paid' WHERE name = 'alice'")
                connection.execute("INSERT INTO telegram_settings(key, value) VALUES ('botName', 'FixtureBot')")
            tables = ("clients", "cascade_routes", "telegram_settings", "telegram_subscriptions")
            before = box.db_rows(*tables)

            result = box.run_cli("telegram", "status")

            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertEqual(box.db_rows(*tables), before)
            self.assertEqual(box.processes.calls, [])
            self.assertEqual(box.http.calls, [])
            box.assert_golden("telegram/status", {"stdout.txt": result.stdout})

    def test_missing_or_uninitialized_database_reports_error_to_cli_and_pipeline(self):
        """B2.1 (XR-REV-034): CLI и каждый шаг таймера сообщают ERROR без traceback и автосоздания."""
        commands = [
            ("client", "list"), ("traffic-sync", "--quiet"), ("activity", "sync", "--quiet"),
            ("telegram", "notify-geoip", "--quiet"), ("client", "enforce-limits", "--quiet"),
            ("client", "expire-due", "--quiet"), ("telegram", "notify-expiry", "--quiet"),
            ("telegram", "notify-daily-summary", "--quiet"),
        ]
        with Sandbox() as box:
            path = box.paths.manager_db
            path.unlink()
            for exists, reason in ((False, "missing"), (True, "not initialized")):
                if exists:
                    path.touch()
                for command in commands:
                    with self.subTest(command=command, reason=reason):
                        result = box.run_cli(*command)
                        self.assertEqual(result.exit_code, 1)
                        self.assertIn(f"ERROR: SQLite manager database is {reason}", result.stderr)
                        self.assertNotIn("Traceback", result.stderr)
                        self.assertEqual(path.exists(), exists)
                        if exists:
                            self.assertEqual(path.read_bytes(), b"")
