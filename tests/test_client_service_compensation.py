"""VIREI-130: порядок записи и компенсация клиентских операций через CLI."""

from contextlib import closing, contextmanager
import json
import sqlite3
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_client_characterization import TABLES, script_processes
from tests.test_client_manager_lock import lock_is_free
from xray_vps_manager.db import database
from xray_vps_manager.clients import service


RELATED_TABLES = (
    "activity_events", "activity_event_risks", "activity_blocklist",
    "activity_blocklist_hits", "activity_capture_clients", "activity_alert_events",
    "activity_client_counters", "activity_client_counter_uniques", "telegram_subscriptions",
)
RESTART = ("systemctl", "restart", "xray")
STOP = ("systemctl", "stop", "xray")
STATS = ("/usr/local/bin/xray", "api", "statsquery", "--server=127.0.0.1:10085", "-pattern", "user>>>")


class ClientServiceCompensationTests(unittest.TestCase):
    def setUp(self):
        self.box = self.enterContext(Sandbox())
        self.enterContext(running_xray(self.box))
        script_processes(self.box)
        self.processes = self.box.processes
        path = self.box.paths.config
        self.candidate_test = (
            str(self.box.paths.xray_bin), "run", "-test", "-format", "json", "-config",
            str(path.with_suffix(".json.tmp")),
        )

    def cli(self, *argv):
        result = self.box.run_cli("client", *argv)
        self.assertEqual(result.exit_code, 0, (argv, result.stdout, result.stderr))
        return result.stdout

    def db_rows(self):
        return self.box.db_rows(*TABLES, *RELATED_TABLES)

    def query(self, statement, parameters=()):
        with database.session() as conn:
            return [tuple(row) for row in conn.execute(statement, parameters)]

    def write_during_apply(self, statement, parameters=()):
        # Timeout=0 сразу выявляет открытую пишущую транзакцию у сценария.
        with closing(sqlite3.connect(self.box.paths.manager_db, timeout=0)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(statement, parameters)

    def seed_credential_traffic(self):
        with database.write_session() as conn:
            conn.execute(
                "INSERT INTO credential_traffic_totals(client_name,connection_tag,incoming_bytes) "
                "VALUES ('alice','vless-reality',123)",
            )
            conn.execute(
                "INSERT INTO credential_traffic_history(client_name,connection_tag,bucket_date,bucket_hour,incoming_bytes) "
                "VALUES ('alice','vless-reality','2026-10-06',12,123)",
            )

    def seed_active_traffic(self):
        email, = self.query("SELECT email FROM clients WHERE name='alice'")[0]
        with database.write_session() as conn:
            for statement in (
                "INSERT INTO traffic_totals(client_name,email,incoming_bytes,last_runtime_uplink,last_runtime_downlink) "
                "VALUES ('alice',?,100,100,0)",
                "INSERT INTO credential_traffic_totals("
                "client_name,connection_tag,email,incoming_bytes,last_runtime_uplink,last_runtime_downlink"
                ") VALUES ('alice','vless-reality',?,100,100,0)",
            ):
                conn.execute(statement, (email,))
            conn.execute(
                "INSERT INTO traffic_history(client_name,bucket_date,bucket_hour,incoming_bytes) "
                "VALUES ('alice','2026-10-06',15,100)"
            )
            conn.execute(
                "INSERT INTO credential_traffic_history(client_name,connection_tag,bucket_date,bucket_hour,incoming_bytes) "
                "VALUES ('alice','vless-reality','2026-10-06',15,100)"
            )
        return email

    def script_execstop(self, email, value, *, activity_locked=False, observations=None, after_sync=None):
        stats = {"stat": [{"name": f"user>>>{email}>>>traffic>>>uplink", "value": value}]}

        def stop(_argv, **_kwargs):
            self.assertFalse(lock_is_free(self.box.paths.manager_lock))
            self.assertTrue(lock_is_free(self.box.paths.traffic_lock))
            if activity_locked:
                self.assertFalse(lock_is_free(self.box.paths.activity_lock))
            if observations is not None:
                observations.append(self.query("SELECT name FROM clients ORDER BY name"))
            result = self.box.run_cli("traffic-sync", "--quiet")
            self.assertEqual(result.exit_code, 0, result.stderr)
            if after_sync is not None:
                after_sync()
            return self.processes.ok()

        self.processes.script({STOP: stop, STATS: self.processes.ok(json.dumps(stats))})

    def seed_linked_state(self):
        self.seed_credential_traffic()
        with database.write_session() as conn:
            for statement in (
                "INSERT INTO traffic_totals(client_name,incoming_bytes) VALUES ('alice',123),('bob',456)",
                "INSERT INTO traffic_history(client_name,bucket_date,bucket_hour,incoming_bytes) "
                "VALUES ('alice','2026-10-06',12,123)",
                "INSERT INTO client_traffic_limits(client_name,period,limit_bytes) VALUES ('alice','daily',50)",
                "INSERT INTO client_traffic_limit_state(client_name,exceeded_bytes) VALUES ('alice',123)",
                "INSERT INTO activity_events(id,event_time,client_name) VALUES (10,'2026-10-06T12:00:00Z','alice')",
                "INSERT INTO activity_event_risks(event_id,risk) VALUES (10,'fixture')",
                "INSERT INTO activity_blocklist(id,value,kind,source_client_name,source_event_id,created_at) "
                "VALUES (11,'blocked.example','domain','alice',10,'2026-10-06T12:00:00Z')",
                "INSERT INTO activity_blocklist_hits(blocklist_id,client_name,hits) VALUES (11,'alice',8),(11,'bob',3)",
                "INSERT INTO activity_capture_clients(client_name) VALUES ('alice')",
                "INSERT INTO activity_alert_events("
                "id,event_time,client_name,risk,dedupe_key,first_seen_at,last_seen_at,raw_ref_event_id"
                ") VALUES (12,'now','alice','fixture','alice-alert','now','now',10),"
                "(13,'now','bob','fixture','bob-alert','now','now',10)",
                "INSERT INTO activity_client_counters(client_name,bucket_type,bucket_start,total_events) "
                "VALUES ('alice','hour','2026-10-06T12:00:00Z',1)",
                "INSERT INTO activity_client_counter_uniques(client_name,bucket_type,bucket_start,unique_kind,value_hash) "
                "VALUES ('alice','hour','2026-10-06T12:00:00Z','host','fixture')",
                "INSERT INTO telegram_subscriptions(chat_id,client_name,client_uuid) "
                "SELECT '100', name, uuid FROM clients WHERE name = 'alice'",
            ):
                conn.execute(statement)

    def test_add_commits_before_check_and_releases_sqlite_before_restart(self):
        path = self.box.paths.config
        config = self.box.config()
        rules = config["routing"]["rules"]
        api = next(rule for rule in rules if rule.get("inboundTag") == ["api"])
        rules.remove(api)
        rules.insert(0, {"type": "field", "protocol": ["bittorrent"], "outboundTag": "blocked"})
        rules.append(api)
        path.write_text(json.dumps(config))
        original = path.read_bytes()

        def validate(argv, **_kwargs):
            self.assertFalse(lock_is_free(self.box.paths.manager_lock))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(self.query("SELECT name FROM clients ORDER BY name"), [("alice",), ("bob",)])
            candidate = json.loads(path.with_suffix(".json.tmp").read_text())
            self.assertEqual(candidate["routing"]["rules"][0], api)
            return self.processes.ok()

        def restart(argv, **_kwargs):
            self.assertFalse(lock_is_free(self.box.paths.manager_lock))
            self.assertNotEqual(path.read_bytes(), original)
            self.write_during_apply("INSERT INTO traffic_totals(client_name,incoming_bytes) VALUES ('alice',42)")
            return self.processes.ok()

        self.processes.script({self.candidate_test: validate, RESTART: restart})
        self.cli("add", "bob", "0")
        self.assertEqual(self.query("SELECT incoming_bytes FROM traffic_totals WHERE client_name='alice'"), [(42,)])
        self.assertLess(self.processes.calls.index(self.candidate_test), self.processes.calls.index(RESTART))

    def test_add_restores_db_after_candidate_rejection_or_cancellation(self):
        self.cli("add-connection", "Second", "8443", "cdn.example", "firefox", "tcp")
        original = self.box.paths.config.read_bytes()
        before = self.db_rows()

        for error, exit_code in ((self.processes.result(1, stderr="fixture test failure"), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(exit_code=exit_code):
                def reject(_argv, **_kwargs):
                    self.assertEqual(self.query(
                        "SELECT connection_tag FROM client_credentials WHERE client_name='alice' ORDER BY connection_tag"
                    ), [("vless-reality",), ("vless-reality-2",)])
                    if isinstance(error, KeyboardInterrupt):
                        raise error
                    return error

                self.processes.script({self.candidate_test: reject})
                offset = len(self.processes.calls)
                result = self.box.run_cli("client", "add", "alice", "0", "--connection", "vless-reality-2", "--payment", "paid")
                self.assertEqual(result.exit_code, exit_code, result.stderr)
                if exit_code == 130:
                    self.assertEqual(result.stderr, "")
                self.assertNotIn(RESTART, self.processes.calls[offset:])
                self.assertEqual(self.box.paths.config.read_bytes(), original)
                self.assertEqual(self.db_rows(), before)

    def test_remove_restores_linked_data_and_keeps_parallel_traffic_write(self):
        self.cli("add", "bob", "0")
        self.seed_linked_state()
        before = self.db_rows()
        original = self.box.paths.config.read_bytes()

        def fail_restart(_argv, **_kwargs):
            self.assertEqual(self.query("SELECT name FROM clients"), [("bob",)])
            self.assertEqual(self.query("SELECT source_client_name, source_event_id FROM activity_blocklist"), [(None, None)])
            self.assertEqual(self.query("SELECT raw_ref_event_id FROM activity_alert_events"), [(None,)])
            self.assertEqual(self.query("SELECT client_name FROM telegram_subscriptions"), [(None,)])
            self.write_during_apply("UPDATE traffic_totals SET incoming_bytes=999 WHERE client_name='bob'")
            return self.processes.result(1, stderr="fixture restart failure")

        self.processes.script({RESTART: [fail_restart, fail_restart, self.processes.ok()]})
        result = self.box.run_cli("client", "remove", "alice")

        self.assertEqual(result.exit_code, 1, result.stderr)
        self.assertIn("Restored backup:", result.stderr)
        self.assertEqual(self.box.paths.config.read_bytes(), original)
        self.assertEqual(self.query("SELECT incoming_bytes FROM traffic_totals WHERE client_name='bob'"), [(999,)])
        self.write_during_apply("UPDATE traffic_totals SET incoming_bytes=456 WHERE client_name='bob'")
        self.assertEqual(self.db_rows(), before)
        self.assertEqual(self.query("PRAGMA foreign_key_check"), [])

    def test_move_restores_source_credential_and_history_on_apply_failure(self):
        self.cli("add-connection", "Second", "8443", "cdn.example", "firefox", "tcp")
        self.seed_credential_traffic()
        before = self.db_rows()
        original = self.box.paths.config.read_bytes()

        def reject(_argv, **_kwargs):
            self.assertEqual(self.query("SELECT connection_tag FROM client_credentials"), [("vless-reality-2",)])
            self.assertEqual(self.query("SELECT * FROM credential_traffic_history"), [])
            return self.processes.result(1, stderr="fixture test failure")

        self.processes.script({self.candidate_test: reject})
        result = self.box.run_cli("client", "move-connection", "alice", "Second")

        self.assertEqual(result.exit_code, 1, result.stderr)
        self.assertEqual(self.box.paths.config.read_bytes(), original)
        self.assertEqual(self.db_rows(), before)

    def test_active_move_keeps_execstop_delta_in_client_totals_only(self):
        self.cli("add-connection", "Second", "8443", "cdn.example", "firefox", "tcp")
        email = self.seed_active_traffic()
        saved_before_apply_finishes = []

        def observe_totals():
            saved_before_apply_finishes.extend(self.query("SELECT incoming_bytes FROM traffic_totals"))

        self.script_execstop(email, 175, after_sync=observe_totals)

        self.cli("move-connection", "alice", "Second")

        self.assertEqual(self.query("SELECT incoming_bytes,last_online_source FROM traffic_totals"), [(175, "traffic")])
        self.assertEqual(saved_before_apply_finishes, [(175,)])
        self.assertEqual(self.query("SELECT incoming_bytes FROM traffic_history"), [(175,)])
        self.assertEqual(self.query("SELECT connection_tag FROM client_credentials"), [("vless-reality-2",)])
        self.assertEqual(self.query("SELECT * FROM credential_traffic_totals"), [])
        self.assertEqual(self.query("SELECT * FROM credential_traffic_history"), [])
        self.assertEqual(self.query("SELECT key FROM manager_metadata WHERE key='clients.trafficHandoff'"), [])

    def test_failed_or_cancelled_remove_restores_execstop_delta_once(self):
        self.cli("add", "bob", "0")
        email = self.seed_active_traffic()
        original = self.box.paths.config.read_bytes()
        failure = self.processes.result(1, stderr="fixture restart failure")
        for exit_code, value, restarts in (
            (1, 175, [failure, failure, self.processes.ok()]),
            (130, 250, [KeyboardInterrupt(), self.processes.ok()]),
        ):
            with self.subTest(exit_code=exit_code):
                observations = []
                self.script_execstop(email, value, activity_locked=True, observations=observations)
                self.processes.script({RESTART: restarts})

                result = self.box.run_cli("client", "remove", "alice")

                self.assertEqual(result.exit_code, exit_code, result.stderr)
                self.assertGreaterEqual(len(observations), 2)
                self.assertEqual(observations, [[("bob",)]] * len(observations))
                self.assertEqual(self.box.paths.config.read_bytes(), original)
                for table in ("traffic_totals", "traffic_history", "credential_traffic_totals", "credential_traffic_history"):
                    self.assertEqual(self.query(f"SELECT incoming_bytes FROM {table} WHERE client_name='alice'"), [(value,)])
                self.assertEqual(self.query("SELECT key FROM manager_metadata WHERE key='clients.trafficHandoff'"), [])

    def test_remove_success_discards_only_removed_client_traffic(self):
        self.cli("add", "bob", "0")
        email = self.seed_active_traffic()
        self.write_during_apply("INSERT INTO traffic_totals(client_name,incoming_bytes) VALUES ('bob',456)")
        self.script_execstop(email, 175, activity_locked=True)

        self.cli("remove", "alice")

        self.assertEqual(self.query("SELECT client_name,incoming_bytes FROM traffic_totals"), [("bob", 456)])
        self.assertEqual(self.query("SELECT * FROM traffic_history WHERE client_name='alice'"), [])
        self.assertEqual(self.query("SELECT key FROM manager_metadata WHERE key='clients.trafficHandoff'"), [])

    def assert_restored_xray_counts_from_new_baseline(self, *command):
        self.cli("add", "bob", "0", "--connection", "vless-reality")
        email = self.seed_active_traffic()
        other_email, = self.query("SELECT email FROM clients WHERE name='bob'")[0]
        self.script_execstop(email, 175)

        def stats(email, value):
            return self.processes.ok(json.dumps({
                "stat": [{"name": f"user>>>{email}>>>traffic>>>uplink", "value": value}],
            }))

        self.processes.script({
            STATS: [stats(email, 175), stats(other_email, 10), stats(email, 200)],
            "systemctl show --property=ActiveState,SubState,NRestarts xray": [
                self.processes.ok("ActiveState=failed\nSubState=failed\nNRestarts=0\n"),
                self.processes.ok("ActiveState=active\nSubState=running\nNRestarts=0\n"),
            ],
        })
        result = self.box.run_cli("client", *command)
        self.assertEqual(result.exit_code, 1, result.stderr)
        self.assertIn("Restored backup:", result.stderr)
        self.assertEqual(self.query(
            "SELECT incoming_bytes,last_runtime_uplink FROM traffic_totals WHERE client_name='alice'"
        ), [(175, 0)])

        sync = self.box.run_cli("traffic-sync", "--quiet")
        self.assertEqual(sync.exit_code, 0, sync.stderr)
        for table in ("traffic_totals", "traffic_history", "credential_traffic_totals", "credential_traffic_history"):
            self.assertEqual(self.query(f"SELECT incoming_bytes FROM {table} WHERE client_name='alice'"), [(375,)])

    def test_remove_rollback_resets_runtime_for_restored_xray(self):
        self.assert_restored_xray_counts_from_new_baseline("remove", "alice")

    def test_move_rollback_resets_runtime_for_restored_xray(self):
        self.cli("add-connection", "Second", "8443", "cdn.example", "firefox", "tcp")
        self.assert_restored_xray_counts_from_new_baseline("move-connection", "alice", "Second")

    def test_move_disabled_client_skips_apply_and_removes_old_credential_traffic(self):
        self.cli("add-connection", "Second", "8443", "cdn.example", "firefox", "tcp")
        self.cli("disable", "alice")
        self.seed_credential_traffic()
        original = self.box.paths.config.read_bytes()
        offset = len(self.processes.calls)

        output = self.cli("move-connection", "alice", "Second")

        self.assertIn("Status: disabled", output)
        self.assertIn("Xray restart is not required for disabled client.", output)
        self.assertIn("vless://", output)
        self.assertNotIn(RESTART, self.processes.calls[offset:])
        self.assertNotIn(self.candidate_test, self.processes.calls[offset:])
        self.assertEqual(self.box.paths.config.read_bytes(), original)
        self.assertEqual(self.query("SELECT connection_tag,enabled FROM client_credentials"), [("vless-reality-2", 0)])
        self.assertEqual(self.query("SELECT * FROM credential_traffic_totals"), [])
        self.assertEqual(self.query("SELECT * FROM credential_traffic_history"), [])

    def test_enable_checks_traffic_written_by_presync(self):
        self.cli("disable", "alice")
        self.write_during_apply(
            "INSERT INTO client_traffic_limits(client_name,period,limit_bytes) VALUES ('alice','daily',100)"
        )
        sync = self.box.paths.sbin_dir / "xray-traffic-sync"
        sync.parent.mkdir(parents=True)
        sync.touch()

        def sync_traffic(_argv, **_kwargs):
            self.assertTrue(lock_is_free(self.box.paths.manager_lock))
            self.write_during_apply("INSERT INTO traffic_totals(client_name,incoming_bytes) VALUES ('alice',123)")
            self.write_during_apply(
                "INSERT INTO traffic_history(client_name,bucket_date,bucket_hour,incoming_bytes) "
                "VALUES ('alice','2026-10-06',12,123)"
            )
            return self.processes.ok()

        self.processes.script({(sync, "--quiet"): sync_traffic})
        offset = len(self.processes.calls)
        result = self.box.run_cli("client", "enable", "alice")

        self.assertEqual(result.exit_code, 1, result.stderr)
        self.assertIn("Traffic limit is exhausted for the current day", result.stderr)
        self.assertEqual(self.processes.calls[offset:], [(str(sync), "--quiet")])
        self.assertEqual(self.query("SELECT enabled FROM clients WHERE name='alice'"), [(0,)])

    def test_enable_checks_traffic_committed_immediately_before_write_session(self):
        self.cli("disable", "alice")
        self.write_during_apply(
            "INSERT INTO client_traffic_limits(client_name,period,limit_bytes) VALUES ('alice','daily',100)"
        )
        write_session = database.write_session

        @contextmanager
        def start_after_traffic_sync():
            self.write_during_apply("INSERT INTO traffic_totals(client_name,incoming_bytes) VALUES ('alice',123)")
            self.write_during_apply(
                "INSERT INTO traffic_history(client_name,bucket_date,bucket_hour,incoming_bytes) "
                "VALUES ('alice','2026-10-06',15,123)"
            )
            with write_session() as conn:
                yield conn

        with mock.patch.object(database, "write_session", start_after_traffic_sync):
            result = self.box.run_cli("client", "enable", "alice")

        self.assertEqual(result.exit_code, 1, result.stderr)
        self.assertIn("Traffic limit is exhausted", result.stderr)
        self.assertEqual(self.query("SELECT enabled FROM clients WHERE name='alice'"), [(0,)])

    def test_cancel_after_sqlite_commit_restores_db_before_apply(self):
        before = self.db_rows()
        original = self.box.paths.config.read_bytes()
        write_session = database.write_session
        interrupted = False

        @contextmanager
        def cancel_after_commit():
            nonlocal interrupted
            with write_session() as conn:
                yield conn
            if not interrupted:
                interrupted = True
                self.assertEqual(self.query("SELECT name FROM clients ORDER BY name"), [("alice",), ("bob",)])
                raise KeyboardInterrupt()

        with mock.patch.object(database, "write_session", cancel_after_commit):
            result = self.box.run_cli("client", "add", "bob", "0")

        self.assertEqual(result.exit_code, 130, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.box.paths.config.read_bytes(), original)
        self.assertEqual(self.db_rows(), before)
        self.assertNotIn(self.candidate_test, self.processes.calls)

    def test_cancel_after_apply_return_restores_both_sides(self):
        before = self.db_rows()
        original = self.box.paths.config.read_bytes()
        apply_config = service.apply_config

        def cancel_after_apply(config, **kwargs):
            apply_config(config, **kwargs)
            self.assertNotEqual(self.box.paths.config.read_bytes(), original)
            raise KeyboardInterrupt()

        with mock.patch.object(service, "apply_config", cancel_after_apply):
            result = self.box.run_cli("client", "add", "bob", "0")

        self.assertEqual(result.exit_code, 130, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.box.paths.config.read_bytes(), original)
        self.assertEqual(self.db_rows(), before)
        self.box.assert_restarted(times=2)

    def test_sqlite_read_error_uses_cli_error_boundary(self):
        self.write_during_apply("DROP TABLE client_traffic_limit_state")
        result = self.box.run_cli("client", "remove", "alice")

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(
            result.stderr, "ERROR: SQLite clients cannot be read: no such table: client_traffic_limit_state\n",
        )
        self.assertNotIn(self.candidate_test, self.processes.calls)

    def test_reports_both_apply_and_compensation_failures(self):
        original = self.box.paths.config.read_bytes()

        def reject(_argv, **_kwargs):
            self.write_during_apply("""
                CREATE TRIGGER prevent_undo BEFORE DELETE ON clients WHEN OLD.name = 'bob'
                BEGIN SELECT RAISE(ABORT, 'fixture compensation failure'); END
            """)
            return self.processes.result(1, stderr="fixture test failure")

        self.processes.script({self.candidate_test: reject})
        result = self.box.run_cli("client", "add", "bob", "0")

        self.assertEqual(result.exit_code, 1, result.stderr)
        self.assertIn("состояние не подтверждено", result.stderr)
        self.assertIn("fixture test failure", result.stderr)
        self.assertIn("fixture compensation failure", result.stderr)
        self.assertEqual(self.box.paths.config.read_bytes(), original)
        self.assertEqual(self.query("SELECT name FROM clients ORDER BY name"), [("alice",), ("bob",)])
