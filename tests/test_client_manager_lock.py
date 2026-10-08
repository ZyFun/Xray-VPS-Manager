"""Регрессия XR-REV-001: писатели client-DB в xray-client работают под manager.lock."""

import ast
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.commands import client as client_command
from xray_vps_manager.commands import traffic_sync
from xray_vps_manager.core import locks as core_locks
from xray_vps_manager.core import paths as core_paths
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import settings as sqlite_settings

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
PACKAGE_DIR = REPO_ROOT / "xray_vps_manager"
CLIENT_DB_WRITES = {
    "save_db",
    "save_config_restart_xray_and_db",
    "normalize_access_deadlines",
    "save_server_env_values",
}
WORKER_TIMEOUT = 30
REAL_ADD_CLIENT = client_command.client_crud.add_client
# Сколько медленный писатель держит окно между load_db и save_db, ожидая второго писателя.
SLOW_WRITER_WINDOW = "2.0"


def reality_inbound(tag: str, port: int) -> dict:
    return {
        "tag": tag,
        "listen": "0.0.0.0",
        "port": port,
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "dest": "example.com:443",
                "serverNames": ["example.com"],
                "privateKey": "test-private-key",
                "shortIds": [""],
            },
        },
    }


def make_sandbox(root: Path) -> None:
    config = {
        "inbounds": [reality_inbound("vless-reality", 443), reality_inbound("vless-reality-2", 8443)],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "blocked", "protocol": "blackhole"}],
        "routing": {"rules": []},
    }
    (root / "config.json").write_text(json.dumps(config))
    connection = database.open_database(root / "manager.db")
    try:
        sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
    finally:
        connection.close()


def write_config(root: Path, config: dict, suffix: str = "") -> Path:
    config_path = root / "config.json"
    backup = root / f"config.json.bak{suffix}.{time.monotonic_ns()}"
    shutil.copy2(config_path, backup)
    config_path.write_text(json.dumps(config))
    return backup


def add_client_without_xray(*args, **kwargs):
    return REAL_ADD_CLIENT(*args, uuid_factory=lambda: str(uuid.uuid4()), **kwargs)


@contextmanager
def sandbox_patches(root: Path, *, restart=None, config_suffix: str = ""):
    with mock.patch.object(core_paths, "MANAGER_LOCK_PATH", root / "manager.lock"), \
        mock.patch.object(database, "MANAGER_DB_PATH", root / "manager.db"), \
        mock.patch.object(client_command, "CONFIG_PATH", root / "config.json"), \
        mock.patch.object(client_command, "save_config", lambda config: write_config(root, config, config_suffix)), \
        mock.patch.object(client_command, "restart_xray_with_config_test", restart or (lambda: None)), \
        mock.patch.object(client_command, "link_for", return_value="vless://test"), \
        mock.patch.object(client_command, "print_payment_summary"), \
        mock.patch.object(client_command.client_connections, "server_env_values", return_value={}), \
        mock.patch.object(client_command.client_crud, "add_client", side_effect=add_client_without_xray):
        yield


def db_client_names(root: Path) -> set:
    return set(client_repository.load_db_sql(db_path=root / "manager.db")["clients"])


def config_client_names(root: Path) -> set:
    config = json.loads((root / "config.json").read_text())
    return {
        item["email"].split("|", 1)[0]
        for inbound in config["inbounds"]
        for item in inbound["settings"]["clients"]
    }


def wait_for_file(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


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


@contextmanager
def foreign_lock_holder(path: Path, holder: str):
    """Занимает manager.lock отдельным open file description, как чужой процесс."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (holder + "\n").encode("utf-8"))
        yield
    finally:
        os.close(fd)


def is_client_db_lock(node: ast.AST) -> bool:
    return isinstance(node, ast.With) and any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Name)
        and item.context_expr.func.id == "client_db_lock"
        for item in node.items
    )


def is_dead_json_source_branch(node: ast.AST) -> bool:
    # `if read_result.source == "json": save_db(db)` в командах чтения: load_db_sql_result
    # всегда возвращает source="sqlite", ветка не выполняется.
    test = getattr(node, "test", None)
    return (
        isinstance(node, ast.If)
        and isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Attribute)
        and test.left.attr == "source"
        and any(isinstance(item, ast.Constant) and item.value == "json" for item in test.comparators)
    )


def unlocked_client_db_writes(tree: ast.Module) -> list:
    found = []

    def visit(node: ast.AST, stack: list, function_name: str) -> None:
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in CLIENT_DB_WRITES
                and not any(is_client_db_lock(item) or is_dead_json_source_branch(item) for item in stack)
            ):
                found.append(f"{function_name}:{child.lineno} {child.func.id}")
            visit(child, stack + [child], function_name)

    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and (
            function.name.startswith("cmd_") or function.name == "run_access_update"
        ):
            visit(function, [], function.name)
    return found


def add_worker(argv: list) -> int:
    """Отдельный процесс xray-client add в общей песочнице.

    role=slow: после load_db ждёт завершения второго писателя внутри окна «xray -test + рестарт».
    role=fast: стартует, когда медленный писатель уже прочитал снимок.
    """
    root, name, role, window = Path(argv[0]), argv[1], argv[2], float(argv[3])

    def restart() -> None:
        if role == "slow":
            (root / "slow-loaded").touch()
            wait_for_file(root / "fast-done", window)

    if role == "fast" and not wait_for_file(root / "slow-loaded", WORKER_TIMEOUT):
        print("slow writer did not load the snapshot", file=sys.stderr)
        return 2
    try:
        with sandbox_patches(root, restart=restart, config_suffix=f".{name}"), redirect_stdout(StringIO()):
            client_command.cmd_add(name, prompt_for_access=False, connection_tag="vless-reality")
    finally:
        if role == "fast":
            (root / "fast-done").touch()
    return 0


def start_worker(*args: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(TESTS_DIR)])
    code = "import sys, test_client_manager_lock as t; sys.exit(t.add_worker(sys.argv[1:]))"
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class ClientWritersLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        make_sandbox(self.root)

    def test_concurrent_add_does_not_lose_client(self) -> None:
        """XR-REV-001: клиент, добавленный между load_db и save_db другого процесса, не теряется."""
        slow = start_worker(str(self.root), "alice", "slow", SLOW_WRITER_WINDOW)
        fast = start_worker(str(self.root), "bob", "fast", SLOW_WRITER_WINDOW)
        results = []
        for process in (slow, fast):
            try:
                stdout, stderr = process.communicate(timeout=WORKER_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            results.append((process.returncode, stdout, stderr))

        for returncode, _stdout, stderr in results:
            self.assertEqual(returncode, 0, stderr)
        self.assertEqual(db_client_names(self.root), {"alice", "bob"})
        self.assertEqual(config_client_names(self.root), {"alice", "bob"})

    def test_add_asks_access_days_before_taking_lock(self) -> None:
        lock_path = self.root / "manager.lock"
        events = []
        real_load_db = client_command.load_db

        def fake_input(_prompt: str) -> str:
            events.append(("input", lock_is_free(lock_path)))
            return "30"

        def spy_load_db():
            events.append(("load_db", lock_is_free(lock_path)))
            return real_load_db()

        with sandbox_patches(self.root), \
            mock.patch.object(client_command.sys.stdin, "isatty", return_value=True), \
            mock.patch("builtins.input", side_effect=fake_input), \
            mock.patch.object(client_command, "load_db", side_effect=spy_load_db), \
            mock.patch.object(client_command.client_settings, "manager_timezone_label", return_value="UTC"), \
            redirect_stdout(StringIO()):
            client_command.cmd_add("alice", connection_tag="vless-reality")

        self.assertEqual(events, [("input", True), ("load_db", False)])
        self.assertTrue(lock_is_free(lock_path))
        entry = client_repository.load_db_sql(db_path=self.root / "manager.db")["clients"]["alice"]
        self.assertTrue(entry.get("expiresAt"))

    def test_add_credential_for_existing_client_does_not_ask_access_days(self) -> None:
        with sandbox_patches(self.root), redirect_stdout(StringIO()):
            client_command.cmd_add("alice", prompt_for_access=False, connection_tag="vless-reality")

        with sandbox_patches(self.root), \
            mock.patch.object(client_command.sys.stdin, "isatty", return_value=True), \
            mock.patch("builtins.input", side_effect=AssertionError("ACCESS_DAYS must not be asked")), \
            redirect_stdout(StringIO()):
            client_command.cmd_add("alice", connection_tag="vless-reality-2")

        entry = client_repository.load_db_sql(db_path=self.root / "manager.db")["clients"]["alice"]
        self.assertEqual(set(entry["credentials"]), {"vless-reality", "vless-reality-2"})

    def test_add_stops_when_client_was_removed_before_lock(self) -> None:
        """59-R01: вопрос ACCESS_DAYS пропущен по снимку, где клиент был, а под lock его уже нет."""
        with sandbox_patches(self.root), redirect_stdout(StringIO()):
            client_command.cmd_add("alice", prompt_for_access=False, connection_tag="vless-reality")
        real_acquire_flock = core_locks.acquire_flock
        removed = []

        def remove_then_acquire(fd: int, timeout: float) -> None:
            # Другой процесс удалил клиента, пока эта команда ждала manager.lock.
            if not removed:
                removed.append("alice")
                client_command.cmd_remove("alice")
            real_acquire_flock(fd, timeout)

        stderr = StringIO()
        with sandbox_patches(self.root), \
            mock.patch.object(client_command.sys.stdin, "isatty", return_value=True), \
            mock.patch("builtins.input", side_effect=AssertionError("ACCESS_DAYS must not be asked")), \
            mock.patch.object(core_locks, "acquire_flock", side_effect=remove_then_acquire), \
            redirect_stdout(StringIO()), \
            redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                client_command.cmd_add("alice", connection_tag="vless-reality-2")

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Client alice was added or removed by another operation", stderr.getvalue())
        self.assertEqual(db_client_names(self.root), set())
        self.assertEqual(config_client_names(self.root), set())

    def test_add_stops_when_client_appeared_before_lock(self) -> None:
        """59-R01: ACCESS_DAYS спрошен для нового клиента, а под lock клиент с этим именем уже есть."""

        def add_other_client_then_answer(_prompt: str) -> str:
            # Другой процесс добавил клиента с тем же именем, пока админ отвечал на вопрос.
            client_command.cmd_add("alice", prompt_for_access=False, connection_tag="vless-reality")
            return "30"

        stderr = StringIO()
        with sandbox_patches(self.root), \
            mock.patch.object(client_command.sys.stdin, "isatty", return_value=True), \
            mock.patch("builtins.input", side_effect=add_other_client_then_answer), \
            mock.patch.object(client_command.client_settings, "manager_timezone_label", return_value="UTC"), \
            redirect_stdout(StringIO()), \
            redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                client_command.cmd_add("alice", connection_tag="vless-reality-2")

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Client alice was added or removed by another operation", stderr.getvalue())
        entry = client_repository.load_db_sql(db_path=self.root / "manager.db")["clients"]["alice"]
        self.assertEqual(set(entry["credentials"]), {"vless-reality"})
        self.assertFalse(entry.get("expiresAt"))

    def test_busy_lock_stops_writer_with_clear_error(self) -> None:
        holder = "pid=4242 cmd=xray-menu since=2026-10-08T10:00:00Z"
        error_lines = [
            f"ERROR: Другая операция менеджера ещё выполняется: {holder}",
            "Дождись её завершения и повтори команду.",
        ]
        user_timeouts = {"MANAGER_LOCK_TIMEOUT": 0.2, "TIMER_MANAGER_LOCK_TIMEOUT": 10}
        timer_timeouts = {"MANAGER_LOCK_TIMEOUT": 10, "TIMER_MANAGER_LOCK_TIMEOUT": 0.2}
        # Команда пользователя ждёт MANAGER_LOCK_TIMEOUT, шаги таймера — TIMER_MANAGER_LOCK_TIMEOUT.
        # Ручной запуск завершается ошибкой; шаг таймера (--quiet) пропускается с WARN и кодом 0,
        # чтобы следующие ExecStart минутной цепочки выполнились (контракт 3.1, 59-R02).
        commands = {
            "add": (
                lambda: client_command.cmd_add("alice", prompt_for_access=False, connection_tag="vless-reality"),
                user_timeouts,
                1,
                error_lines,
            ),
            "expire-due": (
                lambda: client_command.cmd_expire_due(quiet=False),
                timer_timeouts,
                1,
                error_lines,
            ),
            "enforce-limits": (
                lambda: client_command.cmd_enforce_limits(quiet=False),
                timer_timeouts,
                1,
                error_lines,
            ),
            "expire-due --quiet": (
                lambda: client_command.cmd_expire_due(quiet=True),
                timer_timeouts,
                0,
                [
                    f"WARN: Другая операция менеджера ещё выполняется: {holder}. "
                    "Шаг expire-due пропущен, следующий запуск таймера повторит его."
                ],
            ),
            "enforce-limits --quiet": (
                lambda: client_command.cmd_enforce_limits(quiet=True),
                timer_timeouts,
                0,
                [
                    f"WARN: Другая операция менеджера ещё выполняется: {holder}. "
                    "Шаг enforce-limits пропущен, следующий запуск таймера повторит его."
                ],
            ),
        }
        config_before = (self.root / "config.json").read_text()

        for purpose, (command, timeouts, exit_code, stderr_lines) in commands.items():
            with self.subTest(purpose):
                stderr = StringIO()
                with sandbox_patches(self.root), \
                    mock.patch.multiple(client_command, **timeouts), \
                    foreign_lock_holder(self.root / "manager.lock", holder), \
                    redirect_stdout(StringIO()), \
                    redirect_stderr(stderr):
                    started = time.monotonic()
                    with self.assertRaises(SystemExit) as raised:
                        command()
                    elapsed = time.monotonic() - started

                self.assertLess(elapsed, 5)
                self.assertEqual(raised.exception.code, exit_code)
                self.assertEqual(stderr.getvalue().splitlines(), stderr_lines)

        self.assertEqual(db_client_names(self.root), set())
        self.assertEqual((self.root / "config.json").read_text(), config_before)

    def test_client_db_writes_in_commands_happen_under_lock(self) -> None:
        tree = ast.parse((PACKAGE_DIR / "commands" / "client.py").read_text(encoding="utf-8"))

        self.assertEqual(unlocked_client_db_writes(tree), [])


class TrafficSyncWithoutManagerLockTests(unittest.TestCase):
    """traffic-sync вызывается из ExecStop xray.service внутри рестарта под manager.lock."""

    def test_traffic_sync_does_not_reference_manager_lock(self) -> None:
        tree = ast.parse((PACKAGE_DIR / "commands" / "traffic_sync.py").read_text(encoding="utf-8"))
        imported = set()
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)

        self.assertNotIn("xray_vps_manager.core.locks", imported)
        self.assertNotIn("locks", names)
        self.assertNotIn("manager_lock", names)
        self.assertFalse({"save_db", "write_db_to_sqlite_for_write"} & names)

    def test_traffic_sync_finishes_while_manager_lock_is_busy(self) -> None:
        email = "alice|created=2026-06-12T08:00:00Z"
        traffic_db = {
            "clients": {
                "alice": {"email": email, "incoming": 100, "outgoing": 200, "last": {"uplink": 10, "downlink": 20}, "history": {}}
            }
        }
        runtime = {
            f"user>>>{email}>>>traffic>>>uplink": 15,
            f"user>>>{email}>>>traffic>>>downlink": 30,
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with mock.patch.object(core_paths, "MANAGER_LOCK_PATH", root / "manager.lock"), \
                foreign_lock_holder(root / "manager.lock", "pid=4242 cmd=xray-client add since=2026-10-08T10:00:00Z"), \
                mock.patch.object(traffic_sync, "LOCK_PATH", root / "traffic.lock"), \
                mock.patch.object(traffic_sync, "ACCESS_LOG_PATH", root / "missing-access.log"), \
                mock.patch.object(traffic_sync, "known_credentials", return_value={("alice", "vless-reality"): email}), \
                mock.patch.object(traffic_sync, "query_runtime_stats", return_value=runtime), \
                mock.patch.object(
                    traffic_sync,
                    "local_bucket_time",
                    return_value=datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc),
                ), \
                mock.patch.object(traffic_sync, "now", return_value="2026-06-12T08:05:00Z"), \
                mock.patch.object(traffic_sync, "log"), \
                mock.patch.object(traffic_sync.client_repository, "load_db_sql", return_value={"clients": {}}), \
                mock.patch.object(traffic_sync.traffic_repository, "load_traffic_db_for_read", return_value=traffic_db), \
                mock.patch.object(traffic_sync, "save_traffic") as save_traffic:
                result = traffic_sync.sync()

        self.assertEqual(result, 0)
        save_traffic.assert_called_once()


if __name__ == "__main__":
    unittest.main()
