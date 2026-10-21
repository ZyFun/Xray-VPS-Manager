"""Python >= 3.12 при установке: bootstrap.sh и install.sh (шаг 0a, VIREI-81).

Порядок выбора (решение пользователя 2026-10-21): MANAGER_PYTHON, python3.13, python3.12, затем python3,
если он сам >= 3.12 (берётся его версионный путь). Кандидат без модулей sqlite3, ssl, zlib, lzma или zoneinfo
пропускается (81-R01). Без подходящего интерпретатора установка завершается с инструкцией; системный
python3 не заменяется.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from xray_vps_manager.commands import manager_update
from xray_vps_manager.core import interpreter


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "bootstrap.sh"
INSTALL_SH = ROOT / "install.sh"
SCRIPTS = (BOOTSTRAP, INSTALL_SH)
PYTHON_FUNCTIONS = ("manager_python_probe", "manager_python_supported", "find_manager_python", "require_manager_python")
BASH = shutil.which("bash") or "/bin/bash"


def function_source(script: Path, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", script.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"{script.name} must define {name}()")
    return match.group(0)


def write_fake_python(path: Path, version: str, missing: str = "") -> Path:
    """Исполняемая подмена интерпретатора без внешних команд: на пробу `-c` печатает версию и недостающие модули."""
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


class ManagerPythonShellTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in ("readlink", "cat"):
            os.symlink(shutil.which(tool), self.bin / tool)

    def require(self, script: Path, manager_python: str = "") -> subprocess.CompletedProcess:
        code = (
            "set -Eeuo pipefail\n"
            + "".join(function_source(script, name) for name in PYTHON_FUNCTIONS)
            + 'require_manager_python\nprintf "%s\\n" "$XRAY_PYTHON"\n'
        )
        env = {"PATH": str(self.bin)}
        if manager_python:
            env["MANAGER_PYTHON"] = manager_python
        return subprocess.run(
            [BASH, "-c", code],
            env=env,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def assert_selected(self, expected: Path, manager_python: str = "") -> None:
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                result = self.require(script, manager_python)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(expected))

    def assert_refused(self, *messages: str, manager_python: str = "") -> None:
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                result = self.require(script, manager_python)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout, "")
                for message in messages:
                    self.assertIn(message, result.stderr)

    def test_scripts_define_the_same_python_functions(self) -> None:
        for name in PYTHON_FUNCTIONS:
            with self.subTest(function=name):
                self.assertEqual(function_source(BOOTSTRAP, name), function_source(INSTALL_SH, name))

    def test_old_system_python_without_python312_is_refused_with_instructions(self) -> None:
        write_fake_python(self.bin / "python3", "3.9.18")

        self.assert_refused(
            "Python >= 3.12",
            "found: python3 3.9.18",
            "Ubuntu 24.04+",
            "ppa:deadsnakes/ppa",
            "make altinstall",
            "MANAGER_PYTHON=",
            "docs/installation.md",
        )

    def test_missing_python_is_refused(self) -> None:
        self.assert_refused("Python >= 3.12", "found: no python3")

    def test_python312_next_to_old_system_python_is_selected(self) -> None:
        write_fake_python(self.bin / "python3", "3.11.2")
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")

        self.assert_selected(python312)

    def test_python313_is_preferred_to_python312(self) -> None:
        write_fake_python(self.bin / "python3.12", "3.12.3")
        python313 = write_fake_python(self.bin / "python3.13", "3.13.1")

        self.assert_selected(python313)

    def test_manager_python_is_preferred_to_versioned_names(self) -> None:
        write_fake_python(self.bin / "python3.13", "3.13.1")
        configured = write_fake_python(self.root / "opt" / "python3.12", "3.12.7")

        self.assert_selected(configured, manager_python=str(configured))

    def test_invalid_manager_python_is_refused(self) -> None:
        write_fake_python(self.bin / "python3.12", "3.12.3")
        old = write_fake_python(self.root / "opt" / "python3.11", "3.11.2")

        for configured in (str(self.root / "missing" / "python3.12"), str(old), "python3.12"):
            with self.subTest(configured=configured):
                self.assert_refused(
                    f"MANAGER_PYTHON={configured} must be an absolute path to an executable Python >= 3.12",
                    manager_python=configured,
                )

    def test_new_system_python3_is_selected_by_its_versioned_path(self) -> None:
        python314 = write_fake_python(self.bin / "python3.14", "3.14.0")
        os.symlink(python314.name, self.bin / "python3")

        self.assert_selected(python314)

    def test_versioned_name_with_old_version_is_not_selected(self) -> None:
        write_fake_python(self.bin / "python3", "3.9.18")
        write_fake_python(self.bin / "python3.12", "3.11.9")

        self.assert_refused("Python >= 3.12", "found: python3.12 3.11.9; python3 3.9.18")

    def test_python_without_required_modules_is_skipped(self) -> None:
        # 81-R01: самосборный Python без dev-пакетов проходит по версии, но менеджер на нём не запускается.
        write_fake_python(self.bin / "python3.13", "3.13.1", "sqlite3")
        python312 = write_fake_python(self.bin / "python3.12", "3.12.3")

        self.assert_selected(python312)

    def test_only_pythons_without_required_modules_are_refused(self) -> None:
        write_fake_python(self.bin / "python3.13", "3.13.1", "sqlite3 lzma")
        write_fake_python(self.bin / "python3", "3.11.2")

        self.assert_refused(
            "with the sqlite3, ssl, zlib, lzma and zoneinfo modules",
            "found: python3.13 3.13.1 without sqlite3 lzma; python3 3.11.2",
            "libsqlite3-dev",
        )

    def test_manager_python_without_required_modules_is_refused(self) -> None:
        write_fake_python(self.bin / "python3.12", "3.12.3")
        configured = write_fake_python(self.root / "opt" / "python3.12", "3.12.7", "ssl")

        self.assert_refused(
            f"MANAGER_PYTHON={configured} must be an absolute path to an executable Python >= 3.12 with the sqlite3",
            manager_python=str(configured),
        )

    def test_shell_probe_matches_python_probe_on_real_interpreter(self) -> None:
        expected = interpreter.probe([sys.executable])
        self.assertIsNotNone(expected)
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                result = subprocess.run(
                    [BASH, "-c", function_source(script, "manager_python_probe") + 'manager_python_probe "$1"\n', "_", sys.executable],
                    env={"PATH": str(self.bin)},
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.split(),
                    [interpreter.format_version(expected.version), *expected.missing],
                )


class InstallScriptManagerPythonTests(unittest.TestCase):
    def test_install_script_checks_python_before_questions_and_after_apt(self) -> None:
        content = INSTALL_SH.read_text(encoding="utf-8")

        self.assertIn('MANAGER_PYTHON="${MANAGER_PYTHON:-}"', content)
        prompts = content.index("\nprompt_install_options\n")
        early = content.index("\n  require_manager_python\n")
        apt = content.index('apt_get_with_lock_retry install -y "${install_packages[@]}"\n')
        self.assertLess(early, prompts)
        self.assertTrue(content[apt:].startswith('apt_get_with_lock_retry install -y "${install_packages[@]}"\nrequire_manager_python\n'))
        self.assertIn("install_packages=(ca-certificates curl unzip openssl python3 tzdata)", content)

    def test_install_script_runs_embedded_python_with_selected_interpreter(self) -> None:
        content = INSTALL_SH.read_text(encoding="utf-8")

        self.assertEqual(content.count("\"$XRAY_PYTHON\" <<'PY'\n"), 2)
        self.assertIsNone(re.search(r"^\s*python3 <<'PY'", content, re.MULTILINE))

    def test_install_script_records_manager_python_in_server_env(self) -> None:
        content = INSTALL_SH.read_text(encoding="utf-8")
        start = content.index("cat >/usr/local/etc/xray/server.env <<EOF\n")
        server_env = content[start : content.index("\nEOF\n", start)]

        self.assertIn("\nMANAGER_TIMEZONE=${MANAGER_TIMEZONE}\nMANAGER_PYTHON=${XRAY_PYTHON}\n", server_env)

    def test_install_script_installs_every_wrapper_with_selected_shebang(self) -> None:
        content = INSTALL_SH.read_text(encoding="utf-8")

        for wrapper in manager_update.MANAGER_WRAPPERS:
            with self.subTest(wrapper=wrapper):
                self.assertIn(f"\ninstall_manager_wrapper {wrapper}\n", content)
                # install_manager_wrapper заменяет первую строку: в бандле она всегда `#!/usr/bin/env python3`.
                self.assertEqual((ROOT / wrapper).read_text(encoding="utf-8").splitlines()[0], "#!/usr/bin/env python3")
        self.assertNotIn('install -o root -g root -m 0755 "$SCRIPT_DIR/xray-', content)

    def test_install_manager_wrapper_replaces_shebang_with_selected_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out = Path(tmp_dir)
            code = function_source(INSTALL_SH, "install_manager_wrapper") + (
                'install() { /bin/cat "$7" >"$OUT/$(basename "$8")"; echo "$1 $2 $3 $4 $5 $6 $8" >>"$OUT/install.log"; }\n'
                "install_manager_wrapper xray-menu\n"
            )
            result = subprocess.run(
                [BASH, "-c", code],
                env={
                    "PATH": "/usr/bin:/bin",
                    "SCRIPT_DIR": str(ROOT),
                    "XRAY_PYTHON": "/usr/bin/python3.12",
                    "OUT": str(out),
                },
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            source_lines = (ROOT / "xray-menu").read_text(encoding="utf-8").splitlines(keepends=True)
            self.assertEqual((out / "xray-menu").read_text(encoding="utf-8"), "#!/usr/bin/python3.12\n" + "".join(source_lines[1:]))
            self.assertEqual(
                (out / "install.log").read_text(encoding="utf-8"),
                "-o root -g root -m 0755 /usr/local/sbin/xray-menu\n",
            )


class BootstrapManagerPythonTests(unittest.TestCase):
    def test_bootstrap_checks_python_before_download(self) -> None:
        content = BOOTSTRAP.read_text(encoding="utf-8")
        main = content[content.index("main() {") :]

        self.assertIn("apt_get_with_lock_retry install -y ca-certificates curl tar python3\n", content)
        self.assertIn('if [[ "$RUN_INSTALL" != "false" ]]; then\n    require_manager_python\n', main)
        self.assertLess(main.index("require_manager_python"), main.index('download_release "$version" "$tmp_dir"'))
        self.assertLess(main.index("install_dependencies"), main.index("require_manager_python"))

    def test_bootstrap_help_documents_manager_python(self) -> None:
        result = subprocess.run(
            [str(BOOTSTRAP), "--help"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MANAGER_PYTHON", result.stdout)
        self.assertIn("Python >= 3.12", result.stdout)


if __name__ == "__main__":
    unittest.main()
