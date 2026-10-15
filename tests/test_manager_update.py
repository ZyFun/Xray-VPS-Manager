import io
import os
import shutil
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xray_vps_manager.commands import manager_update


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
            ):
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
            ):
                manager_update.restore_wrappers(backup_sbin)

            self.assertTrue((target_sbin / "xray-menu").exists())
            self.assertTrue(os.access(target_sbin / "xray-menu", os.X_OK))
            self.assertEqual(unrelated.stat().st_mode & 0o777, 0o700)


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


if __name__ == "__main__":
    unittest.main()
