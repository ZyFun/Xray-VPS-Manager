"""Контракт core/locks.manager_lock и иерархия core/errors (целевая архитектура, 3.1 и 3.4)."""

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from xray_vps_manager.core import errors
from xray_vps_manager.core import locks
from xray_vps_manager.core import paths as core_paths

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT = 30
HOLDER_CODE = """
import sys
import time
from pathlib import Path
from xray_vps_manager.core import locks, paths

root = Path(sys.argv[1])
hold_seconds = float(sys.argv[2])
paths.MANAGER_LOCK_PATH = root / "manager.lock"
with locks.manager_lock(purpose="holder"):
    (root / "held").touch()
    deadline = time.monotonic() + hold_seconds
    while time.monotonic() < deadline and not (root / "release").exists():
        time.sleep(0.02)
"""


def lock_is_free(path: Path) -> bool:
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


class ManagerLockTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lock_path = self.root / "manager.lock"
        lock_patch = mock.patch.object(core_paths, "MANAGER_LOCK_PATH", self.lock_path)
        lock_patch.start()
        self.addCleanup(lock_patch.stop)

    def start_holder(self, hold_seconds: float) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        process = subprocess.Popen(
            [sys.executable, "-c", HOLDER_CODE, str(self.root), str(hold_seconds)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop_holder, process)
        deadline = time.monotonic() + CHILD_TIMEOUT
        while not (self.root / "held").exists():
            if process.poll() is not None:
                self.fail("lock holder exited early: " + process.stderr.read())
            if time.monotonic() >= deadline:
                self.fail("lock holder did not take manager.lock")
            time.sleep(0.02)
        return process

    def stop_holder(self, process: subprocess.Popen) -> None:
        (self.root / "release").touch()
        try:
            process.communicate(timeout=CHILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    def test_writes_holder_while_held_and_clears_it_after_release(self) -> None:
        with locks.manager_lock(purpose="add"):
            holder = self.lock_path.read_text()
            self.assertFalse(lock_is_free(self.lock_path))

        self.assertRegex(holder, rf"^pid={os.getpid()} cmd=.+ add since=\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z\n$")
        self.assertEqual(self.lock_path.read_text(), "")
        self.assertTrue(lock_is_free(self.lock_path))

    def test_nested_lock_in_same_process_is_reentrant(self) -> None:
        with locks.manager_lock(timeout=0.2, purpose="outer"):
            with locks.manager_lock(timeout=0.2, purpose="inner"):
                self.assertFalse(lock_is_free(self.lock_path))
            self.assertFalse(lock_is_free(self.lock_path))
            self.assertIn("outer", self.lock_path.read_text())

        self.assertTrue(lock_is_free(self.lock_path))

    def test_releases_lock_when_body_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            with locks.manager_lock(purpose="boom"):
                raise RuntimeError("boom")

        self.assertTrue(lock_is_free(self.lock_path))
        with locks.manager_lock(timeout=0.2):
            pass

    def test_timeout_reports_holder_from_other_process(self) -> None:
        holder = self.start_holder(CHILD_TIMEOUT)
        started = time.monotonic()

        with self.assertRaises(errors.LockTimeout) as raised:
            with locks.manager_lock(timeout=0.3, purpose="add"):
                self.fail("lock must not be acquired while another process holds it")

        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        exc = raised.exception
        self.assertIsInstance(exc, errors.ManagerError)
        self.assertRegex(
            exc.message,
            rf"^Другая операция менеджера ещё выполняется: pid={holder.pid} cmd=.+ holder since=\S+Z$",
        )
        self.assertEqual(str(exc), exc.message)
        self.assertTrue(exc.hint)

    def test_waits_until_other_process_releases_lock(self) -> None:
        self.start_holder(0.5)
        started = time.monotonic()

        with locks.manager_lock(timeout=CHILD_TIMEOUT, purpose="add"):
            self.assertIn(f"pid={os.getpid()} ", self.lock_path.read_text())

        self.assertLess(time.monotonic() - started, CHILD_TIMEOUT)

    def test_timeout_is_required(self) -> None:
        with self.assertRaises(TypeError):
            with locks.manager_lock(timeout=None):
                pass
        self.assertTrue(lock_is_free(self.lock_path))


class ManagerErrorTests(unittest.TestCase):
    def test_manager_error_keeps_message_hint_and_detail(self) -> None:
        exc = errors.ManagerError("Client not found: alice", hint="xray-client list", detail="trace")

        self.assertEqual(str(exc), "Client not found: alice")
        self.assertEqual((exc.message, exc.hint, exc.detail), ("Client not found: alice", "xray-client list", "trace"))
        self.assertEqual(exc.exit_code, 1)

    def test_validation_error_is_value_error(self) -> None:
        with self.assertRaises(ValueError):
            raise errors.ValidationError("bad input")

    def test_hierarchy_matches_contract(self) -> None:
        for cls in (
            errors.ValidationError,
            errors.NotFoundError,
            errors.ConflictError,
            errors.StateError,
            errors.LockTimeout,
            errors.ExternalCommandError,
            errors.ApplyConfigError,
            errors.RestoreError,
            errors.Cancelled,
        ):
            self.assertTrue(issubclass(cls, errors.ManagerError), cls)
        self.assertTrue(issubclass(errors.ConfigInvariantError, errors.ApplyConfigError))
        self.assertEqual(errors.Cancelled("stop").exit_code, 130)

    def test_structured_errors_keep_fields(self) -> None:
        command_error = errors.ExternalCommandError(
            "systemctl failed",
            command=["systemctl", "restart", "xray"],
            returncode=1,
            output="failed",
        )
        apply_error = errors.ApplyConfigError(
            "Xray did not start",
            stage="verify",
            backup=Path("/tmp/config.json.bak.1"),
            restored=True,
        )

        self.assertEqual((command_error.command, command_error.returncode, command_error.output), (["systemctl", "restart", "xray"], 1, "failed"))
        self.assertEqual((apply_error.stage, apply_error.backup, apply_error.restored), ("verify", Path("/tmp/config.json.bak.1"), True))


if __name__ == "__main__":
    unittest.main()
