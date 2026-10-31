"""Переходный фасад restore поверх Sandbox, до переноса сценария backup.

Сохраняет фикстуры XR-REV-012 и моделирование ExecStop. Старые точки
подмены шагов restore сосредоточены здесь; процессы идут через core.process.fake.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
from unittest import mock

from tests.support.sandbox import Sandbox
from xray_vps_manager.commands import backup
from xray_vps_manager.core import process, systemd
from xray_vps_manager.core.errors import LockTimeout, RestoreError

CONFIG_ARCNAME = "usr/local/etc/xray/config.json"
SERVER_ENV_ARCNAME = "usr/local/etc/xray/server.env"
MANAGER_DB_ARCNAME = "usr/local/etc/xray/manager.db"
REAL_TEST_AND_RESTART = backup.test_and_restart

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

    def __init__(self, root, *, active=(), restart_outcomes=(), lock_error=None, failing_systemctl=(),
                 xray_running=True, model_exec_stop=False, stop_outcomes=()):
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
        self.xray_running = xray_running
        self.model_exec_stop = model_exec_stop
        self.stop_outcomes = list(stop_outcomes)
        self.exec_stop_files = []
        self.backup_error = None

    def files(self):
        return {arcname: path.read_text() for arcname, path in self.targets.items()}

    def make_archive(self, members, name="xray-backup-test.tar.gz"):
        return write_archive(self.root / "incoming" / name, members)

    def systemctl(self, command, **_kwargs):
        command = list(command)
        if command[:1] == ["systemctl"]:
            self.systemctl_calls.append(command)
        if command[:2] == ["systemctl", "is-active"]:
            active = self.xray_running if command[-1] == "xray.service" else command[-1] in self.active
            code = 0 if active else 3
            return subprocess.CompletedProcess(command, code, "", "")
        if command[:3] == ["systemctl", "stop", "xray"]:
            self.events.append(("stop_xray",))
            outcome = self.stop_outcomes.pop(0) if self.stop_outcomes else None
            if isinstance(outcome, subprocess.CompletedProcess):
                return outcome
            if outcome is not None:
                raise outcome
            if self.xray_running and self.model_exec_stop:
                self.exec_stop_files.append(self.files())
                # ExecStop пишет в manager.db, которая лежит на диске в момент остановки.
                path = self.targets[MANAGER_DB_ARCNAME]
                data = json.loads(path.read_text())
                data["incoming"] += 500
                data["history"].append(500)
                path.write_text(json.dumps(data))
            self.xray_running = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "restart", "xray"]:
            self.xray_running = True
            return subprocess.CompletedProcess(command, 0, "", "")
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

    def stop_systemd_unit(self, unit, **kwargs):
        return process.stop_systemd_unit(unit, **kwargs)

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
        if self.backup_error is not None:
            raise self.backup_error
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
        if self.model_exec_stop:
            REAL_TEST_AND_RESTART()
        else:
            self.xray_running = True
        hook, self.on_restart = self.on_restart, None
        if hook is not None:
            hook()
        outcome = self.restart_outcomes.pop(0) if self.restart_outcomes else None
        if outcome is not None:
            raise outcome

    def event_names(self):
        return [event[0] for event in self.events]

    def patches(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(Sandbox())
            stack.enter_context(process.fake(self.systemctl))
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
                ("stop_systemd_unit", self.stop_systemd_unit),
            ):
                stack.enter_context(mock.patch.object(backup, name, value, create=True))
            stack.enter_context(mock.patch.dict(os.environ))
            stack.enter_context(mock.patch.object(systemd, "verify_systemd_unit_started"))
            stack.enter_context(mock.patch.object(backup, "run"))
            os.environ.pop("SERVER_ADDR", None)
            return stack.pop_all()

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
