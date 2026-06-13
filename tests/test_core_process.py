import subprocess
import unittest
from unittest import mock

from xray_vps_manager.core import process


class CoreProcessTests(unittest.TestCase):
    def completed(self, args, code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(args=args, returncode=code, stdout=stdout, stderr=stderr)

    def test_restart_systemd_unit_returns_after_successful_restart(self) -> None:
        with mock.patch.object(
            process.subprocess,
            "run",
            return_value=self.completed(["systemctl", "restart", "xray"]),
        ) as run:
            result = process.restart_systemd_unit("xray", retry_delay=0)

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
            process.time, "sleep"
        ) as sleep:
            result = process.restart_systemd_unit("xray")

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

        with mock.patch.object(process.subprocess, "run", side_effect=calls), mock.patch.object(process.time, "sleep"):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                process.restart_systemd_unit("xray")

        self.assertIn("first failure", caught.exception.stderr)
        self.assertIn("second failure", caught.exception.stderr)


if __name__ == "__main__":
    unittest.main()
