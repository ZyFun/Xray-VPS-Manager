from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import sys
import unittest
from unittest.mock import patch

from xray_vps_manager import cli
from xray_vps_manager import runner


class CliSmokeTests(unittest.TestCase):
    def test_help_prints_available_commands_without_running_command(self) -> None:
        stdout = StringIO()
        with patch.object(sys, "argv", ["xray-vps-manager", "--help"]), redirect_stdout(stdout):
            cli.main()

        output = stdout.getvalue()
        self.assertIn("Usage: xray-vps-manager COMMAND", output)
        self.assertIn("caddy", output)
        self.assertIn("manager-update", output)
        self.assertIn("telegram", output)

    def test_unknown_command_exits_with_error(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with patch.object(sys, "argv", ["xray-vps-manager", "missing"]), redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                cli.main()

        self.assertEqual(caught.exception.code, 1)
        self.assertIn("Unknown command: missing", stderr.getvalue())

    def test_alias_is_translated_before_dispatch(self) -> None:
        calls = []

        def capture(command: str) -> None:
            calls.append((command, list(sys.argv)))

        with patch.object(sys, "argv", ["xray-vps-manager", "set-cascade", "--test"]):
            with patch("xray_vps_manager.cli.run_command", side_effect=capture) as run_command:
                cli.main()

        run_command.assert_called_once_with("cascade")
        self.assertEqual(calls, [("cascade", ["xray-cascade", "--test"])])

    def test_regular_command_dispatch_sets_legacy_argv_name(self) -> None:
        calls = []

        def capture(command: str) -> None:
            calls.append((command, list(sys.argv)))

        with patch.object(sys, "argv", ["xray-vps-manager", "telegram", "status"]):
            with patch("xray_vps_manager.cli.run_command", side_effect=capture) as run_command:
                cli.main()

        run_command.assert_called_once_with("telegram")
        self.assertEqual(calls, [("telegram", ["xray-telegram", "status"])])

    def test_runner_exposes_main_for_known_command(self) -> None:
        main = runner.command_main("caddy")

        self.assertTrue(callable(main))
        self.assertEqual(main.__name__, "main")


if __name__ == "__main__":
    unittest.main()
