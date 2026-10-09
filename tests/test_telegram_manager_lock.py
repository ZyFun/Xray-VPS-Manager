"""Регрессия XR-REV-001: писатели Telegram и запись drift в load_client_db работают под manager.lock."""

import ast
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import test_client_manager_lock as client_lock_tests
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.commands import client as client_command
from xray_vps_manager.commands import telegram as telegram_command
from xray_vps_manager.core import paths as core_paths
from xray_vps_manager.db import database
from xray_vps_manager.telegram import settings as telegram_settings
from xray_vps_manager.telegram import setup as telegram_setup
from xray_vps_manager.xray import config as xray_config

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
PACKAGE_DIR = REPO_ROOT / "xray_vps_manager"
OWNER_CHAT = "111"
CLIENT_CHAT = "222"
OTHER_CHAT = "333"
CASCADE_TAG = "cascade-de"
HOLDER = "pid=4242 cmd=xray-menu since=2026-10-09T10:00:00Z"
LOCK_ERROR_LINES = [
    f"ERROR: Другая операция менеджера ещё выполняется: {HOLDER}",
    "Дождись её завершения и повтори команду.",
]
DRIFT_WARN = (
    f"WARN: Другая операция менеджера ещё выполняется: {HOLDER}. "
    "Запись маршрутов клиентов из config.json в manager.db пропущена, следующее чтение повторит её."
)
PAYMENT_SECTIONS = ("paymentTotalAmount", "paymentCurrency")


def make_telegram_sandbox(root: Path) -> None:
    """Песочница xray-client и настроенный бот; каскад в config.json ещё не отражён в manager.db (drift)."""
    client_lock_tests.make_sandbox(root)
    config = json.loads((root / "config.json").read_text())
    config["outbounds"].append({"tag": CASCADE_TAG, "protocol": "vless", "settings": {}})
    (root / "config.json").write_text(json.dumps(config))
    telegram_settings.save_db(
        {"enabled": True, "token": "123456:test-token", "chatId": OWNER_CHAT, "chatLabel": "owner"},
        db_path=root / "manager.db",
    )


def message_update(chat_id: str, update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": int(chat_id), "type": "private", "first_name": "Test"},
            "text": text,
        },
    }


@contextmanager
def telegram_patches(root: Path, *, updates=(), on_send=None):
    sent = []

    def curl_json(_db, method, payload=None, timeout=30):
        if method == "getUpdates":
            return {"ok": True, "result": list(updates)}
        if method == "getMe":
            return {"ok": True, "result": {"username": "ExampleVpnBot"}}
        return {"ok": True, "result": {}}

    def send_chat_message(_db, chat_id, text, reply_markup=None, parse_mode=None):
        if on_send is not None:
            on_send()
        sent.append((str(chat_id), text))
        return {"ok": True, "result": {"message_id": len(sent)}}

    with mock.patch.object(core_paths, "MANAGER_LOCK_PATH", root / "manager.lock"), \
        mock.patch.object(database, "MANAGER_DB_PATH", root / "manager.db"), \
        mock.patch.object(telegram_setup, "CONFIG_PATH", root / "config.json"), \
        mock.patch.object(xray_config, "load_config", lambda path=None: json.loads((root / "config.json").read_text())), \
        mock.patch.object(telegram_command, "server_env_values", return_value={}), \
        mock.patch.object(telegram_command.telegram_api, "curl_json", side_effect=curl_json), \
        mock.patch.object(telegram_command.telegram_api, "send_chat_message", side_effect=send_chat_message), \
        mock.patch.object(telegram_command.telegram_api, "answer_callback_query", return_value={"ok": True}):
        yield sent


def stored_settings(root: Path) -> dict:
    return telegram_settings.load_db_sql(db_path=root / "manager.db")


def stored_routes(root: Path) -> dict:
    return client_repository.load_db_sql(db_path=root / "manager.db")["cascadeRoutes"]


def poller_worker(argv: list) -> int:
    """Отдельный процесс xray-telegram poll-users: /status читает client-DB с drift и сохраняет его.

    Между load_db_sql и save_db poller ждёт второго писателя внутри окна.
    """
    root, window = Path(argv[0]), float(argv[1])
    real_save_db = client_repository.save_db

    def slow_save_db(db, *args, **kwargs):
        (root / "poller-saving").touch()
        client_lock_tests.wait_for_file(root / "writer-done", window)
        return real_save_db(db, *args, **kwargs)

    with telegram_patches(root, updates=[message_update(CLIENT_CHAT, 1, "/status")]) as sent, \
        mock.patch.object(telegram_command.client_repository, "save_db", side_effect=slow_save_db):
        returncode = telegram_command.poll_user_subscriptions(quiet=True)
    (root / "poller-sent.json").write_text(json.dumps(sent))
    return returncode


def writer_worker(argv: list) -> int:
    """Отдельный процесс xray-client add: стартует, когда poller уже прочитал снимок client-DB."""
    root, name = Path(argv[0]), argv[1]
    try:
        if not client_lock_tests.wait_for_file(root / "poller-saving", client_lock_tests.WORKER_TIMEOUT):
            print("poller did not reach save_db", file=sys.stderr)
            return 2
        with client_lock_tests.sandbox_patches(root, config_suffix=f".{name}"), redirect_stdout(StringIO()):
            client_command.cmd_add(name, prompt_for_access=False, connection_tag="vless-reality")
    finally:
        (root / "writer-done").touch()
    return 0


def start_worker(function: str, *args: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(TESTS_DIR)])
    code = f"import sys, test_telegram_manager_lock as t; sys.exit(t.{function}(sys.argv[1:]))"
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def is_manager_lock(node: ast.AST) -> bool:
    return isinstance(node, ast.With) and any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Name)
        and item.context_expr.func.id == "manager_lock"
        for item in node.items
    )


def is_save_db_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "save_db") or (
        isinstance(func, ast.Attribute) and func.attr == "save_db"
    )


def unlocked_save_db_calls(path: Path, skip_functions=()) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def visit(node: ast.AST, stack: list, function_name: str) -> None:
        for child in ast.iter_child_nodes(node):
            if is_save_db_call(child) and not any(is_manager_lock(item) for item in stack):
                found.append(f"{function_name}:{child.lineno}")
            visit(child, stack + [child], function_name)

    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name not in skip_functions:
            visit(function, [], function.name)
    return found


class TelegramPollerLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock_path = self.root / "manager.lock"
        make_telegram_sandbox(self.root)

    def test_poller_drift_write_does_not_lose_client_added_by_other_process(self) -> None:
        """XR-REV-001: обработка update poller'ом пишет drift под lock и не стирает клиента другого процесса."""
        poller = start_worker("poller_worker", str(self.root), client_lock_tests.SLOW_WRITER_WINDOW)
        writer = start_worker("writer_worker", str(self.root), "bob")
        results = []
        for process in (poller, writer):
            try:
                stdout, stderr = process.communicate(timeout=client_lock_tests.WORKER_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            results.append((process.returncode, stdout, stderr))

        for returncode, _stdout, stderr in results:
            self.assertEqual(returncode, 0, stderr)
        self.assertEqual(client_lock_tests.db_client_names(self.root), {"bob"})
        self.assertEqual(client_lock_tests.config_client_names(self.root), {"bob"})
        self.assertIn(CASCADE_TAG, stored_routes(self.root))
        sent = json.loads((self.root / "poller-sent.json").read_text())
        self.assertEqual([chat_id for chat_id, _text in sent], [CLIENT_CHAT])
        self.assertTrue(client_lock_tests.lock_is_free(self.lock_path))

    def test_load_client_db_saves_drift_once_and_then_reads_without_lock(self) -> None:
        stderr = StringIO()
        with telegram_patches(self.root):
            first = telegram_command.load_client_db()
            self.assertIn(CASCADE_TAG, stored_routes(self.root))
            self.assertTrue(client_lock_tests.lock_is_free(self.lock_path))
            # Drift уже записан: чтение не ждёт занятую блокировку.
            with mock.patch.object(telegram_command, "ROUTE_DRIFT_LOCK_TIMEOUT", 10), \
                client_lock_tests.foreign_lock_holder(self.lock_path, HOLDER), \
                redirect_stderr(stderr):
                started = time.monotonic()
                second = telegram_command.load_client_db()
                elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn(CASCADE_TAG, first["cascadeRoutes"])
        self.assertEqual(second["cascadeRoutes"], first["cascadeRoutes"])

    def test_load_client_db_skips_drift_write_when_lock_is_busy(self) -> None:
        stderr = StringIO()
        with telegram_patches(self.root), \
            mock.patch.object(telegram_command, "ROUTE_DRIFT_LOCK_TIMEOUT", 0.2), \
            client_lock_tests.foreign_lock_holder(self.lock_path, HOLDER), \
            redirect_stderr(stderr):
            db = telegram_command.load_client_db()

        # Ответ строится по снимку, синхронизированному в памяти; запись повторит следующее чтение.
        self.assertIn(CASCADE_TAG, db["cascadeRoutes"])
        self.assertNotIn(CASCADE_TAG, stored_routes(self.root))
        self.assertEqual(stderr.getvalue().splitlines(), [DRIFT_WARN])

    def test_poller_answers_and_continues_when_lock_is_busy(self) -> None:
        updates = [message_update(CLIENT_CHAT, 1, "/status"), message_update(OTHER_CHAT, 2, "/status")]
        stderr = StringIO()
        with telegram_patches(self.root, updates=updates) as sent, \
            mock.patch.object(telegram_command, "ROUTE_DRIFT_LOCK_TIMEOUT", 0.2), \
            client_lock_tests.foreign_lock_holder(self.lock_path, HOLDER), \
            redirect_stderr(stderr):
            returncode = telegram_command.poll_user_subscriptions(quiet=True)

        self.assertEqual(returncode, 0)
        self.assertEqual([chat_id for chat_id, _text in sent], [CLIENT_CHAT, OTHER_CHAT])
        self.assertEqual(stderr.getvalue().splitlines(), [DRIFT_WARN, DRIFT_WARN])
        self.assertNotIn(CASCADE_TAG, stored_routes(self.root))
        self.assertEqual(stored_settings(self.root)["clientSubscriptionState"]["userUpdateOffset"], 3)

    def test_poller_does_not_hold_lock_while_answering(self) -> None:
        lock_states = []
        updates = [message_update(CLIENT_CHAT, 1, "/status"), message_update(OTHER_CHAT, 2, "/help")]
        with telegram_patches(
            self.root,
            updates=updates,
            on_send=lambda: lock_states.append(client_lock_tests.lock_is_free(self.lock_path)),
        ) as sent:
            returncode = telegram_command.poll_user_subscriptions(quiet=True)

        self.assertEqual(returncode, 0)
        self.assertEqual([chat_id for chat_id, _text in sent], [CLIENT_CHAT, OTHER_CHAT])
        self.assertEqual(lock_states, [True, True])
        self.assertIn(CASCADE_TAG, stored_routes(self.root))
        self.assertTrue(client_lock_tests.lock_is_free(self.lock_path))


class TelegramSetupLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock_path = self.root / "manager.lock"
        make_telegram_sandbox(self.root)

    def write_payment_from_other_process(self) -> None:
        telegram_settings.save_db_sections(
            {"paymentTotalAmount": "1500", "paymentCurrency": "₽"},
            PAYMENT_SECTIONS,
        )

    def test_configure_owner_keeps_settings_written_while_choosing_chat(self) -> None:
        """XR-REV-001: выбор чата владельца идёт без lock, запись — под lock по свежему снимку."""
        events = []
        real_save_db = telegram_settings.save_db

        def choose_chat(_db):
            events.append(("choose_private_chat", client_lock_tests.lock_is_free(self.lock_path)))
            # Пока владелец выбирает чат, другой процесс меняет сумму оплаты.
            self.write_payment_from_other_process()
            return {"id": "555", "label": "@new-owner"}

        def spy_save_db(db, *args, **kwargs):
            events.append(("save_db", client_lock_tests.lock_is_free(self.lock_path)))
            return real_save_db(db, *args, **kwargs)

        with telegram_patches(self.root), \
            mock.patch.object(telegram_setup, "choose_private_chat", side_effect=choose_chat), \
            mock.patch.object(telegram_setup.settings, "save_db", side_effect=spy_save_db), \
            mock.patch.object(telegram_setup, "configure_bot_commands"), \
            redirect_stdout(StringIO()):
            telegram_setup.configure_owner(send_test=False)

        self.assertEqual(events, [("choose_private_chat", True), ("save_db", False)])
        settings = stored_settings(self.root)
        self.assertEqual(settings["chatId"], "555")
        self.assertEqual(settings["chatLabel"], "@new-owner")
        self.assertEqual(settings["botUsername"], "ExampleVpnBot")
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["paymentTotalAmount"], "1500")
        self.assertTrue(client_lock_tests.lock_is_free(self.lock_path))

    def test_owner_stops_when_token_changed_while_choosing_chat(self) -> None:
        """60-R01: имя бота, чат и смещение получены для прежнего token, а под lock в базе уже другой."""

        def choose_chat(_db):
            # Пока владелец выбирает чат, другой процесс завершает setup с новым token.
            telegram_settings.save_db_sections({"token": "654321:other-token"}, ("token",))
            return {"id": "555", "label": "@new-owner"}

        stderr = StringIO()
        with telegram_patches(self.root, updates=[message_update("555", 41, "/start")]), \
            mock.patch.object(telegram_setup, "choose_private_chat", side_effect=choose_chat), \
            mock.patch.object(telegram_setup, "configure_bot_commands") as configure_bot_commands, \
            mock.patch.object(telegram_command, "require_root"), \
            mock.patch.object(telegram_command.sys, "argv", ["xray-telegram", "owner"]), \
            redirect_stdout(StringIO()), \
            redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                telegram_command.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(
            stderr.getvalue().splitlines(),
            [
                "ERROR: Token Telegram-бота изменён другой операцией во время привязки владельца.",
                "Запусти xray-telegram owner ещё раз.",
            ],
        )
        configure_bot_commands.assert_not_called()
        settings = stored_settings(self.root)
        self.assertEqual(settings["token"], "654321:other-token")
        self.assertEqual(settings["chatId"], OWNER_CHAT)
        self.assertEqual(settings["chatLabel"], "owner")
        self.assertEqual(settings["botUsername"], "")
        self.assertEqual(settings["clientSubscriptionState"]["userUpdateOffset"], 0)
        self.assertTrue(client_lock_tests.lock_is_free(self.lock_path))

    def test_setup_keeps_settings_written_while_answering_prompts(self) -> None:
        """XR-REV-001: вопросы setup задаются без lock; другой процесс между ними не теряет изменения."""
        answers = {"BOT_TOKEN: ": "654321:new-token", "Route mode [1-direct]: ": "1"}
        prompts = []

        def fake_input(prompt: str) -> str:
            prompts.append((prompt, client_lock_tests.lock_is_free(self.lock_path)))
            if prompt.startswith("BOT_NAME"):
                self.write_payment_from_other_process()
                return "Helper"
            return answers[prompt]

        with telegram_patches(self.root), \
            mock.patch("builtins.input", side_effect=fake_input), \
            mock.patch.object(telegram_setup, "configure_owner") as configure_owner, \
            redirect_stdout(StringIO()):
            telegram_setup.setup()

        self.assertEqual(
            prompts,
            [("BOT_TOKEN: ", True), ("BOT_NAME [Vireika]: ", True), ("Route mode [1-direct]: ", True)],
        )
        configure_owner.assert_called_once_with(send_test=True)
        settings = stored_settings(self.root)
        self.assertEqual(settings["token"], "654321:new-token")
        self.assertEqual(settings["botName"], "Helper")
        self.assertEqual(settings["routeMode"], "direct")
        self.assertEqual(settings["paymentTotalAmount"], "1500")
        self.assertTrue(client_lock_tests.lock_is_free(self.lock_path))

    def test_busy_lock_stops_telegram_writers_with_clear_error(self) -> None:
        commands = [
            ["payment-amount", "1000"],
            ["payment-domain-rent", "1200"],
            ["payment-rounding", "step", "50"],
            ["payment-details", "none"],
            ["bot-name", "Other"],
            ["enable"],
            ["disable"],
            ["mode", "direct"],
        ]
        before = stored_settings(self.root)

        for args in commands:
            with self.subTest(" ".join(args)):
                stderr = StringIO()
                with telegram_patches(self.root), \
                    mock.patch.object(telegram_command, "MANAGER_LOCK_TIMEOUT", 0.2), \
                    mock.patch.object(telegram_setup, "MANAGER_LOCK_TIMEOUT", 0.2), \
                    mock.patch.object(telegram_command, "require_root"), \
                    mock.patch.object(telegram_command.sys, "argv", ["xray-telegram", *args]), \
                    client_lock_tests.foreign_lock_holder(self.lock_path, HOLDER), \
                    redirect_stdout(StringIO()), \
                    redirect_stderr(stderr):
                    started = time.monotonic()
                    with self.assertRaises(SystemExit) as raised:
                        telegram_command.main()
                    elapsed = time.monotonic() - started

                self.assertLess(elapsed, 5)
                self.assertEqual(raised.exception.code, 1)
                self.assertEqual(stderr.getvalue().splitlines(), LOCK_ERROR_LINES)

        self.assertEqual(stored_settings(self.root), before)

    def test_telegram_writes_happen_under_lock(self) -> None:
        self.assertEqual(unlocked_save_db_calls(PACKAGE_DIR / "telegram" / "setup.py"), [])
        # save_db в commands/telegram.py — обёртка над telegram_settings.save_db; её вызывают под lock.
        self.assertEqual(
            unlocked_save_db_calls(PACKAGE_DIR / "commands" / "telegram.py", skip_functions=("save_db",)),
            [],
        )

    def test_poller_and_admin_do_not_take_manager_lock(self) -> None:
        """Poller не держит lock между апдейтами: admin-действия идут через xray-client, который берёт lock сам."""
        for name in ("poller.py", "admin.py"):
            with self.subTest(name):
                source = (PACKAGE_DIR / "telegram" / name).read_text(encoding="utf-8")
                self.assertNotIn("manager_lock", source)
                self.assertNotIn("core.locks", source)


if __name__ == "__main__":
    unittest.main()
