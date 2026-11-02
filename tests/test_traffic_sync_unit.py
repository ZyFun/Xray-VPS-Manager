"""Минутный unit xray-traffic-sync.service: независимые шаги ExecStart=- (VIREI-69, XR-REV-008)."""

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import unittest

from xray_vps_manager.commands import test as test_command
from xray_vps_manager.core import process as core_process
from xray_vps_manager.core.errors import ExternalCommandError


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"

STEP_COMMANDS = [
    "/usr/local/sbin/xray-traffic-sync --quiet",
    "/usr/local/sbin/xray-activity sync --quiet",
    "/usr/local/sbin/xray-telegram notify-geoip --quiet",
    "/usr/local/sbin/xray-client enforce-limits --quiet",
    "/usr/local/sbin/xray-client expire-due --quiet",
    "/usr/local/sbin/xray-telegram notify-expiry --quiet",
    "/usr/local/sbin/xray-telegram notify-daily-summary --quiet",
]


def unit_block(name):
    # Установщик теперь вызывает тот же канон вместо heredoc.
    from xray_vps_manager.system.units import unit_text
    return unit_text(name)


def directive_lines(block: str, directive: str) -> list:
    return [line for line in block.splitlines() if line.startswith(directive + "=")]


def show_line(argv: str, ignore_errors: bool) -> str:
    # Формат `systemctl show --property=ExecStart`: одна строка на каждую команду итогового списка.
    program = argv.split(" ", 1)[0]
    ignore = "yes" if ignore_errors else "no"
    return (
        f"ExecStart={{ path={program} ; argv[]={argv} ; ignore_errors={ignore} ; "
        "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


def systemctl_show(entries: list) -> str:
    return "\n".join(show_line(argv, ignore) for argv, ignore in entries) + "\n"


NEW_UNIT = systemctl_show([(command, True) for command in STEP_COMMANDS])
OLD_UNIT = systemctl_show([(command, False) for command in STEP_COMMANDS])
SHOW_COMMAND = ["systemctl", "show", "--property=ExecStart", "xray-traffic-sync.service"]


class InstallTrafficSyncUnitTests(unittest.TestCase):
    def test_all_seven_steps_are_independent_and_keep_order(self) -> None:
        lines = directive_lines(unit_block("xray-traffic-sync.service"), "ExecStart")

        self.assertEqual(len(lines), 7)
        for line in lines:
            self.assertTrue(line.startswith("ExecStart=-/"), line)
        self.assertEqual([line[len("ExecStart=-"):] for line in lines], STEP_COMMANDS)

    def test_xray_test_expects_the_same_lines_as_install(self) -> None:
        lines = directive_lines(unit_block("xray-traffic-sync.service"), "ExecStart")

        self.assertEqual(test_command.TRAFFIC_SERVICE_COMMANDS, lines)

    def test_xray_service_exec_stop_is_unchanged(self) -> None:
        block = unit_block("xray.service")

        self.assertEqual(
            directive_lines(block, "ExecStop"),
            ["ExecStop=-+/usr/local/sbin/xray-traffic-sync --quiet"],
        )
        self.assertEqual(
            directive_lines(block, "ExecStart"),
            ["ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray/config.json"],
        )

    def test_client_expire_service_is_unchanged(self) -> None:
        block = unit_block("xray-client-expire.service")

        self.assertEqual(
            directive_lines(block, "ExecStart"),
            ["ExecStart=/usr/local/sbin/xray-client expire-due --quiet"],
        )


def run_traffic_sync_checks(diag) -> None:
    # Повторяет регистрацию в run_diagnostics(); test_run_diagnostics_registers_independence_check_as_warning сверяет её с кодом.
    diag.check("Traffic sync service", test_command.check_traffic_sync_service)
    diag.check(
        "Traffic sync step independence",
        test_command.check_traffic_sync_step_independence,
        fatal=False,
    )


class SystemdExecCommandsTests(unittest.TestCase):
    def show(self, stdout: str, returncode: int = 0):
        result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr="unit not found" if returncode else "")
        with mock.patch.object(core_process, "run_capture", return_value=result) as run_capture:
            value = core_process.systemd_exec_commands("xray-traffic-sync.service")
        run_capture.assert_called_once_with(SHOW_COMMAND, timeout=10)
        return value

    def test_parses_argv_and_ignore_errors_in_order(self) -> None:
        stdout = systemctl_show([(STEP_COMMANDS[0], True), (STEP_COMMANDS[1], False)])

        self.assertEqual(self.show(stdout), [(STEP_COMMANDS[0], True), (STEP_COMMANDS[1], False)])

    def test_empty_exec_start_list(self) -> None:
        self.assertEqual(self.show("ExecStart=\n"), [])

    def test_systemctl_failure_raises_external_command_error(self) -> None:
        with self.assertRaisesRegex(ExternalCommandError, "unit not found"):
            self.show("", returncode=1)


class CheckTrafficSyncServiceTests(unittest.TestCase):
    def run_with_unit(self, func, show_output: str):
        result = SimpleNamespace(returncode=0, stdout=show_output, stderr="")
        with mock.patch.object(core_process, "run_capture", return_value=result) as run_capture:
            value = func()
        run_capture.assert_called_once_with(SHOW_COMMAND, timeout=10)
        return value

    def test_new_unit_passes_both_checks(self) -> None:
        self.assertIn("xray-traffic-sync.service", self.run_with_unit(test_command.check_traffic_sync_service, NEW_UNIT))
        self.assertIn(
            "independent",
            self.run_with_unit(test_command.check_traffic_sync_step_independence, NEW_UNIT),
        )

    def test_old_unit_still_has_all_steps(self) -> None:
        self.assertIn("xray-traffic-sync.service", self.run_with_unit(test_command.check_traffic_sync_service, OLD_UNIT))

    def test_old_unit_steps_are_reported_as_dependent(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "without '-' prefix: 7 of 7"):
            self.run_with_unit(test_command.check_traffic_sync_step_independence, OLD_UNIT)

    def test_partially_updated_unit_lists_dependent_steps(self) -> None:
        entries = [(command, True) for command in STEP_COMMANDS]
        entries[4] = (STEP_COMMANDS[4], False)

        with self.assertRaisesRegex(RuntimeError, "1 of 7.*ExecStart=/usr/local/sbin/xray-client expire-due"):
            self.run_with_unit(test_command.check_traffic_sync_step_independence, systemctl_show(entries))

    def test_drop_in_resetting_to_old_commands_is_reported_as_dependent(self) -> None:
        """69-R01: основной unit с `-`, drop-in сбрасывает `ExecStart=` и задаёт старые команды.

        `systemctl show` отдаёт итоговый список (старые команды, ignore_errors=no), поэтому
        строки `ExecStart=-…` основного unit или комментариев не дают ложного OK.
        """
        with self.assertRaisesRegex(RuntimeError, "7 of 7"):
            self.run_with_unit(test_command.check_traffic_sync_step_independence, OLD_UNIT)

    def test_drop_in_appending_new_commands_without_reset_is_reported_as_dependent(self) -> None:
        """69-R01, круг 2: старый unit без "-" и drop-in, добавивший те же шаги с "-" без сброса `ExecStart=`.

        У `Type=oneshot` выполняются все 14 команд; первая старая команда с ошибкой обрывает цепочку.
        """
        entries = [(command, False) for command in STEP_COMMANDS] + [(command, True) for command in STEP_COMMANDS]
        show_output = systemctl_show(entries)

        self.assertIn("xray-traffic-sync.service", self.run_with_unit(test_command.check_traffic_sync_service, show_output))
        with self.assertRaisesRegex(RuntimeError, "7 of 7"):
            self.run_with_unit(test_command.check_traffic_sync_step_independence, show_output)

    def test_foreign_command_without_prefix_before_steps_is_reported(self) -> None:
        """69-R03: drop-in сбрасывает `ExecStart=`, добавляет постороннюю команду без "-", затем семь шагов с "-"."""
        entries = [("/usr/local/bin/custom-hook --check", False)] + [(command, True) for command in STEP_COMMANDS]
        show_output = systemctl_show(entries)

        self.assertIn("xray-traffic-sync.service", self.run_with_unit(test_command.check_traffic_sync_service, show_output))
        with self.assertRaisesRegex(RuntimeError, "other commands without '-' prefix before later steps: ExecStart=/usr/local/bin/custom-hook --check"):
            self.run_with_unit(test_command.check_traffic_sync_step_independence, show_output)

    def test_foreign_command_without_prefix_at_the_end_does_not_block_steps(self) -> None:
        entries = [(command, True) for command in STEP_COMMANDS] + [("/usr/local/bin/custom-hook --check", False)]

        self.assertIn(
            "independent",
            self.run_with_unit(test_command.check_traffic_sync_step_independence, systemctl_show(entries)),
        )

    def test_missing_step_fails_in_either_form(self) -> None:
        for ignore_errors in (True, False):
            with self.subTest(ignore_errors=ignore_errors):
                entries = [(command, ignore_errors) for command in STEP_COMMANDS if "expire-due" not in command]
                with self.assertRaisesRegex(RuntimeError, "missing service command.*expire-due"):
                    self.run_with_unit(test_command.check_traffic_sync_service, systemctl_show(entries))

    def test_steps_with_extra_arguments_are_missing(self) -> None:
        entries = [(command, True) for command in STEP_COMMANDS]
        entries[0] = (STEP_COMMANDS[0] + " --verbose", True)

        with self.assertRaisesRegex(RuntimeError, "missing service command.*xray-traffic-sync --quiet"):
            self.run_with_unit(test_command.check_traffic_sync_service, systemctl_show(entries))

    def test_old_unit_is_warning_not_failure_in_diagnostics(self) -> None:
        diag = test_command.Diagnostics()
        result = SimpleNamespace(returncode=0, stdout=OLD_UNIT, stderr="")
        with mock.patch.object(core_process, "run_capture", return_value=result), mock.patch("builtins.print"):
            run_traffic_sync_checks(diag)

        self.assertEqual(diag.failures, [])
        self.assertEqual(len(diag.warnings), 1)
        self.assertIn("Traffic sync step independence", diag.warnings[0])

    def test_run_diagnostics_registers_independence_check_as_warning(self) -> None:
        source = " ".join(inspect.getsource(test_command.run_diagnostics).split())

        self.assertIn('diag.check("Traffic sync service", check_traffic_sync_service)', source)
        self.assertIn(
            'diag.check("Traffic sync step independence", check_traffic_sync_step_independence, fatal=False)',
            source,
        )

    def test_new_unit_has_no_warnings_in_diagnostics(self) -> None:
        diag = test_command.Diagnostics()
        result = SimpleNamespace(returncode=0, stdout=NEW_UNIT, stderr="")
        with mock.patch.object(core_process, "run_capture", return_value=result), mock.patch("builtins.print"):
            run_traffic_sync_checks(diag)

        self.assertEqual(diag.failures, [])
        self.assertEqual(diag.warnings, [])


if __name__ == "__main__":
    unittest.main()
