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

if __name__ == "__main__":
    unittest.main()
