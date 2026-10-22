"""Сценарий system/manager_update.write_wrappers: интерпретатор обёрток и MANAGER_PYTHON (шаг 0a, VIREI-81, 81-R03).

Сценарий не печатает: всё, что нужно показать пользователю, возвращается в WrappersResult.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from xray_vps_manager.core import interpreter
from xray_vps_manager.core import paths as core_paths
from xray_vps_manager.system import manager_update

WRAPPERS = ("xray-menu", "xray-test")
BODY = "import sys\n"


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


class WriteWrappersTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.source = self.root / "release"
        self.source.mkdir()
        for name in WRAPPERS:
            (self.source / name).write_text("#!/usr/bin/env python3\n" + BODY, encoding="utf-8")
        self.sbin = self.root / "sbin"
        self.server_env = self.root / "server.env"
        patches = (
            mock.patch.dict(os.environ, {"PATH": str(self.bin)}),
            mock.patch.object(core_paths, "MANAGER_LOCK_PATH", self.root / "manager.lock"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, *, remove_missing: bool = False):
        output = io.StringIO()
        with redirect_stdout(output):
            result = manager_update.write_wrappers(
                self.source,
                self.sbin,
                WRAPPERS,
                server_env_path=self.server_env,
                remove_missing=remove_missing,
                lock_timeout=5,
            )
        self.assertEqual(output.getvalue(), "")
        return result

    def first_lines(self) -> dict:
        return {name: (self.sbin / name).read_text(encoding="utf-8").splitlines()[0] for name in sorted(os.listdir(self.sbin))}

    def test_result_reports_selection_skipped_candidates_and_recording(self) -> None:
        stale = self.root / "removed" / "python3.12"
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")
        self.server_env.write_text(f"SERVER_NAME=Virei\nMANAGER_PYTHON={stale}\n", encoding="utf-8")

        result = self.write()

        self.assertEqual(
            result,
            manager_update.WrappersResult(
                interpreter=interpreter.Interpreter(str(python312), (3, 12, 3)),
                configured=str(stale),
                skipped=((str(stale), "файл не найден или не исполняемый"),),
                recorded=True,
            ),
        )
        self.assertEqual(self.first_lines(), {name: f"#!{python312}" for name in WRAPPERS})
        self.assertEqual((self.sbin / "xray-menu").stat().st_mode & 0o777, 0o755)
        self.assertIn(f"MANAGER_PYTHON={python312}\n", self.server_env.read_text(encoding="utf-8"))

    def test_unchanged_manager_python_is_not_rewritten(self) -> None:
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")
        self.server_env.write_text(f"MANAGER_PYTHON={python312}\n# keep\n", encoding="utf-8")

        result = self.write()

        self.assertFalse(result.recorded)
        self.assertEqual(result.record_error, "")
        self.assertEqual(self.server_env.read_text(encoding="utf-8"), f"MANAGER_PYTHON={python312}\n# keep\n")

    def test_without_usable_python_keeps_env_shebang_and_writes_nothing_to_server_env(self) -> None:
        write_fake_python(self.bin / "python3", "3.9.18")
        self.server_env.write_text("SERVER_NAME=Virei\n", encoding="utf-8")

        result = self.write()

        self.assertIsNone(result.interpreter)
        self.assertEqual(result.skipped, ((str(self.bin / "python3"), "Python 3.9.18 старше 3.12"),))
        self.assertFalse(result.recorded)
        self.assertEqual(self.first_lines(), {name: interpreter.ENV_SHEBANG for name in WRAPPERS})
        self.assertEqual(self.server_env.read_text(encoding="utf-8"), "SERVER_NAME=Virei\n")

    def test_unreadable_server_env_is_reported_and_ignored(self) -> None:
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")
        self.server_env.write_bytes(b"MANAGER_PYTHON=\xff\n")

        result = self.write()

        self.assertEqual(result.interpreter.path, str(python312))
        self.assertEqual(result.configured, "")
        self.assertTrue(result.server_env_error)
        self.assertFalse(result.recorded)
        self.assertTrue(result.record_error)
        self.assertEqual(self.server_env.read_bytes(), b"MANAGER_PYTHON=\xff\n")

    def test_rollback_removes_wrappers_absent_from_backup_only_when_asked(self) -> None:
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")
        self.sbin.mkdir()
        (self.sbin / "xray-old").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        (self.source / "xray-test").unlink()

        with self.assertRaises(FileNotFoundError):
            self.write()
        result = manager_update.write_wrappers(
            self.source,
            self.sbin,
            (*WRAPPERS, "xray-old"),
            server_env_path=self.server_env,
            remove_missing=True,
        )

        self.assertEqual(result.interpreter.path, str(python312))
        self.assertEqual(self.first_lines(), {"xray-menu": f"#!{python312}"})


if __name__ == "__main__":
    unittest.main()
