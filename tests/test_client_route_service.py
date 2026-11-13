"""VIREI-131: изменения маршрута и точечная компенсация без рестарта Xray."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import sqlite3
from subprocess import TimeoutExpired
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox
from xray_vps_manager.clients import repository, routes
from xray_vps_manager.core import fs
from xray_vps_manager.db import database
from xray_vps_manager.xray.client_routes import client_balancer_tag


@contextmanager
def prepared_route(*, override=None):
    with Sandbox() as box:
        config = box.config()
        config["outbounds"].extend(
            {"tag": tag, "protocol": "socks", "settings": {"servers": [{"address": "192.0.2.1", "port": 1080}]}}
            for tag in ("cascade-old", "cascade-new", "cascade-other")
        )
        config["routing"]["rules"].append({"type": "field", "network": "tcp,udp", "outboundTag": "cascade-old"})
        db = repository.load_db()
        db["clients"]["bob"] = {
            "id": "00000000-0000-4000-8000-000000000002",
            "connection": "vless-reality", "selectedCascadeTag": "cascade-other", "paymentType": "paid",
        }
        routes.ensure_all_client_route_config(config, db)
        repository.save_db(db)
        box.paths.config.write_text(json.dumps(config))
        balancer = client_balancer_tag("alice", db["clients"]["alice"])
        api = (str(box.paths.xray_bin), "api")
        server = "--server=127.0.0.1:10085"
        read = (*api, "bi", server, "-json", balancer)
        switch = (*api, "bo", server, "-b", balancer, "cascade-new")
        restore = (*api, "bo", server, "-b", balancer, override or "-r")
        candidate = box.paths.config.with_suffix(".json.tmp")
        check = (str(box.paths.xray_bin), "run", "-test", "-format", "json", "-config", str(candidate))
        runtime = {"override": override, "candidates": ["cascade-old"]}

        def get_override(*args, **kwargs):
            target = runtime["override"]
            old = {"target": target} if target else {}
            return box.processes.ok(json.dumps({
                "balancer": {"override": old, "principleTarget": {"tag": runtime["candidates"]}},
            }))

        def set_override(argv, **kwargs):
            runtime["override"] = None if argv[-1] == "-r" else argv[-1]
            return box.processes.ok()

        box.processes.script({check: box.processes.ok(), read: [get_override, get_override],
                              switch: set_override, restore: set_override})
        yield box, candidate, runtime, (check, read, switch, restore)


class ClientRouteServiceTests(unittest.TestCase):
    def assert_compensated(self, box, original, before, *, code=1):
        result = box.run_cli("client", "route", "alice", "cascade-new")
        self.assertEqual(result.exit_code, code, result.stderr)
        self.assertEqual(box.paths.config.read_bytes(), original)
        self.assertEqual(repository.load_db(), before)
        box.assert_restarted(times=0)
        return result

    def test_invariant_failure_stops_before_xray_process_and_backup(self):
        with prepared_route() as (box, candidate, _, _):
            config = box.config()
            config["outbounds"].reverse()
            box.paths.config.write_text(json.dumps(config))
            result = self.assert_compensated(box, box.paths.config.read_bytes(), repository.load_db())
            self.assertIn("direct", result.stderr)
            self.assertEqual(box.processes.calls, [])
            self.assertFalse(candidate.exists())
            self.assertFalse(list(box.paths.config_dir.glob("config.json.bak.*")))

    def test_candidate_errors_and_cancel_leave_working_bytes_and_no_backup(self):
        for error, code in (
            (OSError("unable to run xray"), 1),
            (KeyboardInterrupt(), 130),
            (TimeoutExpired("xray run -test", 30), 1),
        ):
            with self.subTest(error=type(error).__name__), prepared_route() as (box, candidate, _, commands):
                check, *_ = commands
                original = box.paths.config.read_bytes()
                before = repository.load_db()

                def fail(argv, **kwargs):
                    self.assertEqual(box.paths.config.read_bytes(), original)
                    self.assertEqual(repository.load_db()["clients"]["alice"]["selectedCascadeTag"], "cascade-new")
                    self.assertFalse(list(box.paths.config_dir.glob("config.json.bak.*")))
                    raise error

                box.processes.script({check: fail})
                self.assert_compensated(box, original, before, code=code)
                self.assertEqual(box.processes.calls, [check])
                self.assertFalse(candidate.exists())
                self.assertFalse(list(box.paths.config_dir.glob("config.json.bak.*")))

    def test_cancel_at_commit_boundary_compensates_if_sqlite_committed(self):
        for committed in (False, True):
            with self.subTest(committed=committed), prepared_route() as (box, _, _, _):
                original_write = database.write_session
                original = box.paths.config.read_bytes()
                before = repository.load_db()
                writes = 0

                @contextmanager
                def interrupt_first_write(*args, **kwargs):
                    nonlocal writes
                    writes += 1
                    with original_write(*args, **kwargs) as conn:
                        yield conn
                        if writes == 1 and not committed:
                            raise KeyboardInterrupt
                    if writes == 1 and committed:
                        self.assertEqual(repository.load_db()["clients"]["alice"]["selectedCascadeTag"], "cascade-new")
                        raise KeyboardInterrupt

                with mock.patch.object(database, "write_session", interrupt_first_write):
                    self.assert_compensated(box, original, before, code=130)
                self.assertEqual(box.processes.calls, [])
                self.assertFalse(list(box.paths.config_dir.glob("config.json.bak.*")))

    def test_sqlite_rejects_write_before_any_file_or_runtime_changes(self):
        with prepared_route() as (box, _, _, _):
            path = box.paths.manager_db
            with database.write_session(path) as conn:
                conn.execute(
                    """CREATE TRIGGER reject_route BEFORE UPDATE OF selected_cascade_tag ON clients
                       WHEN new.name = 'alice' BEGIN SELECT RAISE(ABORT, 'write refused'); END"""
                )
            result = self.assert_compensated(box, box.paths.config.read_bytes(), repository.load_db())
            self.assertIn("write refused", result.stderr)
            self.assertEqual(box.processes.calls, [])
            self.assertFalse(list(box.paths.config_dir.glob("config.json.bak.*")))

    def test_file_write_failure_before_or_after_rename(self):
        for renamed in (False, True):
            with self.subTest(renamed=renamed), prepared_route() as (box, _, _, commands):
                path = box.paths.config
                original = path.read_bytes()
                before = repository.load_db()
                replace = fs.replace_file

                def interrupted_write(target, data, **kwargs):
                    if target != path or data == original:
                        return replace(target, data, **kwargs)
                    if renamed:
                        replace(target, data, **kwargs)
                    raise OSError("config write refused")

                with mock.patch.object(fs, "replace_file", interrupted_write):
                    result = self.assert_compensated(box, original, before)
                self.assertIn("config write refused", result.stderr)
                self.assertEqual(box.processes.calls, [commands[0], commands[1], commands[1]])

    def test_runtime_read_failure_rolls_back_sqlite_and_config(self):
        with prepared_route() as (box, _, runtime, commands):
            check, read, *_ = commands
            box.processes.script({read: box.processes.result(1, stderr="cannot find tag")})
            result = self.assert_compensated(box, box.paths.config.read_bytes(), repository.load_db())
            self.assertIn("cannot find tag", result.stderr)
            self.assertEqual(box.processes.calls, [check, read])
            self.assertIsNone(runtime["override"])

    def test_runtime_failure_restores_actual_override_or_removes_it(self):
        for previous, error, code in (
            (None, KeyboardInterrupt(), 130),
            ("cascade-other", TimeoutExpired("api bo", 8), 1),
            ("cascade-other", None, 1),
        ):
            with self.subTest(previous=previous, error=error), prepared_route(override=previous) as (box, _, runtime, commands):
                check, read, switch, restore = commands

                def fail_after_switch(*args, **kwargs):
                    runtime["override"] = "cascade-new"
                    if error is not None:
                        raise error
                    return box.processes.result(1, stderr="RPC failed after applying")

                box.processes.script({switch: fail_after_switch})
                self.assert_compensated(box, box.paths.config.read_bytes(), repository.load_db(), code=code)
                self.assertEqual(runtime["override"], previous)
                self.assertEqual(box.processes.calls, [check, read, switch, restore, read])

    def test_rollback_failure_reports_both_errors_and_attempts_other_compensations(self):
        with prepared_route(override="cascade-other") as (box, _, runtime, commands):
            check, read, switch, restore = commands

            def fail(*args, **kwargs):
                runtime["override"] = "cascade-new"
                return box.processes.result(1, stderr="original RPC failure")

            box.processes.script({switch: fail, restore: KeyboardInterrupt()})
            result = self.assert_compensated(box, box.paths.config.read_bytes(), repository.load_db())
            self.assertIn("состояние не подтверждено", result.stderr)
            self.assertIn("original RPC failure", result.stderr)
            self.assertIn("Балансер Xray: KeyboardInterrupt", result.stderr)
            self.assertEqual(runtime["override"], "cascade-new")
            self.assertEqual(box.processes.calls, [check, read, switch, restore])

    def test_restart_that_loaded_new_selector_makes_runtime_rollback_unconfirmed(self):
        with prepared_route() as (box, _, runtime, commands):
            check, read, switch, restore = commands

            def switch_after_restart(*args, **kwargs):
                runtime["candidates"] = box.config()["routing"]["balancers"][0]["selector"]
                runtime["override"] = "cascade-new"
                raise KeyboardInterrupt

            box.processes.script({switch: switch_after_restart})
            result = self.assert_compensated(box, box.paths.config.read_bytes(), repository.load_db())
            self.assertIsNone(runtime["override"])
            self.assertEqual(runtime["candidates"], ["cascade-new"])
            self.assertIn("состояние не подтверждено", result.stderr)
            self.assertIn("KeyboardInterrupt", result.stderr)
            self.assertIn("другое состояние балансера", result.stderr)
            self.assertEqual(box.processes.calls, [check, read, switch, restore, read])

    def test_success_rotates_backups_and_repeat_does_not_touch_xray(self):
        with prepared_route() as (box, _, runtime, commands):
            check, read, switch, _ = commands
            path = box.paths.config
            original = path.read_bytes()
            before = repository.load_db()
            base = datetime(2026, 10, 6, 12)
            for index in range(12):
                second_stamp = (base - timedelta(seconds=index + 1)).strftime("%Y%m%d%H%M%S")
                micro_stamp = (base - timedelta(microseconds=index + 1)).strftime("%Y%m%d%H%M%S%f")
                path.with_name(f"config.json.bak.{second_stamp}").touch()
                path.with_name(f"config.json.bak.telegram.{micro_stamp}").touch()
            collision = path.with_name("config.json.bak.20261006120000000000")
            collision.write_text("previous backup")
            unrelated = path.with_name("config.json.bak.manual")
            unrelated.touch()

            result = box.run_cli("client", "route", "alice", "cascade-new")
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual(runtime["override"], "cascade-new")
            self.assertEqual(collision.read_text(), "previous backup")
            new_backup = path.with_name("config.json.bak.20261006120000000001")
            self.assertEqual(new_backup.read_bytes(), original)
            self.assertEqual(len(list(box.paths.config_dir.glob("config.json.bak.*"))) - 1, 20)
            self.assertTrue(unrelated.exists())
            self.assertEqual(repository.load_db()["clients"]["bob"], before["clients"]["bob"])
            config = box.config()
            self.assertEqual(config["routing"]["rules"][0]["outboundTag"], "api")
            self.assertEqual(config["outbounds"][0]["tag"], "direct")
            self.assertEqual(config["routing"]["balancers"][0]["selector"], ["cascade-new"])
            self.assertNotIn("fallbackTag", config["routing"]["balancers"][0])

            repeated = box.run_cli("client", "route", "alice", "cascade-new")
            self.assertEqual(repeated.exit_code, 0, repeated.stderr)
            self.assertIn("Country already selected:", repeated.stdout)
            self.assertEqual(box.processes.calls, [check, read, switch])
            box.assert_restarted(times=0)
