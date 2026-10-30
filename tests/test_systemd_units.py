"""VIREI-84 (T1): тексты unit-файлов и списки units менеджера до шага 1 (`system/units.py`, `units sync`).

Golden-файлы лежат в `tests/fixtures/units/`; тексты после R1.4 берутся из канона system/units.py, вызов установщика проверяется отдельно.
Обновление golden — только явным `UPDATE_GOLDEN=1` с ревью диффа.
"""

import os
from pathlib import Path
import re
import subprocess
import unittest
from unittest import mock

from xray_vps_manager.system import units as system_units, unit_sync
from xray_vps_manager.commands import manager_update
from xray_vps_manager.commands import menu_xray_actions
from xray_vps_manager.commands import test as test_command
from xray_vps_manager.core import process as core_process

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "units"
UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN") == "1"

# Порядок, в котором install.sh пишет unit-файлы.
INSTALL_UNITS = [
    "xray.service",
    "xray-traffic-sync.service",
    "xray-telegram-poller.service",
    "xray-caddy-random-tls@.service",
    "xray-caddy-random-tls@.timer",
    "xray-traffic-sync.timer",
    "xray-client-expire.service",
    "xray-client-expire.timer",
]
UNIT_HEREDOC_RE = re.compile(r"^cat >/etc/systemd/system/(\S+) <<('?)EOF\2\n(.*?)\nEOF$", re.MULTILINE | re.DOTALL)
# Golden таймера raw-логов — для этого server.env; время ротации по умолчанию.
RAW_LOG_ENV = {"MANAGER_TIMEZONE": "Europe/Moscow"}


def install_sh_units() -> list:
    """Канон восьми прежних heredoc units; raw-log проверяется отдельно."""
    return [(name, "'", system_units.unit_text(name)) for name in system_units.UNIT_NAMES
            if not name.startswith("xray-raw-log-rotate.")]


class GoldenUnitCase(unittest.TestCase):
    def assert_golden(self, name: str, text: str) -> None:
        path = GOLDEN_DIR / name
        if UPDATE_GOLDEN:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.assertTrue(path.exists(), f"нет golden-файла {path}; создать — UPDATE_GOLDEN=1")
        self.assertEqual(text, path.read_text(), f"текст {name} отличается от golden; обновить — UPDATE_GOLDEN=1 с ревью диффа")


class InstallShUnitGoldenTests(GoldenUnitCase):
    def test_install_sh_writes_these_units_in_order_without_shell_expansion(self) -> None:
        units = install_sh_units()
        script = INSTALL_SH.read_text()
        self.assertFalse(UNIT_HEREDOC_RE.findall(script))
        self.assertIn("xray-vps-manager units sync --no-systemctl", script)

        self.assertEqual([name for name, _quote, _text in units], INSTALL_UNITS)
        self.assertEqual({quote for _name, quote, _text in units}, {"'"})

    def test_install_sh_unit_texts_match_golden_files(self) -> None:
        for name, _quote, text in install_sh_units():
            with self.subTest(name):
                self.assert_golden(name, text)

    def test_caddy_random_tls_units_repeat_install_sh_texts(self) -> None:
        """Перенесённые template units совпадают с исходными текстами установщика."""
        self.assert_golden("xray-caddy-random-tls@.service", system_units.random_tls_service_unit())
        self.assert_golden("xray-caddy-random-tls@.timer", system_units.random_tls_timer_unit())

    def test_raw_log_rotate_units_match_golden_files(self) -> None:
        """Unit-файлы ротации raw-логов генерирует Python (`xray-activity raw-log-timer-sync`), не install.sh."""
        self.assert_golden("xray-raw-log-rotate.service", unit_sync.raw_log_service_unit())
        self.assert_golden("xray-raw-log-rotate.timer", unit_sync.raw_log_timer_unit(dict(RAW_LOG_ENV)))

    def test_client_expire_timer_on_calendar_ignores_manager_timezone(self) -> None:
        """Текущее поведение, баг XR-REV-050: резервный таймер истечения сроков — 00:00 по зоне сервера, без MANAGER_TIMEZONE."""
        units = {name: text for name, _quote, text in install_sh_units()}

        self.assertEqual(
            [line for line in units["xray-client-expire.timer"].splitlines() if line.startswith("OnCalendar=")],
            ["OnCalendar=*-*-* 00:00:00"],
        )
        self.assertIn("OnCalendar=*-*-* 03:00:00 Europe/Moscow\n", unit_sync.raw_log_timer_unit(dict(RAW_LOG_ENV)))


class ManagerUnitListsTests(unittest.TestCase):
    """Какие units разные места менеджера считают своими (XR-REV-043)."""

    def test_menu_check_timers_shows_three_units(self) -> None:
        """Текущее поведение, баг XR-REV-043: «Проверить timers» не показывает raw-log и random-TLS таймеры."""
        calls = []

        menu_xray_actions.check_timers(calls.append)

        self.assertEqual(
            calls,
            [["systemctl", "status", "xray-traffic-sync.timer", "xray-client-expire.timer", "xray-telegram-poller.service", "--no-pager"]],
        )

    def test_xray_test_checks_three_timers_without_random_tls(self) -> None:
        """Текущее поведение, баг XR-REV-043: `xray-test` проверяет raw-log таймер, но не таймеры random-TLS."""
        commands = []

        def systemctl(command, **kwargs):
            commands.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")

        with mock.patch.object(subprocess, "run", side_effect=systemctl):
            detail = test_command.check_timers()

        self.assertEqual(detail, "traffic, raw-log rotation, and expiry timers active")
        self.assertEqual(
            commands,
            [
                ["systemctl", "is-active", "xray-traffic-sync.timer"],
                ["systemctl", "is-active", "xray-raw-log-rotate.timer"],
                ["systemctl", "is-active", "xray-client-expire.timer"],
            ],
        )

    def test_manager_update_and_restore_use_the_same_seven_units(self) -> None:
        expected = (
            "xray-traffic-sync.timer",
            "xray-raw-log-rotate.timer",
            "xray-client-expire.timer",
            "xray-traffic-sync.service",
            "xray-raw-log-rotate.service",
            "xray-client-expire.service",
            "xray-telegram-poller.service",
        )

        self.assertEqual(manager_update.MANAGER_SYSTEMD_UNITS, expected)
        self.assertEqual(core_process.WRITER_STOP_UNITS, expected)
        self.assertEqual(
            core_process.WRITER_RESTART_UNITS,
            ("xray-traffic-sync.timer", "xray-raw-log-rotate.timer", "xray-client-expire.timer", "xray-telegram-poller.service"),
        )


if __name__ == "__main__":
    unittest.main()
