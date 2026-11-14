"""VIREI-130: порядок записи и компенсация клиентских операций через CLI."""

from contextlib import closing, contextmanager
import json
import os
import signal
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_client_characterization import TABLES, script_processes
from tests.test_client_manager_lock import lock_is_free
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import client_policy_recovery
from xray_vps_manager.clients import service


RELATED_TABLES = (
    "activity_events", "activity_event_risks", "activity_blocklist",
    "activity_blocklist_hits", "activity_capture_clients", "activity_alert_events",
    "activity_client_counters", "activity_client_counter_uniques", "telegram_subscriptions",
)
RESTART = ("systemctl", "restart", "xray")
STOP = ("systemctl", "stop", "xray")
STATS = ("/usr/local/bin/xray", "api", "statsquery", "--server=127.0.0.1:10085", "-pattern", "user>>>")
_START_WORKER = subprocess.Popen


def crash_policy_worker(root, command, stage):
    with Sandbox(existing_config_dir=root) as box, running_xray(box):
        script_processes(box)

        def crash(*_args, **_kwargs):
            os.kill(os.getpid(), signal.SIGKILL)

        if stage == "after-commit":
            with mock.patch.object(service, "apply_config", side_effect=crash):
                result = box.run_cli("client", command)
        else:
            box.processes.script({RESTART: crash})
            result = box.run_cli("client", command)
        raise SystemExit(f"Expected SIGKILL: {result}")


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

    def prepare_policy(self, command):
        with database.write_session() as conn:
            if command == "expire-due":
                conn.execute("UPDATE clients SET expires_at='2026-10-06T00:00:00+03:00' WHERE name='alice'")
            else:
                conn.execute("INSERT INTO client_traffic_limits(client_name,period,limit_bytes) VALUES ('alice','daily',50)")

    def crash_policy(self, command, stage):
        worker = (
            "import sys; from tests.test_client_service_compensation import crash_policy_worker; "
            "crash_policy_worker(*sys.argv[1:])"
        )
        with _START_WORKER(
            [sys.executable, "-c", worker, str(self.box.paths.config_dir), command, stage],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as child:
            try:
                stdout, stderr = child.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                self.fail("Не дождались SIGKILL на контрольном шаге")
            self.assertEqual(child.returncode, -signal.SIGKILL, stdout + stderr)

    def pending_policy(self):
        with database.session() as conn:
            return client_policy_recovery.load(conn)

    def prepare_limit_reset(self):
        self.seed_active_traffic()
        self.prepare_policy("enforce-limits")
        self.cli("enforce-limits")
        with database.write_session() as conn:
            conn.execute("UPDATE client_traffic_limit_state SET exceeded_period='2026-10-05'")
            conn.execute("UPDATE traffic_history SET bucket_date='2026-10-05'")

    def test_limit_reset_retries_after_client_commit_and_sigkill(self):
        self.prepare_limit_reset()
        original = self.box.paths.config.read_bytes()
        self.crash_policy("enforce-limits", "after-commit")

        self.assertEqual(self.query("SELECT enabled FROM clients"), [(1,)])
        self.assertEqual(self.box.paths.config.read_bytes(), original)
        self.assertEqual(set(self.pending_policy()), {"alice"})
        self.assertIn("Re-enabled clients after traffic limit reset: alice", self.cli("enforce-limits"))
        self.assertEqual(self.query("SELECT enabled FROM clients"), [(1,)])
        self.assertIn(b'alice|created=', self.box.paths.config.read_bytes())
        self.assertEqual(self.pending_policy(), {})
        calls = list(self.processes.calls)
        self.assertEqual(self.cli("enforce-limits"), "No traffic limits exceeded or reset.\n")
        self.assertEqual(self.processes.calls, calls)

    def test_limit_reset_after_sigkill_rechecks_fresh_traffic(self):
        self.prepare_limit_reset()
        original = self.box.paths.config.read_bytes()
        self.crash_policy("enforce-limits", "after-commit")
        self.write_during_apply(
            "INSERT INTO traffic_history(client_name,bucket_date,bucket_hour,incoming_bytes) "
            "VALUES ('alice','2026-10-06',15,150)"
        )

        self.assertIn("Disabled clients by traffic limit: alice", self.cli("enforce-limits"))
        self.assertEqual(self.query("SELECT enabled,disabled_reason FROM clients"), [(0, "traffic-limit")])
        self.assertEqual(self.box.paths.config.read_bytes(), original)
        self.assertEqual(self.query("SELECT incoming_bytes FROM traffic_history ORDER BY bucket_date"), [(100,), (150,)])
        self.assertEqual(self.pending_policy(), {})

    def test_retry_after_config_replacement_keeps_other_changes_and_needs_no_backup(self):
        self.prepare_limit_reset()
        self.crash_policy("enforce-limits", "before-restart")
        self.assertIn(b'alice|created=', self.box.paths.config.read_bytes())
        for backup in self.box.paths.config_dir.glob("config.json.bak.*"):
            backup.unlink()
        self.cli("set-payment", "alice", "paid")
        config = self.box.config()
        config["log"]["loglevel"] = "debug"
        updated = json.dumps(config).encode()
        self.box.paths.config.write_bytes(updated)
        restarts_before = self.processes.calls.count(RESTART)

        self.assertIn("Re-enabled clients after traffic limit reset: alice", self.cli("enforce-limits"))
        self.assertEqual(self.query("SELECT payment_type FROM clients"), [("paid",)])
        self.assertEqual(self.box.paths.config.read_bytes(), updated)
        self.box.assert_restarted(times=restarts_before + 1)
        self.assertEqual(self.pending_policy(), {})

    def test_recovery_rejects_expired_access_before_restarting_candidate(self):
        self.prepare_limit_reset()
        self.crash_policy("enforce-limits", "before-restart")
        self.prepare_policy("expire-due")

        def check_restart(*_args, **_kwargs):
            self.assertNotIn(b'alice|created=', self.box.paths.config.read_bytes())
            return self.processes.ok()

        self.processes.script({RESTART: check_restart})
        self.cli("enforce-limits")
        self.assertEqual(self.query("SELECT enabled,disabled_reason FROM clients"), [(0, "expired")])
        self.assertEqual(self.pending_policy(), {})

    def test_failed_restart_keeps_config_that_denies_expired_reactivation(self):
        self.prepare_limit_reset()
        self.crash_policy("enforce-limits", "before-restart")
        self.prepare_policy("expire-due")
        restarted_configs = []

        def fail_restart(*_args, **_kwargs):
            restarted_configs.append(self.box.paths.config.read_bytes())
            return self.processes.result(1, stderr="fixture restart failure")

        self.processes.script({RESTART: fail_restart})
        result = self.box.run_cli("client", "enforce-limits")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(len(restarted_configs), 2)
        self.assertTrue(all(b'alice|created=' not in config for config in restarted_configs))
        self.assertNotIn(b'alice|created=', self.box.paths.config.read_bytes())
        self.assertEqual(self.query("SELECT enabled,disabled_reason FROM clients"), [(0, "expired")])
        self.assertEqual(set(self.pending_policy()), {"alice"})

        self.processes.script({RESTART: self.processes.ok()})
        self.cli("enforce-limits")
        self.assertEqual(self.query("SELECT enabled,disabled_reason FROM clients"), [(0, "expired")])
        self.assertEqual(self.pending_policy(), {})

    def test_cancel_inside_apply_after_callback_compensates_expired_reactivation(self):
        self.prepare_limit_reset()
        self.crash_policy("enforce-limits", "before-restart")
        self.prepare_policy("expire-due")
        config_before = self.box.paths.config.read_bytes()
        db_before = self.db_rows()
        pending_before = self.pending_policy()
        apply_config = service.apply_config

        def interrupt_in_callback(config, *, on_applied, **kwargs):
            def cancel(result):
                on_applied(result)
                self.assertNotIn(b'alice|created=', self.box.paths.config.read_bytes())
                raise KeyboardInterrupt

            return apply_config(config, on_applied=cancel, **kwargs)

        with mock.patch.object(service, "apply_config", side_effect=interrupt_in_callback):
            result = self.box.run_cli("client", "enforce-limits")

        self.assertEqual(result.exit_code, 130, result.stderr)
        self.assertEqual(self.box.paths.config.read_bytes(), config_before)
        self.assertEqual(self.db_rows(), db_before)
        self.assertEqual(self.pending_policy(), pending_before)
        self.cli("enforce-limits")
        self.assertNotIn(b'alice|created=', self.box.paths.config.read_bytes())
        self.assertEqual(self.pending_policy(), {})

    def test_pending_reactivation_does_not_override_later_disable(self):
        self.prepare_limit_reset()
        self.crash_policy("enforce-limits", "after-commit")
        self.cli("disable", "alice")
        self.cli("add", "bob", "0")
        self.write_during_apply("INSERT INTO traffic_totals(client_name) VALUES ('bob')")
        self.write_during_apply(
            "INSERT INTO traffic_history(client_name,bucket_date,bucket_hour,incoming_bytes) "
            "VALUES ('bob','2026-10-06',15,150)"
        )
        self.write_during_apply(
            "INSERT INTO client_traffic_limits(client_name,period,limit_bytes) VALUES ('bob','daily',50)"
        )

        self.assertIn("Disabled clients by traffic limit: bob", self.cli("enforce-limits"))
        self.assertEqual(self.query("SELECT name,enabled FROM clients ORDER BY name"), [("alice", 0), ("bob", 0)])
        self.assertNotIn(b'alice|created=', self.box.paths.config.read_bytes())
        self.assertEqual(self.pending_policy(), {})

    def test_pending_reactivation_preserves_targeted_disable_and_recovers_other_credential(self):
        self.cli("add-trojan-connection", "Trojan", "10443", "trojan.example", "--no-caddy")
        self.cli("add", "alice", "0", "--connection", "trojan-tls")
        self.prepare_limit_reset()
        self.crash_policy("enforce-limits", "after-commit")
        self.assertEqual(set(self.pending_policy()["alice"]), {"vless-reality", "trojan-tls"})

        self.cli("disable", "alice", "--connection", "vless-reality")
        self.assertIn("Re-enabled clients after traffic limit reset: alice", self.cli("enforce-limits"))

        self.assertEqual(
            self.query("SELECT connection_tag,enabled FROM client_credentials ORDER BY connection_tag"),
            [("trojan-tls", 1), ("vless-reality", 0)],
        )
        inbounds = {item["tag"]: item for item in self.box.config()["inbounds"]}
        self.assertEqual(inbounds["vless-reality"]["settings"]["clients"], [])
        self.assertEqual(len(inbounds["trojan-tls"]["settings"]["clients"]), 1)
        self.assertEqual(self.pending_policy(), {})

    def test_pending_reactivation_uses_password_rotated_while_credential_is_absent(self):
        self.cli("add-trojan-connection", "Trojan", "10443", "trojan.example", "--no-caddy")
        self.cli("add", "alice", "0", "--connection", "trojan-tls")
        self.prepare_limit_reset()
        self.crash_policy("enforce-limits", "after-commit")
        self.assertNotIn(b'alice|created=', self.box.paths.config.read_bytes())

        self.cli("rotate-trojan-password", "alice", "--connection", "trojan-tls")
        with database.session() as conn:
            row = conn.execute(
                "SELECT xray_client_json FROM client_credentials "
                "WHERE client_name='alice' AND connection_tag='trojan-tls'"
            ).fetchone()
            password = json.loads(row["xray_client_json"])["password"]

        self.assertIn("Re-enabled clients after traffic limit reset: alice", self.cli("enforce-limits"))
        inbounds = {item["tag"]: item for item in self.box.config()["inbounds"]}
        self.assertEqual(len(inbounds["vless-reality"]["settings"]["clients"]), 1)
        self.assertEqual(inbounds["trojan-tls"]["settings"]["clients"][0]["password"], password)
        self.assertEqual(self.pending_policy(), {})

    def test_retry_preserves_marker_after_restart_failure(self):
        self.prepare_limit_reset()
        self.crash_policy("enforce-limits", "before-restart")
        original = self.box.paths.config.read_bytes()
        failure = self.processes.result(1, stderr="fixture restart failure")
        self.processes.script({RESTART: [failure, failure]})

        result = self.box.run_cli("client", "enforce-limits")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(set(self.pending_policy()), {"alice"})
        self.assertEqual(self.box.paths.config.read_bytes(), original)

        self.processes.script({RESTART: self.processes.ok()})
        self.assertIn("Re-enabled clients after traffic limit reset: alice", self.cli("enforce-limits"))
        self.assertEqual(self.pending_policy(), {})

    def test_limit_reset_uses_manager_calendar_day(self):
        self.seed_active_traffic()
        self.prepare_policy("enforce-limits")
        self.cli("enforce-limits")
        self.assertEqual(self.query("SELECT enabled,disabled_reason FROM clients"), [(0, "traffic-limit")])

        # 21:00 UTC — полночь следующего дня в зоне менеджера.
        for instant, enabled, output in (
            ("2026-10-06T20:59:59Z", 0, "No traffic limits exceeded or reset."),
            ("2026-10-06T21:00:00Z", 1, "Re-enabled clients after traffic limit reset: alice"),
        ):
            with self.subTest(instant=instant), Sandbox(
                existing_config_dir=self.box.paths.config_dir, now=instant,
            ) as next_box, running_xray(next_box):
                script_processes(next_box)
                result = next_box.run_cli("client", "enforce-limits")
                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertIn(output, result.stdout)
                self.assertEqual(self.query("SELECT enabled FROM clients"), [(enabled,)])
                next_box.assert_restarted(times=enabled)

    def test_set_exceeded_limit_syncs_after_commit_and_before_enforcement(self):
        self.seed_active_traffic()
        sync = self.box.paths.sbin_dir / "xray-traffic-sync"
        sync.parent.mkdir(parents=True)
        sync.touch()

        def observe_sync(_argv, **_kwargs):
            self.assertTrue(lock_is_free(self.box.paths.manager_lock))
            self.assertEqual(self.query("SELECT limit_bytes FROM client_traffic_limits"), [(53,)])
            self.assertEqual(self.query("SELECT enabled FROM clients"), [(1,)])
            return self.processes.ok()

        self.processes.script({(str(sync), "--quiet"): observe_sync})
        output = self.cli("set-limit", "alice", "daily", "0.00000005")

        self.assertEqual(self.processes.calls[:2], [(str(sync), "--quiet"), self.candidate_test])
        self.assertLess(output.index("Traffic limit is already exceeded"), output.index("Disabled clients by traffic limit"))
        self.assertEqual(self.query("SELECT enabled,disabled_reason FROM clients"), [(0, "traffic-limit")])

    def test_policy_execstop_persists_traffic_after_client_commit(self):
        email = self.seed_active_traffic()
        self.prepare_policy("expire-due")

        def observe_commit_and_traffic():
            self.assertEqual(self.query("SELECT enabled,disabled_reason FROM clients"), [(0, "expired")])
            self.assertEqual(self.query("SELECT incoming_bytes FROM traffic_totals"), [(175,)])
            self.assertEqual(self.query("SELECT incoming_bytes FROM credential_traffic_history"), [(175,)])

        self.script_execstop(email, 175, after_sync=observe_commit_and_traffic)
        self.assertIn("Disabled expired clients: alice", self.cli("expire-due"))
        self.assertEqual(self.query("SELECT incoming_bytes FROM traffic_history"), [(175,)])
        self.assertEqual(self.query("PRAGMA journal_mode"), [("wal",)])
        self.assertEqual(self.query("PRAGMA user_version"), [(8,)])

    def test_policy_failures_compensate_then_retry(self):
        email = self.seed_active_traffic()
        original = self.box.paths.config.read_bytes()
        failure = self.processes.result(1, stderr="fixture restart failure")
        value = 100
        for command, reason in (("expire-due", "expired"), ("enforce-limits", "traffic-limit")):
            self.prepare_policy(command)
            before = self.box.db_rows("clients", "client_credentials", "client_traffic_limit_state", "reality_connections")

            for exit_code, restarts in (
                (1, [failure, failure, self.processes.ok()]),
                (130, [KeyboardInterrupt(), self.processes.ok()]),
            ):
                with self.subTest(command=command, exit_code=exit_code):
                    value += 25

                    def observe_client():
                        self.assertEqual(self.query("SELECT enabled,disabled_reason FROM clients"), [(0, reason)])

                    self.script_execstop(email, value, after_sync=observe_client)
                    self.processes.script({RESTART: restarts})
                    result = self.box.run_cli("client", command, "--quiet")

                    self.assertEqual(result.exit_code, exit_code, result.stderr)
                    if exit_code == 1:
                        self.assertIn("Restored backup:", result.stderr)
                    else:
                        self.assertEqual(result.stderr, "")
                    self.assertEqual(self.box.paths.config.read_bytes(), original)
                    self.assertEqual(self.box.db_rows(
                        "clients", "client_credentials", "client_traffic_limit_state", "reality_connections",
                    ), before)
                    self.assertEqual(self.query("SELECT incoming_bytes FROM traffic_totals"), [(value,)])

            self.processes.script({RESTART: self.processes.ok()})
            self.cli(command)
            self.assertEqual(self.query("SELECT enabled,disabled_reason FROM clients"), [(0, reason)])
            calls = list(self.processes.calls)
            before_retry = self.db_rows()
            self.cli(command, "--quiet")
            self.assertEqual(self.db_rows(), before_retry)
            self.assertEqual(self.processes.calls, calls)

            self.processes.script({STOP: self.processes.ok()})
            self.box.paths.config.write_bytes(original)
            with database.write_session() as conn:
                conn.execute("UPDATE clients SET expires_at=NULL, expired_at=NULL, disabled_at=NULL, disabled_reason=NULL, enabled=1")
                conn.execute("UPDATE client_credentials SET enabled=1")

    def test_policy_noop_does_not_persist_missing_connection(self):
        with database.write_session() as conn:
            conn.execute("DELETE FROM client_credentials")
            conn.execute("DELETE FROM clients")
            conn.execute("DELETE FROM reality_connections")
            conn.execute("CREATE TRIGGER reject_connection_insert BEFORE INSERT ON reality_connections "
                         "BEGIN SELECT RAISE(ABORT, 'Unexpected connection write'); END")
        before = self.box.paths.config.read_bytes()
        self.assertEqual(self.cli("enforce-limits"), "No traffic limits exceeded or reset.\n")
        self.assertEqual(self.cli("expire-due"), "No expired clients.\n")
        self.assertEqual(self.query("SELECT tag FROM reality_connections"), [])
        self.assertEqual(self.query("SELECT name FROM clients"), [])
        self.assertEqual(self.box.paths.config.read_bytes(), before)
        self.assertEqual(self.processes.calls, [])

    def test_policy_noop_preserves_restored_db_and_needs_no_writer(self):
        # Резервная копия SQLite хранится одним файлом в режиме DELETE.
        restored = database.backup_database(self.box.paths.manager_db)
        self.box.paths.manager_db.write_bytes(restored.read_bytes())
        before = self.box.paths.manager_db.read_bytes()
        for command in ("expire-due", "enforce-limits"):
            with self.subTest(command=command), database.session() as writer:
                with database.transaction(writer, immediate=True):
                    result = self.box.run_cli("client", command)
                    self.assertEqual(result.exit_code, 0, result.stderr)
                    self.assertIn("No ", result.stdout)
        self.assertEqual(self.box.paths.manager_db.read_bytes(), before)
        self.assertEqual(self.query("PRAGMA journal_mode"), [("delete",)])
        self.assertEqual(self.processes.calls, [])

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
