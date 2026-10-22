"""Контракт core/fs.replace_directory: замена каталога двумя rename (VIREI-74, XR-REV-032)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xray_vps_manager.core import fs


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


class ReplaceDirectoryTests(unittest.TestCase):
    def test_replace_directory_creates_destination_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            destination = root / "lib" / "package"
            make_tree(source, {"runner.py": "value = 1\n", "sub/mod.py": "mod = 1\n"})

            fs.replace_directory(source, destination)

            self.assertEqual(snapshot(destination), snapshot(source))
            self.assertFalse(destination.with_name("package.new").exists())
            self.assertFalse(destination.with_name("package.old").exists())

    def test_replace_directory_runs_prepare_on_new_tree_before_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            destination = root / "lib" / "package"
            make_tree(source, {"runner.py": "value = 2\n"})
            make_tree(destination, {"runner.py": "value = 1\n", "legacy.py": "old = 1\n"})
            old = snapshot(destination)
            prepare_calls = []

            def prepare(path: Path) -> None:
                prepare_calls.append((path.name, snapshot(destination), snapshot(path)))
                (path / "generated.py").write_text("prepared = 1\n", encoding="utf-8")

            fs.replace_directory(source, destination, prepare=prepare)

            self.assertEqual(
                prepare_calls,
                [("package.new", old, {"runner.py": "value = 2\n"})],
            )
            self.assertEqual(
                snapshot(destination),
                {"runner.py": "value = 2\n", "generated.py": "prepared = 1\n"},
            )
            self.assertFalse(destination.with_name("package.new").exists())
            self.assertFalse(destination.with_name("package.old").exists())

    def test_replace_directory_prepare_failure_leaves_destination_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            destination = root / "lib" / "package"
            make_tree(source, {"runner.py": "value = 2\n"})
            make_tree(destination, {"runner.py": "value = 1\n"})
            old = snapshot(destination)

            def prepare(path: Path) -> None:
                raise PermissionError("chmod failed")

            with self.assertRaises(PermissionError):
                fs.replace_directory(source, destination, prepare=prepare)

            self.assertEqual(snapshot(destination), old)
            self.assertFalse(destination.with_name("package.new").exists())
            self.assertFalse(destination.with_name("package.old").exists())

    def test_replace_directory_uses_two_renames_and_restores_old_on_second_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            destination = root / "lib" / "package"
            make_tree(source, {"runner.py": "value = 2\n"})
            make_tree(destination, {"runner.py": "value = 1\n"})
            old = snapshot(destination)
            renames = []
            real_rename = os.rename

            def flaky_rename(src, dst, *args, **kwargs):
                renames.append((Path(src).name, Path(dst).name))
                if len(renames) == 2:
                    raise OSError("rename failed")
                return real_rename(src, dst, *args, **kwargs)

            with mock.patch.object(fs.os, "rename", flaky_rename):
                with self.assertRaises(OSError):
                    fs.replace_directory(source, destination)

            self.assertEqual(
                renames,
                [("package", "package.old"), ("package.new", "package"), ("package.old", "package")],
            )
            self.assertEqual(snapshot(destination), old)
            self.assertFalse(destination.with_name("package.new").exists())
            self.assertFalse(destination.with_name("package.old").exists())


    def test_replace_directory_restores_old_left_between_renames_before_cleanup(self) -> None:
        # 74-R01: прерывание между двумя rename оставляет рабочий путь пустым, а полный старый
        # каталог — в .old. Следующий запуск не должен удалять .old до того, как вернёт его на место.
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            destination = root / "lib" / "package"
            old_dir = destination.with_name("package.old")
            new_dir = destination.with_name("package.new")
            make_tree(source, {"runner.py": "value = 2\n"})
            make_tree(old_dir, {"runner.py": "value = 1\n"})
            make_tree(new_dir, {"runner.py": "value = 2\n"})

            def failing_copytree(src, dst, *args, **kwargs):
                raise OSError("disk full")

            with mock.patch.object(fs.shutil, "copytree", failing_copytree):
                with self.assertRaises(OSError):
                    fs.replace_directory(source, destination)

            self.assertEqual(snapshot(destination), {"runner.py": "value = 1\n"})
            self.assertFalse(old_dir.exists())
            self.assertFalse(new_dir.exists())

    def test_replace_directory_removes_stale_old_when_destination_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            destination = root / "lib" / "package"
            make_tree(source, {"runner.py": "value = 3\n"})
            make_tree(destination, {"runner.py": "value = 2\n"})
            make_tree(destination.with_name("package.old"), {"partial.py": "x = 1\n"})

            fs.replace_directory(source, destination)

            self.assertEqual(snapshot(destination), {"runner.py": "value = 3\n"})
            self.assertEqual(sorted(path.name for path in destination.parent.iterdir()), ["package"])


class ReplaceFileTests(unittest.TestCase):
    """core/fs.replace_file: обёртка в /usr/local/sbin заменяется одним rename (VIREI-81)."""

    def test_replace_file_writes_new_content_and_leaves_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sbin = Path(tmp_dir) / "sbin"
            destination = sbin / "xray-menu"

            fs.replace_file(destination, b"#!/usr/bin/python3.12\nvalue = 1\n")

            self.assertEqual(destination.read_bytes(), b"#!/usr/bin/python3.12\nvalue = 1\n")
            self.assertEqual(sorted(path.name for path in sbin.iterdir()), ["xray-menu"])

    def test_replace_file_prepares_copy_before_it_goes_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sbin = Path(tmp_dir)
            destination = sbin / "xray-menu"
            destination.write_bytes(b"old\n")
            prepare_calls = []

            def prepare(path: Path) -> None:
                prepare_calls.append((path.parent == sbin, path.name != destination.name, path.read_bytes(), destination.read_bytes()))
                os.chmod(path, 0o755)

            fs.replace_file(destination, b"new\n", prepare=prepare)

            self.assertEqual(prepare_calls, [(True, True, b"new\n", b"old\n")])
            self.assertEqual(destination.read_bytes(), b"new\n")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o755)
            self.assertEqual(sorted(path.name for path in sbin.iterdir()), ["xray-menu"])

    def test_replace_file_failure_leaves_destination_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sbin = Path(tmp_dir)
            destination = sbin / "xray-menu"
            destination.write_bytes(b"old\n")

            def prepare(path: Path) -> None:
                raise PermissionError("chown failed")

            with self.assertRaises(PermissionError):
                fs.replace_file(destination, b"new\n", prepare=prepare)
            with mock.patch.object(fs.os, "replace", side_effect=OSError("rename failed")):
                with self.assertRaises(OSError):
                    fs.replace_file(destination, b"new\n")

            self.assertEqual(destination.read_bytes(), b"old\n")
            self.assertEqual(sorted(path.name for path in sbin.iterdir()), ["xray-menu"])

    def test_set_root_executable_sets_mode_and_changes_owner_only_as_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            command = Path(tmp_dir) / "xray-menu"
            command.write_bytes(b"#!/usr/bin/python3.12\n")
            os.chmod(command, 0o600)

            with mock.patch.object(fs.os, "geteuid", return_value=1000), mock.patch.object(fs.shutil, "chown") as chown:
                fs.set_root_executable(command)
            self.assertEqual(command.stat().st_mode & 0o777, 0o755)
            chown.assert_not_called()

            with mock.patch.object(fs.os, "geteuid", return_value=0), mock.patch.object(fs.shutil, "chown") as chown:
                fs.set_root_executable(command)
            chown.assert_called_once_with(command, user="root", group="root")

    def test_remove_path_removes_file_symlink_and_directory_and_ignores_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "target"
            target.write_text("keep\n", encoding="utf-8")
            link = root / "link"
            os.symlink(target, link)
            make_tree(root / "tree", {"sub/file.py": "x = 1\n"})
            (root / "file").write_text("x\n", encoding="utf-8")

            for name in ("link", "tree", "file", "missing"):
                fs.remove_path(root / name)

            self.assertEqual(sorted(path.name for path in root.iterdir()), ["target"])
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    def test_replace_file_overwrites_stale_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sbin = Path(tmp_dir)
            destination = sbin / "xray-menu"
            stale = sbin / ".xray-menu.new"
            stale.write_bytes(b"partial")

            fs.replace_file(destination, b"new\n")

            self.assertEqual(destination.read_bytes(), b"new\n")
            self.assertEqual(sorted(path.name for path in sbin.iterdir()), ["xray-menu"])


class PruneBackupsTests(unittest.TestCase):
    """Ротация копий `<файл>.bak.<цифры>` (VIREI-241)."""

    def make_copies(self, path: Path, count: int) -> list:
        copies = []
        for index in range(count):
            copy = path.with_name(f"{path.name}.bak.2026010100000000{index:04d}")
            copy.write_text(f"copy {index}\n", encoding="utf-8")
            copies.append(copy)
        return copies

    def test_prune_backups_keeps_newest_copies_and_removes_older(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "api.example.com.caddy"
            path.write_text("site\n", encoding="utf-8")
            copies = self.make_copies(path, 8)

            removed = fs.prune_backups(path, keep=5)

            self.assertEqual(removed, copies[:3])
            self.assertEqual(sorted(p for p in Path(tmp_dir).iterdir() if p != path), copies[3:])
            self.assertEqual(path.read_text(encoding="utf-8"), "site\n")

    def test_prune_backups_orders_copies_by_timestamp_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "Caddyfile"
            short = path.with_name("Caddyfile.bak.9")
            long_old = path.with_name("Caddyfile.bak.10")
            long_new = path.with_name("Caddyfile.bak.11")
            for copy in (short, long_old, long_new):
                copy.write_text("x\n", encoding="utf-8")

            removed = fs.prune_backups(path, keep=2)

            self.assertEqual(removed, [short])
            self.assertTrue(long_old.exists())
            self.assertTrue(long_new.exists())

    def test_prune_backups_touches_only_exact_copies_of_this_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            path = root / "example.com.caddy"
            path.write_text("site\n", encoding="utf-8")
            copies = self.make_copies(path, 3)
            foreign_names = [
                "api.example.com.caddy",
                "api.example.com.caddy.bak.20250101000000000000",
                "example.com.caddy.bak.telegram.20250101000000",
                "example.com.caddy.bak.",
                "example.com.caddy.bak.20250101000000000000.tar.gz",
                "exampleXcom.caddy.bak.20250101000000000000",
                "example.com.caddy.bak.2025a",
                "config.json.bak.20250101000000",
            ]
            for name in foreign_names:
                (root / name).write_text("foreign\n", encoding="utf-8")
            (root / "example.com.caddy.bak.20240101000000000000").mkdir()

            removed = fs.prune_backups(path, keep=1)

            self.assertEqual(removed, copies[:2])
            for name in foreign_names:
                self.assertTrue((root / name).exists(), name)
            self.assertTrue((root / "example.com.caddy.bak.20240101000000000000").is_dir())
            self.assertTrue(copies[2].exists())

    def test_prune_backups_keeps_everything_when_copies_fit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "site.caddy"
            copies = self.make_copies(path, 5)

            self.assertEqual(fs.prune_backups(path, keep=5), [])
            self.assertTrue(all(copy.exists() for copy in copies))

    def test_prune_backups_ignores_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertEqual(fs.prune_backups(Path(tmp_dir) / "missing" / "site.caddy", keep=5), [])

    def test_prune_backups_keeps_protected_copy_when_clock_went_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "site.caddy"
            future = [path.with_name(f"site.caddy.bak.2099010100000000{index:04d}") for index in range(6)]
            for copy in future:
                copy.write_text("future\n", encoding="utf-8")
            current = path.with_name("site.caddy.bak.20261022000000000000")
            current.write_text("current\n", encoding="utf-8")

            removed = fs.prune_backups(path, keep=5, protect=(current, None))

            self.assertTrue(current.exists())
            self.assertEqual(removed, future[:2])
            self.assertEqual(sorted(p for p in Path(tmp_dir).iterdir()), sorted([current, *future[2:]]))

    def test_prune_backups_with_protected_copy_keeps_everything_below_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "site.caddy"
            for count in (1, 3, 4):
                for item in Path(tmp_dir).iterdir():
                    item.unlink()
                copies = self.make_copies(path, count)
                current = path.with_name("site.caddy.bak.20261022000000000000")
                current.write_text("current\n", encoding="utf-8")

                removed = fs.prune_backups(path, keep=5, protect=(current,))

                self.assertEqual(removed, [], count)
                self.assertTrue(all(copy.exists() for copy in [*copies, current]), count)

    def test_prune_backups_requires_keeping_at_least_one_copy(self) -> None:
        with self.assertRaises(ValueError):
            fs.prune_backups(Path("/nonexistent/site.caddy"), keep=0)


if __name__ == "__main__":
    unittest.main()
