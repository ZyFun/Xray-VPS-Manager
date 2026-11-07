"""Новый API R3.1: кандидат, полное окно старта, порты и атомарный откат."""

from contextlib import contextmanager
from copy import deepcopy
from itertools import count
import json
import os
from pathlib import Path
import signal
import subprocess
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox
from xray_vps_manager.core import fs, systemd
from xray_vps_manager.core.errors import ApplyConfigError, ConfigInvariantError
from xray_vps_manager.xray import apply as apply_module
from xray_vps_manager.xray.apply import apply_config
from xray_vps_manager.xray.invariants import validate


FIXTURE = Path(__file__).parent / "fixtures/config/reality-single.json"
ACTIVE = "ActiveState=active\nSubState=running\nNRestarts=0\n"
FAILED = "ActiveState=failed\nSubState=failed\nNRestarts=0\n"
SHOW = "systemctl show --property=ActiveState,SubState,NRestarts xray"


class InvariantTests(unittest.TestCase):
    def config(self):
        return json.loads(FIXTURE.read_text())

    def rejected(self, config):
        before = deepcopy(config)
        with self.assertRaises(ConfigInvariantError) as raised:
            validate(config)
        self.assertEqual(raised.exception.stage, "invariants")
        self.assertEqual(config, before)

    def test_default_direct_and_required_blocked(self):
        for mutation in (lambda c: c["outbounds"].reverse(),
                         lambda c: c["outbounds"].pop()):
            with self.subTest(mutation=mutation):
                config = self.config()
                mutation(config)
                self.rejected(config)

    def test_catchall_absent_direct_cascade_warp_and_duplicate(self):
        for tag in (None, "direct", "cascade-fixture", "warp-out"):
            with self.subTest(tag=tag):
                config = self.config()
                if tag:
                    if tag != "direct":
                        config["outbounds"].append({"tag": tag, "protocol": "freedom"})
                    rule = {"type": "field", "network": "tcp,udp", "outboundTag": tag}
                    config["routing"]["rules"].append(rule)
                before = deepcopy(config)
                validate(config)
                self.assertEqual(config, before)
                if tag:
                    config["routing"]["rules"].append(deepcopy(rule))
                    self.rejected(config)

    def test_tags_are_unique_in_each_namespace(self):
        for key in ("inbounds", "outbounds", "balancers"):
            config = self.config()
            collection = config.setdefault("routing", {}).setdefault("balancers", []) if key == "balancers" else config[key]
            collection.extend([{"tag": "duplicate"}, {"tag": "duplicate"}])
            with self.subTest(key=key):
                self.rejected(config)

    def test_api_first_and_references(self):
        mutations = (
            lambda c: c["routing"]["rules"].reverse(),
            lambda c: c.pop("api"),
            lambda c: c["routing"]["rules"].append({"inboundTag": ["missing"], "outboundTag": "direct"}),
            lambda c: c["routing"]["rules"].append({"outboundTag": "missing"}),
            lambda c: c["routing"]["rules"].append({"balancerTag": "missing"}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                config = self.config()
                mutation(config)
                self.rejected(config)

    def routed_config(self):
        config = self.config()
        config["outbounds"].append({"tag": "cascade-fixture", "protocol": "freedom"})
        config["routing"]["balancers"] = [{"tag": "client-route-fixture", "selector": ["cascade-fixture"]}]
        config["routing"]["rules"].extend([
            {"type": "field", "user": ["alice"], "balancerTag": "client-route-fixture"},
            {"type": "field", "network": "tcp,udp", "outboundTag": "cascade-fixture"},
        ])
        return config

    def test_client_balancer_has_existing_outbound_and_cascade_catchall(self):
        config = self.routed_config()
        validate(config)
        for selector in ([], ["missing"], ["cascade-fixture", "missing"]):
            candidate = deepcopy(config)
            candidate["routing"]["balancers"][0]["selector"] = selector
            self.rejected(candidate)
        for tag in (None, "warp-out", "direct"):
            candidate = deepcopy(config)
            if tag is None:
                candidate["routing"]["rules"].pop()
            else:
                candidate["routing"]["rules"][-1]["outboundTag"] = tag
                if tag == "warp-out":
                    candidate["outbounds"].append({"tag": tag, "protocol": "wireguard"})
            self.rejected(candidate)

    def test_protection_rules_precede_warning_when_present(self):
        for field, value in (("protocol", "bittorrent"), ("ip", "geoip:private")):
            config = self.config()
            config["outbounds"].append({"tag": "geoip-warning-fixture", "protocol": "freedom"})
            rules = config["routing"]["rules"]
            rules[:] = rules[:1] + [
                {"type": "field", field: [value], "outboundTag": "blocked"},
                {"type": "field", "ip": ["geoip:ru"], "outboundTag": "geoip-warning-fixture"},
            ]
            validate(config)
            rules[1], rules[2] = rules[2], rules[1]
            self.rejected(config)

    def test_email_requires_only_nonempty_string_for_managed_clients(self):
        for protocol, tag in (("vless", "vless-reality"), ("trojan", "trojan-tls")):
            for email in ("alice", "alice|created=old", "alice|connection=legacy", None, "", " \t", 42):
                config = self.config()
                inbound = config["inbounds"][1]
                inbound.update(protocol=protocol, tag=tag)
                inbound["settings"]["clients"][0]["email"] = email
                with self.subTest(protocol=protocol, email=email):
                    if isinstance(email, str) and email.strip():
                        before = deepcopy(config)
                        validate(config)
                        self.assertEqual(config, before)
                    else:
                        self.rejected(config)

    def test_unmanaged_inbound_email_is_not_restricted(self):
        config = self.config()
        config["inbounds"].append({"tag": "custom-socks", "protocol": "socks", "settings": {"clients": [{}]}})
        validate(config)

    def test_custom_objects_are_not_required_to_have_manager_tags(self):
        config = self.config()
        config["inbounds"].extend([{"protocol": "socks"}, {"protocol": "http"}])
        config["outbounds"].extend([{"protocol": "freedom"}, {"tag": "custom", "protocol": "freedom"}])
        config["routing"]["rules"].append({"type": "field", "network": "tcp,udp", "outboundTag": "custom"})
        validate(config)


@contextmanager
def service(box, *, states=None, restart=None, connect=None):
    box.processes.script({
        "systemctl stop xray": box.processes.ok(),
        "systemctl reset-failed xray": box.processes.ok(),
        "systemctl restart xray": restart or box.processes.ok(),
        SHOW: states or box.processes.ok(ACTIVE),
    })
    with mock.patch("time.monotonic", side_effect=count(0, 0.5).__next__), \
            mock.patch("time.sleep"), mock.patch("socket.create_connection", side_effect=connect) as socket_call:
        yield socket_call


class ApplyTests(unittest.TestCase):
    def candidate(self, box):
        config = box.config()
        config["log"]["loglevel"] = "debug"
        config["inbounds"][1]["port"] = 8443
        return config

    def script_test(self, box, *, result=None):
        original = box.paths.config.read_bytes()
        candidate = box.paths.config.with_suffix(".json.tmp")

        def test(argv, **kwargs):
            self.assertEqual(argv[argv.index("-format") + 1], "json")
            self.assertEqual(box.paths.config.read_bytes(), original)
            self.assertEqual(list(box.paths.config.parent.glob("config.json.bak.*")), [])
            self.assertEqual(candidate.stat().st_mode & 0o777, 0o640)
            return result or box.processes.ok()

        box.processes.script({(str(box.paths.xray_bin), "run", "-test", "-format", "json", "-config", str(candidate)): test})
        return original

    def assert_clean(self, box):
        self.assertFalse(box.paths.config.with_suffix(".json.tmp").exists())
        self.assertEqual(list(box.paths.config.parent.glob(".*.new")), [])

    def test_success_tests_candidate_before_backup_and_verifies_full_window(self):
        with Sandbox() as box:
            original = self.script_test(box)
            candidate = self.candidate(box)
            before = deepcopy(candidate)
            with service(box) as connect:
                result = apply_config(candidate)
            self.assertTrue(result.changed)
            self.assertTrue(result.restarted)
            self.assertEqual(result.verified_ports, (10085, 8443))
            self.assertEqual(result.backup.read_bytes(), original)
            self.assertEqual(result.backup.stat().st_mode & 0o777, 0o640)
            self.assertEqual(box.paths.config.stat().st_mode & 0o777, 0o640)
            self.assertEqual(box.config(), before)
            self.assertEqual(candidate, before)
            self.assertGreaterEqual(sum(cmd[1] == "show" for cmd in box.processes.calls), 3)
            self.assertEqual([c.args[0] for c in connect.call_args_list], [("127.0.0.1", 10085), ("127.0.0.1", 8443)])
            box.assert_restarted(times=1)
            self.assert_clean(box)

    def test_late_failure_after_restart_zero_restores_exact_file_and_old_ports(self):
        with Sandbox() as box:
            original = self.script_test(box)
            snapshots = []
            def restart(argv, **kwargs):
                snapshots.append(box.paths.config.read_bytes())
                return box.processes.ok()
            states = [box.processes.ok(ACTIVE), box.processes.ok(FAILED)] + [box.processes.ok(ACTIVE)] * 4
            with service(box, states=states, restart=restart) as connect, self.assertRaises(ApplyConfigError) as raised:
                apply_config(self.candidate(box))
            error = raised.exception
            self.assertEqual(error.stage, "verify")
            self.assertTrue(error.restored)
            self.assertEqual(box.paths.config.read_bytes(), original)
            self.assertNotEqual(snapshots[0], original)
            self.assertEqual(snapshots[1], original)
            self.assertEqual([c.args[0] for c in connect.call_args_list], [("127.0.0.1", 10085), ("127.0.0.1", 443)])
            self.assert_clean(box)

    def test_interrupt_while_building_success_result_restores_original(self):
        result_type = apply_module.ApplyResult
        for restart in (True, False):
            with self.subTest(restart=restart), Sandbox() as box:
                original = self.script_test(box)

                def interrupted_result(*args, **kwargs):
                    result_type(*args, **kwargs)
                    signal.raise_signal(signal.SIGINT)

                with service(box), mock.patch.object(apply_module, "ApplyResult", side_effect=interrupted_result), \
                        self.assertRaises(ApplyConfigError) as raised:
                    apply_config(self.candidate(box), restart=restart)
                self.assertEqual(raised.exception.exit_code, 130)
                self.assertEqual(raised.exception.restored, restart)
                self.assertEqual(box.paths.config.read_bytes(), original)
                box.assert_restarted(times=2 if restart else 0)
                self.assert_clean(box)

    def test_closed_candidate_port_rolls_back(self):
        with Sandbox() as box:
            original = self.script_test(box)
            def connect(endpoint, **kwargs):
                if endpoint[1] == 8443:
                    raise ConnectionRefusedError("закрыт")
                return mock.MagicMock()
            with service(box, connect=connect), self.assertRaises(ApplyConfigError) as raised:
                apply_config(self.candidate(box))
            self.assertEqual(raised.exception.stage, "verify")
            self.assertTrue(raised.exception.restored)
            self.assertEqual(box.paths.config.read_bytes(), original)
            box.assert_restarted(times=2)

    def test_test_failure_or_timeout_leaves_working_file_untouched(self):
        for failed in ("exit", "timeout"):
            with self.subTest(failed=failed), Sandbox() as box:
                original = self.script_test(box, result=box.processes.result(1, stderr="invalid candidate"))
                if failed == "timeout":
                    cmd = next(iter(box.processes._scripts))
                    box.processes.script({cmd: subprocess.TimeoutExpired(cmd, 30)})
                with self.assertRaises(ApplyConfigError) as raised:
                    apply_config(self.candidate(box))
                self.assertEqual(raised.exception.stage, "test")
                self.assertFalse(raised.exception.restored)
                self.assertIsNone(raised.exception.backup)
                self.assertEqual(box.paths.config.read_bytes(), original)
                self.assertEqual(len(box.processes.calls), 1)
                self.assert_clean(box)

    def test_invalid_invariants_do_not_touch_even_stale_candidate(self):
        with Sandbox() as box:
            original = box.paths.config.read_bytes()
            tmp = box.paths.config.with_suffix(".json.tmp")
            tmp.write_text("previous")
            candidate = self.candidate(box)
            candidate["outbounds"].reverse()
            with self.assertRaises(ApplyConfigError) as raised:
                apply_config(candidate)
            self.assertEqual(raised.exception.stage, "invariants")
            self.assertEqual(tmp.read_text(), "previous")
            self.assertEqual(box.paths.config.read_bytes(), original)
            self.assertEqual(box.processes.calls, [])

    def test_same_bytes_still_test_but_do_not_backup_or_restart(self):
        with Sandbox() as box:
            config = box.config()
            box.paths.config.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
            self.script_test(box)
            result = apply_config(config)
            self.assertFalse(result.changed)
            self.assertIsNone(result.backup)
            self.assertFalse(result.restarted)
            self.assertEqual(result.verified_ports, ())
            self.assertEqual(len(box.processes.calls), 1)
            self.assert_clean(box)

    def test_restart_false_writes_without_systemd(self):
        with Sandbox() as box:
            self.script_test(box)
            result = apply_config(self.candidate(box), restart=False)
            self.assertTrue(result.changed)
            self.assertFalse(result.restarted)
            self.assertEqual(result.verified_ports, ())
            self.assertEqual(len(box.processes.calls), 1)
            self.assert_clean(box)

    def test_restart_failure_retries_then_restores(self):
        with Sandbox() as box:
            original = self.script_test(box)
            attempts = [box.processes.result(1, stderr="restart failed")] * 2 + [box.processes.ok()]
            with service(box, restart=attempts), self.assertRaises(ApplyConfigError) as raised:
                apply_config(self.candidate(box))
            self.assertEqual(raised.exception.stage, "restart")
            self.assertTrue(raised.exception.restored)
            self.assertEqual(box.paths.config.read_bytes(), original)
            box.assert_restarted(times=3)

    def test_cancellation_after_stop_restores_service_and_allows_db_compensation(self):
        for rollback_fails in (False, True):
            with self.subTest(rollback_fails=rollback_fails), Sandbox() as box:
                original = self.script_test(box)
                with service(box):
                    box.processes.script({
                        "systemctl reset-failed xray": [KeyboardInterrupt(), box.processes.ok()],
                        SHOW: box.processes.ok(FAILED if rollback_fails else ACTIVE),
                    })
                    with self.assertRaises(ApplyConfigError) as raised:
                        apply_config(self.candidate(box))
                error = raised.exception
                self.assertEqual(error.stage, "rollback" if rollback_fails else "restart")
                self.assertEqual(error.exit_code, 1 if rollback_fails else 130)
                self.assertEqual(error.restored, not rollback_fails)
                self.assertEqual(box.paths.config.read_bytes(), original)
                self.assertIn("KeyboardInterrupt", error.detail)
                box.assert_restarted(times=1)
                self.assert_clean(box)

    def test_cancellation_during_candidate_test_cleans_up_without_writing(self):
        with Sandbox() as box:
            original = self.script_test(box)
            candidate = box.paths.config.with_suffix(".json.tmp")
            box.processes.script({(str(box.paths.xray_bin), "run", "-test", "-format", "json", "-config", str(candidate)): KeyboardInterrupt()})
            with self.assertRaises(ApplyConfigError) as raised:
                apply_config(self.candidate(box))
            self.assertEqual(raised.exception.stage, "test")
            self.assertEqual(raised.exception.exit_code, 130)
            self.assertEqual(box.paths.config.read_bytes(), original)
            self.assert_clean(box)

    def test_sigint_after_real_config_rename_rolls_back(self):
        for restart in (True, False):
            with self.subTest(restart=restart), Sandbox() as box:
                original = self.script_test(box)
                replace = os.replace
                interrupted = False
                def interrupt(source, destination):
                    nonlocal interrupted
                    replace(source, destination)
                    if destination == box.paths.config and not interrupted:
                        interrupted = True
                        signal.raise_signal(signal.SIGINT)
                with service(box), mock.patch.object(os, "replace", side_effect=interrupt), self.assertRaises(ApplyConfigError) as raised:
                    apply_config(self.candidate(box), restart=restart)
                self.assertTrue(interrupted)
                self.assertEqual(raised.exception.stage, "write")
                self.assertEqual(raised.exception.exit_code, 130)
                self.assertEqual(raised.exception.restored, restart)
                self.assertEqual(box.paths.config.read_bytes(), original)
                box.assert_restarted(times=int(restart))
                self.assert_clean(box)

    def test_rollback_restart_failure_reports_both_errors(self):
        with Sandbox() as box:
            original = self.script_test(box)
            states = [box.processes.ok(FAILED), box.processes.ok("ActiveState=failed\nSubState=auto-restart\nNRestarts=1\n")]
            with service(box, states=states), self.assertRaises(ApplyConfigError) as raised:
                apply_config(self.candidate(box))
            self.assertEqual(raised.exception.stage, "rollback")
            self.assertFalse(raised.exception.restored)
            self.assertIn("Применение:", raised.exception.detail)
            self.assertIn("Откат:", raised.exception.detail)
            self.assertLessEqual(len(raised.exception.detail.splitlines()), 12)
            self.assertEqual(box.paths.config.read_bytes(), original)

    def test_write_error_keeps_original_without_restart(self):
        for target in ("backup", "config"):
            with self.subTest(target=target), Sandbox() as box:
                original = self.script_test(box)
                replace = fs.replace_file
                def fail(destination, data, prepare=None):
                    if (target == "backup" and ".bak." in destination.name) or (target == "config" and destination == box.paths.config):
                        raise OSError("write failed")
                    return replace(destination, data, prepare)
                with mock.patch.object(fs, "replace_file", side_effect=fail), self.assertRaises(ApplyConfigError) as raised:
                    apply_config(self.candidate(box))
                self.assertEqual(raised.exception.stage, "write")
                self.assertEqual(box.paths.config.read_bytes(), original)
                self.assertEqual(len(box.processes.calls), 1)
                self.assert_clean(box)

    def test_rollback_write_failure_preserves_backup_and_reports_failure(self):
        with Sandbox() as box:
            original = self.script_test(box)
            replace = fs.replace_file
            def fail(destination, data, prepare=None):
                if destination == box.paths.config and data == original:
                    raise OSError("restore write failed")
                return replace(destination, data, prepare)
            with service(box, states=box.processes.ok(FAILED)), mock.patch.object(fs, "replace_file", side_effect=fail), self.assertRaises(ApplyConfigError) as raised:
                apply_config(self.candidate(box))
            self.assertEqual(raised.exception.stage, "rollback")
            self.assertFalse(raised.exception.restored)
            self.assertEqual(raised.exception.backup.read_bytes(), original)
            self.assertIn("restore write failed", raised.exception.detail)

    def test_candidate_cleanup_failure_stops_before_write(self):
        with Sandbox() as box:
            original = self.script_test(box)
            unlink = Path.unlink
            def fail(path, *args, **kwargs):
                if path == box.paths.config.with_suffix(".json.tmp"):
                    raise PermissionError("cleanup failed")
                return unlink(path, *args, **kwargs)
            with mock.patch.object(Path, "unlink", fail), self.assertRaises(ApplyConfigError) as raised:
                apply_config(self.candidate(box))
            self.assertEqual(raised.exception.stage, "test")
            self.assertEqual(box.paths.config.read_bytes(), original)
            self.assertEqual(list(box.paths.config.parent.glob("config.json.bak.*")), [])

    def test_only_local_tcp_endpoints_are_probed(self):
        with Sandbox() as box:
            self.script_test(box)
            config = self.candidate(box)
            for tag, listen, network in (("remote", "192.0.2.1", "tcp"), ("udp", "0.0.0.0", "quic"), ("v6", "::", "tcp"), ("unix", "/tmp/xray-fixture.sock", "tcp")):
                config["inbounds"].append({"tag": tag, "protocol": "socks", "port": 1443, "listen": listen, "streamSettings": {"network": network}})
            with service(box) as connect:
                result = apply_config(config)
            self.assertEqual([c.args[0] for c in connect.call_args_list], [("127.0.0.1", 10085), ("127.0.0.1", 8443), ("::1", 1443)])
            self.assertEqual(result.verified_ports, (10085, 8443, 1443))

    def test_reset_failed_unloaded_unit_uses_canonical_recovery(self):
        with Sandbox() as box:
            self.script_test(box)
            with service(box):
                box.processes.script({
                    "systemctl reset-failed xray": box.processes.result(1, stderr="Failed to reset failed state of unit xray.service: Unit xray.service not loaded."),
                    "systemctl show --property=LoadState,ActiveState,SubState,NRestarts,MainPID,ControlPID,Job --all xray": box.processes.ok("LoadState=loaded\nActiveState=inactive\nSubState=dead\nNRestarts=0\nMainPID=0\nControlPID=0\nJob=\n"),
                })
                result = apply_config(self.candidate(box))
            self.assertTrue(result.restarted)
            box.assert_restarted(times=1)

    def test_zero_timeout_reports_no_verified_ports(self):
        with Sandbox() as box:
            self.script_test(box)
            box.processes.script({"systemctl restart xray": box.processes.ok()})
            result = apply_config(self.candidate(box), verify_timeout=0)
            self.assertTrue(result.restarted)
            self.assertEqual(result.verified_ports, ())
            self.assertEqual(len(box.processes.calls), 2)
