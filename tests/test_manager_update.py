import io
import os
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
        self.assertEqual(calls[-1], ["/bin/systemctl", "reset-failed", *manager_update.MANAGER_SYSTEMD_UNITS])

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


if __name__ == "__main__":
    unittest.main()
