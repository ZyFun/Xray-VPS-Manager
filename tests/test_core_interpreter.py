"""Интерпретатор обёрток менеджера: выбор Python >= 3.12 и shebang (шаг 0a, VIREI-81).

Порядок выбора (решение пользователя 2026-10-21): MANAGER_PYTHON, python3.13, python3.12, затем python3,
если он сам >= 3.12 (в shebang пишется его версионный путь, а не системная ссылка python3). Кандидат без
модулей REQUIRED_MODULES пропускается (81-R01).
"""

import contextlib
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from xray_vps_manager.core import interpreter


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


class InterpreterSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def which(self, name: str):
        """PATH из одного каталога self.bin."""
        path = self.bin / name
        return str(path) if path.exists() and os.access(path, os.X_OK) else None

    def test_configured_manager_python_wins_over_versioned_names(self) -> None:
        configured = write_fake_python(self.root / "opt" / "python3.12", "3.12.7")
        write_fake_python(self.bin / "python3.13", "3.13.1")
        write_fake_python(self.bin / "python3.12", "3.12.3")

        selection = interpreter.select_interpreter(str(configured), which=self.which)

        self.assertEqual(selection, interpreter.Selection(interpreter.Interpreter(str(configured), (3, 12, 7)), ()))

    def test_python313_is_preferred_to_python312_without_manager_python(self) -> None:
        write_fake_python(self.bin / "python3.12", "3.12.3")
        write_fake_python(self.bin / "python3.13", "3.13.1")

        selection = interpreter.select_interpreter("", which=self.which)

        self.assertEqual(selection.interpreter, interpreter.Interpreter(str(self.bin / "python3.13"), (3, 13, 1)))

    def path_of_length(self, length: int) -> Path:
        """Путь к файлу в self.root ровно из ``length`` символов."""
        padding = length - len(str(self.root)) - 1
        self.assertGreater(padding, 0)
        return self.root / ("p" * padding)

    def test_interpreter_path_must_fit_kernel_shebang_buffer(self) -> None:
        # 81-R05: ядро читает строку `#!<путь>` буфером (128 байт до Linux 5.1); длинный путь даёт Exec format error.
        longest = write_fake_python(self.path_of_length(interpreter.MAX_SHEBANG_BYTES - 2), "3.12.7")
        too_long = write_fake_python(self.path_of_length(interpreter.MAX_SHEBANG_BYTES - 1), "3.12.7")
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")

        self.assertEqual(interpreter.MAX_SHEBANG_BYTES, 127)
        self.assertEqual(interpreter.check_interpreter(str(longest)), (interpreter.Interpreter(str(longest), (3, 12, 7)), ""))
        self.assertEqual(interpreter.check_interpreter(str(too_long)), (None, "строка shebang длиннее 127 байт"))
        selection = interpreter.select_interpreter(str(too_long), which=self.which)
        self.assertEqual(selection.interpreter.path, str(python312))
        self.assertEqual(selection.skipped, ((str(too_long), "строка shebang длиннее 127 байт"),))

    def test_interpreter_path_may_contain_any_non_whitespace_character(self) -> None:
        # 81-R06: те же ограничения shebang, что в bootstrap.sh/install.sh; путь с `@` (Homebrew) подходит.
        brewed = write_fake_python(self.root / "opt" / "python@3.12" / "bin" / "python3.12", "3.12.7")
        tabbed = write_fake_python(self.root / "opt" / "with\ttab" / "python3.12", "3.12.7")
        multibyte = write_fake_python(self.root / ("п" * (125 - len(str(self.root)) - 1)), "3.12.7")

        self.assertEqual(interpreter.check_interpreter(str(brewed)), (interpreter.Interpreter(str(brewed), (3, 12, 7)), ""))
        self.assertEqual(interpreter.check_interpreter(str(tabbed)), (None, "пробел в пути не поддерживается в shebang"))
        self.assertEqual(interpreter.check_interpreter(str(multibyte)), (None, "строка shebang длиннее 127 байт"))

    def test_unusable_manager_python_falls_back_to_path_candidates(self) -> None:
        write_fake_python(self.bin / "python3.12", "3.12.3")
        old = write_fake_python(self.root / "opt" / "python3.11", "3.11.2")
        with_space = write_fake_python(self.root / "dir with space" / "python3.12", "3.12.3")
        not_executable = write_fake_python(self.root / "noexec" / "python3.12", "3.12.3")
        os.chmod(not_executable, 0o644)

        for configured, reason in (
            (str(self.root / "missing" / "python3.12"), "файл не найден или не исполняемый"),
            (str(old), "Python 3.11.2 старше 3.12"),
            ("python3.12", "не абсолютный путь"),
            (str(with_space), "пробел в пути не поддерживается в shebang"),
            (str(not_executable), "файл не найден или не исполняемый"),
            (str(self.root / "opt"), "файл не найден или не исполняемый"),
        ):
            with self.subTest(configured=configured):
                selection = interpreter.select_interpreter(configured, which=self.which)
                self.assertEqual(selection.interpreter.path, str(self.bin / "python3.12"))
                self.assertEqual(selection.skipped, ((configured, reason),))

    def test_python3_counts_only_when_new_enough_and_is_written_by_real_path(self) -> None:
        target = write_fake_python(self.bin / "python3.14", "3.14.0")
        os.symlink(target.name, self.bin / "python3")

        selection = interpreter.select_interpreter("", which=self.which)

        self.assertEqual(selection.interpreter, interpreter.Interpreter(str(target), (3, 14, 0)))

    def test_versioned_name_reporting_old_version_is_skipped(self) -> None:
        write_fake_python(self.bin / "python3.13", "3.11.9")
        write_fake_python(self.bin / "python3.12", "3.12.3")

        selection = interpreter.select_interpreter("", which=self.which)

        self.assertEqual(selection.interpreter.path, str(self.bin / "python3.12"))
        self.assertEqual(selection.skipped, ((str(self.bin / "python3.13"), "Python 3.11.9 старше 3.12"),))

    def test_python_without_required_modules_is_skipped(self) -> None:
        # 81-R01: самосборный Python без dev-пакетов проходит по версии, но менеджер на нём не запускается.
        configured = write_fake_python(self.root / "opt" / "python3.12", "3.12.7", "sqlite3")
        write_fake_python(self.bin / "python3.13", "3.13.1", "sqlite3 lzma")
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")

        selection = interpreter.select_interpreter(str(configured), which=self.which)

        self.assertEqual(selection.interpreter, interpreter.Interpreter(str(python312), (3, 12, 3)))
        self.assertEqual(
            selection.skipped,
            (
                (str(configured), "у Python 3.12.7 нет модулей sqlite3"),
                (str(self.bin / "python3.13"), "у Python 3.13.1 нет модулей sqlite3, lzma"),
            ),
        )

    def test_no_supported_interpreter_returns_none(self) -> None:
        write_fake_python(self.bin / "python3", "3.9.18")
        write_fake_python(self.bin / "python3.12", "3.11.9")

        selection = interpreter.select_interpreter("", which=self.which)

        self.assertIsNone(selection.interpreter)
        self.assertEqual(
            selection.skipped,
            (
                (str(self.bin / "python3.12"), "Python 3.11.9 старше 3.12"),
                (str(self.bin / "python3"), "Python 3.9.18 старше 3.12"),
            ),
        )
        self.assertEqual(interpreter.select_interpreter("", which=lambda name: None), interpreter.Selection(None, ()))

    def test_candidate_found_twice_is_checked_once(self) -> None:
        python312 = write_fake_python(self.bin / "python3.12", "3.11.9")
        os.symlink(python312.name, self.bin / "python3")

        selection = interpreter.select_interpreter(str(python312), which=self.which)

        self.assertEqual(selection, interpreter.Selection(None, ((str(python312), "Python 3.11.9 старше 3.12"),)))

    def test_probe_version_reads_version_and_rejects_non_python(self) -> None:
        fake = write_fake_python(self.bin / "python3.12", "3.12.3")
        not_python = self.root / "not-python"
        not_python.write_text("#!/bin/sh\necho hello\n", encoding="utf-8")
        os.chmod(not_python, 0o755)
        failing = self.root / "failing"
        failing.write_text("#!/bin/sh\necho 3.12.3\nexit 1\n", encoding="utf-8")
        os.chmod(failing, 0o755)

        self.assertEqual(interpreter.probe_version([str(fake)]), (3, 12, 3))
        self.assertEqual(interpreter.probe([str(fake)]), interpreter.Probe((3, 12, 3), ()))
        self.assertIsNone(interpreter.probe_version([str(not_python)]))
        self.assertIsNone(interpreter.probe_version([str(failing)]))
        self.assertIsNone(interpreter.probe_version([str(self.root / "missing")]))
        self.assertIsNone(interpreter.probe_version([]))

    def test_real_probe_reports_version_and_missing_modules_of_running_python(self) -> None:
        expected_missing = []
        for name in interpreter.REQUIRED_MODULES:
            try:
                importlib.import_module(name)
            except Exception:
                expected_missing.append(name)

        result = interpreter.probe([sys.executable])

        self.assertEqual(result, interpreter.Probe(tuple(sys.version_info[:3]), tuple(expected_missing)))

    def test_real_probe_ignores_modules_planted_in_current_directory(self) -> None:
        # -I: обновление идёт от root, одноимённый ssl.py в текущем каталоге не должен импортироваться.
        (self.root / "ssl.py").write_text("raise ImportError('planted')\n", encoding="utf-8")
        (self.root / "sqlite3.py").write_text("raise ImportError('planted')\n", encoding="utf-8")
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            result = interpreter.probe([sys.executable])
        finally:
            os.chdir(previous)

        self.assertIsNotNone(result)
        self.assertNotIn("ssl", result.missing)
        self.assertNotIn("sqlite3", result.missing)


class WrappersCheckTests(unittest.TestCase):
    """Обход shebang установленных обёрток для xray-test (81-R03: общая проверка — в core/interpreter)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.sbin = self.root / "sbin"
        self.sbin.mkdir()
        self.paths = [self.sbin / "xray-menu", self.sbin / "xray-test", self.sbin / "xray-absent"]

    def write_wrappers(self, *lines: str) -> None:
        for path, line in zip(self.paths, lines):
            path.write_text(f"{line}\nimport sys\n", encoding="utf-8")

    def test_wrapper_shebangs_and_command(self) -> None:
        python312 = write_fake_python(self.root / "opt" / "python3.12", "3.12.3")
        self.write_wrappers("#!/usr/bin/env python3", f"#!{python312}")

        self.assertEqual(
            interpreter.wrapper_shebangs(self.paths),
            {"#!/usr/bin/env python3": ("xray-menu",), f"#!{python312}": ("xray-test",)},
        )
        self.assertEqual(interpreter.wrappers_command(self.paths), ["/usr/bin/env", "python3"])
        self.assertEqual(interpreter.wrappers_command(self.paths[1:]), [str(python312)])
        self.assertEqual(interpreter.wrappers_command([self.sbin / "missing"]), ["python3"])

    def test_check_wrappers_names_the_first_failed_rule(self) -> None:
        good = write_fake_python(self.root / "opt" / "python3.12", "3.12.7")
        old = write_fake_python(self.root / "opt" / "python3.9", "3.9.18")
        broken = write_fake_python(self.root / "opt" / "python3.13", "3.13.1", "sqlite3")
        removed = self.root / "removed" / "python3.12"

        self.assertEqual(interpreter.check_wrappers(self.paths).problem, interpreter.WRAPPERS_NOT_INSTALLED)
        cases = (
            (("#!/usr/bin/env python3", f"#!{good}"), interpreter.WRAPPERS_MIXED, ""),
            (("import sys", "import sys"), interpreter.WRAPPERS_NO_SHEBANG, ""),
            (("#!/usr/bin/env python3", "#!/usr/bin/env python3"), interpreter.WRAPPERS_ENV, ""),
            ((f"#!{removed}", f"#!{removed}"), interpreter.WRAPPERS_NOT_RUNNING, str(removed)),
            ((f"#!{old}", f"#!{old}"), interpreter.WRAPPERS_OLD, str(old)),
            ((f"#!{broken}", f"#!{broken}"), interpreter.WRAPPERS_MISSING_MODULES, str(broken)),
            ((f"#!{good}", f"#!{good}"), "", str(good)),
        )
        for lines, problem, path in cases:
            with self.subTest(problem=problem or "ok"):
                self.write_wrappers(*lines)
                check = interpreter.check_wrappers(self.paths)
                self.assertEqual(check.problem, problem)
                self.assertEqual(check.interpreter, path)
        self.assertEqual(check.probe, interpreter.Probe((3, 12, 7), ()))
        self.assertEqual(check.shebangs, {f"#!{good}": ("xray-menu", "xray-test")})


class ShebangTests(unittest.TestCase):
    def test_shebang_line_uses_absolute_path_or_env_fallback(self) -> None:
        self.assertEqual(interpreter.shebang_line("/usr/bin/python3.12"), "#!/usr/bin/python3.12")
        self.assertEqual(interpreter.shebang_line(""), "#!/usr/bin/env python3")
        self.assertEqual(interpreter.ENV_SHEBANG, "#!/usr/bin/env python3")

    def test_shebang_command_and_absolute_interpreter(self) -> None:
        self.assertEqual(interpreter.shebang_command("#!/usr/bin/env python3"), ["/usr/bin/env", "python3"])
        self.assertEqual(interpreter.shebang_command("#! /usr/bin/python3.12"), ["/usr/bin/python3.12"])
        self.assertEqual(interpreter.shebang_command("import sys"), [])
        self.assertEqual(interpreter.shebang_command(""), [])

        self.assertEqual(interpreter.absolute_interpreter(["/usr/bin/python3.12"]), "/usr/bin/python3.12")
        self.assertEqual(interpreter.absolute_interpreter(["/usr/bin/env", "python3"]), "")
        self.assertEqual(interpreter.absolute_interpreter(["python3"]), "")
        self.assertEqual(interpreter.absolute_interpreter([]), "")

    def test_with_shebang_replaces_any_first_shebang_line_of_a_wrapper(self) -> None:
        wrapper = b"#!/usr/bin/env python3\nimport sys\nprint(sys.argv)\n"
        self.assertEqual(
            interpreter.with_shebang(wrapper, "#!/usr/bin/python3.12"),
            b"#!/usr/bin/python3.12\nimport sys\nprint(sys.argv)\n",
        )
        self.assertEqual(
            interpreter.with_shebang(b"#!/usr/bin/python3.12\nvalue = 1\n", interpreter.ENV_SHEBANG),
            b"#!/usr/bin/env python3\nvalue = 1\n",
        )
        self.assertEqual(interpreter.with_shebang(b"#!/usr/bin/env python3", "#!/usr/bin/python3.12"), b"#!/usr/bin/python3.12\n")
        # 81-R04: путь интерпретатора без «python» в имени тоже заменяется (обёртки менеджера — всегда Python).
        self.assertEqual(
            interpreter.with_shebang(b"#!/opt/runtime/bin/py\nimport sys\n", "#!/usr/bin/python3.12"),
            b"#!/usr/bin/python3.12\nimport sys\n",
        )
        self.assertEqual(interpreter.with_shebang(b"value = 1\n", "#!/usr/bin/python3.12"), b"#!/usr/bin/python3.12\nvalue = 1\n")
        self.assertEqual(interpreter.with_shebang(b"", interpreter.ENV_SHEBANG), b"#!/usr/bin/env python3\n")

    def test_read_shebang_returns_first_line_without_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            crlf = root / "crlf"
            crlf.write_bytes(b"#!/usr/bin/python3.12\r\nimport sys\n")
            empty = root / "empty"
            empty.write_bytes(b"")

            self.assertEqual(interpreter.read_shebang(crlf), "#!/usr/bin/python3.12")
            self.assertEqual(interpreter.read_shebang(empty), "")
            self.assertEqual(interpreter.read_shebang(root / "missing"), "")


if __name__ == "__main__":
    unittest.main()
