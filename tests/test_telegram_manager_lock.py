"""Блокировки писателей Telegram и чтение клиентских маршрутов при обработке update."""

import ast
import fcntl
from dataclasses import replace
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
from types import SimpleNamespace

from tests import test_client_manager_lock as client_lock_tests
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.commands import client as client_command
from xray_vps_manager.commands import telegram as telegram_command
from xray_vps_manager.core import locks as core_locks, paths as core_paths
from xray_vps_manager.core.errors import ConflictError, LockTimeout
from xray_vps_manager.db import database
from xray_vps_manager.telegram import poller as telegram_poller
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

    relocated = replace(core_paths.paths(), telegram_poller_lock=root / "telegram-poller.lock")
    with mock.patch.object(core_paths, "paths", return_value=relocated), \
        mock.patch.object(core_paths, "MANAGER_LOCK_PATH", root / "manager.lock"), \
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
    """Poller читает снимок для /status и продолжает после завершения параллельного xray-client add."""
    root = Path(argv[0])
    real_load_db_sql = client_repository.load_db_sql

    def load_then_wait_for_writer(*args, **kwargs):
        db = real_load_db_sql(*args, **kwargs)
        (root / "poller-loaded").touch()
        if not client_lock_tests.wait_for_file(root / "writer-done", client_lock_tests.WORKER_TIMEOUT // 2):
            raise AssertionError("Писатель не завершился во время обработки update")
        return db

    with telegram_patches(root, updates=[message_update(CLIENT_CHAT, 1, "/status")]) as sent, \
        mock.patch.object(client_repository, "load_db_sql", side_effect=load_then_wait_for_writer), \
        mock.patch.object(client_repository, "save_db") as save_db:
        returncode = telegram_command.poll_user_subscriptions(quiet=True)
        save_db.assert_not_called()
    (root / "poller-sent.json").write_text(json.dumps(sent))
    return returncode


def writer_worker(argv: list) -> int:
    """Отдельный процесс xray-client add: стартует, когда poller уже прочитал снимок client-DB."""
    root, name = Path(argv[0]), argv[1]
    try:
        if not client_lock_tests.wait_for_file(root / "poller-loaded", client_lock_tests.WORKER_TIMEOUT):
            print("poller did not load the client snapshot", file=sys.stderr)
            return 2
        with client_lock_tests.sandbox_patches(root, config_suffix=f".{name}"), redirect_stdout(StringIO()):
            client_command.cmd_add(name, prompt_for_access=False, connection_tag="vless-reality")
    finally:
        (root / "writer-done").touch()
    return 0


def new_owner_worker(argv: list) -> int:
    root = Path(argv[0])
    flock = fcntl.flock

    def observe_wait(fd, operation):
        try:
            return flock(fd, operation)
        except BlockingIOError:
            slot = argv[2] if len(argv) > 2 else ""
            (root / ("owner-waiting" + slot)).touch()
            if "pause" in argv and not client_lock_tests.wait_for_file(root / ("resume-owner" + slot), 5):
                raise AssertionError("Не получен сигнал продолжения привязки")
            raise

    with telegram_patches(root), mock.patch.object(fcntl, "flock", side_effect=observe_wait), \
        mock.patch.object(telegram_setup, "choose_private_chat", return_value={"id": OTHER_CHAT, "label": "new"}), \
        mock.patch.object(telegram_setup, "configure_bot_commands"):
        telegram_setup.configure_owner(send_test=False)
    return 0


def start_worker(function: str, *args: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    code = f"import sys; from tests import test_telegram_manager_lock as t; sys.exit(t.{function}(sys.argv[1:]))"
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
    names = ("save_db", "save_db_sections", "save_owner_binding")
    return (isinstance(func, ast.Name) and func.id in names) or (
        isinstance(func, ast.Attribute) and func.attr in names
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

    def test_poller_read_does_not_lose_client_added_by_other_process(self) -> None:
        """B2.2: poller отвечает по снимку и не сохраняет его поверх результата параллельного add."""
        poller = start_worker("poller_worker", str(self.root))
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

    def test_load_client_db_syncs_drift_only_in_memory(self) -> None:
        """B2.2 исправляет XR-REV-014: раньше первое чтение сохраняло подписи, теперь они только в снимке."""
        db_path = self.root / "manager.db"
        before = db_path.read_bytes()
        with telegram_patches(self.root), \
            client_lock_tests.foreign_lock_holder(self.lock_path, HOLDER), \
            mock.patch.object(fcntl, "flock", side_effect=AssertionError("Чтение попыталось взять flock")) as lock, \
            mock.patch.object(client_repository, "save_db") as save_db:
            db = telegram_command.load_client_db()

        lock.assert_not_called()
        save_db.assert_not_called()
        self.assertEqual(db["cascadeRoutes"][CASCADE_TAG]["country"], "США")
        self.assertEqual(db_path.read_bytes(), before)
        self.assertNotIn(CASCADE_TAG, stored_routes(self.root))

    def test_poller_answers_and_continues_when_lock_is_busy(self) -> None:
        updates = [message_update(CLIENT_CHAT, 1, "/status"), message_update(OTHER_CHAT, 2, "/status")]
        stderr = StringIO()
        with telegram_patches(self.root, updates=updates) as sent, \
            client_lock_tests.foreign_lock_holder(self.lock_path, HOLDER), \
            redirect_stderr(stderr):
            returncode = telegram_command.poll_user_subscriptions(quiet=True)

        self.assertTrue((self.root / "telegram-poller.lock").is_file())
        self.assertEqual(returncode, 0)
        self.assertEqual([chat_id for chat_id, _text in sent], [CLIENT_CHAT, OTHER_CHAT])
        self.assertEqual(stderr.getvalue(), "")
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
        self.assertNotIn(CASCADE_TAG, stored_routes(self.root))
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

    def test_owner_waits_for_inflight_admin_command(self) -> None:
        worker = None
        def extend(command, **kwargs):
            nonlocal worker
            self.assertEqual(command[1:], ["extend-days", "fixture", "14"])
            worker = start_worker("new_owner_worker", str(self.root))
            self.addCleanup(worker.kill)
            self.assertTrue(client_lock_tests.wait_for_file(self.root / "owner-waiting", 5))
            self.assertTrue(client_lock_tests.lock_is_free(self.lock_path))
            self.assertEqual(stored_settings(self.root)["chatId"], OWNER_CHAT)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with telegram_patches(self.root, updates=[message_update(OWNER_CHAT, 41, "14")]), \
            mock.patch.object(telegram_command, "load_client_db", return_value={"clients": {"fixture": {"enabled": True}}}), \
            mock.patch.object(telegram_command, "run_capture", side_effect=extend) as run:
            telegram_settings.save_db_sections({"adminState": {OWNER_CHAT: {
                "action": "extend-subscription-days", "client": "fixture",
            }}}, ("adminState",))
            self.assertEqual(telegram_command.poll_user_subscriptions(quiet=True), 0)
        run.assert_called_once()
        stdout, stderr = worker.communicate(timeout=5)
        self.assertEqual(worker.returncode, 0, stdout + stderr)
        self.assertEqual(stored_settings(self.root)["chatId"], OTHER_CHAT)

    def test_background_poller_cannot_overtake_waiting_owner(self) -> None:
        root = self.root
        with telegram_patches(root):
            with core_locks.telegram_poller_lock():
                worker = start_worker("new_owner_worker", str(root), "pause")
                self.addCleanup(worker.kill)
                self.assertTrue(client_lock_tests.wait_for_file(root / "owner-waiting", 5))
            try:
                # Пачка отпущена, но owner ещё не получил CPU для следующей попытки.
                with mock.patch.object(core_locks, "hold_file_lock", side_effect=AssertionError("Опрос занял основной flock")):
                    with self.assertRaises(LockTimeout), core_locks.telegram_poller_lock():
                        self.fail("Следующая пачка обогнала ожидающую команду owner")
            finally:
                (root / "resume-owner").touch()
            stdout, stderr = worker.communicate(timeout=5)
            self.assertEqual(worker.returncode, 0, stdout + stderr)
            self.assertEqual(stored_settings(root)["chatId"], OTHER_CHAT)

    def test_owner_registers_while_poller_is_entering(self) -> None:
        root = self.root
        worker = None
        write_holder = core_locks.write_lock_holder

        def owner_arrives(fd, line):
            nonlocal worker
            write_holder(fd, line)
            worker = start_worker("new_owner_worker", str(root), "pause")
            self.addCleanup(worker.kill)
            self.assertTrue(client_lock_tests.wait_for_file(root / "owner-waiting", 5))

        with telegram_patches(root):
            try:
                with mock.patch.object(core_locks, "write_lock_holder", side_effect=owner_arrives):
                    with self.assertRaises(LockTimeout), core_locks.telegram_poller_lock():
                        self.fail("Пачка началась после регистрации ожидающего владельца")
                with self.assertRaises(LockTimeout), core_locks.telegram_poller_lock():
                    self.fail("Следующая пачка обогнала владельца")
            finally:
                (root / "resume-owner").touch()
            stdout, stderr = worker.communicate(timeout=5)
            self.assertEqual(worker.returncode, 0, stdout + stderr)
            self.assertEqual(stored_settings(root)["chatId"], OTHER_CHAT)

    def test_linux_read_probe_detects_each_waiting_owner(self) -> None:
        root = self.root
        workers = []
        with telegram_patches(root):
            with core_locks.telegram_poller_lock():
                for slot in ("1", "2"):
                    worker = start_worker("new_owner_worker", str(root), "pause", slot)
                    self.addCleanup(worker.kill)
                    workers.append((slot, worker))
                    self.assertTrue(client_lock_tests.wait_for_file(root / ("owner-waiting" + slot), 5))
            for slot, worker in workers:
                with (root / "telegram-poller.entry.lock").open("rb+") as entry:
                    # glibc F_TEST запрашивает совместимость с блокировкой чтения.
                    with self.assertRaises((BlockingIOError, PermissionError)):
                        fcntl.lockf(entry, fcntl.LOCK_SH | fcntl.LOCK_NB)
                (root / ("resume-owner" + slot)).touch()
                stdout, stderr = worker.communicate(timeout=5)
                self.assertEqual(worker.returncode, 0, stdout + stderr)
            with core_locks.telegram_poller_lock():
                self.assertEqual(stored_settings(root)["chatId"], OTHER_CHAT)

    def test_owner_timeout_keeps_previous_binding(self) -> None:
        stderr = StringIO()
        self.assertEqual(telegram_setup.OWNER_POLL_LOCK_TIMEOUT, 60)
        with telegram_patches(self.root), client_lock_tests.foreign_lock_holder(self.root / "telegram-poller.lock", HOLDER), \
            mock.patch.object(telegram_setup, "choose_private_chat", return_value={"id": OTHER_CHAT, "label": "new"}), \
            mock.patch.object(telegram_setup, "OWNER_POLL_LOCK_TIMEOUT", 0.025), \
            mock.patch.object(telegram_setup, "pending_owner_updates") as fetch, \
            mock.patch("os.geteuid", return_value=0), mock.patch.object(sys, "argv", ["xray-telegram", "owner"]), \
            redirect_stdout(StringIO()), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                telegram_command.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Опрос Telegram уже выполняется", stderr.getvalue())
        fetch.assert_not_called()
        self.assertEqual(stored_settings(self.root)["chatId"], OWNER_CHAT)

    def test_owner_rechecks_queue_if_sequence_changes_during_binding(self) -> None:
        for first_empty in (False, True):
            with self.subTest(first_empty=first_empty):
                update = message_update(OWNER_CHAT, 401, "14")
                with telegram_patches(self.root) as sent, redirect_stdout(StringIO()):
                    original = telegram_settings.load_db()
                    original["clientSubscriptionState"] = {"userUpdateOffset": 9001, "userUpdateOffsetConfirmed": True}
                    original["adminState"] = {OWNER_CHAT: {"action": "extend-subscription-days", "client": "fixture"}}
                    telegram_settings.save_db(original)
                    reads = []

                    def pending(db, polled_at):
                        state = db["clientSubscriptionState"]
                        reads.append(state.get("userUpdateSequence", 0))
                        if len(reads) == 1:
                            # Другой poller успел зарезервировать начало новой последовательности.
                            telegram_settings.claim_user_updates(dict(state), {400: {}}, "2026-06-12T22:00:00Z")
                            if first_empty:
                                return {}
                        return {401: update}

                    with mock.patch.object(telegram_setup, "choose_private_chat", return_value={"id": OWNER_CHAT, "label": "owner"}), \
                        mock.patch.object(telegram_setup, "pending_owner_updates", side_effect=pending), \
                        mock.patch.object(telegram_setup, "configure_bot_commands"):
                        telegram_setup.configure_owner(send_test=False)
                    self.assertEqual(reads, [0, 1])
                    self.assertEqual(telegram_settings.load_db()["clientSubscriptionState"]["userUpdateOffset"], 402)

                    def fetch(db, method, payload=None, **kwargs):
                        return {"result": [update] if payload.get("offset", 0) <= 401 else []}

                    with mock.patch.object(telegram_command.telegram_api, "curl_json", side_effect=fetch), \
                        mock.patch.object(telegram_command, "load_client_db", return_value={"clients": {"fixture": {}}}), \
                        mock.patch.object(telegram_command, "run_capture") as run:
                        self.assertEqual(telegram_command.poll_user_subscriptions(quiet=True), 0)
                    self.assertEqual(sent, [])
                    run.assert_not_called()

    def test_delayed_reservation_preserves_time_of_request(self) -> None:
        started = "2026-07-10T12:00:00Z"
        resumed = "2026-07-19T12:00:00Z"
        for bind_owner in (False, True):
            with self.subTest(bind_owner=bind_owner), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                make_telegram_sandbox(root)
                now = started
                queue = [message_update(CLIENT_CHAT, 9000, "/help")]

                def transport(db, method, payload=None, **kwargs):
                    nonlocal now
                    if method == "getMe":
                        return {"result": {"username": "bot"}}
                    now = resumed  # Процесс возобновился после истечения старой очереди.
                    return {"result": [u for u in queue if u["update_id"] >= payload.get("offset", 0)]}

                with telegram_patches(root) as sent, redirect_stdout(StringIO()), \
                    mock.patch.object(telegram_setup.api, "curl_json", side_effect=transport), \
                    mock.patch.object(telegram_setup, "utc_stamp", side_effect=lambda: now), \
                    mock.patch.object(telegram_command, "utc_stamp", side_effect=lambda: now), \
                    mock.patch.object(telegram_setup, "choose_private_chat", return_value={"id": OWNER_CHAT, "label": "owner"}), \
                    mock.patch.object(telegram_setup, "configure_bot_commands"):
                    if bind_owner:
                        telegram_setup.configure_owner(send_test=False)
                    else:
                        self.assertEqual(telegram_command.poll_user_subscriptions(quiet=True), 0)
                    self.assertEqual(stored_settings(root)["clientSubscriptionState"]["lastUserPoll"], started)
                    replies = len(sent)
                    queue[:] = [message_update(OTHER_CHAT, 8999, "/help")]
                    self.assertEqual(telegram_command.poll_user_subscriptions(quiet=True), 0)
                    self.assertEqual([chat for chat, _ in sent[replies:]], [OTHER_CHAT])

    def test_owner_migrates_legacy_cursor_before_polling_new_sequence(self) -> None:
        queue = [message_update(OWNER_CHAT, 400, "/start")]
        requests = []

        def transport(db, method, payload=None, **kwargs):
            if method == "getMe":
                return {"result": {"username": "bot"}}
            offset = payload.get("offset", 0)
            requests.append(offset)
            # При слишком далёкой позиции Bot API возвращает голову очереди.
            if queue and offset <= queue[-1]["update_id"] + 11:
                queue[:] = [u for u in queue if u["update_id"] >= offset]
            return {"result": queue[:]}

        with telegram_patches(self.root) as sent, redirect_stdout(StringIO()), \
            mock.patch.object(telegram_setup.api, "curl_json", side_effect=transport), \
            mock.patch.object(telegram_setup, "choose_private_chat", return_value={"id": OWNER_CHAT, "label": "owner"}), \
            mock.patch.object(telegram_setup, "configure_bot_commands"):
            old = telegram_settings.load_db()
            old["clientSubscriptionState"] = {"userUpdateOffset": 9001}
            telegram_settings.save_db(old)
            telegram_setup.configure_owner(send_test=False)
            queue.append(message_update(CLIENT_CHAT, 401, "/help"))
            self.assertEqual(telegram_command.poll_user_subscriptions(quiet=True), 0)
            self.assertEqual(telegram_command.poll_user_subscriptions(quiet=True), 0)
            self.assertEqual(requests, [9001, 400, 401, 402])
            self.assertEqual([chat for chat, _ in sent], [CLIENT_CHAT])
            self.assertEqual(telegram_settings.load_db()["clientSubscriptionState"]["userUpdateOffset"], 402)

    def test_partial_owner_response_keeps_reserved_commands_excluded(self) -> None:
        updates = [message_update(OTHER_CHAT, 41, "/help"), message_update(OTHER_CHAT, 42, "14")]
        with telegram_patches(self.root, updates=updates) as sent, redirect_stdout(StringIO()):
            old = telegram_settings.load_db()
            self.assertEqual(telegram_command.poll_user_subscriptions(quiet=True), 0)
            before_binding = list(sent)
            # Запись напоминаний из старого снимка не должна удалить отпечатки закреплённой пачки.
            telegram_settings.save_db_sections(old, ("clientSubscriptionState",))
            with mock.patch.object(telegram_setup, "choose_private_chat", return_value={"id": OTHER_CHAT, "label": "new"}), \
                mock.patch.object(telegram_setup, "pending_owner_updates", return_value={41: updates[0]}), \
                mock.patch.object(telegram_setup, "configure_bot_commands"):
                telegram_setup.configure_owner(send_test=False)
            state = stored_settings(self.root)["clientSubscriptionState"]
            self.assertEqual((state["userUpdateOffset"], state["userUpdateSequence"]), (43, 0))
            def fetch(db, method, payload=None, **kwargs):
                return {"result": [u for u in updates if u["update_id"] >= payload.get("offset", 0)]}
            with mock.patch.object(telegram_command.telegram_api, "curl_json", side_effect=fetch), \
                mock.patch.object(telegram_command, "run_capture") as run:
                self.assertEqual(telegram_command.poll_user_subscriptions(quiet=True), 0)
            self.assertEqual(sent, before_binding)
            run.assert_not_called()

    def test_older_owner_request_keeps_timestamp_of_newer_claim(self) -> None:
        with telegram_patches(self.root):
            db = telegram_settings.load_db()
            initial = dict(db["clientSubscriptionState"])
            telegram_settings.claim_user_updates(dict(initial), {41: {}, 42: {}}, "2026-06-12T12:00:03Z")
            self.assertTrue(telegram_settings.save_owner_binding(db, initial, {41: {}}, "2026-06-12T12:00:00Z"))
            state = telegram_settings.load_db()["clientSubscriptionState"]
            self.assertEqual((state["userUpdateOffset"], state["lastUserPoll"]), (43, "2026-06-12T12:00:03Z"))

    def test_owner_does_not_bind_when_final_queue_read_fails(self) -> None:
        with telegram_patches(self.root), redirect_stdout(StringIO()), \
            mock.patch.object(telegram_setup, "choose_private_chat", return_value={"id": OTHER_CHAT, "label": "new"}), \
            mock.patch.object(telegram_setup.api, "curl_json", side_effect=[{"result": {"username": "bot"}}, RuntimeError("getUpdates")]), \
            mock.patch.object(telegram_setup, "configure_bot_commands") as configure:
            with self.assertRaisesRegex(RuntimeError, "getUpdates"):
                telegram_setup.configure_owner(send_test=False)
        self.assertEqual(stored_settings(self.root)["chatId"], OWNER_CHAT)
        configure.assert_not_called()

    def test_owner_confirms_new_offset_after_idle_period(self) -> None:
        for offset in (41, 45, 50):
            with self.subTest(offset=offset):
                moment = "2026-06-12T22:00:00Z"
                with telegram_patches(self.root, updates=[message_update(OWNER_CHAT, 44, "/help")]), \
                    mock.patch.object(telegram_setup, "choose_private_chat", return_value={"id": OWNER_CHAT, "label": "owner"}), \
                    mock.patch.object(telegram_setup, "configure_bot_commands"), \
                    mock.patch.object(telegram_setup, "utc_stamp", return_value=moment), redirect_stdout(StringIO()):
                    original = telegram_settings.load_db()
                    original["clientSubscriptionState"] = {"userUpdateOffset": offset, "userUpdateOffsetConfirmed": True,
                                                           "lastUserPoll": "2026-05-01T12:00:00Z"}
                    telegram_settings.save_db(original)
                    telegram_setup.configure_owner(send_test=False)
                    with mock.patch.object(telegram_command.telegram_api, "curl_json", return_value={"ok": True, "result": []}) as fetch, \
                        mock.patch.object(telegram_command, "utc_stamp", return_value=moment):
                        self.assertEqual(telegram_command.poll_user_subscriptions(quiet=True), 0)
                    self.assertEqual(fetch.call_args.kwargs["payload"]["offset"], 45)

    def test_configure_owner_keeps_settings_written_while_choosing_chat(self) -> None:
        """XR-REV-001: выбор чата владельца идёт без lock, запись — под lock по свежему снимку."""
        events = []
        real_save_db_sections = telegram_settings.save_db_sections

        def choose_chat(_db):
            events.append(("choose_private_chat", client_lock_tests.lock_is_free(self.lock_path)))
            # Пока владелец выбирает чат, другой процесс меняет сумму оплаты (мимо spy).
            real_save_db_sections({"paymentTotalAmount": "1500", "paymentCurrency": "₽"}, PAYMENT_SECTIONS)
            return {"id": "555", "label": "@new-owner"}

        real_owner_binding = telegram_settings.save_owner_binding

        def spy_owner_binding(db, *args, **kwargs):
            events.append(("save_owner_binding", client_lock_tests.lock_is_free(self.lock_path)))
            return real_owner_binding(db, *args, **kwargs)

        with telegram_patches(self.root), \
            mock.patch.object(telegram_setup, "choose_private_chat", side_effect=choose_chat), \
            mock.patch.object(telegram_setup.settings, "save_owner_binding", side_effect=spy_owner_binding), \
            mock.patch.object(telegram_setup, "configure_bot_commands"), \
            redirect_stdout(StringIO()):
            telegram_setup.configure_owner(send_test=False)

        self.assertEqual(events, [("choose_private_chat", True), ("save_owner_binding", False)])
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
            mock.patch("os.geteuid", return_value=0), \
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

    def add_telegram_socks_route(self) -> None:
        """Telegram SOCKS для cascade уже в config.json, а routeMode в базе ещё direct: setup оборвался."""
        config = json.loads((self.root / "config.json").read_text())
        config["inbounds"].append(
            {"tag": telegram_setup.TELEGRAM_SOCKS_TAG, "listen": "127.0.0.1", "port": 10810, "protocol": "socks", "settings": {}}
        )
        config["routing"]["rules"].append(
            {"type": "field", "inboundTag": [telegram_setup.TELEGRAM_SOCKS_TAG], "outboundTag": CASCADE_TAG}
        )
        (self.root / "config.json").write_text(json.dumps(config))

    def test_adopt_cascade_asks_without_lock_and_writes_route_mode_under_lock(self) -> None:
        self.add_telegram_socks_route()
        events = []

        def answer(_prompt: str = "") -> str:
            events.append(("input", client_lock_tests.lock_is_free(self.lock_path)))
            return "y"

        with telegram_patches(self.root), \
            mock.patch("builtins.input", side_effect=answer), \
            redirect_stdout(StringIO()):
            db = telegram_setup.maybe_adopt_existing_cascade_route(stored_settings(self.root))

        self.assertEqual(events, [("input", True)])
        self.assertEqual(db["routeMode"], "cascade")
        self.assertEqual(stored_settings(self.root)["routeMode"], "cascade")
        self.assertTrue(client_lock_tests.lock_is_free(self.lock_path))

    def test_adopt_cascade_stops_when_socks_removed_while_answering(self) -> None:
        """58-R02: пока админ отвечает, другая сессия выполнила `mode direct` и удалила Telegram SOCKS."""
        self.add_telegram_socks_route()

        def answer(_prompt: str = "") -> str:
            config = json.loads((self.root / "config.json").read_text())
            telegram_setup.remove_telegram_proxy_config(config)
            (self.root / "config.json").write_text(json.dumps(config))
            return "y"

        with telegram_patches(self.root), \
            mock.patch("builtins.input", side_effect=answer), \
            redirect_stdout(StringIO()):
            with self.assertRaises(ConflictError) as raised:
                telegram_setup.maybe_adopt_existing_cascade_route(stored_settings(self.root))

        self.assertEqual(
            raised.exception.message,
            "Telegram SOCKS для cascade удалён другой операцией во время вопроса о маршруте.",
        )
        self.assertEqual(raised.exception.hint, "Запусти xray-telegram owner ещё раз.")
        self.assertNotEqual(stored_settings(self.root).get("routeMode"), "cascade")
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
                    mock.patch("os.geteuid", return_value=0), \
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
        # save_db_sections в commands/telegram.py — обёртка для poller/admin/notifications, они пишут без lock.
        self.assertEqual(
            unlocked_save_db_calls(PACKAGE_DIR / "commands" / "telegram.py", skip_functions=("save_db_sections",)),
            [],
        )

    def test_poller_and_admin_do_not_take_manager_lock(self) -> None:
        """Poller не держит lock между апдейтами: admin-действия идут через xray-client, который берёт lock сам."""
        for name in ("poller.py", "admin.py"):
            with self.subTest(name):
                source = (PACKAGE_DIR / "telegram" / name).read_text(encoding="utf-8")
                self.assertNotIn("manager_lock", source)
                self.assertNotIn("core.locks", source)

    def test_country_switch_waits_for_route_longer_than_lock_timeout(self) -> None:
        """58-R01: бот ждёт xray-client route дольше, чем тот ждёт manager.lock, иначе kill опережает LockTimeout."""
        self.assertGreater(telegram_poller.CLIENT_ROUTE_TIMEOUT, client_command.MANAGER_LOCK_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
