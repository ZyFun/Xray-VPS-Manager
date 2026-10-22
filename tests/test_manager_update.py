import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import xray_vps_manager
from xray_vps_manager import runner
from xray_vps_manager.commands import manager_update
from xray_vps_manager.core import errors as core_errors
from xray_vps_manager.core import locks as core_locks
from xray_vps_manager.core import paths as core_paths


class ManagerUpdateTests(unittest.TestCase):
    def test_normalize_tag_adds_v_for_plain_version(self) -> None:
        self.assertEqual(manager_update.normalize_tag("1.0.1"), "v1.0.1")
        self.assertEqual(manager_update.normalize_tag("v1.0.1"), "v1.0.1")

    def test_release_archive_url_uses_tag_archive(self) -> None:
        self.assertEqual(
            manager_update.release_archive_url("v1.0.1"),
            "https://github.com/ZyFun/Xray-VPS-Manager/archive/refs/tags/v1.0.1.tar.gz",
        )

    def test_release_source_requires_bootstrap(self) -> None:
        self.assertIn("bootstrap.sh", manager_update.SOURCE_ITEMS)
        self.assertIn("bootstrap.sh", manager_update.REQUIRED_RELEASE_ITEMS)
        self.assertIn("xray-raw-log-rotate.timer", manager_update.MANAGER_SYSTEMD_UNITS)
        self.assertIn("xray-raw-log-rotate.service", manager_update.MANAGER_SYSTEMD_UNITS)

    def test_safe_extract_archive_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            archive = root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                data = b"bad"
                info = tarfile.TarInfo("../outside")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

            with self.assertRaises(manager_update.ManagerUpdateError):
                manager_update.safe_extract_archive(archive, root / "extract")

    def test_install_release_source_copies_source_wrappers_and_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            release = root / "release"
            source_dir = root / "source"
            sbin_dir = root / "sbin"
            lib_dir = root / "lib"
            package_dir = lib_dir / "xray_vps_manager"
            release.mkdir()

            wrappers = ("xray-menu", "xray-manager-update")
            items = (*wrappers, "bootstrap.sh", "install.sh", "README.md", "xray_vps_manager")
            for wrapper in wrappers:
                (release / wrapper).write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            (release / "bootstrap.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            (release / "install.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            (release / "README.md").write_text("README\n", encoding="utf-8")
            (release / "xray_vps_manager").mkdir()
            (release / "xray_vps_manager" / "runner.py").write_text("value = 1\n", encoding="utf-8")

            with mock.patch.multiple(
                manager_update,
                SOURCE_DIR=source_dir,
                SBIN_DIR=sbin_dir,
                MANAGER_LIB_DIR=lib_dir,
                MANAGER_PACKAGE_DIR=package_dir,
                MANAGER_WRAPPERS=wrappers,
                SOURCE_ITEMS=items,
                REQUIRED_RELEASE_ITEMS=items,
                SERVER_ENV_PATH=root / "missing-server.env",
            ), redirect_stdout(io.StringIO()):
                manager_update.install_release_source(release)

            self.assertTrue((source_dir / "README.md").exists())
            self.assertTrue(os.access(source_dir / "bootstrap.sh", os.X_OK))
            self.assertTrue((sbin_dir / "xray-manager-update").exists())
            self.assertTrue((package_dir / "runner.py").exists())
            self.assertTrue(os.access(sbin_dir / "xray-manager-update", os.X_OK))

    def test_restart_manager_services_reloads_systemd_and_try_restarts_units(self) -> None:
        calls = []

        def fake_run(command: list[str], timeout: int = 60):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(manager_update.shutil, "which", return_value="/bin/systemctl"):
            with mock.patch.object(manager_update, "run", side_effect=fake_run):
                with redirect_stdout(io.StringIO()):
                    manager_update.restart_manager_services()

        self.assertEqual(calls[0], ["/bin/systemctl", "daemon-reload"])
        self.assertIn(
            ["/bin/systemctl", "try-restart", "xray-telegram-poller.service"],
            calls,
        )
        self.assertIn(
            ["/bin/systemctl", "try-restart", "xray-raw-log-rotate.timer"],
            calls,
        )
        self.assertEqual(calls[-1], ["/bin/systemctl", "reset-failed", *manager_update.MANAGER_SYSTEMD_UNITS])

    def test_sync_raw_log_units_calls_installed_activity_wrapper_with_systemctl(self) -> None:
        calls = []

        def fake_run(command: list[str], timeout: int = 60):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir:
            sbin_dir = Path(tmp_dir)
            (sbin_dir / "xray-activity").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            with mock.patch.object(manager_update, "SBIN_DIR", sbin_dir), \
                mock.patch.object(manager_update, "run", side_effect=fake_run), \
                redirect_stdout(io.StringIO()):
                manager_update.sync_raw_log_units()

        self.assertEqual(calls, [[str(sbin_dir / "xray-activity"), "raw-log-timer-sync"]])

    def test_restore_wrappers_does_not_rewrite_unrelated_sbin_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            backup_sbin = root / "backup-sbin"
            target_sbin = root / "target-sbin"
            backup_sbin.mkdir()
            target_sbin.mkdir()
            (backup_sbin / "xray-menu").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            unrelated = target_sbin / "unrelated-command"
            unrelated.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            os.chmod(unrelated, 0o700)

            with mock.patch.multiple(
                manager_update,
                SBIN_DIR=target_sbin,
                MANAGER_WRAPPERS=("xray-menu",),
                SERVER_ENV_PATH=root / "missing-server.env",
            ), redirect_stdout(io.StringIO()):
                manager_update.restore_wrappers(backup_sbin)

            self.assertTrue((target_sbin / "xray-menu").exists())
            self.assertTrue(os.access(target_sbin / "xray-menu", os.X_OK))
            self.assertEqual(unrelated.stat().st_mode & 0o777, 0o700)


WRAPPER_BODY = "import sys\nfrom xray_vps_manager.runner import run_command\n"
ENV_WRAPPER = "#!/usr/bin/env python3\n" + WRAPPER_BODY


def write_fake_python(path: Path, version: str, missing: str = "") -> Path:
    """Исполняемая подмена интерпретатора: на пробу `-c` печатает версию и недостающие модули."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "-c" ]; then\n'
        f'    echo "{version} {missing}"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o755)
    return path


def env_values(path: Path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


class WrapperInterpreterTests(unittest.TestCase):
    """VIREI-81: обёртки получают абсолютный shebang Python >= 3.12, MANAGER_PYTHON следует за ним.

    Порядок выбора (решение пользователя 2026-10-21): MANAGER_PYTHON → python3.13 → python3.12 → python3 >= 3.12.
    Откат выставляет shebang заново тем же выбором, а не копирует его из бэкапа.
    """

    WRAPPERS = ("xray-menu", "xray-manager-update")

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.sbin = self.root / "sbin"
        self.server_env = self.root / "etc" / "server.env"
        self.server_env.parent.mkdir()
        self.release = self.root / "release"
        for wrapper in self.WRAPPERS:
            make_tree(self.release, {wrapper: ENV_WRAPPER})
        patches = (
            mock.patch.multiple(
                manager_update,
                SOURCE_DIR=self.root / "source",
                SBIN_DIR=self.sbin,
                MANAGER_LIB_DIR=self.root / "lib",
                MANAGER_PACKAGE_DIR=self.root / "lib" / "xray_vps_manager",
                BACKUP_DIR=self.root / "manager-backups",
                MANAGER_WRAPPERS=self.WRAPPERS,
                SOURCE_ITEMS=(*self.WRAPPERS, "xray_vps_manager"),
                REQUIRED_RELEASE_ITEMS=(*self.WRAPPERS, "xray_vps_manager"),
                SERVER_ENV_PATH=self.server_env,
            ),
            mock.patch.dict(os.environ, {"PATH": str(self.bin)}),
            mock.patch.object(core_paths, "MANAGER_LOCK_PATH", self.root / "etc" / "manager.lock"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def first_lines(self) -> dict:
        return {wrapper: (self.sbin / wrapper).read_text(encoding="utf-8").splitlines()[0] for wrapper in self.WRAPPERS}

    def assert_wrappers_use(self, shebang: str) -> None:
        self.assertEqual(self.first_lines(), {wrapper: shebang for wrapper in self.WRAPPERS})
        for wrapper in self.WRAPPERS:
            path = self.sbin / wrapper
            self.assertEqual(path.read_text(encoding="utf-8"), shebang + "\n" + WRAPPER_BODY)
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        self.assertEqual(sorted(path.name for path in self.sbin.iterdir()), sorted(self.WRAPPERS))

    def install_wrappers(self) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            manager_update.install_wrappers(self.release)
        return output.getvalue()

    def test_install_wrappers_writes_shebang_from_manager_python(self) -> None:
        configured = write_fake_python(self.root / "opt" / "python3.12", "3.12.7")
        write_fake_python(self.bin / "python3.13", "3.13.1")
        self.server_env.write_text(f"SERVER_NAME=Virei\nMANAGER_PYTHON={configured}\n", encoding="utf-8")
        before = self.server_env.read_text(encoding="utf-8")

        output = self.install_wrappers()

        self.assert_wrappers_use(f"#!{configured}")
        self.assertEqual(self.server_env.read_text(encoding="utf-8"), before)
        self.assertIn(f"OK: Обёртки менеджера используют {configured} (Python 3.12.7)", output)
        self.assertNotIn("WARN", output)

    def test_install_wrappers_prefers_python313_and_records_manager_python(self) -> None:
        write_fake_python(self.bin / "python3.12", "3.12.3")
        write_fake_python(self.bin / "python3.13", "3.13.1")
        self.server_env.write_text("SERVER_ADDR=203.0.113.10\nSERVER_NAME=Virei\nMANAGER_TIMEZONE=Europe/Moscow\n", encoding="utf-8")

        output = self.install_wrappers()

        self.assert_wrappers_use(f"#!{self.bin / 'python3.13'}")
        self.assertEqual(
            env_values(self.server_env),
            {
                "SERVER_ADDR": "203.0.113.10",
                "SERVER_NAME": "Virei",
                "MANAGER_TIMEZONE": "Europe/Moscow",
                "MANAGER_PYTHON": str(self.bin / "python3.13"),
            },
        )
        self.assertIn(f"MANAGER_PYTHON={self.bin / 'python3.13'}", output)

    def test_install_wrappers_uses_python3_real_path_when_it_is_new_enough(self) -> None:
        target = write_fake_python(self.bin / "python3.14", "3.14.0")
        os.symlink(target.name, self.bin / "python3")
        self.server_env.write_text("SERVER_NAME=Virei\n", encoding="utf-8")

        self.install_wrappers()

        self.assert_wrappers_use(f"#!{target}")
        self.assertEqual(env_values(self.server_env)["MANAGER_PYTHON"], str(target))

    def test_install_wrappers_skips_manager_python_longer_than_shebang_limit(self) -> None:
        # 81-R05: иначе обёртки и автооткат получили бы `#!<путь>`, который ядро не запускает.
        long_dir = self.root / ("d" * (130 - len(str(self.root))))
        too_long = write_fake_python(long_dir / "python3.12", "3.12.7")
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")
        self.server_env.write_text(f"MANAGER_PYTHON={too_long}\n", encoding="utf-8")

        output = self.install_wrappers()

        self.assert_wrappers_use(f"#!{python312}")
        self.assertIn(f"WARN: MANAGER_PYTHON={too_long} пропущен: строка shebang длиннее 127 байт.", output)
        self.assertEqual(env_values(self.server_env)["MANAGER_PYTHON"], str(python312))

    def test_install_wrappers_replaces_stale_manager_python_with_warning(self) -> None:
        write_fake_python(self.bin / "python3.12", "3.12.3")
        stale = self.root / "removed" / "python3.13"
        self.server_env.write_text(f"SERVER_NAME=Virei\nMANAGER_PYTHON={stale}\n", encoding="utf-8")

        output = self.install_wrappers()

        self.assert_wrappers_use(f"#!{self.bin / 'python3.12'}")
        self.assertIn(f"WARN: MANAGER_PYTHON={stale}", output)
        self.assertEqual(env_values(self.server_env)["MANAGER_PYTHON"], str(self.bin / "python3.12"))

    def test_update_without_python312_keeps_env_shebang_and_warns(self) -> None:
        # Существующая установка на старой ОС: обновление не падает, код по-прежнему совместим с 3.9.
        write_fake_python(self.bin / "python3", "3.9.18")
        make_tree(self.release / "xray_vps_manager", {"runner.py": "value = 1\n"})
        self.server_env.write_text("SERVER_NAME=Virei\n", encoding="utf-8")

        output = io.StringIO()
        with redirect_stdout(output):
            manager_update.install_release_source(self.release)

        self.assert_wrappers_use("#!/usr/bin/env python3")
        self.assertIn(f"WARN: {self.bin / 'python3'} пропущен: Python 3.9.18 старше 3.12.", output.getvalue())
        self.assertIn("WARN: Python >= 3.12 с модулями sqlite3, ssl, zlib, lzma, zoneinfo не найден", output.getvalue())
        self.assertEqual(env_values(self.server_env), {"SERVER_NAME": "Virei"})
        self.assertTrue((self.root / "lib" / "xray_vps_manager" / "runner.py").exists())

    def test_unreadable_server_env_does_not_break_restore_of_wrappers(self) -> None:
        # Автооткат неудачного обновления вызывает restore_wrappers: битый server.env не должен его прерывать.
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")
        broken = b"SERVER_NAME=Virei\nMANAGER_PYTHON=\xff\xfe\n"
        self.server_env.write_bytes(broken)
        backup_sbin = self.root / "backup" / "sbin"
        make_tree(backup_sbin, {wrapper: ENV_WRAPPER for wrapper in self.WRAPPERS})

        output = io.StringIO()
        with redirect_stdout(output):
            manager_update.restore_wrappers(backup_sbin)

        self.assert_wrappers_use(f"#!{python312}")
        self.assertIn(f"WARN: {self.server_env} не прочитан", output.getvalue())
        self.assertEqual(self.server_env.read_bytes(), broken)

    def test_busy_manager_lock_skips_manager_python_with_warning(self) -> None:
        # 81-R02: MANAGER_PYTHON пишется под manager_lock; занятая другим процессом блокировка
        # не прерывает обновление — обёртки записаны, ключ допишет следующее обновление.
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")
        self.server_env.write_text("SERVER_NAME=Virei\n", encoding="utf-8")
        lock_path = self.root / "etc" / "manager.lock"
        release = self.root / "release-lock"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, os, sys, time\n"
                "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "open(sys.argv[2] + '.held', 'w').close()\n"
                "deadline = time.monotonic() + 30\n"
                "while time.monotonic() < deadline and not os.path.exists(sys.argv[2]):\n"
                "    time.sleep(0.02)\n",
                str(lock_path),
                str(release),
            ]
        )
        self.addCleanup(holder.wait, 30)
        self.addCleanup(release.touch)
        deadline = time.monotonic() + 30
        while not Path(str(release) + ".held").exists():
            self.assertIsNone(holder.poll(), "lock holder exited early")
            self.assertLess(time.monotonic(), deadline, "lock holder did not take manager.lock")
            time.sleep(0.02)

        output = io.StringIO()
        with mock.patch.object(manager_update, "MANAGER_PYTHON_LOCK_TIMEOUT", 0.2), redirect_stdout(output):
            manager_update.install_wrappers(self.release)

        self.assert_wrappers_use(f"#!{python312}")
        self.assertIn(f"WARN: MANAGER_PYTHON не записан в {self.server_env}: ", output.getvalue())
        self.assertEqual(env_values(self.server_env), {"SERVER_NAME": "Virei"})

    def test_install_wrappers_does_not_create_missing_server_env(self) -> None:
        write_fake_python(self.bin / "python3.12", "3.12.3")

        self.install_wrappers()

        self.assert_wrappers_use(f"#!{self.bin / 'python3.12'}")
        self.assertFalse(self.server_env.exists())

    def test_restore_wrappers_reapplies_selected_interpreter_to_backup_wrappers(self) -> None:
        configured = write_fake_python(self.root / "opt" / "python3.12", "3.12.7")
        self.server_env.write_text(f"MANAGER_PYTHON={configured}\n", encoding="utf-8")
        backup_sbin = self.root / "backup" / "sbin"
        make_tree(
            backup_sbin,
            {
                # бэкап до переключения (первое обновление выполнял старый код) и бэкап с удалённым интерпретатором
                "xray-menu": ENV_WRAPPER,
                "xray-manager-update": "#!/removed/python3.13\n" + WRAPPER_BODY,
            },
        )

        with redirect_stdout(io.StringIO()):
            manager_update.restore_wrappers(backup_sbin)

        self.assert_wrappers_use(f"#!{configured}")

    def test_update_and_rollback_skip_interpreter_without_required_modules(self) -> None:
        # 81-R01: Python 3.13 без _sqlite3 не попадает ни в обёртки, ни в MANAGER_PYTHON, поэтому
        # автооткат неудачного обновления не закрепляет интерпретатор, на котором менеджер не запускается.
        write_fake_python(self.bin / "python3.13", "3.13.1", "sqlite3")
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")
        self.server_env.write_text("SERVER_NAME=Virei\n", encoding="utf-8")
        make_tree(self.sbin, {wrapper: ENV_WRAPPER for wrapper in self.WRAPPERS})
        make_tree(self.root / "lib" / "xray_vps_manager", {"runner.py": "value = 1\n"})
        make_tree(self.release / "xray_vps_manager", {"runner.py": "value = 2\n"})

        output = io.StringIO()
        with redirect_stdout(output):
            backup = manager_update.create_backup("v2.2.0")
            manager_update.install_release_source(self.release)
            self.assert_wrappers_use(f"#!{python312}")
            manager_update.restore_backup(backup)

        self.assert_wrappers_use(f"#!{python312}")
        self.assertEqual(env_values(self.server_env)["MANAGER_PYTHON"], str(python312))
        self.assertIn(f"WARN: {self.bin / 'python3.13'} пропущен: у Python 3.13.1 нет модулей sqlite3.", output.getvalue())

    def test_only_interpreters_without_required_modules_keep_env_shebang(self) -> None:
        write_fake_python(self.bin / "python3.12", "3.12.7", "sqlite3 ssl")
        configured = write_fake_python(self.root / "opt" / "python3.13", "3.13.1", "zlib")
        self.server_env.write_text(f"SERVER_NAME=Virei\nMANAGER_PYTHON={configured}\n", encoding="utf-8")
        backup_sbin = self.root / "backup" / "sbin"
        make_tree(backup_sbin, {wrapper: ENV_WRAPPER for wrapper in self.WRAPPERS})

        output = io.StringIO()
        with redirect_stdout(output):
            manager_update.restore_wrappers(backup_sbin)

        self.assert_wrappers_use("#!/usr/bin/env python3")
        self.assertIn(f"WARN: MANAGER_PYTHON={configured} пропущен: у Python 3.13.1 нет модулей zlib.", output.getvalue())
        self.assertIn(f"WARN: {self.bin / 'python3.12'} пропущен: у Python 3.12.7 нет модулей sqlite3, ssl.", output.getvalue())
        self.assertEqual(env_values(self.server_env)["MANAGER_PYTHON"], str(configured))

    def test_rollback_replaces_shebang_of_removed_interpreter_without_python_in_name(self) -> None:
        # 81-R04: MANAGER_PYTHON=/opt/runtime/bin/py попал в обёртки и в бэкап, затем интерпретатор удалён.
        runtime = write_fake_python(self.root / "opt" / "runtime" / "bin" / "py", "3.12.5")
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")
        self.server_env.write_text(f"MANAGER_PYTHON={runtime}\n", encoding="utf-8")
        make_tree(self.sbin, {wrapper: ENV_WRAPPER for wrapper in self.WRAPPERS})
        make_tree(self.root / "lib" / "xray_vps_manager", {"runner.py": "value = 1\n"})

        with redirect_stdout(io.StringIO()):
            manager_update.install_wrappers(self.release)
            self.assert_wrappers_use(f"#!{runtime}")
            backup = manager_update.create_backup("v2.2.1")
            runtime.unlink()
            manager_update.restore_backup(backup)

        self.assertEqual((backup / "sbin" / "xray-menu").read_text(encoding="utf-8").splitlines()[0], f"#!{runtime}")
        self.assert_wrappers_use(f"#!{python312}")
        self.assertEqual(env_values(self.server_env)["MANAGER_PYTHON"], str(python312))

    def test_update_then_rollback_keep_absolute_shebang(self) -> None:
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")
        self.server_env.write_text("SERVER_NAME=Virei\n", encoding="utf-8")
        make_tree(self.sbin, {wrapper: ENV_WRAPPER for wrapper in self.WRAPPERS})
        make_tree(self.root / "lib" / "xray_vps_manager", {"runner.py": "value = 1\n"})
        make_tree(self.release / "xray_vps_manager", {"runner.py": "value = 2\n"})

        with redirect_stdout(io.StringIO()):
            before_switch = manager_update.create_backup("v2.2.0")
            manager_update.install_release_source(self.release)
            self.assert_wrappers_use(f"#!{python312}")
            after_switch = manager_update.create_backup("v2.2.1")
            self.assertEqual(
                (after_switch / "sbin" / "xray-menu").read_text(encoding="utf-8").splitlines()[0],
                f"#!{python312}",
            )

            manager_update.restore_backup(after_switch)
            self.assert_wrappers_use(f"#!{python312}")
            manager_update.restore_backup(before_switch)

        self.assert_wrappers_use(f"#!{python312}")
        self.assertEqual((before_switch / "sbin" / "xray-menu").read_text(encoding="utf-8"), ENV_WRAPPER)
        self.assertEqual(env_values(self.server_env)["MANAGER_PYTHON"], str(python312))


def snapshot(root: Path):
    if not root.exists():
        return None
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def make_tree(root: Path, files: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


OLD_PACKAGE = {"runner.py": "value = 1\n", "cli.py": "cli = 1\n", "legacy.py": "old = 1\n"}
NEW_PACKAGE = {"runner.py": "value = 2\n", "cli.py": "cli = 2\n", "core/fs.py": "fs = 2\n"}


class PackageReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.release = self.root / "release"
        self.lib_dir = self.root / "lib"
        self.package_dir = self.lib_dir / "xray_vps_manager"
        self.new_dir = self.lib_dir / "xray_vps_manager.new"
        self.old_dir = self.lib_dir / "xray_vps_manager.old"
        make_tree(self.release / "xray_vps_manager", NEW_PACKAGE)
        make_tree(self.package_dir, OLD_PACKAGE)
        patcher = mock.patch.multiple(
            manager_update,
            SOURCE_DIR=self.root / "source",
            SBIN_DIR=self.root / "sbin",
            MANAGER_LIB_DIR=self.lib_dir,
            MANAGER_PACKAGE_DIR=self.package_dir,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def observe_package_operations(self):
        observed = []
        real_copytree = shutil.copytree
        real_rename = os.rename

        def observe(operation: str, target: str) -> None:
            observed.append((operation, target, snapshot(self.package_dir), snapshot(self.old_dir)))

        def observing_copytree(src, dst, *args, **kwargs):
            if Path(dst).parent == self.lib_dir:  # copytree рекурсивно вызывает себя для подкаталогов
                observe("copytree", Path(dst).name)
            return real_copytree(src, dst, *args, **kwargs)

        def observing_rename(src, dst, *args, **kwargs):
            observe("rename", f"{Path(src).name} -> {Path(dst).name}")
            return real_rename(src, dst, *args, **kwargs)

        copytree_patch = mock.patch.object(shutil, "copytree", observing_copytree)
        rename_patch = mock.patch.object(os, "rename", observing_rename)
        return observed, copytree_patch, rename_patch

    def assert_live_package_always_complete(self, observed) -> None:
        # Решение VIREI-74: замена двумя rename. Единственный момент без живого каталога —
        # между rename(live -> .old) и rename(.new -> live); тогда полный старый пакет лежит в .old.
        self.assertTrue(observed)
        for operation, target, live, old_copy in observed:
            if target == "xray_vps_manager.new -> xray_vps_manager":
                self.assertIsNone(live, f"{operation} {target}: до второго rename живой каталог уже занят: {live}")
                self.assertEqual(old_copy, OLD_PACKAGE, f"{operation} {target}: в .old не полный старый пакет")
                continue
            self.assertIn(
                live,
                (OLD_PACKAGE, NEW_PACKAGE),
                f"{operation} {target}: живой каталог пакета не полный старый и не полный новый: {live}",
            )

    def test_install_python_package_keeps_live_package_complete_at_every_step(self) -> None:
        observed, copytree_patch, rename_patch = self.observe_package_operations()
        with copytree_patch, rename_patch:
            manager_update.install_python_package(self.release)

        self.assert_live_package_always_complete(observed)
        self.assertEqual(
            [(operation, target) for operation, target, _, _ in observed],
            [
                ("copytree", "xray_vps_manager.new"),
                ("rename", "xray_vps_manager -> xray_vps_manager.old"),
                ("rename", "xray_vps_manager.new -> xray_vps_manager"),
            ],
        )
        self.assertEqual(snapshot(self.package_dir), NEW_PACKAGE)

    def test_install_python_package_prepares_new_tree_before_it_goes_live(self) -> None:
        (self.release / "xray_vps_manager" / "._runner.py").write_text("junk", encoding="utf-8")
        os.chmod(self.release / "xray_vps_manager" / "runner.py", 0o600)
        prepare_calls = []

        def recorder(name):
            real = getattr(manager_update, name)

            def wrapper(path: Path) -> None:
                prepare_calls.append((name, Path(path).name, snapshot(self.package_dir)))
                real(path)

            return wrapper

        with mock.patch.object(manager_update, "cleanup_appledouble", recorder("cleanup_appledouble")),             mock.patch.object(manager_update, "set_tree_permissions", recorder("set_tree_permissions")),             mock.patch.object(manager_update, "chown_tree", recorder("chown_tree")):
            manager_update.install_python_package(self.release)

        self.assertEqual(
            prepare_calls,
            [
                ("cleanup_appledouble", "xray_vps_manager.new", OLD_PACKAGE),
                ("set_tree_permissions", "xray_vps_manager.new", OLD_PACKAGE),
                ("chown_tree", "xray_vps_manager.new", OLD_PACKAGE),
            ],
        )
        self.assertEqual(snapshot(self.package_dir), NEW_PACKAGE)
        self.assertFalse((self.package_dir / "._runner.py").exists())
        self.assertEqual((self.package_dir / "runner.py").stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.package_dir / "core").stat().st_mode & 0o777, 0o755)

    def test_install_python_package_removes_new_and_old_after_success(self) -> None:
        manager_update.install_python_package(self.release)

        self.assertEqual(snapshot(self.package_dir), NEW_PACKAGE)
        self.assertEqual(sorted(path.name for path in self.lib_dir.iterdir()), ["xray_vps_manager"])

    def test_install_python_package_copy_failure_leaves_old_package_untouched(self) -> None:
        def failing_copytree(src, dst, *args, **kwargs):
            Path(dst).mkdir(parents=True)
            (Path(dst) / "runner.py").write_text("partial", encoding="utf-8")
            raise OSError("disk full")

        with mock.patch.object(shutil, "copytree", failing_copytree):
            with self.assertRaises(OSError):
                manager_update.install_python_package(self.release)

        self.assertEqual(snapshot(self.package_dir), OLD_PACKAGE)
        self.assertEqual(sorted(path.name for path in self.lib_dir.iterdir()), ["xray_vps_manager"])

    def test_install_python_package_second_rename_failure_restores_old_package(self) -> None:
        renames = []
        real_rename = os.rename

        def flaky_rename(src, dst, *args, **kwargs):
            renames.append((Path(src).name, Path(dst).name))
            if len(renames) == 2:
                raise OSError("rename failed")
            return real_rename(src, dst, *args, **kwargs)

        with mock.patch.object(os, "rename", flaky_rename):
            with self.assertRaises(OSError):
                manager_update.install_python_package(self.release)

        self.assertEqual(renames[1], ("xray_vps_manager.new", "xray_vps_manager"))
        self.assertEqual(snapshot(self.package_dir), OLD_PACKAGE)
        self.assertEqual(sorted(path.name for path in self.lib_dir.iterdir()), ["xray_vps_manager"])

    def test_install_python_package_ignores_stale_new_and_old_directories(self) -> None:
        make_tree(self.new_dir, {"runner.py": "stale new\n"})
        make_tree(self.old_dir, {"runner.py": "stale old\n"})

        manager_update.install_python_package(self.release)

        self.assertEqual(snapshot(self.package_dir), NEW_PACKAGE)
        self.assertEqual(sorted(path.name for path in self.lib_dir.iterdir()), ["xray_vps_manager"])

    def test_install_python_package_recovers_old_package_left_between_renames(self) -> None:
        # 74-R01: прошлый запуск прерван между двумя rename — рабочего каталога нет, старый пакет в .old.
        self.package_dir.rename(self.old_dir)
        make_tree(self.new_dir, NEW_PACKAGE)

        def failing_copytree(src, dst, *args, **kwargs):
            raise OSError("disk full")

        with mock.patch.object(shutil, "copytree", failing_copytree):
            with self.assertRaises(OSError):
                manager_update.install_python_package(self.release)

        self.assertEqual(snapshot(self.package_dir), OLD_PACKAGE)
        self.assertEqual(sorted(path.name for path in self.lib_dir.iterdir()), ["xray_vps_manager"])

        manager_update.install_python_package(self.release)

        self.assertEqual(snapshot(self.package_dir), NEW_PACKAGE)
        self.assertEqual(sorted(path.name for path in self.lib_dir.iterdir()), ["xray_vps_manager"])

    def test_restore_backup_keeps_live_package_complete_at_every_step(self) -> None:
        backup = self.root / "backup"
        make_tree(backup / "package" / "xray_vps_manager", NEW_PACKAGE)
        observed, copytree_patch, rename_patch = self.observe_package_operations()

        with copytree_patch, rename_patch:
            manager_update.restore_backup(backup)

        self.assert_live_package_always_complete(observed)
        self.assertEqual(
            [(operation, target) for operation, target, _, _ in observed],
            [
                ("copytree", "xray_vps_manager.new"),
                ("rename", "xray_vps_manager -> xray_vps_manager.old"),
                ("rename", "xray_vps_manager.new -> xray_vps_manager"),
            ],
        )
        self.assertEqual(snapshot(self.package_dir), NEW_PACKAGE)
        self.assertEqual(sorted(path.name for path in self.lib_dir.iterdir()), ["xray_vps_manager"])


class ManagerUpdateLockingTests(unittest.TestCase):
    """74-R03: два одновременных --update/--rollback не должны заменять пакет параллельно."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patches = (
            mock.patch.object(core_paths, "MANAGER_UPDATE_LOCK_PATH", self.root / "manager-update.lock"),
            mock.patch.object(manager_update, "require_root", lambda: None),
            mock.patch.object(manager_update, "current_manager_version", lambda: "2.1.0"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_update_fails_without_changes_when_another_update_holds_lock(self) -> None:
        download = mock.Mock()
        with mock.patch.object(manager_update, "download_release_archive", download):
            with core_locks.manager_update_lock(purpose="--rollback"):
                with self.assertRaises(manager_update.ManagerUpdateError) as ctx:
                    manager_update.update_to_tag("v2.1.1", force=True, run_xray_test=False)

        download.assert_not_called()
        self.assertIn(core_locks.MANAGER_UPDATE_LOCK_MESSAGE, str(ctx.exception))

    def test_rollback_fails_without_changes_when_another_update_holds_lock(self) -> None:
        restore = mock.Mock()
        with mock.patch.object(manager_update, "choose_backup", lambda name: self.root / "backup"), \
            mock.patch.object(manager_update, "restore_backup", restore):
            with core_locks.manager_update_lock(purpose="--update"):
                with self.assertRaises(manager_update.ManagerUpdateError):
                    manager_update.rollback(None, run_xray_test=False)

        restore.assert_not_called()

    def test_update_and_auto_rollback_run_under_lock(self) -> None:
        held = []

        def lock_is_held(*args, **kwargs):
            try:
                with core_locks.manager_update_lock():
                    held.append(False)
            except core_errors.LockTimeout:
                held.append(True)
            return self.root

        def failing_install(source):
            lock_is_held()
            raise OSError("disk full")

        with mock.patch.multiple(
            manager_update,
            download_release_archive=mock.Mock(return_value=self.root / "a.tar.gz"),
            safe_extract_archive=mock.Mock(return_value=self.root),
            validate_release_source=mock.Mock(),
            create_backup=mock.Mock(return_value=self.root / "backup"),
            install_release_source=failing_install,
            restore_backup=lock_is_held,
            sync_raw_log_units=mock.Mock(),
            restart_manager_services=mock.Mock(),
        ), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                manager_update.update_to_tag("v2.1.1", force=True, run_xray_test=False)

        self.assertEqual(held, [True, True])
        with core_locks.manager_update_lock():
            pass

class FakeGitHubResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class ManagerUpdateCheckTests(unittest.TestCase):
    """VIREI-84 (T1): `xray-manager-update --check` с фейковым GitHub до шага 1 (`core/http.py`); сеть не используется."""

    CURRENT = f"v{xray_vps_manager.__version__}"
    RELEASE_PAGE = "https://github.com/ZyFun/Xray-VPS-Manager/releases/tag/"

    def run_check(self, response) -> tuple:
        requests = []

        def urlopen(request, timeout=None):
            requests.append((request.full_url, request.get_header("User-agent"), timeout))
            if isinstance(response, Exception):
                raise response
            return FakeGitHubResponse(response)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(urllib.request, "urlopen", side_effect=urlopen), \
                mock.patch.object(sys, "argv", ["xray-manager-update", "--check"]), \
                mock.patch("os.geteuid", return_value=1000), redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                runner.run_command("manager-update")
        self.assertEqual(requests, [(manager_update.GITHUB_API_LATEST, "xray-manager-update", 20)])
        self.assertEqual(stderr.getvalue(), "")
        return caught.exception.code, stdout.getvalue()

    def release(self, tag: str) -> dict:
        return {"tag_name": tag, "html_url": self.RELEASE_PAGE + tag}

    def header(self, tag: str, page: bool = True) -> str:
        lines = f"Current manager version: {self.CURRENT}\nLatest GitHub release: {tag}\n"
        if page:
            lines += f"Release page: {self.RELEASE_PAGE}{tag}\n"
        return lines

    def test_check_asks_latest_release_of_manager_repository(self) -> None:
        self.assertEqual(
            manager_update.GITHUB_API_LATEST,
            "https://api.github.com/repos/ZyFun/Xray-VPS-Manager/releases/latest",
        )
        self.assertEqual(manager_update.current_manager_version(), self.CURRENT)

    def test_check_reports_newer_equal_and_older_release_with_exit_0_without_root(self) -> None:
        cases = {
            "v99.0.0": f"\033[32mOK: Доступно обновление менеджера: {self.CURRENT} -> v99.0.0\033[0m\n",
            self.CURRENT: "\033[32mOK: Установлена версия latest release.\033[0m\n",
            "v0.0.1": f"\033[33mWARN: Локальная версия новее latest release: {self.CURRENT} > v0.0.1\033[0m\n",
        }

        for tag, verdict in cases.items():
            with self.subTest(tag=tag):
                self.assertEqual(self.run_check(self.release(tag)), (0, self.header(tag) + verdict))

    def test_check_with_non_semver_tag_and_without_release_page_warns_and_exits_0(self) -> None:
        self.assertEqual(
            self.run_check({"tag_name": "nightly"}),
            (
                0,
                self.header("nightly", page=False)
                + "\033[33mWARN: Не удалось сравнить версии как semver; сравни теги вручную.\033[0m\n",
            ),
        )

    def test_check_errors_print_fail_and_exit_1(self) -> None:
        url = manager_update.GITHUB_API_LATEST
        cases = {
            "404": (
                urllib.error.HTTPError(url, 404, "Not Found", {}, None),
                "В репозитории ZyFun/Xray-VPS-Manager пока нет опубликованных релизов.",
            ),
            "500": (
                urllib.error.HTTPError(url, 500, "Server Error", {}, None),
                "Не удалось получить latest release: HTTP 500",
            ),
            "network": (urllib.error.URLError("timed out"), "Не удалось подключиться к GitHub: timed out"),
            "no tag": ({"html_url": self.RELEASE_PAGE}, "GitHub latest release не содержит tag_name."),
        }

        for name, (response, message) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.run_check(response), (1, f"\033[31mFAIL: {message}\033[0m\n"))


if __name__ == "__main__":
    unittest.main()
