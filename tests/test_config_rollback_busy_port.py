"""Регрессия XR-REV-059 ч. 2 и XR-REV-010 (VIREI-71).

`save_config_restart_xray_and_db` откатывает config.json при любой ошибке после рестарта Xray, а смена PORT
отказывает до записи конфига, если порт занят в системе другим процессом, а не только другим Xray inbound.
"""

import errno
import json
import socket
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from tests import test_client_manager_lock as client_lock_tests
from tests import test_routing_manager_lock as routing_lock_tests
from tests.support.sandbox import Sandbox
from tests.test_restart_verification import FakeClock, show_output
from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.commands import client_connections as client_command
from xray_vps_manager.commands import menu_reality_actions
from xray_vps_manager.core import process, systemd
from xray_vps_manager.db import database
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
        self.box = self.enterContext(Sandbox())
        self.enterContext(mock.patch.object(systemd, "time", FakeClock()))
        self.enterContext(mock.patch("socket.create_connection", return_value=mock.MagicMock()))
        self.before = self.stored_config()
        self.restore_restarts = 0
        self.restart_error = None
        self.restore_error = None
        ok = self.box.processes.ok()

        def restart(_args, **_kwargs):
            restored = self.stored_config() == self.before
            if restored:
                self.restore_restarts += 1
            error = self.restore_error if restored else self.restart_error
            if error is not None:
                raise error
            return ok

        self.box.processes.script({
            (str(self.box.paths.xray_bin), "run", "-test", "-format", "json", "-config", str(self.box.paths.config.with_suffix('.json.tmp'))): ok,
            ("systemctl", "stop", "xray"): ok,
            ("systemctl", "reset-failed", "xray"): ok,
            ("systemctl", "restart", "xray"): restart,
            ("systemctl", "show", "--property=ActiveState,SubState,NRestarts", "xray"):
                self.box.processes.ok(show_output()),
        })

    def stored_config(self) -> str:
        return self.box.paths.config.read_text()

    def save(self, *, invalid_client=False):
        self.restore_restarts = 0
        db = client_repository.load_db()
        config = self.box.config()
        config["inbounds"][1]["port"] = 9443
        db["connections"]["vless-reality"]["port"] = 9443
        if invalid_client:
            db["clients"]["bob"] = {}
        stderr = StringIO()
        with redirect_stderr(stderr):
            try:
                result = client_command.save_config_restart_xray_and_db(config, db)
            except (SystemExit, KeyboardInterrupt) as exc:
                result = exc
        return result, stderr.getvalue()

    def test_save_db_value_error_restores_backup(self) -> None:
        """Критерий приёмки: save_db, бросающий ValueError, → restore_config_backup вызван, код 1 с ERROR:."""
        result, stderr = self.save(invalid_client=True)

        self.assertIsInstance(result, SystemExit)
        self.assertEqual(result.code, 1)
        (backup,) = self.box.paths.config.parent.glob("config.json.bak.*")
        self.assertEqual(backup.read_text(), self.before)
        self.assertEqual(
            stderr.splitlines(),
            [f"ERROR: New config failed: Client has no UUID: bob. Restored backup: {backup}"],
        )
        self.assertEqual(self.stored_config(), self.before)
        self.assertEqual(self.restore_restarts, 1)

    def test_other_save_db_errors_restore_backup(self) -> None:
        errors = {
            "IntegrityError": sqlite3.IntegrityError("UNIQUE constraint failed: clients.uuid"),
            "RuntimeError": RuntimeError("manager.db is not ready"),
        }
        for title, error in errors.items():
            with self.subTest(title), mock.patch.object(database, "write_session", side_effect=error):
                result, stderr = self.save()

                self.assertIsInstance(result, SystemExit)
                self.assertEqual(result.code, 1)
                self.assertTrue(stderr.startswith(f"ERROR: New config failed: {error}. Restored backup: "), stderr)
                self.assertEqual(self.stored_config(), self.before)
                self.assertEqual(self.restore_restarts, 1)

    def test_restart_failure_still_restores_backup(self) -> None:
        self.restart_error = process.SystemdUnitStartError("xray", "xray ActiveState=failed SubState=failed NRestarts=0")
        result, stderr = self.save()

        self.assertIsInstance(result, SystemExit)
        self.assertEqual(result.code, 1)
        self.assertIn("ActiveState=failed", stderr)
        self.assertIn("Restored backup: ", stderr)
        self.assertEqual(self.stored_config(), self.before)
        self.assertEqual(self.restore_restarts, 1)

    def test_interrupt_during_restart_restores_backup_and_propagates(self) -> None:
        """Ctrl+C на рестарте и проверке старта Xray: manager.db ещё не тронута, конфиг возвращается."""
        self.restart_error = KeyboardInterrupt()
        result, stderr = self.save()

        self.assertIsInstance(result, KeyboardInterrupt)
        self.assertEqual(stderr, "")
        self.assertEqual(self.stored_config(), self.before)
        self.assertEqual(self.restore_restarts, 1)

    def test_interrupt_after_db_commit_keeps_new_config(self) -> None:
        """71-R03: прерывание после COMMIT внутри save_db не откатывает один конфиг — config и БД остаются согласованы."""
        write_session = database.write_session

        @contextmanager
        def interrupted_session(*args, **kwargs):
            with write_session(*args, **kwargs) as conn:
                yield conn
            raise KeyboardInterrupt()

        with mock.patch.object(database, "write_session", interrupted_session):
            result, stderr = self.save()

        self.assertIsInstance(result, KeyboardInterrupt)
        self.assertEqual(stderr, "")
        self.assertEqual(client_repository.load_db()["connections"]["vless-reality"]["port"], 9443)
        self.assertEqual(json.loads(self.stored_config())["inbounds"][1]["port"], 9443)
        self.assertEqual(self.restore_restarts, 0)

    def test_restore_failure_reports_both_errors(self) -> None:
        self.restore_error = process.SystemdUnitStartError("xray", "xray ActiveState=failed SubState=failed NRestarts=0")
        result, stderr = self.save(invalid_client=True)

        self.assertIsInstance(result, SystemExit)
        self.assertEqual(result.code, 1)
        lines = stderr.splitlines()
        # VIREI-124: каноническая ошибка отката сохраняет detail отдельными строками.
        self.assertTrue(lines[0].startswith("ERROR: New config failed: Client has no UUID: bob. "), stderr)
        self.assertIn("also failed:", lines[0])
        self.assertIn("xray did not start after restart", stderr)
        self.assertEqual(self.restore_restarts, 1)

    def test_success_keeps_new_config_without_restore(self) -> None:
        result, stderr = self.save()

        self.assertIsInstance(result, Path)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(self.stored_config())["inbounds"][1]["port"], 9443)
        self.assertEqual(self.restore_restarts, 0)

    def test_disable_trojan_client_only_in_config_stops_before_apply(self) -> None:
        """XR-REV-059: запись Trojan-клиента без UUID отклоняется до применения."""
        trojan_config_only_client(self.box.paths.config.parent)
        self.before = self.stored_config()
        result = self.box.run_cli("client", "disable", "bob")

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stderr, "ERROR: Client has no UUID: bob\n")
        self.assertEqual(self.stored_config(), self.before)
        self.box.assert_restarted(times=0)
        self.assertEqual(self.restore_restarts, 0)
        self.assertNotIn("bob", client_repository.load_db()["clients"])


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
        with client_lock_tests.sandbox_patches(self.root) as box, \
            mock.patch.object(process, "tcp_port_in_use", side_effect=probe) as port_probe:
            with database.write_session() as conn:
                client_repository.save_connection(conn, "trojan-tls-2", {
                    "name": "Trojan", "protocol": "trojan", "security": "tls", "transport": "ws", "port": 1443,
                    "publicHost": "vpn.example.com", "localPort": 10100, "publicPort": 1443, "caddy": False,
                })
            result = box.run_cli(
                "client", "update-trojan-connection", "trojan-tls-2",
                "--domain", "vpn.example.com", "--local-port", local_port,
            )
        return result, port_probe

    def stored_local_port(self) -> int:
        config = json.loads((self.root / "config.json").read_text())
        return next(item["port"] for item in config["inbounds"] if item["tag"] == "trojan-tls-2")

    def test_busy_local_port_is_refused_before_config_write(self) -> None:
        before = (self.root / "config.json").read_text()

        result, probe = self.update("10101", lambda *_args: True)

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(
            result.stderr.splitlines(),
            ["ERROR: LOCAL_PORT is already in use on this server by another process: 10101"],
        )
        self.assertEqual((self.root / "config.json").read_text(), before)
        self.assertEqual(list(self.root.glob("config.json.bak*")), [])
        probe.assert_called_once_with(10101, "127.0.0.1")

    def test_free_local_port_is_applied(self) -> None:
        result, probe = self.update("10101", lambda *_args: False)

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.stored_local_port(), 10101)
        probe.assert_called_once_with(10101, "127.0.0.1")

    def test_unchanged_local_port_is_not_probed(self) -> None:
        result, probe = self.update("10100", lambda *_args: True)

        self.assertEqual(result.exit_code, 0, result.stderr)
        probe.assert_not_called()
        self.assertEqual(self.stored_local_port(), 10100)


if __name__ == "__main__":
    unittest.main()
