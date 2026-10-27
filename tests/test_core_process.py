import subprocess
import unittest
from unittest import mock

from xray_vps_manager.core import process, systemd


class CoreProcessTests(unittest.TestCase):
    def completed(self, args, code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(args=args, returncode=code, stdout=stdout, stderr=stderr)

    def test_restart_systemd_unit_returns_after_successful_restart(self) -> None:
        with mock.patch.object(
            process.subprocess,
            "run",
            return_value=self.completed(["systemctl", "restart", "xray"]),
        ) as run:
            result = process.restart_systemd_unit("xray", retry_delay=0, verify_timeout=0)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["systemctl", "restart", "xray"])

    def test_restart_systemd_unit_resets_failed_state_before_retry(self) -> None:
        calls = [
            self.completed(["systemctl", "restart", "xray"], code=1, stderr="start-limit-hit"),
            self.completed(["systemctl", "reset-failed", "xray"]),
            self.completed(["systemctl", "restart", "xray"]),
        ]

        with mock.patch.object(process.subprocess, "run", side_effect=calls) as run, mock.patch.object(
            systemd.time, "sleep"
        ) as sleep:
            result = process.restart_systemd_unit("xray", verify_timeout=0)

        self.assertEqual(result.returncode, 0)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["systemctl", "restart", "xray"],
            ["systemctl", "reset-failed", "xray"],
            ["systemctl", "restart", "xray"],
        ])
        sleep.assert_called_once_with(1.0)

    def test_restart_systemd_unit_raises_when_retry_fails(self) -> None:
        calls = [
            self.completed(["systemctl", "restart", "xray"], code=1, stderr="first failure"),
            self.completed(["systemctl", "reset-failed", "xray"]),
            self.completed(["systemctl", "restart", "xray"], code=1, stderr="second failure"),
        ]

        with mock.patch.object(process.subprocess, "run", side_effect=calls), mock.patch.object(systemd.time, "sleep"):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                process.restart_systemd_unit("xray", verify_timeout=0)

        self.assertIn("first failure", caught.exception.stderr)
        self.assertIn("second failure", caught.exception.stderr)

    def test_restart_systemd_unit_retry_passes_timeouts_and_skips_sleep_without_delay(self) -> None:
        """VIREI-84 (T1): restart — с таймаутом вызова, reset-failed — всегда 10 с; retry_delay=0 — без паузы."""
        calls = [
            self.completed(["systemctl", "restart", "xray"], code=1),
            self.completed(["systemctl", "reset-failed", "xray"]),
            self.completed(["systemctl", "restart", "xray"]),
        ]

        with mock.patch.object(process.subprocess, "run", side_effect=calls) as run, mock.patch.object(
            systemd.time, "sleep"
        ) as sleep:
            process.restart_systemd_unit("xray", timeout=45, retry_delay=0, verify_timeout=0)

        self.assertEqual(
            [(call.args[0][1], call.kwargs["timeout"]) for call in run.call_args_list],
            [("restart", 45), ("reset-failed", 10), ("restart", 45)],
        )
        sleep.assert_not_called()

    def test_verified_restart_prepares_unit_with_timeouts_before_restart(self) -> None:
        """VIREI-84 (T1): проверка старта из A4 — stop и restart с таймаутом вызова, reset-failed — 10 с, show — остаток окна, не меньше 2 с."""
        commands = []

        def systemctl(command, **kwargs):
            commands.append((command[1], kwargs["timeout"]))
            stdout = "ActiveState=active\nSubState=running\nNRestarts=0\n" if command[1] == "show" else ""
            return self.completed(command, stdout=stdout)

        clock = mock.Mock()
        clock.now = 100.0
        clock.monotonic.side_effect = lambda: clock.now
        clock.sleep.side_effect = lambda seconds: setattr(clock, "now", clock.now + seconds)
        with mock.patch.object(process.subprocess, "run", side_effect=systemctl), mock.patch.object(systemd, "time", clock):
            process.restart_systemd_unit("xray", timeout=45, verify_timeout=3.0, poll_interval=3.0)

        self.assertEqual(commands[:3], [("stop", 45), ("reset-failed", 10), ("restart", 45)])
        self.assertEqual(commands[3:], [("show", 3.0), ("show", 2.0)])


if __name__ == "__main__":
    unittest.main()
