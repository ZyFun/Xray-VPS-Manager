"""VIREI-84 (T1): коды выхода и вывод трёх вариантов `die()` до шага 1 (R1.5: единая проверка root и перехват в `runner`).

Каждая команда запускается отдельным процессом так же, как обёртка `/usr/local/sbin/xray-*`: `runner.run_command`.
Варианты `die()`: `xray-client` — `ERROR:` в stderr; `xray-update` — `FAIL:` в stdout и код из `die(message, code)`;
`xray-menu` — `ERROR:` в stderr для проверки root и `SystemExit(message)` из шапки (сообщение без префикса).
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT = 30
STRICT_MESSAGE = "MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time."
# Как обёртка, но с подменённым euid и путями: argv — команда, euid, JSON [[модуль, атрибут, путь]], аргументы команды.
CHILD_CODE = """
import importlib
import json
import os
import sys
from pathlib import Path

command, euid, patches = sys.argv[1], int(sys.argv[2]), json.loads(sys.argv[3])
os.geteuid = lambda: euid
for module_name, attribute, value in patches:
    setattr(importlib.import_module(module_name), attribute, Path(value))
sys.argv = [f"xray-{command}", *sys.argv[4:]]
from xray_vps_manager.runner import run_command
run_command(command)
"""


class DieExitCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def run_command(self, command: str, *args: str, root: bool = True, patches=()) -> tuple:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "-c", CHILD_CODE, command, "0" if root else "1000", json.dumps(list(patches)), *args],
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            timeout=CHILD_TIMEOUT,
        )
        return result.returncode, result.stdout, result.stderr

    def test_client_die_prints_error_to_stderr_and_exits_1(self) -> None:
        self.assertEqual(
            self.run_command("client", "list", root=False),
            (1, "", "ERROR: Run this script as root.\n"),
        )
        self.assertEqual(
            self.run_command("client", "set-timezone", "Mars/Base"),
            (1, "", f"ERROR: {STRICT_MESSAGE}\n"),
        )

    def test_update_die_prints_fail_to_stdout_and_exits_1(self) -> None:
        self.assertEqual(
            self.run_command("update", "--check", root=False),
            (1, "FAIL: Run this script as root.\n", ""),
        )
        self.assertEqual(
            self.run_command(
                "update",
                "--rollback",
                patches=[("xray_vps_manager.commands.update", "BACKUP_DIR", str(self.root / "missing"))],
            ),
            (1, "FAIL: Нет сохранённых предыдущих версий Xray для отката.\n", ""),
        )

    def test_menu_root_check_prints_error_to_stderr_and_exits_1(self) -> None:
        self.assertEqual(self.run_command("menu", root=False), (1, "", "ERROR: Run this script as root.\n"))

    def test_menu_header_system_exit_prints_bare_message_and_exits_1(self) -> None:
        """Текущее поведение, баг XR-REV-033: невалидная зона закрывает меню до первой строки шапки."""
        env_path = self.root / "server.env"
        env_path.write_text("MANAGER_TIMEZONE=Mars/Base\n")

        self.assertEqual(
            self.run_command(
                "menu",
                patches=[("xray_vps_manager.commands.menu_status", "SERVER_ENV_PATH", str(env_path))],
            ),
            (1, "\n", f"{STRICT_MESSAGE}\n"),
        )


if __name__ == "__main__":
    unittest.main()
