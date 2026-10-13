"""Регрессия XR-REV-059 ч. 2 и XR-REV-010 (VIREI-71).

`save_config_restart_xray_and_db` откатывает config.json при любой ошибке после рестарта Xray, а смена PORT
отказывает до записи конфига, если порт занят в системе другим процессом, а не только другим Xray inbound.
"""

import errno
import json
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import test_client_manager_lock as client_lock_tests
import test_routing_manager_lock as routing_lock_tests
from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.commands import client as client_command
from xray_vps_manager.commands import menu_reality_actions
from xray_vps_manager.core import process
from xray_vps_manager.xray import config as xray_config

TROJAN_PASSWORD = "p" * 32


def trojan_config_only_client(root: Path) -> None:
    """Trojan-клиент bob есть только в config.json, записи в manager.db нет (XR-REV-059)."""
    config = json.loads((root / "config.json").read_text())
    inbound = client_connections.make_trojan_tls_inbound("trojan-tls", 2083, "/etc/ssl/cert.pem", "/etc/ssl/key.pem")
    inbound["settings"]["clients"].append(
        {"password": TROJAN_PASSWORD, "email": "bob|created=2026-10-01T00:00:00Z", "level": 0}
    )
    config["inbounds"].append(inbound)
    (root / "config.json").write_text(json.dumps(config))


def add_trojan_ws_connection(root: Path) -> None:
    config = json.loads((root / "config.json").read_text())
    config["inbounds"].append(client_connections.make_trojan_ws_inbound("trojan-tls-2", 10100, "/trojan"))
    (root / "config.json").write_text(json.dumps(config))


class SaveConfigRestartRollbackTests(unittest.TestCase):
    """Любая ошибка после записи config.json в xray-client возвращает прежний конфиг и завершает команду с ERROR:."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        client_lock_tests.make_sandbox(self.root)
        self.before = (self.root / "config.json").read_text()

    def new_config(self) -> dict:
        config = json.loads(self.before)
        config["inbounds"][1]["port"] = 9443
        return config

    def save(self, *, save_db_error=None, restart_error=None, restore_error=None, interrupt_after_commit=False):
        """Вызывает save_config_restart_xray_and_db в песочнице; возвращает (результат, stderr, рестарты отката)."""
        restore_restarts = []
        self.db_committed = False

        def restart() -> None:
            if restart_error is not None:
                raise restart_error

        def save_db(db):
            if save_db_error is not None:
                raise save_db_error
            client_repository.save_db(db)
            self.db_committed = True
            if interrupt_after_commit:
                # Ctrl+C после COMMIT, до возврата из save_db.
                raise KeyboardInterrupt()

        def restore_restart(unit, **kwargs):
            restore_restarts.append(unit)
            if restore_error is not None:
                raise restore_error
            return subprocess.CompletedProcess(["systemctl", "restart", unit], 0, "", "")

        stderr = StringIO()
        with ExitStack() as stack:
            stack.enter_context(client_lock_tests.sandbox_patches(self.root, restart=restart))
            stack.enter_context(mock.patch.object(client_command, "save_db", side_effect=save_db))
            stack.enter_context(mock.patch.object(client_command, "restart_systemd_unit", side_effect=restore_restart))
            stack.enter_context(mock.patch("shutil.chown"))
            stack.enter_context(redirect_stderr(stderr))
            db = client_command.load_db()
            try:
                result = client_command.save_config_restart_xray_and_db(self.new_config(), db)
            except BaseException as exc:  # noqa: BLE001 — тест проверяет, что именно вышло наружу
                result = exc
        return result, stderr.getvalue(), restore_restarts

    def stored_config(self) -> str:
        return (self.root / "config.json").read_text()

    def test_save_db_value_error_restores_backup(self) -> None:
        """Критерий приёмки: save_db, бросающий ValueError, → restore_config_backup вызван, код 1 с ERROR:."""
        with mock.patch.object(
            client_command, "restore_config_backup", wraps=client_command.restore_config_backup
        ) as restore:
            result, stderr, restarts = self.save(save_db_error=ValueError("Client has no UUID: bob"))

        self.assertIsInstance(result, SystemExit)
        self.assertEqual(result.code, 1)
        restore.assert_called_once()
        backup = restore.call_args.args[0]
        self.assertEqual(
            stderr.splitlines(),
            [f"ERROR: New config failed: Client has no UUID: bob. Restored backup: {backup}"],
        )
        self.assertEqual(self.stored_config(), self.before)
        self.assertEqual(restarts, ["xray"])

    def test_other_save_db_errors_restore_backup(self) -> None:
        errors = {
            "IntegrityError": sqlite3.IntegrityError("UNIQUE constraint failed: clients.uuid"),
            "RuntimeError": RuntimeError("manager.db is not ready"),
        }
        for title, error in errors.items():
            with self.subTest(title):
                result, stderr, restarts = self.save(save_db_error=error)

                self.assertIsInstance(result, SystemExit)
                self.assertEqual(result.code, 1)
                self.assertTrue(stderr.startswith(f"ERROR: New config failed: {error}. Restored backup: "), stderr)
                self.assertEqual(self.stored_config(), self.before)
                self.assertEqual(restarts, ["xray"])

    def test_restart_failure_still_restores_backup(self) -> None:
        error = process.SystemdUnitStartError("xray", "xray ActiveState=failed SubState=failed NRestarts=0")
        result, stderr, restarts = self.save(restart_error=error)

        self.assertIsInstance(result, SystemExit)
        self.assertEqual(result.code, 1)
        self.assertIn("ActiveState=failed", stderr)
        self.assertIn("Restored backup: ", stderr)
        self.assertEqual(self.stored_config(), self.before)
        self.assertEqual(restarts, ["xray"])

    def test_interrupt_during_restart_restores_backup_and_propagates(self) -> None:
        """Ctrl+C на рестарте и проверке старта Xray: manager.db ещё не тронута, конфиг возвращается."""
        result, stderr, restarts = self.save(restart_error=KeyboardInterrupt())

        self.assertIsInstance(result, KeyboardInterrupt)
        self.assertEqual(stderr, "")
        self.assertEqual(self.stored_config(), self.before)
        self.assertEqual(restarts, ["xray"])

    def test_interrupt_after_db_commit_keeps_new_config(self) -> None:
        """71-R03: прерывание после COMMIT внутри save_db не откатывает один конфиг — config и БД остаются согласованы."""
        result, stderr, restarts = self.save(interrupt_after_commit=True)

        self.assertIsInstance(result, KeyboardInterrupt)
        self.assertEqual(stderr, "")
        self.assertTrue(self.db_committed)
        self.assertEqual(json.loads(self.stored_config())["inbounds"][1]["port"], 9443)
        self.assertEqual(restarts, [])

    def test_restore_failure_reports_both_errors(self) -> None:
        restore_error = process.SystemdUnitStartError("xray", "xray ActiveState=failed SubState=failed NRestarts=0")
        result, stderr, restarts = self.save(
            save_db_error=ValueError("Client has no UUID: bob"),
            restore_error=restore_error,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertEqual(result.code, 1)
        lines = stderr.splitlines()
        self.assertEqual(len(lines), 1, stderr)
        self.assertTrue(lines[0].startswith("ERROR: New config failed: Client has no UUID: bob. "), stderr)
        self.assertIn("also failed: xray did not start after restart", lines[0])
        self.assertEqual(restarts, ["xray"])

    def test_success_keeps_new_config_without_restore(self) -> None:
        result, stderr, restarts = self.save()

        self.assertIsInstance(result, Path)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(self.stored_config())["inbounds"][1]["port"], 9443)
        self.assertEqual(restarts, [])

    def test_disable_trojan_client_only_in_config_restores_config(self) -> None:
        """Сценарий XR-REV-059: disable Trojan-клиента без записи в SQLite падает в save_db — конфиг возвращается."""
        trojan_config_only_client(self.root)
        before = self.stored_config()
        restarts = []
        stderr = StringIO()
        with client_lock_tests.sandbox_patches(self.root), \
            mock.patch.object(client_command, "restart_systemd_unit", side_effect=lambda unit, **_: restarts.append(unit)), \
            mock.patch("shutil.chown"), \
            redirect_stderr(stderr), \
            redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                client_command.cmd_disable("bob")

        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(
            stderr.getvalue().startswith("ERROR: New config failed: Client has no UUID: bob. Restored backup: "),
            stderr.getvalue(),
        )
        self.assertEqual(self.stored_config(), before)
        self.assertEqual(restarts, ["xray"])
        self.assertNotIn("bob", client_lock_tests.db_client_names(self.root))


class FakeProbeSocket:
    def __init__(self, family, log, busy, bind_errors) -> None:
        self.family = family
        self.log = log
        self.busy = busy
        self.bind_errors = bind_errors
        self.options = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def close(self) -> None:
        return None

    def setsockopt(self, level, option, value) -> None:
        self.options.append((level, option, value))

    def bind(self, address) -> None:
        self.log.append((self.family, address, tuple(self.options)))
        if self.family in self.busy:
            raise OSError(errno.EADDRINUSE, "Address already in use")
        if self.family in self.bind_errors:
            raise OSError(self.bind_errors[self.family], "bind failed")


class TcpPortInUseTests(unittest.TestCase):
    """core/process.tcp_port_in_use: пробный bind без listen; занят только при EADDRINUSE."""

    def probe(self, port, host="", *, busy=(), bind_errors=None, unavailable=()):
        log = []

        def factory(family, _kind):
            if family in unavailable:
                raise OSError(errno.EAFNOSUPPORT, "Address family not supported")
            return FakeProbeSocket(family, log, busy, bind_errors or {})

        with mock.patch.object(process.socket, "socket", side_effect=factory):
            return process.tcp_port_in_use(port, host), log

    def test_listening_socket_on_host_is_busy(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        self.assertTrue(process.tcp_port_in_use(port, "127.0.0.1"))

        listener.close()
        self.assertFalse(process.tcp_port_in_use(port, "127.0.0.1"))

    def test_wildcard_host_probes_dual_stack_and_ipv4(self) -> None:
        for host in ("", "0.0.0.0", "::"):
            with self.subTest(host or "listen не задан"):
                busy, log = self.probe(2443, host, busy=(socket.AF_INET,))

                self.assertTrue(busy)
                self.assertEqual([(family, address) for family, address, _ in log], [
                    (socket.AF_INET6, ("::", 2443)),
                    (socket.AF_INET, ("0.0.0.0", 2443)),
                ])
                ipv6_options = log[0][2]
                self.assertIn((socket.SOL_SOCKET, socket.SO_REUSEADDR, 1), ipv6_options)
                self.assertIn((socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0), ipv6_options)

    def test_specific_host_probes_only_that_address(self) -> None:
        busy, log = self.probe(10101, "127.0.0.1")

        self.assertFalse(busy)
        self.assertEqual([(family, address) for family, address, _ in log], [(socket.AF_INET, ("127.0.0.1", 10101))])

    def test_unavailable_ipv6_falls_back_to_ipv4(self) -> None:
        busy, log = self.probe(2443, "", busy=(socket.AF_INET,), unavailable=(socket.AF_INET6,))

        self.assertTrue(busy)
        self.assertEqual([(family, address) for family, address, _ in log], [(socket.AF_INET, ("0.0.0.0", 2443))])

    def test_bind_errors_other_than_in_use_are_not_busy(self) -> None:
        busy, _log = self.probe(
            2443,
            "",
            bind_errors={socket.AF_INET6: errno.EADDRNOTAVAIL, socket.AF_INET: errno.EACCES},
        )

        self.assertFalse(busy)

    def test_unix_socket_listen_is_not_probed(self) -> None:
        for host in ("/run/xray/xhttp.sock", "@xray-xhttp"):
            with self.subTest(host):
                busy, log = self.probe(2443, host)

                self.assertFalse(busy)
                self.assertEqual(log, [])


class ChangedPortListenTests(unittest.TestCase):
    """xray/config.changed_port_listen: адрес для системной проверки нужен только при смене порта подключения."""

    def config(self, listen=None) -> dict:
        inbound = client_lock_tests.reality_inbound("vless-reality", 443)
        if listen is None:
            inbound.pop("listen")
        else:
            inbound["listen"] = listen
        return {"inbounds": [inbound]}

    def test_unchanged_port_is_not_checked(self) -> None:
        self.assertIsNone(xray_config.changed_port_listen(self.config("0.0.0.0"), "vless-reality", 443))

    def test_changed_port_returns_inbound_listen(self) -> None:
        self.assertEqual(xray_config.changed_port_listen(self.config("0.0.0.0"), "vless-reality", 2443), "0.0.0.0")
        self.assertEqual(xray_config.changed_port_listen(self.config("127.0.0.1"), "vless-reality", 2443), "127.0.0.1")
        self.assertEqual(xray_config.changed_port_listen(self.config(), "vless-reality", 2443), "")


class MenuPortBusyTests(routing_lock_tests.RoutingLockTestCase):
    """Меню «Обновить PORT»: порт, занятый в системе (Caddy, другой сервис), отклоняется до записи config.json."""

    BUSY_MESSAGE = "PORT is already in use on this server by another process: 2443"

    def run_menu(self, action, answers, probe):
        events = []
        asked = []

        def record(kind, detail=""):
            events.append((kind, detail))

        def fake_input(prompt=""):
            asked.append(prompt)
            return answers.pop(0)

        with routing_lock_tests.routing_patches(self.root, record=record), \
            mock.patch.object(menu_reality_actions, "tcp_port_in_use", side_effect=probe) as port_probe, \
            mock.patch("builtins.input", side_effect=fake_input), \
            redirect_stdout(StringIO()):
            try:
                action()
                outcome = None
            except SystemExit as exc:
                outcome = exc
        return outcome, events, asked, port_probe

    def test_update_port_refuses_port_busy_in_system_before_writing(self) -> None:
        before = self.snapshot()

        outcome, events, _asked, probe = self.run_menu(
            menu_reality_actions.update_port, ["1", "2443"], lambda *_args: True
        )

        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.code, self.BUSY_MESSAGE)
        self.assertEqual(events, [])
        self.assertEqual(self.snapshot(), before)
        probe.assert_called_once_with(2443, "0.0.0.0")

    def test_update_port_and_sni_refuses_before_asking_sni(self) -> None:
        before = self.snapshot()

        outcome, events, asked, _probe = self.run_menu(
            menu_reality_actions.update_port_and_sni, ["1", "2443", "example.org"], lambda *_args: True
        )

        self.assertEqual(outcome.code, self.BUSY_MESSAGE)
        self.assertEqual(len(asked), 2, "REALITY_SNI не должен спрашиваться после отказа по PORT")
        self.assertEqual(events, [])
        self.assertEqual(self.snapshot(), before)

    def test_port_taken_while_answering_is_refused_under_lock(self) -> None:
        before = self.snapshot()
        answers = iter([False, True])

        outcome, events, _asked, probe = self.run_menu(
            menu_reality_actions.update_port, ["1", "2443"], lambda *_args: next(answers)
        )

        self.assertEqual(outcome.code, self.BUSY_MESSAGE)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(events, [])
        self.assertEqual(self.snapshot(), before)

    def test_free_port_is_applied(self) -> None:
        outcome, events, _asked, probe = self.run_menu(
            menu_reality_actions.update_port, ["1", "2443"], lambda *_args: False
        )

        self.assertIsNone(outcome)
        self.assertEqual(routing_lock_tests.reality_port(self.root), 2443)
        self.assertTrue(events)
        self.assertEqual(probe.call_count, 2)

    def test_unchanged_port_is_not_probed(self) -> None:
        outcome, _events, _asked, probe = self.run_menu(
            menu_reality_actions.update_port_and_sni, ["1", "443", "example.org"], lambda *_args: True
        )

        self.assertIsNone(outcome)
        probe.assert_not_called()
        self.assertEqual(routing_lock_tests.reality_port(self.root), 443)


class TrojanLocalPortBusyTests(unittest.TestCase):
    """xray-client update-trojan-connection --local-port: занятый в системе порт отклоняется до записи config.json."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        client_lock_tests.make_sandbox(self.root)
        add_trojan_ws_connection(self.root)

    def update(self, local_port, probe):
        stderr = StringIO()
        with client_lock_tests.sandbox_patches(self.root), \
            mock.patch.object(client_command, "tcp_port_in_use", side_effect=probe) as port_probe, \
            mock.patch.object(client_command, "update_trojan_caddy_site", return_value=None), \
            redirect_stderr(stderr), \
            redirect_stdout(StringIO()):
            try:
                client_command.cmd_trojan_connection_update(
                    "trojan-tls-2",
                    domain_value="vpn.example.com",
                    local_port_value=local_port,
                )
                outcome = None
            except SystemExit as exc:
                outcome = exc
        return outcome, stderr.getvalue(), port_probe

    def stored_local_port(self) -> int:
        config = json.loads((self.root / "config.json").read_text())
        return next(item["port"] for item in config["inbounds"] if item["tag"] == "trojan-tls-2")

    def test_busy_local_port_is_refused_before_config_write(self) -> None:
        before = (self.root / "config.json").read_text()

        outcome, stderr, probe = self.update("10101", lambda *_args: True)

        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.code, 1)
        self.assertEqual(
            stderr.splitlines(),
            ["ERROR: LOCAL_PORT is already in use on this server by another process: 10101"],
        )
        self.assertEqual((self.root / "config.json").read_text(), before)
        self.assertEqual(list(self.root.glob("config.json.bak*")), [])
        probe.assert_called_once_with(10101, "127.0.0.1")

    def test_free_local_port_is_applied(self) -> None:
        outcome, stderr, probe = self.update("10101", lambda *_args: False)

        self.assertIsNone(outcome, stderr)
        self.assertEqual(self.stored_local_port(), 10101)
        probe.assert_called_once_with(10101, "127.0.0.1")

    def test_unchanged_local_port_is_not_probed(self) -> None:
        outcome, stderr, probe = self.update("10100", lambda *_args: True)

        self.assertIsNone(outcome, stderr)
        probe.assert_not_called()
        self.assertEqual(self.stored_local_port(), 10100)


if __name__ == "__main__":
    unittest.main()
