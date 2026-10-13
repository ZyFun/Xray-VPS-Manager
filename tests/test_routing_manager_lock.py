"""Регрессия XR-REV-001: каскады, bypass, WARP и меню подключений пишут config.json и client-DB под manager.lock."""

import ast
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import test_client_manager_lock as client_lock_tests
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.clients import settings as client_settings
from xray_vps_manager.commands import client as client_command
from xray_vps_manager.commands import menu_reality_actions
from xray_vps_manager.commands import set_bypass
from xray_vps_manager.commands import set_cascade
from xray_vps_manager.commands import traffic_sync
from xray_vps_manager.commands import warp
from xray_vps_manager.core import locks as core_locks
from xray_vps_manager.core import paths as core_paths
from xray_vps_manager.core.server_env import ORDERED_ENV_KEYS, read_server_env, write_server_env
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import bypass as sqlite_bypass
from xray_vps_manager.xray import bypass as bypass_config
from xray_vps_manager.xray import cascade as cascade_config
from xray_vps_manager.xray import config as xray_config
from xray_vps_manager.xray import outbound_links

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
PACKAGE_DIR = REPO_ROOT / "xray_vps_manager"
VLESS_LINK = "vless://11111111-1111-1111-1111-111111111111@example.com:443?encryption=none&security=none&type=tcp#Example"
WARP_ENDPOINT = "engage.cloudflareclient.com:2408"
RESTART_XRAY = ["systemctl", "restart", "xray"]
HOLDER = "pid=4242 cmd=xray-client add since=2026-10-11T10:00:00Z"
LOCK_MESSAGE = f"Другая операция менеджера ещё выполняется: {HOLDER}"
LOCK_HINT = "Дождись её завершения и повтори команду."
LOCK_ERROR_LINES = [f"ERROR: {LOCK_MESSAGE}", LOCK_HINT]
DRIFT_WARN = (
    f"WARN: {LOCK_MESSAGE}. "
    "Запись маршрутов клиентов из config.json в manager.db пропущена, следующее чтение повторит её."
)
LOCK_TIMEOUTS = {
    set_cascade: {"MANAGER_LOCK_TIMEOUT": 0.2, "ROUTE_DRIFT_LOCK_TIMEOUT": 0.2},
    set_bypass: {"MANAGER_LOCK_TIMEOUT": 0.2},
    warp: {"MANAGER_LOCK_TIMEOUT": 0.2},
    menu_reality_actions: {"MANAGER_LOCK_TIMEOUT": 0.2},
}
# ExecStop=-+/usr/local/sbin/xray-traffic-sync --quiet должен завершиться, пока сценарий держит manager.lock.
EXEC_STOP_TIMEOUT = 20


def warp_outbound(endpoint_override=None) -> dict:
    return {
        "tag": warp.WARP_OUTBOUND_TAG,
        "protocol": "wireguard",
        "settings": {"peers": [{"endpoint": endpoint_override or WARP_ENDPOINT}]},
    }


def bypass_record(enabled: bool = True) -> dict:
    return {
        "tag": "bypass-ru",
        "name": "ru",
        "regionCode": "RU",
        "regionLabel": "Россия",
        "label": "example.com:443",
        "enabled": enabled,
        "created": "2026-10-11T08:00:00Z",
        "updated": "2026-10-11T08:00:00Z",
    }


def make_routing_sandbox(root: Path) -> None:
    """Песочница xray-client: каскады main (активен) и backup, bypass-ru для RU, outbound WARP без маршрута."""
    client_lock_tests.make_sandbox(root)
    config = json.loads((root / "config.json").read_text())
    for name in ("main", "backup"):
        tag = cascade_config.cascade_tag(name)
        set_cascade.configure_cascade(config, outbound_links.parse_vless_outbound(VLESS_LINK, tag)[0])
    env_values = {}
    bypass_config.upsert_bypass_outbound(config, outbound_links.parse_vless_outbound(VLESS_LINK, "bypass-ru")[0])
    bypass_config.ensure_geoip_domain_strategy(config, env_values)
    bypass_config.apply_bypass_route(config, "bypass-ru", "RU")
    config["outbounds"].append(warp_outbound())
    (root / "config.json").write_text(json.dumps(config))
    write_server_env(env_values, root / "server.env", ORDERED_ENV_KEYS)
    (root / "wgcf-profile.conf").write_text("[Interface]\n")
    connection = database.open_database(root / "manager.db")
    try:
        sqlite_bypass.upsert_route(connection, "bypass-ru", bypass_record())
    finally:
        connection.close()


def lock_held(root: Path) -> bool:
    return not client_lock_tests.lock_is_free(root / "manager.lock")


def no_sleep_time() -> SimpleNamespace:
    return SimpleNamespace(sleep=lambda _seconds: None, time=time.time, monotonic=time.monotonic)


@contextmanager
def routing_patches(root: Path, *, restart=None, record=None):
    """Файлы песочницы вместо /usr/local/etc/xray; фейковые xray -test, systemctl restart xray, curl и wgcf.

    record(kind, detail) получает каждую запись файла (chown), внешнюю команду и запись manager.db.
    """
    config_path = root / "config.json"
    env_path = root / "server.env"
    record = record or (lambda _kind, _detail="": None)
    real_save_db = client_repository.save_db

    def run(command, **_kwargs):
        record("run", " ".join(str(item) for item in command[1:3]))
        if list(command) == RESTART_XRAY and restart is not None:
            restart()

    def restart_xray(*_args, **_kwargs):
        # restart_systemd_unit(..., ports=...) в apply_config: тот же фейковый рестарт без systemctl и TCP-проб.
        run(RESTART_XRAY)

    def run_capture(command, timeout=None, **_kwargs):
        # curl через временный SOCKS: пробы WARP видят warp=on, проверка выключенного WARP — warp=off.
        warp_state = "off" if any(f":{warp.VERIFY_SOCKS_PORT}" in str(item) for item in command) else "on"
        return subprocess.CompletedProcess(command, 0, stdout=f"ip=192.0.2.1\nwarp={warp_state}\n", stderr="")

    def chown(path, *_args, **_kwargs):
        record("write", Path(path).name)

    def save_db(db, *args, **kwargs):
        record("db", "clients")
        return real_save_db(db, *args, **kwargs)

    def bypass_write(name):
        real = getattr(sqlite_bypass, name)

        def write(*args, **kwargs):
            record("db", f"bypass {name}")
            return real(*args, **kwargs)

        return write

    with ExitStack() as stack:
        enter = stack.enter_context
        enter(mock.patch.object(core_paths, "MANAGER_LOCK_PATH", root / "manager.lock"))
        enter(mock.patch.object(database, "MANAGER_DB_PATH", root / "manager.db"))
        enter(mock.patch("shutil.chown", side_effect=chown))
        enter(mock.patch.object(client_repository, "save_db", side_effect=save_db))
        for name in ("upsert_route", "set_enabled", "delete_route"):
            enter(mock.patch.object(sqlite_bypass, name, side_effect=bypass_write(name)))
        for module in (set_cascade, set_bypass, warp):
            enter(mock.patch.object(module, "CONFIG_PATH", config_path))
            enter(mock.patch.object(module, "run", side_effect=run))
            enter(mock.patch.object(module, "restart_systemd_unit", side_effect=restart_xray))
            enter(mock.patch.object(module, "run_capture", side_effect=run_capture))
            enter(mock.patch.object(module, "wait_for_tcp", return_value=True))
            enter(mock.patch.object(module, "time", no_sleep_time()))
        enter(mock.patch.object(set_bypass, "read_server_env", side_effect=lambda: read_server_env(env_path)))
        enter(
            mock.patch.object(
                set_bypass,
                "write_server_env_values",
                side_effect=lambda values: write_server_env(values, env_path, ORDERED_ENV_KEYS),
            )
        )
        enter(mock.patch.object(set_bypass, "notify_config_event"))
        enter(mock.patch.object(warp, "WGCF_PROFILE", root / "wgcf-profile.conf"))
        enter(mock.patch.object(warp, "restart_xray", side_effect=restart_xray))
        enter(mock.patch.object(warp, "ensure_warp_profile"))
        enter(mock.patch.object(warp, "profile_endpoint", return_value=WARP_ENDPOINT))
        enter(mock.patch.object(warp, "warp_outbound_from_profile", side_effect=warp_outbound))
        enter(mock.patch.object(menu_reality_actions, "CONFIG_PATH", config_path))
        enter(mock.patch.object(menu_reality_actions, "run", side_effect=run))
        enter(mock.patch.object(menu_reality_actions, "restart_systemd_unit", side_effect=restart_xray))
        # Пробный bind нового PORT не должен зависеть от портов машины, на которой идут тесты.
        enter(mock.patch.object(menu_reality_actions, "tcp_port_in_use", return_value=False))
        enter(mock.patch.object(menu_reality_actions, "load_xray_config", side_effect=lambda: xray_config.load_config(config_path)))
        enter(
            mock.patch.object(
                menu_reality_actions,
                "save_config",
                side_effect=lambda config: xray_config.save_config(config, config_path),
            )
        )
        enter(mock.patch.object(menu_reality_actions, "save_db", side_effect=save_db))
        enter(mock.patch.object(menu_reality_actions, "server_env_values", side_effect=lambda: read_server_env(env_path)))
        enter(
            mock.patch.object(
                menu_reality_actions,
                "save_server_env_values",
                side_effect=lambda values: client_settings.save_server_env_values(values, env_path),
            )
        )
        enter(mock.patch.object(menu_reality_actions, "refresh_initial_link"))
        yield


def stored_config(root: Path) -> dict:
    return json.loads((root / "config.json").read_text())


def stored_bypass_route(root: Path, tag: str = "bypass-ru") -> dict:
    connection = database.open_database(root / "manager.db")
    try:
        return sqlite_bypass.get_route(connection, tag)
    finally:
        connection.close()


def stored_cascade_routes(root: Path) -> dict:
    return client_repository.load_db_sql(db_path=root / "manager.db")["cascadeRoutes"]


def reality_port(root: Path, tag: str = "vless-reality") -> int:
    return next(item["port"] for item in stored_config(root)["inbounds"] if item["tag"] == tag)


RACE_SCENARIOS = {
    # Сценарий без вопросов: снимок config/client-DB загружен, второй писатель приходит во время рестарта Xray.
    "cascade-use": (lambda: set_cascade.cmd_use(["backup"]), "restart", []),
    # Первый рестарт — проба endpoint, которая подменяет config.json и затем возвращает исходный текст.
    "warp-enable": (warp.cmd_enable, "restart", []),
    # Интерактивные сценарии: второй писатель приходит, пока админ отвечает на последний вопрос.
    "bypass-disable": (lambda: set_bypass.cmd_disable([]), "input", ["1"]),
    "menu-update-port": (menu_reality_actions.update_port, "input", ["1", "2443"]),
}


def routing_worker(argv: list) -> int:
    """Отдельный процесс маршрутизации или меню подключений в общей песочнице.

    В окне сценария (рестарт Xray или последний вопрос) ждёт, пока второй процесс добавит клиента.
    """
    root, scenario, window = Path(argv[0]), argv[1], float(argv[2])
    action, window_at, answers = RACE_SCENARIOS[scenario]
    answers = list(answers)

    def wait_for_writer() -> None:
        if not (root / "slow-loaded").exists():
            (root / "slow-loaded").touch()
            client_lock_tests.wait_for_file(root / "fast-done", window)

    def fake_input(_prompt: str = "") -> str:
        if window_at == "input" and len(answers) == 1:
            wait_for_writer()
        return answers.pop(0)

    restart = wait_for_writer if window_at == "restart" else None
    with routing_patches(root, restart=restart), \
        mock.patch("builtins.input", side_effect=fake_input), \
        redirect_stdout(StringIO()):
        action()
    return 0


def exec_stop_traffic_sync(argv: list) -> int:
    """ExecStop xray.service: xray-traffic-sync --quiet в отдельном процессе во время рестарта Xray."""
    root = Path(argv[0])
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
    with mock.patch.object(core_paths, "MANAGER_LOCK_PATH", root / "manager.lock"), \
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
        mock.patch.object(traffic_sync, "save_traffic"):
        return traffic_sync.sync()


def start_worker(function: str, *args: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(TESTS_DIR)])
    code = f"import sys, test_routing_manager_lock as t; sys.exit(t.{function}(sys.argv[1:]))"
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def start_client_add_worker(root: Path, name: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(TESTS_DIR)])
    code = "import sys, test_client_manager_lock as t; sys.exit(t.add_worker(sys.argv[1:]))"
    return subprocess.Popen(
        [sys.executable, "-c", code, str(root), name, "fast", client_lock_tests.SLOW_WRITER_WINDOW],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def communicate(process: subprocess.Popen, timeout: float) -> tuple:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    return process.returncode, stdout, stderr


def is_manager_lock(node: ast.AST) -> bool:
    return isinstance(node, ast.With) and any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Name)
        and item.context_expr.func.id == "manager_lock"
        for item in node.items
    )


def call_name(node: ast.AST) -> str:
    if not isinstance(node, ast.Call):
        return ""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def unlocked_calls(path: Path, names: set, skip_functions=()) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def visit(node: ast.AST, stack: list, function_name: str) -> None:
        for child in ast.iter_child_nodes(node):
            if call_name(child) in names and not any(is_manager_lock(item) for item in stack):
                found.append(f"{function_name}:{child.lineno} {call_name(child)}")
            visit(child, stack + [child], function_name)

    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name not in skip_functions:
            visit(function, [], function.name)
    return found


class RoutingLockTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lock_path = self.root / "manager.lock"
        make_routing_sandbox(self.root)

    def snapshot(self) -> tuple:
        return (
            (self.root / "config.json").read_text(),
            (self.root / "server.env").read_text(),
            client_repository.load_db_sql(db_path=self.root / "manager.db"),
            stored_bypass_route(self.root),
        )


class RoutingWritersRaceTests(RoutingLockTestCase):
    def run_race(self, scenario: str) -> None:
        slow = start_worker("routing_worker", str(self.root), scenario, client_lock_tests.SLOW_WRITER_WINDOW)
        fast = start_client_add_worker(self.root, "alice")
        results = [communicate(process, client_lock_tests.WORKER_TIMEOUT) for process in (slow, fast)]

        for returncode, _stdout, stderr in results:
            self.assertEqual(returncode, 0, stderr)
        self.assertEqual(client_lock_tests.db_client_names(self.root), {"alice"})
        self.assertEqual(client_lock_tests.config_client_names(self.root), {"alice"})
        self.assertTrue(client_lock_tests.lock_is_free(self.lock_path))

    def test_cascade_use_does_not_lose_client_added_during_restart(self) -> None:
        """XR-REV-001: xray-set-cascade use не записывает устаревший снимок client-DB и config.json."""
        self.run_race("cascade-use")

        self.assertEqual(cascade_config.active_cascade_tag(stored_config(self.root)), "cascade-backup")
        self.assertIn("cascade-backup", stored_cascade_routes(self.root))

    def test_warp_enable_does_not_lose_client_added_during_endpoint_probe(self) -> None:
        """XR-REV-001: проба endpoint и включение WARP не возвращают config.json без клиента другого процесса."""
        self.run_race("warp-enable")

        self.assertEqual(warp.current_catchall_tag(stored_config(self.root)), warp.WARP_OUTBOUND_TAG)

    def test_bypass_disable_does_not_lose_client_added_while_choosing_route(self) -> None:
        """XR-REV-001: xray-set-bypass disable читает config.json под lock после выбора маршрута."""
        self.run_race("bypass-disable")

        self.assertFalse(stored_bypass_route(self.root)["enabled"])
        self.assertFalse(bypass_config.configured_bypass_for_warning(stored_config(self.root), "RU"))

    def test_menu_update_port_does_not_lose_client_added_while_answering(self) -> None:
        """XR-REV-001: «Обновить PORT» в меню применяет ответы к config.json, прочитанному под lock."""
        self.run_race("menu-update-port")

        self.assertEqual(reality_port(self.root), 2443)
        connection = client_repository.load_db_sql(db_path=self.root / "manager.db")["connections"]["vless-reality"]
        self.assertEqual(connection["port"], 2443)


class RoutingPromptsAndWritesTests(RoutingLockTestCase):
    def run_interactive(self, action, answers: list) -> list:
        events = []
        answers = list(answers)

        def record(kind: str, detail: str = "") -> None:
            events.append((kind, detail, lock_held(self.root)))

        def fake_input(prompt: str = "") -> str:
            record("input", prompt.strip())
            return answers.pop(0)

        def readline() -> str:
            record("input", "stdin")
            return VLESS_LINK + "\n"

        with routing_patches(self.root, record=record), \
            mock.patch("builtins.input", side_effect=fake_input), \
            mock.patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: True, readline=readline)), \
            redirect_stdout(StringIO()):
            action()

        self.assertEqual(answers, [])
        self.assertTrue(client_lock_tests.lock_is_free(self.lock_path))
        return events

    def test_prompts_are_asked_before_lock_and_writes_happen_under_lock(self) -> None:
        scenarios = {
            "xray-set-cascade add": (lambda: set_cascade.cmd_add([]), ["extra", "Германия"]),
            "xray-set-cascade use": (lambda: set_cascade.cmd_use([]), ["2"]),
            "xray-set-cascade remove": (lambda: set_cascade.cmd_remove([]), ["2", "y"]),
            "xray-set-cascade test-select": (lambda: set_cascade.cmd_test([], choose=True), ["1"]),
            "xray-set-bypass add": (lambda: set_bypass.cmd_add([]), ["nl", "1", "", "y"]),
            "xray-set-bypass enable": (lambda: set_bypass.cmd_enable([]), ["1"]),
            "xray-set-bypass region": (lambda: set_bypass.cmd_region([]), ["1", "2"]),
            "xray-set-bypass target": (lambda: set_bypass.cmd_target([]), ["1", "ya.ru"]),
            "xray-set-bypass disable": (lambda: set_bypass.cmd_disable([]), ["1"]),
            "xray-set-bypass remove": (lambda: set_bypass.cmd_remove([]), ["1", "y"]),
            "xray-set-bypass test": (lambda: set_bypass.cmd_test([]), ["1", ""]),
            "menu: Обновить PORT": (menu_reality_actions.update_port, ["1", "2443"]),
            "menu: Обновить REALITY_SNI": (menu_reality_actions.update_sni, ["1", "example.org"]),
            "menu: Обновить PORT и REALITY_SNI": (menu_reality_actions.update_port_and_sni, ["1", "2443", "example.org"]),
            "menu: Обновить FINGERPRINT": (menu_reality_actions.update_fingerprint, ["1", "2"]),
        }
        for title, (action, answers) in scenarios.items():
            with self.subTest(title):
                self.setUp()
                events = self.run_interactive(action, answers)
                prompts = [event for event in events if event[0] == "input"]
                writes = [event for event in events if event[0] != "input"]

                self.assertTrue(prompts)
                self.assertTrue(writes)
                self.assertEqual([event for event in prompts if event[2]], [], "вопрос задан под manager.lock")
                self.assertEqual([event for event in writes if not event[2]], [], "запись без manager.lock")

    def test_non_interactive_writes_happen_under_lock(self) -> None:
        scenarios = {
            "xray-set-cascade country": lambda: set_cascade.cmd_country(["main", "Германия"]),
            "xray-set-cascade --disable": set_cascade.cmd_disable,
            "xray-set-cascade test": lambda: set_cascade.cmd_test(["main"]),
            "xray-warp create": warp.cmd_create,
            "xray-warp enable": warp.cmd_enable,
            "xray-warp disable": warp.cmd_disable,
            "xray-warp verify-disabled": warp.cmd_verify_disabled,
            "xray-warp test": warp.cmd_test,
            "xray-warp remove": warp.cmd_remove,
        }
        for title, action in scenarios.items():
            with self.subTest(title):
                self.setUp()
                events = self.run_interactive(action, [])

                self.assertTrue(events)
                self.assertEqual([event for event in events if not event[2]], [], "запись без manager.lock")

    def test_bypass_add_stops_when_other_route_took_region_after_confirmation(self) -> None:
        """Замена подтверждена для bypass-ru, а под lock регион занял другой маршрут: команда ничего не пишет."""
        real_active_route = sqlite_bypass.active_route_for_region

        def active_route(connection, region_code):
            route = real_active_route(connection, region_code)
            if lock_held(self.root) and route:
                # Пока админ подтверждал замену, другая операция включила RU на bypass-de.
                return dict(route, tag="bypass-de")
            return route

        before = self.snapshot()
        stderr = StringIO()
        with mock.patch.object(sqlite_bypass, "active_route_for_region", side_effect=active_route), \
            redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                self.run_interactive(lambda: set_bypass.cmd_add([]), ["nl", "1", "", "y"])

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("GeoIP region RU is already enabled on bypass-de. Use --replace to replace it.", stderr.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_bypass_target_stops_when_route_changed_while_answering(self) -> None:
        def change_route_then_answer(_prompt: str = "") -> str:
            # Другая операция изменила тестовую цель, пока админ вводил свою.
            connection = database.open_database(self.root / "manager.db")
            try:
                sqlite_bypass.upsert_route(connection, "bypass-ru", dict(bypass_record(), testTarget="https://other.example/"))
            finally:
                connection.close()
            return "ya.ru"

        stderr = StringIO()
        with routing_patches(self.root), \
            mock.patch("builtins.input", side_effect=change_route_then_answer), \
            mock.patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: True)), \
            redirect_stdout(StringIO()), \
            redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                set_bypass.cmd_target(["ru"])

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("GeoIP bypass route bypass-ru was changed by another operation. Run the command again.", stderr.getvalue())
        self.assertEqual(stored_bypass_route(self.root)["testTarget"], "https://other.example/")

    def test_bypass_test_reads_region_and_target_after_waiting_for_lock(self) -> None:
        """61-R01: регион и сохранённая тестовая цель bypass-теста читаются заново под manager.lock."""
        cases = {
            "сохранённая цель": (["ru"], "https://ya.ru/"),
            "явная цель": (["ru", "--test-target", "example.org"], "https://example.org/"),
            "явный отказ от цели": (["ru", "--test-target", "-"], ""),
        }
        for title, (args, expected_target) in cases.items():
            with self.subTest(title):
                self.setUp()
                real_acquire_flock = core_locks.acquire_flock
                changed = []

                def change_region_then_acquire(fd: int, timeout: float) -> None:
                    # Пока тест ждал manager.lock, другая операция сменила регион и задала тестовую цель.
                    if not changed:
                        changed.append("US")
                        set_bypass.cmd_region(["ru", "US", "--test-target", "ya.ru"])
                    real_acquire_flock(fd, timeout)

                with routing_patches(self.root), \
                    mock.patch.object(core_locks, "acquire_flock", side_effect=change_region_then_acquire), \
                    mock.patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: False)), \
                    mock.patch.object(set_bypass, "test_bypass") as test_bypass, \
                    redirect_stdout(StringIO()):
                    set_bypass.cmd_test(args)

                test_bypass.assert_called_once_with("bypass-ru", "US", expected_target)

    def test_cascade_list_drift_write_keeps_client_added_after_snapshot(self) -> None:
        """XR-REV-001: запись drift маршрутов в xray-set-cascade list не стирает клиента другого процесса."""
        real_load_db_sql = client_repository.load_db_sql
        added = []

        def load_then_add_client(*args, **kwargs):
            db = real_load_db_sql(*args, **kwargs)
            if not added:
                added.append("bob")
                # Другой процесс добавил клиента между снимком list и записью drift.
                with client_lock_tests.sandbox_patches(self.root):
                    client_command.cmd_add("bob", prompt_for_access=False, connection_tag="vless-reality")
            return db

        with routing_patches(self.root), \
            mock.patch.object(client_repository, "load_db_sql", side_effect=load_then_add_client), \
            redirect_stdout(StringIO()):
            set_cascade.cmd_status()

        self.assertEqual(client_lock_tests.db_client_names(self.root), {"bob"})
        self.assertEqual(set(stored_cascade_routes(self.root)), {"cascade-main", "cascade-backup"})
        self.assertTrue(client_lock_tests.lock_is_free(self.lock_path))


class RoutingBusyLockTests(RoutingLockTestCase):
    def run_main(self, module, args: list) -> tuple:
        stderr = StringIO()
        with routing_patches(self.root), \
            mock.patch.multiple(module, **LOCK_TIMEOUTS[module]), \
            mock.patch("os.geteuid", return_value=0), \
            mock.patch.object(sys, "argv", [module.__name__, *args]), \
            mock.patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: False, readline=lambda: VLESS_LINK + "\n")), \
            client_lock_tests.foreign_lock_holder(self.lock_path, HOLDER), \
            redirect_stdout(StringIO()), \
            redirect_stderr(stderr):
            started = time.monotonic()
            with self.assertRaises(SystemExit) as raised:
                module.main()
            elapsed = time.monotonic() - started
        return raised.exception.code, stderr.getvalue().splitlines(), elapsed

    def test_busy_lock_stops_routing_writers_with_clear_error(self) -> None:
        commands = [
            (set_cascade, ["add", "extra", "--country", "Германия"]),
            (set_cascade, ["use", "backup"]),
            (set_cascade, ["country", "main", "Германия"]),
            (set_cascade, ["remove", "backup"]),
            (set_cascade, ["--disable"]),
            (set_cascade, ["test", "main"]),
            (set_bypass, ["add", "nl", "--region", "NL"]),
            (set_bypass, ["enable", "ru"]),
            (set_bypass, ["region", "ru", "US"]),
            (set_bypass, ["target", "ru", "ya.ru"]),
            (set_bypass, ["disable", "ru"]),
            (set_bypass, ["remove", "ru"]),
            (set_bypass, ["test", "ru"]),
            (warp, ["create"]),
            (warp, ["enable"]),
            (warp, ["disable"]),
            (warp, ["verify-disabled"]),
            (warp, ["remove"]),
            (warp, ["test"]),
        ]
        before = self.snapshot()

        for module, args in commands:
            with self.subTest(f"{module.__name__.rsplit('.', 1)[-1]} {' '.join(args)}"):
                code, stderr_lines, elapsed = self.run_main(module, args)

                self.assertLess(elapsed, 5)
                self.assertEqual(code, 1)
                self.assertEqual(stderr_lines, LOCK_ERROR_LINES)
                self.assertEqual(self.snapshot(), before)

    def test_busy_lock_stops_menu_connection_updates_with_clear_error(self) -> None:
        actions = {
            "Обновить PORT": (menu_reality_actions.update_port, ["1", "2443"]),
            "Обновить REALITY_SNI": (menu_reality_actions.update_sni, ["1", "example.org"]),
            "Обновить PORT и REALITY_SNI": (menu_reality_actions.update_port_and_sni, ["1", "2443", "example.org"]),
            "Обновить FINGERPRINT": (menu_reality_actions.update_fingerprint, ["1", "2"]),
        }
        before = self.snapshot()

        for title, (action, answers) in actions.items():
            with self.subTest(title):
                with routing_patches(self.root), \
                    mock.patch.multiple(menu_reality_actions, **LOCK_TIMEOUTS[menu_reality_actions]), \
                    mock.patch("builtins.input", side_effect=list(answers)), \
                    client_lock_tests.foreign_lock_holder(self.lock_path, HOLDER), \
                    redirect_stdout(StringIO()):
                    started = time.monotonic()
                    with self.assertRaises(SystemExit) as raised:
                        action()
                    elapsed = time.monotonic() - started

                self.assertLess(elapsed, 5)
                self.assertEqual(raised.exception.code, f"{LOCK_MESSAGE}\n{LOCK_HINT}")
                self.assertEqual(self.snapshot(), before)

    def test_cascade_list_skips_drift_write_when_lock_is_busy(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with routing_patches(self.root), \
            mock.patch.multiple(set_cascade, **LOCK_TIMEOUTS[set_cascade]), \
            client_lock_tests.foreign_lock_holder(self.lock_path, HOLDER), \
            redirect_stdout(stdout), \
            redirect_stderr(stderr):
            set_cascade.cmd_status()

        self.assertEqual(stderr.getvalue().splitlines(), [DRIFT_WARN])
        self.assertIn("cascade-backup", stdout.getvalue())
        self.assertEqual(stored_cascade_routes(self.root), {})


class ExecStopTrafficSyncTests(RoutingLockTestCase):
    """traffic-sync из ExecStop xray.service завершается, пока сценарий перезапускает Xray под manager.lock."""

    def test_xray_restart_under_lock_does_not_wait_for_traffic_sync(self) -> None:
        scenarios = {
            "xray-set-cascade --disable": (set_cascade.cmd_disable, []),
            "xray-set-bypass disable": (lambda: set_bypass.cmd_disable(["ru"]), []),
            "xray-warp remove": (warp.cmd_remove, []),
            "menu: Обновить PORT": (menu_reality_actions.update_port, ["1", "2443"]),
        }
        for title, (action, answers) in scenarios.items():
            with self.subTest(title):
                self.setUp()
                stops = []

                def restart() -> None:
                    started = time.monotonic()
                    held = lock_held(self.root)
                    returncode, _stdout, stderr = communicate(
                        start_worker("exec_stop_traffic_sync", str(self.root)),
                        EXEC_STOP_TIMEOUT,
                    )
                    stops.append((held, returncode, time.monotonic() - started, stderr))

                with routing_patches(self.root, restart=restart), \
                    mock.patch("builtins.input", side_effect=list(answers)), \
                    redirect_stdout(StringIO()):
                    action()

                self.assertTrue(stops)
                for held, returncode, elapsed, stderr in stops:
                    self.assertTrue(held, "рестарт Xray выполнен без manager.lock")
                    self.assertEqual(returncode, 0, stderr)
                    self.assertLess(elapsed, EXEC_STOP_TIMEOUT)

    def test_routing_writes_happen_under_lock(self) -> None:
        checks = {
            "set_cascade.py": ({"apply_config", "save_client_db_optional", "test_cascade"}, ()),
            "set_bypass.py": (
                {"apply_config", "save_route", "write_server_env_values", "test_bypass", "set_enabled", "delete_route"},
                ("save_route",),
            ),
            "warp.py": (
                {
                    "apply_config",
                    "save_client_db_optional",
                    "choose_working_endpoint",
                    "probe_warp_disabled",
                    "test_warp",
                    "install_config_without_backup",
                },
                ("probe_warp_endpoint", "probe_warp_disabled", "test_warp"),
            ),
            "menu_reality_actions.py": ({"apply_config", "update_connection_db", "write_server_env"}, ()),
        }
        for name, (calls, skip_functions) in checks.items():
            with self.subTest(name):
                self.assertEqual(unlocked_calls(PACKAGE_DIR / "commands" / name, calls, skip_functions), [])


if __name__ == "__main__":
    unittest.main()
