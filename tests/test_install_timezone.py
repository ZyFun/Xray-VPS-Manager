"""Выбор, проверка и сохранение часового пояса при установке."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_install_manager_python import BASH, INSTALL_SH, function_source


class InstallTimezoneTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.server_env = Path(tmp.name) / "server.env"

        content = INSTALL_SH.read_text(encoding="utf-8")
        start = content.index("\nXRAY_GITHUB_ZIP_URL=")
        end = content.index('\nif [[ "$(id -u)" != "0" ]]; then', start)
        defaults = content[start:end]

        functions = (
            "validate_host", "validate_port", "validate_fingerprint", "validate_transport",
            "normalize_initial_protocol", "initial_has_vless", "initial_has_trojan",
            "validate_grpc_service_name", "validate_xhttp_path", "validate_trojan_ws_path",
            "validate_trojan_connection_name", "validate_xhttp_mode", "validate_server_name",
            "validate_manager_timezone", "resolve_install_path", "validate_xray_source",
            "validate_install_options", "timezone_candidates", "print_timezone_presets",
            "prompt_timezone_search", "prompt_manager_timezone", "prompt_install_options",
        )
        self.shell_setup = (
            "set -Eeuo pipefail\n"
            + defaults
            + "".join(function_source(INSTALL_SH, name) for name in functions)
        )

        # Выполняем штатный heredoc, перенаправляя запись во временный каталог.
        writer = "cat >/usr/local/etc/xray/server.env <<EOF\n"
        start = content.index(writer) + len(writer)
        end = content.index("\nEOF\n", start) + len("\nEOF\n")
        self.write_env = 'server_addr=192.0.2.1\ncat >"$1" <<EOF\n' + content[start:end]

    def run_timezone(
        self, *, timezone: str | None = None, select: str = ""
    ) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"]}
        if timezone is not None:
            env["MANAGER_TIMEZONE"] = timezone
        code = self.shell_setup + select + "prompt_install_options\n" + self.write_env
        return subprocess.run(
            [BASH, "-c", code, "install-timezone", str(self.server_env)],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

    def assert_saved_timezone(self, result: subprocess.CompletedProcess, timezone: str) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = [
            line for line in self.server_env.read_text(encoding="utf-8").splitlines()
            if line.startswith("MANAGER_TIMEZONE=")
        ]
        self.assertEqual(saved, [f"MANAGER_TIMEZONE={timezone}"])

    def test_utc_preset_selected_by_number_is_saved_verbatim(self) -> None:
        result = self.run_timezone(select=r"""
choice="$(print_timezone_presets | awk '$2 == "UTC" { sub(/\)$/, "", $1); print $1 }')"
[[ -n "$choice" ]]
prompt_manager_timezone <<< "$choice"
""")
        self.assert_saved_timezone(result, "UTC")

    def test_explicit_utc_from_environment_is_saved_verbatim(self) -> None:
        self.assert_saved_timezone(self.run_timezone(timezone="UTC"), "UTC")

    def test_enter_accepts_empty_timezone_default(self) -> None:
        result = self.run_timezone(select='prompt_manager_timezone <<< ""\n')
        self.assert_saved_timezone(result, "")

    def test_system_timezone_selection_clears_prefilled_timezone(self) -> None:
        result = self.run_timezone(timezone="Europe/Moscow", select='prompt_manager_timezone <<< "0"\n')
        self.assert_saved_timezone(result, "")

    def test_timezones_with_slash_are_still_accepted(self) -> None:
        for timezone in ("Europe/Moscow", "Etc/UTC"):
            with self.subTest(timezone=timezone):
                self.assert_saved_timezone(self.run_timezone(timezone=timezone), timezone)

    def test_invalid_timezone_formats_are_still_rejected(self) -> None:
        for timezone in (
            "/etc/localtime", "Europe/../Moscow", "Europe/Mos cow", "Europe//Moscow",
            "utc", "UTC ", "GMT",
        ):
            with self.subTest(timezone=timezone):
                result = self.run_timezone(timezone=timezone)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(
                    result.stderr.strip(),
                    "MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time.",
                )
                self.assertFalse(self.server_env.exists())
