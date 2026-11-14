"""T3. Контракты VIREI-124/125 (вариант В) и ротация VIREI-126."""

from contextlib import contextmanager, redirect_stderr
from copy import deepcopy
from io import StringIO
from itertools import count
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox
from xray_vps_manager.routing import blocklist
from xray_vps_manager.clients import repository
from xray_vps_manager.commands import (
    backup, client_connections as client, menu_activity_actions, menu_reality_actions, menu_xray_actions,
    set_bypass, set_cascade, update, warp,
)
from xray_vps_manager.telegram import setup
from xray_vps_manager.core.errors import ApplyConfigError

MIGRATED = (client, menu_reality_actions, menu_xray_actions, blocklist, setup, warp, set_cascade, set_bypass, menu_activity_actions)


@contextmanager
def running_xray(box):
    """Реальный алгоритм проверки старта с ответом systemd и доступными TCP-портами."""
    box.processes.script({
        "systemctl show --property=ActiveState,SubState,NRestarts xray": box.processes.ok(
            "ActiveState=active\nSubState=running\nNRestarts=0\n"
        ),
    })
    with mock.patch("time.monotonic", side_effect=count(0, 4).__next__), \
            mock.patch("time.sleep"), mock.patch("socket.create_connection") as connect:
        yield connect


class CommandJournal:
    """Снимок файла в момент запуска каждой проверки и каждой попытки рестарта."""

    def __init__(self, box, failure, failed_restarts, observe):
        self.entries = []
        self.observe = observe
        self.path = box.paths.config
        self.failure = failure
        self.remaining = failed_restarts
        self.processes = box.processes
        test_args = ("run", "-test", "-config", str(self.path))
        box.processes.script({
            ("/usr/local/bin/xray", *test_args): self.test_config,
            (str(box.paths.xray_bin), *test_args): self.test_config,
            (str(box.paths.xray_bin), "run", "-test", "-format", "json", "-config", str(self.path.with_suffix('.json.tmp'))): self.test_config,
            "systemctl stop xray": box.processes.ok(),
            "systemctl reset-failed xray": box.processes.ok(),
            "systemctl restart xray": self.restart,
        })

    def record(self, kind, argv):
        self.entries.append((kind, self.path.read_bytes()))
        self.observe(kind, argv)

    def test_config(self, argv, **kwargs):
        self.record("test", argv)
        return self.processes.result(1 if self.failure == "test" else 0)

    def restart(self, argv, **kwargs):
        self.record("restart", argv)
        if self.failure == "restart" and self.remaining:
            self.remaining -= 1
            return self.processes.result(1)
        return self.processes.ok()


# Число попыток для отказавшего systemctl restart, до перехода в откат.
WRITERS = (
    (client, SystemExit, 2),
    (blocklist, ApplyConfigError, 2),
    (setup, ApplyConfigError, 2),
    (menu_reality_actions, SystemExit, 2),
    (warp, ApplyConfigError, 2),
    (set_cascade, ApplyConfigError, 2),
    (set_bypass, ApplyConfigError, 2),
    (menu_activity_actions, SystemExit, 2),
    (menu_xray_actions, SystemExit, 2),
)


class ApplyCharacterizationTests(unittest.TestCase):
    def test_config_writers(self):
        """Общий apply проверяет кандидат и возвращает C0; VIREI-126 ротирует только при создании копии."""
        for module, error, attempts in WRITERS:
            for failure in (None, "test", "restart"):
                with self.subTest(module=module.__name__, failure=failure), Sandbox(now="2026-10-06T12:34:56.789123Z") as box:
                    self.check_writer(box, module, error, attempts, failure)

    def check_writer(self, box, module, error, attempts, failure):
        path = box.paths.config
        original = path.read_bytes()
        candidate = json.loads(original)
        candidate["log"]["loglevel"] = "debug"
        candidate["inbounds"][1]["port"] = 8443
        before = json.loads(original)
        before["log"]["loglevel"] = "none"
        before["inbounds"][1]["port"] = 2443
        expected_before = deepcopy(before)
        expected_candidate = deepcopy(candidate)
        if module is client:
            expected_candidate["routing"]["balancers"] = []
        prefix = "config.json.bak."
        new_copy = path.with_name(prefix + "20261006123456789123")
        # В шаге 3 порог ротации — 20 копий.
        older = {path.with_name(prefix + f"20010101{index:012d}") for index in range(25)}
        historical = json.loads(original)
        historical["log"]["loglevel"] = "info"
        for old in older:
            old.write_text(json.dumps(historical))

        copies_at_test = []
        payment_at_command = []

        def observe(kind, argv):
            if kind == "test":
                copies_at_test.append(new_copy.read_bytes() if new_copy.exists() else None)
            if module is client:
                payment_at_command.append(repository.load_db()["clients"]["alice"]["paymentType"])

        journal = CommandJournal(box, failure, attempts, observe)
        restore_dict = module in (set_cascade, set_bypass)

        def apply():
            if module is client:
                db = repository.load_db()
                db["clients"]["alice"]["paymentType"] = "paid"
                return client.save_config_restart_xray_and_db(candidate, db)
            if restore_dict:
                return module.apply_config(candidate, before)
            return module.apply_config(candidate)

        with running_xray(box) as connect, redirect_stderr(StringIO()):
            if failure:
                with self.assertRaises(error) as raised:
                    apply()
                if error is SystemExit:
                    if module in (menu_reality_actions, menu_activity_actions, menu_xray_actions):
                        self.assertIsInstance(raised.exception.code, str)
                        if module in MIGRATED:
                            self.assertIn("New config failed:", raised.exception.code)
                        if module not in MIGRATED or failure != "test":
                            self.assertIn("Restored backup:", raised.exception.code)
                    else:
                        self.assertEqual(raised.exception.code, 1)
            else:
                returned = apply()

        copies = set(path.parent.glob("config.json.bak.*"))
        created = not (module in MIGRATED and failure == "test")
        expected_copies = set(sorted(older)[-19:]) | {new_copy} if created else older
        self.assertEqual(copies, expected_copies)
        self.assertEqual(copies_at_test, [None if module in MIGRATED else original])
        test_args = ("run", "-test", "-format", "json", "-config", str(path.with_suffix('.json.tmp'))) if module in MIGRATED else ("run", "-test", "-config", str(path))
        self.assertEqual(box.processes.calls[0][1:], test_args)
        if created:
            self.assertEqual(new_copy.read_bytes(), original)
        expected = ["test"]
        if failure != "test":
            expected.extend(["restart"] * (attempts if failure == "restart" else 1))
        if failure and created:
            expected.append("restart")
        if module is client:
            self.assertEqual(payment_at_command, ["free"] * len(expected))
            self.assertEqual(repository.load_db()["clients"]["alice"]["paymentType"], "free" if failure else "paid")
        self.assertEqual([kind for kind, _ in journal.entries], expected)
        verified = module in MIGRATED
        service_calls = [cmd[1] for cmd in box.processes.calls if cmd[0] == "systemctl"]
        if verified:
            expected_service = ["stop", "reset-failed", "restart"] * expected.count("restart") + (["show"] if expected.count("restart") else [])
        elif module is setup and failure == "restart":
            expected_service = ["restart", "reset-failed", "restart", "restart"]
        else:
            expected_service = ["restart"] * expected.count("restart")
        self.assertEqual(service_calls, expected_service)
        endpoints = [call.args[0] for call in connect.call_args_list]
        port = 8443
        if failure:
            port = 443
        self.assertEqual(endpoints, [("127.0.0.1", 10085), ("127.0.0.1", port)] if verified and expected.count("restart") else [])
        entries = journal.entries
        if module in MIGRATED:
            self.assertEqual(entries[0][1], original)
            entries = entries[1:]
        candidate_entries = entries[:-1] if failure else entries
        for _, snapshot in candidate_entries:
            self.assertEqual(json.loads(snapshot), expected_candidate)
        if failure:
            self.assertEqual(journal.entries[-1][1], path.read_bytes())
            self.assertEqual(path.read_bytes(), original)
        else:
            self.assertEqual(returned, new_copy)
            self.assertEqual(json.loads(path.read_bytes()), expected_candidate)

    def test_existing_config_in_backup_and_update(self):
        """backup проверяет файл сам; при откате update проверка предшествует restart_xray."""
        for module in (backup, update):
            for failure in (None, "test", "restart"):
                with self.subTest(module=module.__name__, failure=failure), Sandbox() as box:
                    path = box.paths.config
                    original = path.read_bytes()
                    binaries = []

                    def observe(kind, argv):
                        if module is update:
                            if kind == "test":
                                self.assertEqual(Path(argv[0]), box.paths.xray_bin)
                            binaries.append(box.paths.xray_bin.read_bytes())

                    journal = CommandJournal(box, failure, 2, observe)
                    if module is update:
                        source = box.root / "backup"
                        source.mkdir()
                        (source / "xray").write_bytes(b"fixture")
                        box.paths.xray_bin.parent.mkdir(parents=True, exist_ok=True)
                        box.paths.xray_bin.write_bytes(b"installed")
                        action = lambda: update.restore_backup(source)
                    else:
                        action = backup.test_and_restart
                    with running_xray(box) as connect:
                        if failure:
                            with self.assertRaises(ApplyConfigError):
                                action()
                        else:
                            action()
                    expected = ["test"]
                    if failure != "test":
                        expected += ["restart"] * (2 if failure else 1)
                    self.assertEqual(journal.entries, [(cmd, original) for cmd in expected])
                    service_calls = [cmd[1] for cmd in box.processes.calls if cmd[0] == "systemctl"]
                    expected_service = ["stop", "reset-failed", "restart"] * expected.count("restart")
                    self.assertEqual(service_calls, expected_service + ([] if failure else ["show"]))
                    endpoints = [call.args[0] for call in connect.call_args_list]
                    self.assertEqual(endpoints, [] if failure else [("127.0.0.1", 10085), ("127.0.0.1", 443)])
                    self.assertEqual(box.processes.calls[0][1:], ("run", "-test", "-config", str(path)))
                    if module is update:
                        self.assertEqual(binaries, [b"fixture"] * len(expected))
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(list(path.parent.glob("config.json.bak.*")), [])
