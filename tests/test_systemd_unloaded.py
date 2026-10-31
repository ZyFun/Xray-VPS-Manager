"""VIREI-251: проверяемый рестарт сервиса, выгруженного systemd после stop."""

import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

from xray_vps_manager.core import process, systemd
from tests.test_restart_verification import FakeClock, FakeSocket, completed, config_with_port, show_output
from tests.support.sandbox import Sandbox
from xray_vps_manager.commands import set_cascade


class UnloadedSystemctl:
    """Ранее запущенный сервис теряет состояние и NRestarts при выгрузке после stop."""

    def __init__(self, *, properties=None, show_code=0, stop_code=0, reset_error=None,
                 restart_codes=(0,), early_restart=False, hangs=None):
        self.loaded = True
        self.active = True
        self.restarts = 8
        self.commands = []
        self.options = []
        self.properties = properties or {}
        self.show_code = show_code
        self.stop_code = stop_code
        self.reset_error = reset_error
        self.restart_codes = iter(restart_codes)
        self.early_restart = early_restart
        self.hangs = hangs

    def run(self, command, **kwargs):
        self.commands.append(list(command))
        self.options.append(kwargs)
        action = command[1]
        if action == self.hangs:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if action == "stop":
            if self.stop_code:
                return completed(command, self.stop_code, stderr="stop denied")
            self.active = self.loaded = False
            self.restarts = 0
        elif action == "reset-failed":
            if self.reset_error:
                return completed(command, 1, stderr=self.reset_error)
            if not self.loaded:
                name = command[-1] if command[-1].endswith(".service") else command[-1] + ".service"
                return completed(command, 1, stderr=(
                    f"Failed to reset failed state of unit {name}: Unit {name} not loaded.\n"))
            self.restarts = 0
        elif action == "restart":
            self.loaded = self.active = True
            self.restarts = int(self.early_restart)
            code = next(self.restart_codes)
            return completed(command, code, stderr="start failed" if code else "")
        elif action == "show":
            self.loaded = True  # show загружает существующий unit из файла.
            state = show_output("active" if self.active else "inactive",
                                "running" if self.active else "dead", self.restarts)
            if "LoadState" in command[2]:
                fields = dict(line.split("=", 1) for line in state.splitlines())
                fields.update(LoadState="loaded", MainPID="0", ControlPID="0", Job="")
                fields.update(self.properties)
                state = "".join(f"{key}={value}\n" for key, value in fields.items() if value is not None)
                return completed(command, self.show_code, stdout=state,
                                 stderr="show denied" if self.show_code else "")
            return completed(command, stdout=state)
        else:
            raise AssertionError(command)
        return completed(command)


class UnloadedRestartTests(unittest.TestCase):
    def run_restart(self, ctl, *, open_port=True, unit="xray", **kwargs):
        clock = FakeClock()
        with process.fake(ctl), mock.patch.object(systemd, "time", clock), \
                mock.patch.object(process.socket, "create_connection") as connect:
            if open_port:
                connect.return_value = FakeSocket()
            else:
                connect.side_effect = ConnectionRefusedError()
            result = systemd.restart_unit(unit, ports=[("127.0.0.1", 443)], **kwargs)
        return result, clock

    def test_previously_running_unit_unloaded_after_stop_starts_and_verifies(self):
        """B-фикс VIREI-251: not-loaded после stop больше не обрывает проверяемый запуск."""
        ctl = UnloadedSystemctl()
        result, clock = self.run_restart(ctl)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.args, ["systemctl", "restart", "xray"])
        self.assertGreaterEqual(clock.now - 100.0, 3.0)
        self.assertEqual([cmd[1] for cmd in ctl.commands[:4]], ["stop", "reset-failed", "show", "restart"])
        self.assertEqual(ctl.options[1]["env"]["LC_ALL"], "C")
        self.assertEqual(ctl.options[2]["timeout"], 10)
        self.assertIn("--all", ctl.commands[2])

    def test_explicit_service_name_is_supported(self):
        result, _ = self.run_restart(UnloadedSystemctl(), unit="example.service")
        self.assertEqual(result.args[-1], "example.service")

    def test_unknown_deleted_masked_or_broken_unit_prevents_restart(self):
        for load in ("not-found", "masked", "error", "bad-setting", "", None):
            with self.subTest(load=load):
                ctl = UnloadedSystemctl(properties={"LoadState": load})
                with self.assertRaises(systemd.SystemdUnitStartError) as caught:
                    self.run_restart(ctl)
                detail = str(caught.exception)
                self.assertIn(f"LoadState={load if load is not None else '<missing>'!r}", detail)
                self.assertIn("state after stop not confirmed", detail)
                self.assertIn("Unit xray.service not loaded.", detail)
                self.assertNotIn("restart", [cmd[1] for cmd in ctl.commands])

    def test_missing_or_invalid_zero_properties_prevent_restart(self):
        """Пустое/нечисловое поле нельзя трактовать как подтверждённый ноль."""
        for key in ("NRestarts", "MainPID", "ControlPID"):
            for value in (None, "", "invalid", "1"):
                with self.subTest(key=key, value=value):
                    ctl = UnloadedSystemctl(properties={key: value})
                    with self.assertRaises(systemd.SystemdUnitStartError) as caught:
                        self.run_restart(ctl)
                    self.assertIn(f"{key}={value if value is not None else '<missing>'!r}", str(caught.exception))
                    self.assertNotIn("restart", [cmd[1] for cmd in ctl.commands])

    def test_old_process_or_concurrent_job_prevents_restart(self):
        for fields in ({"ActiveState": "active", "SubState": "running", "MainPID": "321"},
                       {"ActiveState": "activating", "SubState": "auto-restart"},
                       {"ActiveState": "deactivating", "SubState": "stop-sigterm"},
                       {"ActiveState": "failed", "SubState": "failed"},
                       {"SubState": "running"}, {"ActiveState": None}, {"SubState": None},
                       {"Job": None}, {"Job": "invalid"}, {"Job": "42"}):
            with self.subTest(fields=fields):
                ctl = UnloadedSystemctl(properties=fields)
                with self.assertRaises(systemd.SystemdUnitStartError) as caught:
                    self.run_restart(ctl)
                for key, value in fields.items():
                    self.assertIn(f"{key}={value if value is not None else '<missing>'!r}", str(caught.exception))
                self.assertIsInstance(caught.exception, subprocess.CalledProcessError)
                self.assertNotIn("restart", [cmd[1] for cmd in ctl.commands])

    def test_permission_and_other_reset_errors_are_not_ignored(self):
        for error in ("Access denied", "Connection reset by peer", "reset failed",
                      "Failed to reset failed state of unit other.service: Unit other.service not loaded.",
                      "Failed to reset failed state of unit xray.service: Unit xray.service not loaded.\nAccess denied"):
            with self.subTest(error=error):
                ctl = UnloadedSystemctl(reset_error=error)
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.run_restart(ctl)
                self.assertEqual(caught.exception.stderr, error)
                self.assertEqual([cmd[1] for cmd in ctl.commands], ["stop", "reset-failed"])

    def test_stop_failure_prevents_reset_and_start(self):
        ctl = UnloadedSystemctl(stop_code=1)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_restart(ctl)
        self.assertEqual([cmd[1] for cmd in ctl.commands], ["stop"])

    def test_show_failure_does_not_accept_even_safe_looking_stdout(self):
        ctl = UnloadedSystemctl(show_code=1)
        with self.assertRaises(systemd.SystemdUnitStartError) as caught:
            self.run_restart(ctl)
        self.assertIn("show denied", str(caught.exception))
        self.assertNotIn("restart", [cmd[1] for cmd in ctl.commands])

    def test_timeouts_remain_compatible_with_rollback(self):
        for action in ("stop", "reset-failed", "show", "restart"):
            with self.subTest(action=action):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.run_restart(UnloadedSystemctl(hangs=action))
                self.assertIsInstance(caught.exception, systemd.SystemdUnitStartError)
                self.assertIn("timed out", str(caught.exception))

    def test_retry_also_handles_unloaded_unit(self):
        ctl = UnloadedSystemctl(restart_codes=(1, 0))
        result, clock = self.run_restart(ctl)
        self.assertEqual(result.returncode, 0)
        self.assertEqual([cmd[1] for cmd in ctl.commands[:8]],
                         ["stop", "reset-failed", "show", "restart"] * 2)
        self.assertGreaterEqual(clock.now - 100.0, 4.0)

    def test_early_new_restart_is_not_used_as_baseline(self):
        for codes in ((0,), (1, 0)):
            with self.subTest(codes=codes):
                with self.assertRaises(systemd.SystemdUnitStartError) as caught:
                    self.run_restart(UnloadedSystemctl(restart_codes=codes, early_restart=True))
                self.assertIn("NRestarts=1", str(caught.exception))

    def test_start_failure_after_retry_is_propagated(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_restart(UnloadedSystemctl(restart_codes=(1, 2)))
        self.assertEqual(caught.exception.returncode, 2)
        self.assertEqual(caught.exception.stderr, "start failed\nstart failed")

    def test_closed_port_is_still_a_failure(self):
        with self.assertRaises(systemd.SystemdUnitStartError) as caught:
            self.run_restart(UnloadedSystemctl(), open_port=False)
        self.assertIn("ports not open", str(caught.exception))

    def test_zero_verify_timeout_preserves_unverified_retry(self):
        ctl = UnloadedSystemctl(restart_codes=(1, 0), reset_error="Access denied")
        result, _ = self.run_restart(ctl, verify_timeout=0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual([cmd[1] for cmd in ctl.commands], ["restart", "reset-failed", "restart"])

    def test_failure_after_unloaded_recovery_reaches_config_rollback(self):
        """Настоящий сценарий через Sandbox: два отказа запуска → прежний конфиг и успешный откат."""
        ctl = UnloadedSystemctl(restart_codes=(1, 1, 0))
        before = config_with_port(443)
        with Sandbox() as box, mock.patch.object(systemd, "time", FakeClock()), \
                mock.patch.object(process.socket, "create_connection", return_value=FakeSocket()), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            box.paths.config.write_text(json.dumps(before))
            box.processes.script({("systemctl", action, "xray"): ctl.run
                                  for action in ("stop", "reset-failed", "restart")})
            for properties in ("ActiveState,SubState,NRestarts",
                               "LoadState,ActiveState,SubState,NRestarts,MainPID,ControlPID,Job"):
                args = ("--all",) if "LoadState" in properties else ()
                box.processes.script({("systemctl", "show", "--property=" + properties, *args, "xray"): ctl.run})
            box.processes.script({("/usr/local/bin/xray", "run", "-test", "-config", str(box.paths.config)):
                                  box.processes.ok()})
            with self.assertRaises(SystemExit):
                set_cascade.apply_config(config_with_port(2443), before)
            self.assertEqual(json.loads(box.paths.config.read_text()), before)
            self.assertIn("Restored previous config", stderr.getvalue())
        self.assertEqual([cmd[1] for cmd in ctl.commands].count("restart"), 3)


if __name__ == "__main__":
    unittest.main()
