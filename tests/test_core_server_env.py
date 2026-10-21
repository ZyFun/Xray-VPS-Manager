from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from xray_vps_manager.core import errors
from xray_vps_manager.core import paths as core_paths
from xray_vps_manager.core.server_env import ORDERED_ENV_KEYS, read_server_env, update_server_env, write_server_env

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT = 30
# Как `xray-set-bypass add`: под manager_lock читает server.env в начале операции, а пишет после apply_config.
WRITER_CODE = """
import sys
import time
from pathlib import Path
from xray_vps_manager.core import locks, paths
from xray_vps_manager.core.server_env import read_server_env, write_server_env

root = Path(sys.argv[1])
paths.MANAGER_LOCK_PATH = root / "manager.lock"
env_path = root / "server.env"
with locks.manager_lock(purpose="add"):
    values = read_server_env(env_path)
    (root / "held").touch()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (root / "release").exists():
        time.sleep(0.02)
    values["ACTIVITY_XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY"] = "AsIs"
    write_server_env(values, env_path)
"""


class ServerEnvTests(unittest.TestCase):
    def test_read_server_env_ignores_comments_blank_lines_and_invalid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "server.env"
            env_path.write_text(
                "\n".join(
                    [
                        "# comment",
                        "",
                        "SERVER_ADDR=example.com",
                        "SERVER_NAME='Demo Server'",
                        'MANAGER_TIMEZONE="Europe/Moscow"',
                        "broken line",
                    ]
                )
                + "\n"
            )

            self.assertEqual(
                read_server_env(env_path),
                {
                    "SERVER_ADDR": "example.com",
                    "SERVER_NAME": "Demo Server",
                    "MANAGER_TIMEZONE": "Europe/Moscow",
                },
            )

    def test_read_server_env_can_require_valid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "server.env"
            env_path.write_text("SERVER_ADDR=example.com\nbroken line\n")

            with self.assertRaisesRegex(RuntimeError, "invalid line without '='"):
                read_server_env(env_path, strict=True)

    def test_read_server_env_can_require_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing = Path(tmp_dir) / "missing.env"

            self.assertEqual(read_server_env(missing), {})
            with self.assertRaisesRegex(RuntimeError, "not found:"):
                read_server_env(missing, require_exists=True)

    def test_write_server_env_orders_known_keys_and_drops_legacy_geoip_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "server.env"

            write_server_env(
                {
                    "CUSTOM": "value",
                    "SERVER_NAME": "Demo",
                    "SERVER_ADDR": "example.com",
                    "ACTIVITY_GEOIP_WARNING_CODE": "RU",
                    "MANAGER_TIMEZONE": "Europe/Moscow",
                },
                env_path,
            )

            self.assertEqual(
                env_path.read_text().splitlines(),
                [
                    "SERVER_ADDR=example.com",
                    "SERVER_NAME=Demo",
                    "MANAGER_TIMEZONE=Europe/Moscow",
                    "CUSTOM=value",
                ],
            )
            self.assertEqual(read_server_env(env_path)["CUSTOM"], "value")

    def test_manager_python_is_known_key_after_manager_timezone(self) -> None:
        # VIREI-81: интерпретатор обёрток хранится в server.env рядом с MANAGER_TIMEZONE.
        self.assertIn("MANAGER_PYTHON", ORDERED_ENV_KEYS)
        self.assertEqual(ORDERED_ENV_KEYS.index("MANAGER_PYTHON"), ORDERED_ENV_KEYS.index("MANAGER_TIMEZONE") + 1)

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "server.env"

            write_server_env(
                {"SECURITY_AUDIT_LAST_RUN": "2026-10-01T00:00:00Z", "MANAGER_PYTHON": "/usr/bin/python3.12", "MANAGER_TIMEZONE": ""},
                env_path,
            )

            self.assertEqual(
                env_path.read_text().splitlines(),
                ["MANAGER_TIMEZONE=", "MANAGER_PYTHON=/usr/bin/python3.12", "SECURITY_AUDIT_LAST_RUN=2026-10-01T00:00:00Z"],
            )

    def test_update_server_env_changes_given_keys_and_keeps_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "server.env"
            env_path.write_text("SERVER_ADDR=example.com\nCUSTOM=value\nMANAGER_PYTHON=/old/python3.12\nSERVER_NAME=Demo\n")

            with mock.patch.object(core_paths, "MANAGER_LOCK_PATH", Path(tmp_dir) / "manager.lock"):
                result = update_server_env({"MANAGER_PYTHON": "/usr/bin/python3.12"}, env_path)

            expected = {
                "SERVER_ADDR": "example.com",
                "SERVER_NAME": "Demo",
                "MANAGER_PYTHON": "/usr/bin/python3.12",
                "CUSTOM": "value",
            }
            self.assertEqual(result, expected)
            self.assertEqual(read_server_env(env_path), expected)
            self.assertEqual(
                env_path.read_text().splitlines(),
                ["SERVER_ADDR=example.com", "SERVER_NAME=Demo", "MANAGER_PYTHON=/usr/bin/python3.12", "CUSTOM=value"],
            )


class UpdateServerEnvLockTests(unittest.TestCase):
    """81-R02: точечная запись server.env идёт под manager_lock и не теряет ключи писателей под этой блокировкой."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env_path = self.root / "server.env"
        self.env_path.write_text("SERVER_NAME=Virei\n")
        lock_patch = mock.patch.object(core_paths, "MANAGER_LOCK_PATH", self.root / "manager.lock")
        lock_patch.start()
        self.addCleanup(lock_patch.stop)

    def start_writer(self) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        process = subprocess.Popen(
            [sys.executable, "-c", WRITER_CODE, str(self.root)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.finish_writer, process)
        deadline = time.monotonic() + CHILD_TIMEOUT
        while not (self.root / "held").exists():
            if process.poll() is not None:
                self.fail("server.env writer exited early: " + process.stderr.read())
            if time.monotonic() >= deadline:
                self.fail("server.env writer did not take manager.lock")
            time.sleep(0.02)
        return process

    def finish_writer(self, process: subprocess.Popen) -> None:
        (self.root / "release").touch()
        try:
            process.communicate(timeout=CHILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    def test_update_waits_for_writer_holding_manager_lock_and_keeps_its_key(self) -> None:
        writer = self.start_writer()
        release = threading.Timer(0.3, (self.root / "release").touch)
        release.start()
        self.addCleanup(release.cancel)

        update_server_env({"MANAGER_PYTHON": "/usr/bin/python3.12"}, self.env_path, timeout=CHILD_TIMEOUT)
        self.finish_writer(writer)

        self.assertEqual(writer.returncode, 0)
        self.assertEqual(
            read_server_env(self.env_path),
            {
                "SERVER_NAME": "Virei",
                "MANAGER_PYTHON": "/usr/bin/python3.12",
                "ACTIVITY_XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY": "AsIs",
            },
        )

    def test_update_gives_up_after_lock_timeout_without_writing(self) -> None:
        self.start_writer()

        with self.assertRaises(errors.LockTimeout):
            update_server_env({"MANAGER_PYTHON": "/usr/bin/python3.12"}, self.env_path, timeout=0.2)

        self.assertEqual(self.env_path.read_text(), "SERVER_NAME=Virei\n")


if __name__ == "__main__":
    unittest.main()
