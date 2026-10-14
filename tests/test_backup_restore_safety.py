"""Регрессия XR-REV-012 (+ NEW-P2-2): безопасный `xray-backup restore`.

Restore останавливает всех писателей (`WRITER_STOP_UNITS`), запоминает активные units до остановки
и запускает только их, проверяет состав архива до первого копирования, откатывается к pre-restore
бэкапу при любой ошибке (включая `SystemExit`) и работает под `manager.lock`.
"""

import contextlib
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xray_vps_manager.activity import cleanup
from xray_vps_manager.commands import backup
from xray_vps_manager.core import process
from xray_vps_manager.core.errors import LockTimeout, RestoreError

CONFIG_ARCNAME = "usr/local/etc/xray/config.json"
SERVER_ENV_ARCNAME = "usr/local/etc/xray/server.env"
MANAGER_DB_ARCNAME = "usr/local/etc/xray/manager.db"

CURRENT = {
    CONFIG_ARCNAME: '{"marker": "current"}\n',
    SERVER_ENV_ARCNAME: "SERVER_NAME=current\n",
    MANAGER_DB_ARCNAME: "current-db",
}
ARCHIVED = {
    CONFIG_ARCNAME: '{"marker": "archived"}\n',
    SERVER_ENV_ARCNAME: "SERVER_NAME=archived\n",
    MANAGER_DB_ARCNAME: "archived-db",
}


def write_archive(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for arcname, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    return path


class RestoreSandbox:
    """Файлы менеджера во временном каталоге и запись вызовов systemctl, блокировки и шагов restore."""

    def __init__(self, root, *, active=(), restart_outcomes=(), lock_error=None, failing_systemctl=()):
        self.root = Path(root)
        self.config_dir = self.root / "etc" / "xray"
        self.config_dir.mkdir(parents=True)
        self.targets = {
            CONFIG_ARCNAME: self.config_dir / "config.json",
            SERVER_ENV_ARCNAME: self.config_dir / "server.env",
            MANAGER_DB_ARCNAME: self.config_dir / "manager.db",
        }
        for arcname, text in CURRENT.items():
            self.targets[arcname].write_text(text)
        self.backup_dir = self.root / "backups"
        self.active = set(active)
        self.restart_outcomes = list(restart_outcomes)
        self.lock_error = lock_error
        self.failing_systemctl = set(failing_systemctl)
        self.events = []
        self.systemctl_calls = []
        self.restarted = []
        self.pre_backups = []
        self.lock_held = False
        self.on_restart = None

    def files(self):
        return {arcname: path.read_text() for arcname, path in self.targets.items()}

    def make_archive(self, members, name="xray-backup-test.tar.gz"):
        return write_archive(self.root / "incoming" / name, members)

    def systemctl(self, command, **_kwargs):
        command = list(command)
        if command[:1] == ["systemctl"]:
            self.systemctl_calls.append(command)
        if command[:2] == ["systemctl", "is-active"]:
            code = 0 if command[-1] in self.active else 3
            return subprocess.CompletedProcess(command, code, "", "")
        if len(command) > 1 and command[1] in self.failing_systemctl:
            return subprocess.CompletedProcess(command, 1, "", f"systemctl {command[1]} failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    def active_units(self, units, **_kwargs):
        selected = process.active_units(units, runner=self.systemctl)
        self.events.append(("active_units", selected))
        return selected

    def stop_writer_units(self, **_kwargs):
        self.events.append(("stop_writer_units",))
        return process.stop_writer_units(runner=self.systemctl)

    def restart_units(self, units, **_kwargs):
        selected = tuple(units)
        self.restarted.append(selected)
        self.events.append(("restart_units", selected))
        return process.restart_units(selected, runner=self.systemctl)

    @contextlib.contextmanager
    def manager_lock(self, timeout=None, *, purpose=""):
        self.events.append(("lock", purpose))
        if self.lock_error is not None:
            raise self.lock_error
        if self.lock_held:
            # Блокировку держит другой restore: второй процесс дожидается таймаута.
            raise LockTimeout("Другая операция менеджера ещё выполняется: pid=1 cmd=xray-backup restore")
        self.lock_held = True
        try:
            yield
        finally:
            self.lock_held = False
            self.events.append(("unlock",))

    def create_backup(self, path_only=False, quiet=False, sync=True):
        self.events.append(("create_backup", sync))
        path = self.backup_dir / f"xray-backup-pre-restore-{len(self.pre_backups) + 1}.tar.gz"
        write_archive(path, self.files())
        self.pre_backups.append(path)
        return path

    def sync_traffic(self):
        self.events.append(("sync_traffic",))

    def backup_manager_db_before_restore(self):
        self.events.append(("pre_restore_db",))
        return None

    def test_and_restart(self):
        self.events.append(("test_and_restart",))
        hook, self.on_restart = self.on_restart, None
        if hook is not None:
            hook()
        outcome = self.restart_outcomes.pop(0) if self.restart_outcomes else None
        if outcome is not None:
            raise outcome

    def event_names(self):
        return [event[0] for event in self.events]

    def patches(self):
        stack = contextlib.ExitStack()
        backup_files = [(arcname, path, True) for arcname, path in self.targets.items()]
        for name, value in (
            ("CONFIG_DIR", self.config_dir),
            ("CONFIG_PATH", self.targets[CONFIG_ARCNAME]),
            ("SERVER_ENV_PATH", self.targets[SERVER_ENV_ARCNAME]),
            ("MANAGER_DB_PATH", self.targets[MANAGER_DB_ARCNAME]),
            ("BACKUP_DIR", self.backup_dir),
            ("BACKUP_FILES", backup_files),
            ("BACKUP_DIRS", []),
            ("chown_xray", lambda _path: None),
            ("run_capture", self.systemctl),
            ("create_backup", self.create_backup),
            ("sync_traffic", self.sync_traffic),
            ("backup_manager_db_before_restore", self.backup_manager_db_before_restore),
            ("test_and_restart", self.test_and_restart),
        ):
            stack.enter_context(mock.patch.object(backup, name, value))
        # До исправления этих имён в commands/backup.py нет: create=True оставляет тест падать на проверках.
        for name, value in (
            ("active_units", self.active_units),
            ("stop_writer_units", self.stop_writer_units),
            ("restart_units", self.restart_units),
            ("manager_lock", self.manager_lock),
        ):
            stack.enter_context(mock.patch.object(backup, name, value, create=True))
        stack.enter_context(mock.patch.dict(os.environ))
        os.environ.pop("SERVER_ADDR", None)
        return stack

    def restore(self, archive):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = None
        with self.patches(), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                backup.restore_backup(str(archive))
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()


class WriterUnitsTests(unittest.TestCase):
    def test_writer_units_helpers_live_in_core_process(self) -> None:
        self.assertEqual(
            process.WRITER_STOP_UNITS,
            (
                "xray-traffic-sync.timer",
                "xray-raw-log-rotate.timer",
                "xray-client-expire.timer",
                "xray-traffic-sync.service",
                "xray-raw-log-rotate.service",
                "xray-client-expire.service",
                "xray-telegram-poller.service",
            ),
        )
        self.assertIs(cleanup.WRITER_STOP_UNITS, process.WRITER_STOP_UNITS)
        self.assertIs(cleanup.WRITER_RESTART_UNITS, process.WRITER_RESTART_UNITS)
        self.assertIs(cleanup.active_units, process.active_units)
        self.assertIs(cleanup.stop_writer_units, process.stop_writer_units)
        self.assertIs(cleanup.restart_units, process.restart_units)

    def test_stop_writer_units_stops_full_writer_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sandbox = RestoreSandbox(tmp_dir)
            stopped = process.stop_writer_units(runner=sandbox.systemctl)

        self.assertEqual(stopped, process.WRITER_STOP_UNITS)
        self.assertEqual(sandbox.systemctl_calls, [["systemctl", "stop", *process.WRITER_STOP_UNITS]])


class RestoreSafetyTests(unittest.TestCase):
    ACTIVE = ("xray-traffic-sync.timer", "xray-telegram-poller.service")

    def test_successful_restore_stops_writers_under_lock_and_starts_only_active_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sandbox = RestoreSandbox(tmp_dir, active=self.ACTIVE)
            archive = sandbox.make_archive(ARCHIVED)

            code, stdout, stderr = sandbox.restore(archive)

            self.assertIsNone(code, stderr)
            self.assertEqual(sandbox.files(), ARCHIVED)
            self.assertIn("Xray status: active", stdout)
            self.assertIn(["systemctl", "stop", *process.WRITER_STOP_UNITS], sandbox.systemctl_calls)
            self.assertEqual(sandbox.restarted, [self.ACTIVE])
            self.assertIn(["systemctl", "start", *self.ACTIVE], sandbox.systemctl_calls)
            self.assertFalse([call for call in sandbox.systemctl_calls if "enable" in call])
            # Restore не запускает sync (CLI пишут manager.db без блокировки); pre-restore бэкап — под блокировкой,
            # после остановки писателей, без sync (67-R01).
            self.assertEqual(
                sandbox.event_names(),
                [
                    "lock",
                    "active_units",
                    "stop_writer_units",
                    "create_backup",
                    "pre_restore_db",
                    "test_and_restart",
                    "restart_units",
                    "unlock",
                ],
            )
            self.assertIn(("create_backup", False), sandbox.events)
            self.assertIn(("lock", "restore"), sandbox.events)

    def test_second_restore_during_first_writes_nothing_before_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sandbox = RestoreSandbox(tmp_dir, active=self.ACTIVE)
            archive = sandbox.make_archive(ARCHIVED)
            second_archive = sandbox.make_archive(CURRENT, name="xray-backup-second.tar.gz")
            second = {}

            def second_restore():
                events = len(sandbox.events)
                calls = len(sandbox.systemctl_calls)
                try:
                    backup.restore_backup(str(second_archive))
                except SystemExit as exc:
                    second["code"] = exc.code
                second["events"] = sandbox.events[events:]
                second["systemctl"] = sandbox.systemctl_calls[calls:]
                second["files"] = sandbox.files()

            # Второй restore запускается, пока первый держит блокировку и применяет архив.
            sandbox.on_restart = second_restore
            code, _stdout, stderr = sandbox.restore(archive)

            self.assertIsNone(code, stderr)
            self.assertEqual(second["code"], 1)
            # До блокировки второй restore только читает архив: ни sync, ни pre-restore бэкапа, ни systemctl.
            self.assertEqual(second["events"], [("lock", "restore")])
            self.assertEqual(second["systemctl"], [])
            self.assertEqual(second["files"], ARCHIVED)
            self.assertEqual(sandbox.files(), ARCHIVED)

    def test_restart_failure_rolls_back_and_restarts_original_units(self) -> None:
        failures = (
            SystemExit(1),
            RuntimeError("xray failed"),
            subprocess.CalledProcessError(1, ["systemctl", "restart", "xray"], "", "xray failed"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp_dir:
                sandbox = RestoreSandbox(tmp_dir, active=self.ACTIVE, restart_outcomes=[failure])
                archive = sandbox.make_archive(ARCHIVED)

                code, _stdout, stderr = sandbox.restore(archive)

                self.assertEqual(code, 1)
                self.assertEqual(sandbox.files(), CURRENT)
                self.assertEqual(sandbox.event_names().count("test_and_restart"), 2)
                self.assertEqual(sandbox.restarted, [self.ACTIVE])
                self.assertFalse([call for call in sandbox.systemctl_calls if "enable" in call])
                self.assertIn("Rolling back to pre-restore backup", stderr)
                self.assertIn("Restore failed", stderr)
                # Сообщение и код выхода — после запуска units и снятия блокировки.
                self.assertEqual(sandbox.event_names()[-2:], ["restart_units", "unlock"])

    def test_rollback_failure_still_restarts_original_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sandbox = RestoreSandbox(
                tmp_dir,
                active=self.ACTIVE,
                restart_outcomes=[RuntimeError("restored config failed"), RuntimeError("rollback config failed")],
            )
            archive = sandbox.make_archive(ARCHIVED)

            code, _stdout, stderr = sandbox.restore(archive)

            self.assertEqual(code, 1)
            self.assertEqual(sandbox.restarted, [self.ACTIVE])
            self.assertIn("restored config failed", stderr)
            self.assertIn("rollback config failed", stderr)
            self.assertNotIn("Traceback", stderr)

    def test_archive_without_manager_db_is_rejected_before_any_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sandbox = RestoreSandbox(tmp_dir, active=self.ACTIVE)
            members = dict(ARCHIVED)
            members.pop(MANAGER_DB_ARCNAME)
            archive = sandbox.make_archive(members)

            code, _stdout, stderr = sandbox.restore(archive)

            self.assertEqual(code, 1)
            self.assertEqual(sandbox.files(), CURRENT)
            self.assertIn(MANAGER_DB_ARCNAME, stderr)
            self.assertNotIn("create_backup", sandbox.event_names())
            self.assertFalse([call for call in sandbox.systemctl_calls if call[1] in ("stop", "start", "enable")])

    def test_apply_restore_checks_required_members_before_copying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sandbox = RestoreSandbox(tmp_dir)
            temp_dir = Path(tmp_dir) / "extracted"
            for arcname, text in ARCHIVED.items():
                if arcname == MANAGER_DB_ARCNAME:
                    continue
                (temp_dir / arcname).parent.mkdir(parents=True, exist_ok=True)
                (temp_dir / arcname).write_text(text)

            with sandbox.patches():
                with self.assertRaises(RestoreError) as caught:
                    backup.apply_restore(temp_dir)

            self.assertIn(MANAGER_DB_ARCNAME, str(caught.exception))
            self.assertEqual(sandbox.files(), CURRENT)

    def test_role_with_disabled_timers_and_poller_keeps_them_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sandbox = RestoreSandbox(tmp_dir, active=())
            archive = sandbox.make_archive(ARCHIVED)

            code, _stdout, stderr = sandbox.restore(archive)

            self.assertIsNone(code, stderr)
            self.assertFalse([call for call in sandbox.systemctl_calls if call[1] in ("start", "enable")])
            self.assertEqual(sandbox.files(), ARCHIVED)
            self.assertEqual(sandbox.restarted, [()])

    def test_lock_timeout_aborts_before_any_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_error = LockTimeout(
                "Другая операция менеджера ещё выполняется: pid=1 cmd=xray-client add",
                hint="Дождись её завершения и повтори команду.",
            )
            sandbox = RestoreSandbox(tmp_dir, active=self.ACTIVE, lock_error=lock_error)
            archive = sandbox.make_archive(ARCHIVED)

            code, _stdout, stderr = sandbox.restore(archive)

            self.assertEqual(code, 1)
            self.assertEqual(sandbox.files(), CURRENT)
            self.assertIn("Другая операция менеджера ещё выполняется", stderr)
            self.assertEqual(sandbox.event_names(), ["lock"])
            self.assertFalse([call for call in sandbox.systemctl_calls if call[1] != "is-active"])

    def test_unit_start_failure_after_successful_restore_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sandbox = RestoreSandbox(tmp_dir, active=self.ACTIVE, failing_systemctl=("start",))
            archive = sandbox.make_archive(ARCHIVED)

            code, stdout, stderr = sandbox.restore(archive)

            self.assertEqual(code, 1)
            self.assertEqual(sandbox.files(), ARCHIVED)
            self.assertIn("Xray status: active", stdout)
            self.assertIn("xray-telegram-poller.service", stderr)
            self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
