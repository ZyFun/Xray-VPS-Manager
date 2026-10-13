"""Регрессия XR-REV-010: успех рестарта Xray определяется фактическим стартом и открытыми портами."""

import contextlib
import io
import json
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from xray_vps_manager.commands import backup, menu_reality_actions, set_cascade, update, warp
from xray_vps_manager.core import process
from xray_vps_manager.xray import config as xray_config


def completed(args, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=code, stdout=stdout, stderr=stderr)


def show_output(active="active", sub="running", restarts=0):
    return f"ActiveState={active}\nSubState={sub}\nNRestarts={restarts}\n"


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def close(self):
        return None


class FakeClock:
    """Монотонные часы: sleep сдвигает время, чтобы опрос не ждал по-настоящему."""

    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class SimpleClock:
    """Замена модуля time для WARP-проб: sleep не ждёт."""

    @staticmethod
    def sleep(_seconds):
        return None

    @staticmethod
    def time():
        return 0.0

    @staticmethod
    def monotonic():
        return 0.0


class FakeSystemctl:
    """Подменённый systemctl: restart=0, затем заданная последовательность ответов `show`."""

    def __init__(self, show_states, restart_code=0):
        self.show_states = list(show_states)
        self.restart_code = restart_code
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        if command[1] == "restart":
            return completed(command, self.restart_code)
        if command[1] == "show":
            state = self.show_states.pop(0) if len(self.show_states) > 1 else self.show_states[0]
            return completed(command, 0, stdout=show_output(*state))
        return completed(command)


class SequencedSystemctl:
    """systemctl, у которого каждый restart начинает свою последовательность ответов `show`."""

    def __init__(self, per_restart_states):
        self.per_restart_states = [list(states) for states in per_restart_states]
        self.current = []
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        if command[1] == "restart":
            self.current = self.per_restart_states.pop(0)
            return completed(command, 0)
        if command[1] == "show":
            state = self.current.pop(0) if len(self.current) > 1 else self.current[0]
            return completed(command, 0, stdout=show_output(*state))
        return completed(command)


class AutoRestartSystemctl:
    """Старый systemd сохраняет NRestarts при ручном restart из auto-restart (70-R07)."""

    def __init__(self, *, early_restart=False, stop_code=0, reset_code=0, restart_codes=(0,)):
        self.state = ("activating", "auto-restart")
        self.restarts = 8
        self.early_restart = early_restart
        self.stop_code = stop_code
        self.reset_code = reset_code
        self.restart_codes = iter(restart_codes)
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        action = command[1]
        if action == "stop":
            if self.stop_code == 0:
                self.state = ("inactive", "dead")
            return completed(command, self.stop_code, stderr="stop failed" if self.stop_code else "")
        if action == "reset-failed":
            if self.reset_code == 0:
                self.restarts = 0
            return completed(command, self.reset_code, stderr="reset failed" if self.reset_code else "")
        if action == "restart":
            code = next(self.restart_codes)
            self.state = ("activating", "auto-restart") if code else ("active", "running")
            self.restarts += 1 if self.early_restart else 0
            return completed(command, code)
        if action == "show":
            return completed(command, stdout=show_output(*self.state, self.restarts))
        raise AssertionError(command)


def open_socket(address, timeout=None):
    return FakeSocket()


class HangingShowSystemctl:
    """systemctl, у которого `show` после первого restart зависает (TimeoutExpired), а после второго — active."""

    def __init__(self):
        self.restarts = 0
        self.commands = []
        self.show_timeouts = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        if command[1] == "restart":
            self.restarts += 1
            return completed(command, 0)
        if command[1] == "show":
            self.show_timeouts.append(kwargs.get("timeout"))
            if self.restarts == 1:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            return completed(command, 0, stdout=show_output())
        return completed(command)


class RestartVerificationTests(unittest.TestCase):
    def run_restart(self, systemctl, open_ports=None, **kwargs):
        clock = FakeClock()
        open_ports = set(open_ports or ())

        def connect(address, timeout=None):
            if tuple(address) in open_ports:
                return FakeSocket()
            raise ConnectionRefusedError(f"{address} closed")

        with mock.patch.object(process.subprocess, "run", side_effect=systemctl), \
            mock.patch.object(process, "time", clock), \
            mock.patch.object(process.socket, "create_connection", side_effect=connect):
            return process.restart_systemd_unit("xray", **kwargs), clock

    def test_unit_that_fails_after_active_raises(self) -> None:
        """restart=0, первый опрос active, второй failed → исключение, которое ловят существующие except."""
        systemctl = FakeSystemctl([("active", "running", 0), ("failed", "failed", 0)])
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_restart(systemctl, verify_timeout=3.0)
        self.assertIn("failed", str(caught.exception))
        self.assertIn("xray", str(caught.exception))
        self.assertEqual(systemctl.commands[2], ["systemctl", "restart", "xray"])
        self.assertEqual(systemctl.commands[3][:2], ["systemctl", "show"])

    def test_auto_restart_counts_as_failure(self) -> None:
        systemctl = FakeSystemctl([("active", "running", 0), ("activating", "auto-restart", 0)])
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_restart(systemctl, verify_timeout=3.0)
        self.assertIn("auto-restart", str(caught.exception))

    def test_restart_counter_growth_counts_as_failure(self) -> None:
        systemctl = FakeSystemctl([("active", "running", 0), ("active", "running", 1)])
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_restart(systemctl, verify_timeout=3.0)
        self.assertIn("NRestarts", str(caught.exception))

    def test_automatic_restart_before_first_poll_raises(self) -> None:
        """Автоперезапуск до первого show не становится baseline, даже если порт открыт (70-R06)."""
        systemctl = FakeSystemctl([("active", "running", 1)])
        with self.assertRaises(process.SystemdUnitStartError) as caught:
            self.run_restart(
                systemctl,
                open_ports={("127.0.0.1", 443)},
                verify_timeout=3.0,
                ports=[("127.0.0.1", 443)],
            )
        self.assertIn("NRestarts=1", str(caught.exception))
        self.assertIn("automatically", str(caught.exception))

    def test_automatic_restart_before_first_poll_after_retry_raises(self) -> None:
        """После reset-failed и повторного restart первый show с NRestarts=1 — ошибка (70-R06)."""
        systemctl = FakeSystemctl([("active", "running", 1)])
        restart_codes = iter([1, 0])

        def run(command, **kwargs):
            if command[1] == "restart":
                systemctl.commands.append(list(command))
                return completed(command, next(restart_codes))
            return systemctl(command, **kwargs)

        with mock.patch.object(process.subprocess, "run", side_effect=run), \
            mock.patch.object(process, "time", FakeClock()), \
            mock.patch.object(process.socket, "create_connection", side_effect=open_socket):
            with self.assertRaises(process.SystemdUnitStartError):
                process.restart_systemd_unit("xray", ports=[("127.0.0.1", 443)])
        self.assertEqual([item[1] for item in systemctl.commands][:6],
                         ["stop", "reset-failed", "restart", "stop", "reset-failed", "restart"])

    def test_manual_restart_resets_historical_restart_count(self) -> None:
        """Старые автоперезапуски сбрасываются ручным restart и не мешают успешной проверке (70-R06)."""
        systemctl = FakeSystemctl([("active", "running", 8)])

        def run(command, **kwargs):
            if command[1] == "restart":
                systemctl.show_states = [("active", "running", 0)]
            return systemctl(command, **kwargs)

        result, clock = self.run_restart(run, verify_timeout=3.0)
        self.assertEqual(result.returncode, 0)
        self.assertGreaterEqual(clock.now - 100.0, 3.0)

    def test_initial_auto_restart_with_historical_count_recovers(self) -> None:
        """Исходный auto-restart/NRestarts=8 не вызывает ложный отказ после успешного запуска (70-R07)."""
        systemctl = AutoRestartSystemctl()
        result, clock = self.run_restart(
            systemctl, open_ports={("127.0.0.1", 443)}, ports=[("127.0.0.1", 443)],
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(systemctl.restarts, 0)
        self.assertEqual([command[1] for command in systemctl.commands][:3], ["stop", "reset-failed", "restart"])
        self.assertGreaterEqual(clock.now - 100.0, 3.0)

    def test_initial_auto_restart_does_not_hide_new_early_restart(self) -> None:
        """Сброс старого счётчика не скрывает новый автоперезапуск до первого show (70-R06/70-R07)."""
        systemctl = AutoRestartSystemctl(early_restart=True)
        with self.assertRaises(process.SystemdUnitStartError) as caught:
            self.run_restart(systemctl, open_ports={("127.0.0.1", 443)}, ports=[("127.0.0.1", 443)])
        self.assertIn("NRestarts=1", str(caught.exception))

    def test_initial_auto_restart_recovers_after_retry(self) -> None:
        """Повторная попытка также сбрасывает сохранённый счётчик из auto-restart (70-R07)."""
        systemctl = AutoRestartSystemctl(restart_codes=(1, 0))
        result, _clock = self.run_restart(systemctl)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(systemctl.restarts, 0)
        self.assertEqual([command[1] for command in systemctl.commands][:6],
                         ["stop", "reset-failed", "restart", "stop", "reset-failed", "restart"])

    def test_failed_stop_prevents_restart(self) -> None:
        """Неудачная остановка не позволяет считать последующий запуск проверенным (70-R07)."""
        systemctl = AutoRestartSystemctl(stop_code=1)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_restart(systemctl)
        self.assertEqual(caught.exception.cmd, ["systemctl", "stop", "xray"])
        self.assertEqual([command[1] for command in systemctl.commands], ["stop"])

    def test_failed_counter_reset_prevents_restart(self) -> None:
        """Ошибка сброса счётчика поднимает CalledProcessError для отката, без нового запуска (70-R07)."""
        systemctl = AutoRestartSystemctl(reset_code=1)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_restart(systemctl)
        self.assertEqual(caught.exception.cmd, ["systemctl", "reset-failed", "xray"])
        self.assertEqual([command[1] for command in systemctl.commands], ["stop", "reset-failed"])

    def test_closed_port_until_timeout_raises(self) -> None:
        systemctl = FakeSystemctl([("active", "running", 0)])
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_restart(systemctl, open_ports=set(), verify_timeout=2.0, ports=[("127.0.0.1", 443)])
        self.assertIn("127.0.0.1:443", str(caught.exception))

    def test_open_ports_do_not_shorten_observation_window(self) -> None:
        """Открытые порты не завершают проверку раньше окна: порт может слушать чужой процесс (70-R01)."""
        systemctl = FakeSystemctl([("active", "running", 0)])
        result, clock = self.run_restart(
            systemctl,
            open_ports={("127.0.0.1", 443), ("127.0.0.1", 10085)},
            verify_timeout=3.0,
            ports=[("127.0.0.1", 443), ("127.0.0.1", 10085)],
        )
        self.assertEqual(result.returncode, 0)
        self.assertGreaterEqual(clock.now - 100.0, 3.0)
        self.assertGreaterEqual(sum(1 for item in systemctl.commands if item[1] == "show"), 2)

    def test_failure_after_active_with_open_ports_raises(self) -> None:
        """restart=0, первый опрос active, порт отвечает (например, Caddy), затем failed → исключение (70-R01)."""
        systemctl = FakeSystemctl([("active", "running", 0), ("failed", "failed", 0)])
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_restart(
                systemctl,
                open_ports={("127.0.0.1", 443)},
                verify_timeout=3.0,
                ports=[("127.0.0.1", 443)],
            )
        self.assertIn("ActiveState=failed", str(caught.exception))

    def test_without_ports_unit_must_stay_active_for_whole_window(self) -> None:
        systemctl = FakeSystemctl([("active", "running", 0)])
        result, clock = self.run_restart(systemctl, verify_timeout=1.0, poll_interval=0.5)
        self.assertEqual(result.returncode, 0)
        self.assertGreaterEqual(clock.now - 100.0, 1.0)
        self.assertGreaterEqual(sum(1 for item in systemctl.commands if item[1] == "show"), 2)

    def test_verify_timeout_zero_skips_verification(self) -> None:
        systemctl = FakeSystemctl([("failed", "failed", 0)])
        result, _clock = self.run_restart(systemctl, verify_timeout=0)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(systemctl.commands, [["systemctl", "restart", "xray"]])

    def test_verification_runs_after_retry_restart(self) -> None:
        calls = iter([1, 0])
        systemctl = FakeSystemctl([("active", "running", 0), ("failed", "failed", 0)])
        original = systemctl.__call__

        def restart_then_show(command, **kwargs):
            if command[1] == "restart":
                systemctl.commands.append(list(command))
                return completed(command, next(calls))
            return original(command, **kwargs)

        clock = FakeClock()
        with mock.patch.object(process.subprocess, "run", side_effect=restart_then_show), \
            mock.patch.object(process, "time", clock):
            with self.assertRaises(subprocess.CalledProcessError):
                process.restart_systemd_unit("xray", verify_timeout=3.0)
        self.assertEqual([item[1] for item in systemctl.commands][:6],
                         ["stop", "reset-failed", "restart", "stop", "reset-failed", "restart"])
        self.assertEqual(systemctl.commands[-1][:2], ["systemctl", "show"])


class SystemctlTimeoutTests(unittest.TestCase):
    """Зависание systemctl внутри helper'а становится SystemdUnitStartError, а не TimeoutExpired (70-R02)."""

    def test_show_timeout_raises_start_error_bounded_by_window(self) -> None:
        systemctl = HangingShowSystemctl()
        with mock.patch.object(process.subprocess, "run", side_effect=systemctl), \
            mock.patch.object(process, "time", FakeClock()):
            with self.assertRaises(process.SystemdUnitStartError) as caught:
                process.restart_systemd_unit("xray", verify_timeout=3.0)
        self.assertIsInstance(caught.exception, subprocess.CalledProcessError)
        self.assertIn("timed out", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, subprocess.TimeoutExpired)
        self.assertEqual(systemctl.show_timeouts, [3.0])

    def test_show_timeout_has_floor_near_end_of_window(self) -> None:
        systemctl = FakeSystemctl([("active", "running", 0)])
        timeouts = []

        def run(command, **kwargs):
            if command[1] == "show":
                timeouts.append(kwargs.get("timeout"))
            return systemctl(command, **kwargs)

        with mock.patch.object(process.subprocess, "run", side_effect=run), \
            mock.patch.object(process, "time", FakeClock()):
            process.restart_systemd_unit("xray", verify_timeout=3.0, poll_interval=1.0)
        self.assertEqual(timeouts[0], 3.0)
        self.assertTrue(all(timeout >= process.SHOW_TIMEOUT_FLOOR for timeout in timeouts))
        self.assertLessEqual(max(timeouts), 3.0)

    def test_restart_timeout_raises_start_error(self) -> None:
        def run(command, **kwargs):
            if command[1] == "restart":
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            return completed(command)

        with mock.patch.object(process.subprocess, "run", side_effect=run):
            with self.assertRaises(process.SystemdUnitStartError) as caught:
                process.restart_systemd_unit("xray", verify_timeout=3.0)
        self.assertIn("systemctl restart xray timed out", str(caught.exception))

    def test_preparation_timeout_raises_start_error(self) -> None:
        """Таймаут stop или reset-failed до запуска попадает в существующий путь отката (70-R07)."""
        for action in ("stop", "reset-failed"):
            with self.subTest(action=action):
                def run(command, **kwargs):
                    if command[1] == action:
                        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
                    return completed(command)

                with mock.patch.object(process.subprocess, "run", side_effect=run) as mocked:
                    with self.assertRaises(process.SystemdUnitStartError) as caught:
                        process.restart_systemd_unit("xray")
                self.assertIn(f"systemctl {action} xray timed out", str(caught.exception))
                self.assertFalse(any(call.args[0][1] == "restart" for call in mocked.call_args_list))


class InboundListenEndpointsTests(unittest.TestCase):
    def test_tcp_inbounds_map_wildcards_to_loopback(self) -> None:
        config = {
            "inbounds": [
                {"tag": "vless-reality", "listen": "0.0.0.0", "port": 443, "protocol": "vless"},
                {"tag": "trojan-tls", "listen": "127.0.0.1", "port": 10100, "protocol": "trojan",
                 "streamSettings": {"network": "ws"}},
                {"tag": "api", "listen": "127.0.0.1", "port": 10085, "protocol": "dokodemo-door"},
                {"tag": "v6", "listen": "::", "port": 8443, "protocol": "vless"},
                {"tag": "public-ip", "listen": "203.0.113.5", "port": 9443, "protocol": "vless"},
            ]
        }
        self.assertEqual(
            xray_config.inbound_listen_endpoints(config),
            [("127.0.0.1", 443), ("127.0.0.1", 10100), ("127.0.0.1", 10085), ("::1", 8443), ("203.0.113.5", 9443)],
        )

    def test_non_tcp_and_unix_inbounds_are_skipped(self) -> None:
        config = {
            "inbounds": [
                {"tag": "wg", "port": 51820, "protocol": "wireguard"},
                {"tag": "kcp", "port": 444, "protocol": "vless", "streamSettings": {"network": "mkcp"}},
                {"tag": "range", "port": "100-200", "protocol": "vless"},
                {"tag": "unix", "listen": "/run/xray.sock", "protocol": "vless"},
                {"tag": "none", "protocol": "vless"},
                {"tag": "ok", "port": 443, "protocol": "vless", "streamSettings": {"network": "xhttp"}},
            ]
        }
        self.assertEqual(xray_config.inbound_listen_endpoints(config), [("127.0.0.1", 443)])

    def test_protocol_network_without_tcp_is_skipped(self) -> None:
        """settings.network без tcp (dokodemo-door, shadowsocks) не попадает в TCP-пробы (70-R04)."""
        def inbound(tag, port, protocol, network=None):
            item = {"tag": tag, "listen": "127.0.0.1", "port": port, "protocol": protocol, "settings": {}}
            if network is not None:
                item["settings"]["network"] = network
            return item

        config = {
            "inbounds": [
                inbound("dns-udp", 53, "dokodemo-door", "udp"),
                inbound("dns-tcp", 5353, "dokodemo-door", "tcp"),
                inbound("dns-both", 5354, "dokodemo-door", "tcp,udp"),
                inbound("dns-both-spaced", 5355, "dokodemo-door", " UDP , TCP "),
                inbound("dns-list-udp", 5356, "dokodemo-door", ["udp"]),
                inbound("dns-list-both", 5357, "dokodemo-door", ["udp", "tcp"]),
                inbound("ss-udp", 8388, "shadowsocks", "udp"),
                inbound("api", 10085, "dokodemo-door"),
                {"tag": "no-settings", "listen": "127.0.0.1", "port": 10086, "protocol": "dokodemo-door"},
            ]
        }
        self.assertEqual(
            xray_config.inbound_listen_endpoints(config),
            [
                ("127.0.0.1", 5353),
                ("127.0.0.1", 5354),
                ("127.0.0.1", 5355),
                ("127.0.0.1", 5357),
                ("127.0.0.1", 10085),
                ("127.0.0.1", 10086),
            ],
        )


def config_with_port(port):
    return {"inbounds": [{"tag": "vless-reality", "listen": "0.0.0.0", "port": port, "protocol": "vless"}]}


class ApplyConfigRollbackTests(unittest.TestCase):
    """Проверка старта из helper'а через существующие except откатывает конфиг на прежний."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.json"
        self.config_path.write_text(json.dumps(config_with_port(443)))

    def failing_restart(self, calls):
        def restart(unit, **kwargs):
            calls.append((unit, kwargs))
            if len(calls) == 1:
                raise process.SystemdUnitStartError(unit, "xray.service ActiveState=failed")
            return completed(["systemctl", "restart", unit])

        return restart

    def test_menu_reality_apply_config_restores_backup_when_xray_does_not_start(self) -> None:
        calls = []
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(menu_reality_actions, "CONFIG_PATH", self.config_path))
            stack.enter_context(mock.patch.object(menu_reality_actions, "save_config",
                                                  side_effect=lambda config: xray_config.save_config(config, self.config_path)))
            stack.enter_context(mock.patch.object(menu_reality_actions, "run"))
            stack.enter_context(mock.patch.object(menu_reality_actions, "restart_systemd_unit", side_effect=self.failing_restart(calls)))
            stack.enter_context(mock.patch("shutil.chown"))
            with self.assertRaises(SystemExit) as caught:
                menu_reality_actions.apply_config(config_with_port(2443))
        self.assertIn("Restored backup", str(caught.exception))
        self.assertIn("ActiveState=failed", str(caught.exception))
        self.assertEqual(json.loads(self.config_path.read_text())["inbounds"][0]["port"], 443)
        self.assertEqual(calls[0][1]["ports"], [("127.0.0.1", 2443)])
        self.assertEqual(calls[1][1]["ports"], [("127.0.0.1", 443)])

    def test_set_cascade_apply_config_restores_previous_config_when_xray_does_not_start(self) -> None:
        calls = []
        before = config_with_port(443)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(set_cascade, "CONFIG_PATH", self.config_path))
            stack.enter_context(mock.patch.object(set_cascade, "run"))
            stack.enter_context(mock.patch.object(set_cascade, "restart_systemd_unit", side_effect=self.failing_restart(calls)))
            stack.enter_context(mock.patch("shutil.chown"))
            stderr = stack.enter_context(mock.patch.object(set_cascade.sys, "stderr", io.StringIO()))
            with self.assertRaises(SystemExit) as caught:
                set_cascade.apply_config(config_with_port(2443), before)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("Restored previous config", stderr.getvalue())
        self.assertIn("ActiveState=failed", stderr.getvalue())
        self.assertEqual(json.loads(self.config_path.read_text()), before)
        self.assertEqual(calls[0][1]["ports"], [("127.0.0.1", 2443)])
        self.assertEqual(calls[1][1]["ports"], [("127.0.0.1", 443)])

    def test_menu_reality_rolls_back_when_xray_fails_after_active_on_busy_port(self) -> None:
        """Сценарий XR-REV-010 через настоящий helper: PORT → 2443 занят чужим процессом, Xray падает после active."""
        systemctl = SequencedSystemctl([
            [("active", "running", 0), ("failed", "failed", 0)],
            [("active", "running", 0)],
        ])
        clock = FakeClock()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(menu_reality_actions, "CONFIG_PATH", self.config_path))
            stack.enter_context(mock.patch.object(menu_reality_actions, "save_config",
                                                  side_effect=lambda config: xray_config.save_config(config, self.config_path)))
            stack.enter_context(mock.patch.object(menu_reality_actions, "run"))
            stack.enter_context(mock.patch.object(process.subprocess, "run", side_effect=systemctl))
            stack.enter_context(mock.patch.object(process, "time", clock))
            stack.enter_context(mock.patch.object(process.socket, "create_connection", side_effect=open_socket))
            stack.enter_context(mock.patch("shutil.chown"))
            with self.assertRaises(SystemExit) as caught:
                menu_reality_actions.apply_config(config_with_port(2443))
        self.assertIn("Restored backup", str(caught.exception))
        self.assertIn("ActiveState=failed", str(caught.exception))
        self.assertEqual(json.loads(self.config_path.read_text())["inbounds"][0]["port"], 443)
        self.assertEqual([item[1] for item in systemctl.commands].count("restart"), 2)

    def test_menu_reality_rolls_back_when_systemctl_show_times_out(self) -> None:
        """Зависший `systemctl show` после рестарта нового конфига → откат на прежний конфиг (70-R02)."""
        systemctl = HangingShowSystemctl()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(menu_reality_actions, "CONFIG_PATH", self.config_path))
            stack.enter_context(mock.patch.object(menu_reality_actions, "save_config",
                                                  side_effect=lambda config: xray_config.save_config(config, self.config_path)))
            stack.enter_context(mock.patch.object(menu_reality_actions, "run"))
            stack.enter_context(mock.patch.object(process.subprocess, "run", side_effect=systemctl))
            stack.enter_context(mock.patch.object(process, "time", FakeClock()))
            stack.enter_context(mock.patch.object(process.socket, "create_connection", side_effect=open_socket))
            stack.enter_context(mock.patch("shutil.chown"))
            with self.assertRaises(SystemExit) as caught:
                menu_reality_actions.apply_config(config_with_port(2443))
        self.assertIn("Restored backup", str(caught.exception))
        self.assertIn("timed out", str(caught.exception))
        self.assertEqual(json.loads(self.config_path.read_text())["inbounds"][0]["port"], 443)
        self.assertEqual(systemctl.restarts, 2)

    def test_backup_test_and_restart_passes_inbound_ports(self) -> None:
        calls = []
        with mock.patch.object(backup, "CONFIG_PATH", self.config_path), \
            mock.patch.object(backup, "run"), \
            mock.patch.object(backup, "restart_systemd_unit", side_effect=lambda unit, **kw: calls.append((unit, kw))):
            backup.test_and_restart()
        self.assertEqual(calls, [("xray", {"ports": [("127.0.0.1", 443)]})])

    def test_update_restart_xray_passes_inbound_ports_and_propagates_failure(self) -> None:
        calls = []
        with mock.patch.object(update, "CONFIG_PATH", self.config_path), \
            mock.patch.object(update, "restart_systemd_unit", side_effect=self.failing_restart(calls)):
            with self.assertRaises(subprocess.CalledProcessError):
                update.restart_xray()
        self.assertEqual(calls, [("xray", {"ports": [("127.0.0.1", 443)]})])


class WarpProbeStartFailureTests(unittest.TestCase):
    """Сбой старта временного Xray в WARP-пробе — отрицательный результат, а не исключение (70-R05)."""

    DOMAIN = "engage.cloudflareclient.com:2408"
    IPV4 = "162.159.192.1:2408"

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.json"
        self.original = json.dumps(config_with_port(443))
        self.config_path.write_text(self.original)
        self.restarts = []

    def start_failure(self):
        return process.SystemdUnitStartError("xray", "xray ActiveState=failed SubState=failed NRestarts=0")

    def patches(self, stack, restart_effects):
        effects = list(restart_effects)

        def restart_xray():
            self.restarts.append(self.config_path.read_text())
            effect = effects.pop(0)
            if effect is not None:
                raise effect

        trace = subprocess.CompletedProcess(["curl"], 0, stdout="ip=192.0.2.1\nwarp=on\n", stderr="")
        stack.enter_context(mock.patch.object(warp, "CONFIG_PATH", self.config_path))
        stack.enter_context(mock.patch.object(warp, "restart_xray", side_effect=restart_xray))
        stack.enter_context(mock.patch.object(warp, "run"))
        stack.enter_context(mock.patch.object(warp, "install_config_without_backup",
                                              side_effect=lambda config: self.config_path.write_text(json.dumps(config))))
        stack.enter_context(mock.patch.object(warp, "upsert_warp_outbound"))
        stack.enter_context(mock.patch.object(warp, "wait_for_tcp", return_value=True))
        stack.enter_context(mock.patch.object(warp, "cloudflare_trace_via_proxy", return_value=trace))
        stack.enter_context(mock.patch.object(warp, "time", SimpleClock()))
        stack.enter_context(mock.patch.object(warp, "manager_lock", lambda *_a, **_k: contextlib.nullcontext()))
        stack.enter_context(mock.patch("shutil.chown"))
        return stack.enter_context(mock.patch.object(warp.sys, "stderr", io.StringIO()))

    def test_domain_start_failure_falls_back_to_ipv4(self) -> None:
        with ExitStack() as stack:
            self.patches(stack, [self.start_failure(), None, None, None])
            stack.enter_context(mock.patch.object(warp, "profile_endpoint", return_value=self.DOMAIN))
            stack.enter_context(mock.patch.object(warp, "ipv4_endpoint_candidates", return_value=[self.IPV4]))
            stdout = stack.enter_context(mock.patch("sys.stdout", io.StringIO()))
            chosen = warp.choose_working_endpoint(required=True)
        self.assertEqual(chosen, self.IPV4)
        self.assertIn(f"FAIL {self.DOMAIN}", stdout.getvalue())
        self.assertIn("did not start", stdout.getvalue())
        self.assertEqual(len(self.restarts), 4)
        self.assertEqual(self.config_path.read_text(), self.original)

    def test_restore_failure_after_probe_stays_fatal(self) -> None:
        with ExitStack() as stack:
            self.patches(stack, [self.start_failure(), self.start_failure()])
            with self.assertRaises(process.SystemdUnitStartError):
                warp.probe_warp_endpoint(self.DOMAIN)
        self.assertEqual(self.config_path.read_text(), self.original)

    def test_verify_disabled_reports_start_failure(self) -> None:
        with ExitStack() as stack:
            stderr = self.patches(stack, [self.start_failure(), None])
            with self.assertRaises(SystemExit) as caught:
                warp.cmd_verify_disabled()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("Could not verify that WARP is disabled", stderr.getvalue())
        self.assertIn("did not start", stderr.getvalue())
        self.assertEqual(self.config_path.read_text(), self.original)

    def test_warp_test_reports_start_failure(self) -> None:
        with ExitStack() as stack:
            stderr = self.patches(stack, [self.start_failure(), None])
            stack.enter_context(mock.patch("sys.stdout", io.StringIO()))
            with self.assertRaises(SystemExit) as caught:
                warp.test_warp()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("did not start", stderr.getvalue())
        self.assertEqual(len(self.restarts), 2)
        self.assertEqual(self.config_path.read_text(), self.original)


if __name__ == "__main__":
    unittest.main()
