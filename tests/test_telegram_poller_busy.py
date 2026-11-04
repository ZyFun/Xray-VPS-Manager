"""Конкуренция работающего poller с отдельным писателем manager.db (B2.5)."""

from contextlib import closing, ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
import sqlite3
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path
import time
import unittest
from unittest import mock

from tests import test_telegram_poller as fixtures
from xray_vps_manager.clients import repository as clients
from xray_vps_manager.commands import telegram as telegram_command
from xray_vps_manager.core import paths as core_paths
from xray_vps_manager.db import database
from xray_vps_manager.telegram import admin, poller, settings


HOLDER = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
if sys.argv[2] == "read":
    connection.execute("PRAGMA locking_mode=EXCLUSIVE")
connection.execute("BEGIN EXCLUSIVE")
print("locked", flush=True)
sys.stdin.readline()
connection.execute("COMMIT")
connection.close()
"""


def competing_manual_poll(root):
    with core_paths.use(core_paths.Paths.under(root)), mock.patch.object(
        telegram_command, "load_db", side_effect=AssertionError("Занятый опрос прочитал данные диалога"),
    ):
        return telegram_command.poll_user_subscriptions()


class TelegramPollerBusyTests(unittest.TestCase):
    def test_busy_during_read_subscription_callback_message_and_offset(self):
        for stage in ("read", "callback", "subscription", "reply", "offset", "admin"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                db_path = Path(tmp) / "manager.db"
                database.initialize(db_path).close()
                stack.enter_context(mock.patch.object(database, "MANAGER_DB_PATH", db_path))
                settings.save_db({
                    "enabled": True, "chatId": "99", "token": "fixture",
                    "clientSubscriptions": {"7": {"clientId": "fixture"}},
                    "clientSubscriptionState": {"userUpdateOffset": 41},
                })
                child = None
                started = None
                waits = []
                requests = []
                calls = []
                sent = []
                answers = []
                reservations = []
                def stored():
                    return settings.load_db()["clientSubscriptionState"]["userUpdateOffset"]

                def hold():
                    nonlocal child, started
                    if child is not None:
                        return
                    child = subprocess.Popen(
                        [sys.executable, "-c", HOLDER, str(db_path), stage],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    stack.enter_context(child)
                    stack.callback(child.kill)
                    self.assertEqual(child.stdout.readline().strip(), "locked")
                    started = time.monotonic()
                    if stage != "read":
                        self.assertEqual(stored(), 41 if stage == "offset" else 43)

                def release(_delay):
                    # Здесь первая попытка SQLite уже исчерпала настоящий busy_timeout.
                    waits.append(time.monotonic() - started)
                    _, stderr = child.communicate(input="\n", timeout=3)
                    self.assertEqual(child.returncode, 0, stderr)

                stack.enter_context(mock.patch.object(database, "DEFAULT_BUSY_TIMEOUT_MS", 20))
                stack.enter_context(mock.patch.object(database, "time", mock.Mock(sleep=release)))
                is_admin = stage == "admin"
                update = {"update_id": 41, "callback_query": {
                    "id": "button", "data": "admin:menu" if is_admin else "client:unsubscribe",
                    "message": {"chat": {"type": "private", "id": 99 if is_admin else 7}, "message_id": 10},
                }}
                def transport(db, method, payload=None, timeout=0):
                    if method != "getUpdates":
                        calls.append(method)
                        return {"ok": True}
                    requests.append(payload["offset"])
                    return {"result": [update, {"update_id": 42}] if payload["offset"] == 41 else []}

                def save(db, sections):
                    if (
                        (stage == "callback" and not answers)
                        or (stage == "subscription" and sections == ("clientSubscriptions",))
                    ):
                        hold()
                    settings.save_db_sections(db, sections)


                def claim(state, updates, stamp):
                    if stage == "offset":
                        hold()
                    previous = settings.claim_user_updates(state, updates, stamp)
                    reservations.append((previous, stored()))
                    return previous

                def send(*args, **kwargs):
                    sent.append(args[2])
                    if stage in ("reply", "admin"):
                        hold()
                    return {"result": {"message_id": 27}}

                ctx = fixtures.TelegramPollerTests().make_context({}, [], [])
                ctx = replace(ctx, load_db=settings.load_db, claim_user_updates=claim, curl_json=transport,
                              save_db_sections=save, send_chat_message=send,
                              answer_callback_query=lambda *args, **kwargs: answers.append(args[1]),
                              admin_context=replace(ctx.admin_context, save_db_sections=save, send_chat_message=send))
                if stage == "read":
                    hold()
                stderr = StringIO()
                with redirect_stderr(stderr), redirect_stdout(StringIO()):
                    self.assertEqual(poller.poll_user_subscriptions(ctx), 0)
                    self.assertEqual(poller.poll_user_subscriptions(ctx), 0)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(len(waits), 1)
                self.assertGreaterEqual(waits[0], 0.018)
                self.assertEqual(requests, [41, 43])
                self.assertEqual(reservations, [(41, 43), (43, 43)])
                self.assertEqual(answers, ["button"])
                self.assertEqual(len(sent), 1)
                self.assertEqual(calls, [] if is_admin else ["deleteMyCommands"])
                db = settings.load_db()
                if is_admin:
                    guard = db["adminState"][admin.ADMIN_CALLBACK_GUARDS_KEY]["99"]
                else:
                    self.assertNotIn("7", db["clientSubscriptions"])
                    guard = db["clientSubscriptionState"]["callbackGuards"]["7"]
                self.assertEqual(guard["activeMessageId"], 27)

    def test_status_waits_for_first_read_of_restored_database(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            path = Path(tmp) / "manager.db"
            database.initialize(path).close()
            settings.save_db({"enabled": True, "chatId": "99", "token": "fixture"}, db_path=path)
            restored = database.backup_database(path)
            writer = stack.enter_context(closing(sqlite3.connect(restored, isolation_level=None)))
            self.assertEqual(writer.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            connect = database.connect
            locked = False

            def before_statement(statement):
                nonlocal locked
                if statement == "BEGIN" and not locked:
                    writer.execute("BEGIN EXCLUSIVE")
                    locked = True

            def connect_to_restored(*args, **kwargs):
                reader = connect(*args, **kwargs)
                reader.set_trace_callback(before_statement)
                return reader

            def load_clients():
                with mock.patch.object(database, "connect", connect_to_restored):
                    return clients.load_db_sql(db_path=restored)

            stack.enter_context(mock.patch.object(database, "DEFAULT_BUSY_TIMEOUT_MS", 20))
            timer = stack.enter_context(mock.patch.object(database, "time"))
            timer.sleep.side_effect = lambda delay: writer.execute("COMMIT")
            update = {"update_id": 41, "message": {"text": "/status", "chat": {"type": "private", "id": 7}}}
            events = []
            ctx = fixtures.TelegramPollerTests().make_context({}, [update], events)
            ctx = replace(ctx, load_client_db=load_clients, load_db=lambda: settings.load_db(db_path=path),
                          claim_user_updates=partial(settings.claim_user_updates, db_path=path),
                          save_db_sections=lambda db, sections: settings.save_db_sections(db, sections, db_path=path))
            self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)
            timer.sleep.assert_called_once()
            self.assertTrue(locked)
            messages = [event[2] for event in events if event[0] == "send"]
            self.assertEqual(len(messages), 1)
            self.assertIn("не подписан", messages[0].lower())
            self.assertEqual(settings.load_db(db_path=path)["clientSubscriptionState"]["userUpdateOffset"], 42)

    def test_acknowledging_help_does_not_scan_subscriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manager.db"
            database.initialize(path).close()
            settings.save_db({"enabled": True, "chatId": "99", "token": "fixture",
                              "clientSubscriptions": {"7": {"clientId": "fixture"}}}, db_path=path)
            updates = [{"update_id": n, "message": {"text": "/help", "chat": {"type": "private", "id": 7}}}
                       for n in (41, 42)]
            events = []
            ctx = fixtures.TelegramPollerTests().make_context({}, updates, events)
            ctx = replace(ctx, load_db=lambda: settings.load_db(db_path=path),
                          claim_user_updates=partial(settings.claim_user_updates, db_path=path),
                          save_db_sections=lambda db, sections: settings.save_db_sections(db, sections, db_path=path))
            with mock.patch.object(settings, "_save_changed_subscriptions", wraps=settings._save_changed_subscriptions) as scan:
                self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)
            scan.assert_not_called()
            self.assertEqual(len([e for e in events if e[0] == "send"]), 2)
            db = settings.load_db(db_path=path)
            self.assertEqual(db["clientSubscriptionState"]["userUpdateOffset"], 43)
            self.assertEqual(db["clientSubscriptions"]["7"]["clientId"], "fixture")

    def test_manual_poll_claims_overlapping_updates_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manager.db"
            database.initialize(path).close()
            settings.save_db({"enabled": True, "chatId": "99", "token": "fixture",
                              "clientSubscriptionState": {"userUpdateOffset": 41}}, db_path=path)
            updates = [{"update_id": n, "message": {"text": "/traffic", "chat": {"type": "private", "id": n}}}
                       for n in (41, 42)]
            events = []
            common = replace(fixtures.TelegramPollerTests().make_context({}, [], events),
                             load_db=partial(settings.load_db, db_path=path),
                             claim_user_updates=partial(settings.claim_user_updates, db_path=path),
                             save_db_sections=partial(settings.save_db_sections, db_path=path))
            def manual_fetch(db, method, payload, timeout):
                self.assertEqual(payload["offset"], 41)
                return {"result": updates[:1]}

            def service_fetch(db, method, payload, timeout):
                self.assertEqual(payload["offset"], 41)
                # У службы уже есть ответ на запрос с offset 41. Ручной опрос опередил её запись.
                self.assertEqual(poller.poll_user_subscriptions(replace(common, curl_json=manual_fetch), quiet=True), 0)
                return {"result": updates}

            service = replace(common, curl_json=service_fetch)
            self.assertEqual(poller.poll_user_subscriptions(service, quiet=True), 0)
            self.assertEqual([event[1] for event in events if event[0] == "send"], ["41", "42"])
            self.assertEqual(settings.load_db(db_path=path)["clientSubscriptionState"]["userUpdateOffset"], 43)

    def test_new_update_sequence_survives_old_snapshot_and_is_acknowledged_once(self):
        for first_id in (400, 9000):
            with self.subTest(first_id=first_id), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "manager.db"
                database.initialize(path).close()
                settings.save_db({"enabled": True, "chatId": "99", "token": "fixture",
                                  "clientSubscriptionState": {"userUpdateOffset": 9001, "userUpdateOffsetConfirmed": True}}, db_path=path)
                stale = settings.load_db(db_path=path)
                events, requests = [], []
                def fetch(db, method, payload, timeout):
                    requests.append(payload.get("offset"))
                    update = {"update_id": first_id, "message": {"text": "/help", "chat": {"id": 7, "type": "private"}}}
                    return {"result": [update] if len(requests) == 1 else []}

                ctx = replace(fixtures.TelegramPollerTests().make_context({}, [], events), curl_json=fetch,
                              load_db=partial(settings.load_db, db_path=path),
                              claim_user_updates=partial(settings.claim_user_updates, db_path=path))
                poller.poll_user_subscriptions(ctx, quiet=True)
                stale["clientSubscriptionState"]["expiryReminders"] = {"7": "today"}
                settings.save_db_sections(stale, ("clientSubscriptionState",), db_path=path)
                poller.poll_user_subscriptions(ctx, quiet=True)
                poller.poll_user_subscriptions(ctx, quiet=True)
                self.assertEqual(requests, [None, first_id + 1, None])
                self.assertEqual(len([e for e in events if e[0] == "send"]), 1)
                state = settings.load_db(db_path=path)["clientSubscriptionState"]
                self.assertEqual(state["userUpdateOffset"], first_id + 1)
                self.assertEqual(state["expiryReminders"], {"7": "today"})

    def test_first_poll_of_legacy_cursor_skips_ambiguous_command_once(self):
        for stamp in ("2026-06-12T21:59:00Z", "2026-05-01T12:00:00Z", None):
            with self.subTest(stamp=stamp), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "manager.db"
                database.initialize(path).close()
                settings.save_db({"enabled": True, "chatId": "99", "token": "fixture", "clientSubscriptionState": {
                    "userUpdateOffset": 9001, **({"lastUserPoll": stamp} if stamp else {}),
                }}, db_path=path)
                # ID 9000 мог быть уже обработан старой версией. Первый запрос подтверждает его.
                pending = [{"update_id": 9000, "message": {"text": "/help", "chat": {"id": 7, "type": "private"}}}]
                events, requests = [], []
                def fetch(db, method, payload, timeout):
                    offset = payload.get("offset", 0)
                    requests.append(offset)
                    pending[:] = [item for item in pending if item["update_id"] >= offset]
                    return {"result": pending[:]}
                ctx = replace(fixtures.TelegramPollerTests().make_context({}, [], events), curl_json=fetch,
                              load_db=partial(settings.load_db, db_path=path),
                              claim_user_updates=partial(settings.claim_user_updates, db_path=path))
                poller.poll_user_subscriptions(ctx, quiet=True)
                self.assertEqual(pending, [])
                self.assertFalse(events)
                pending.append({"update_id": 8999, "message": {"text": "/help", "chat": {"id": 7, "type": "private"}}})
                poller.poll_user_subscriptions(ctx, quiet=True)
                poller.poll_user_subscriptions(ctx, quiet=True)
                self.assertEqual(requests, [9001, 0, 9000])
                self.assertEqual(len([event for event in events if event[0] == "send"]), 1)

    def test_legacy_head_fallback_reserves_ambiguous_batch_without_replaying_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manager.db"
            database.initialize(path).close()
            settings.save_db({"enabled": True, "chatId": "99", "token": "fixture", "clientSubscriptionState": {
                "userUpdateOffset": 9001}}, db_path=path)
            events, requests = [], []
            def message(uid):
                return {"update_id": uid, "message": {"text": "/help", "chat": {"id": uid, "type": "private"}}}
            pending = [message(400), message(401)]
            def fetch(db, method, payload, timeout):
                offset = payload.get("offset", 0)
                requests.append(offset)
                if offset == 9001:
                    # Первый запрос укорочен; между ним и перечитыванием owner ищет чаты без offset.
                    requests.append(0)
                    return {"result": pending[:1]}
                if len(requests) > 1 and requests[-2] == offset:
                    return {"result": pending[:1]}
                pending[:] = [u for u in pending if u["update_id"] >= offset]
                return {"result": pending[:]}
            ctx = replace(fixtures.TelegramPollerTests().make_context({}, [], events), curl_json=fetch,
                          load_db=partial(settings.load_db, db_path=path),
                          claim_user_updates=partial(settings.claim_user_updates, db_path=path))
            poller.poll_user_subscriptions(ctx, quiet=True)
            self.assertFalse(events)
            self.assertEqual(settings.load_db(db_path=path)["clientSubscriptionState"]["userUpdateOffset"], 402)
            pending.append(message(402))
            poller.poll_user_subscriptions(ctx, quiet=True)
            poller.poll_user_subscriptions(ctx, quiet=True)
            self.assertEqual(requests, [9001, 0, 400, 402, 403])
            self.assertEqual([e[1] for e in events if e[0] == "send"], ["402"])

    def test_offset_is_kept_through_last_second_of_queue_retention(self):
        for second, clock_step, expected_offset in ((86399, 0, 43), (86400, 0, 43), (86401, 0, None), (86399, 3, None)):
            with self.subTest(second=second, clock_step=clock_step), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "manager.db"
                database.initialize(path).close()
                settings.save_db({"enabled": True, "chatId": "99", "token": "fixture"}, db_path=path)
                start = datetime(2026, 6, 12, 12, tzinfo=timezone.utc)
                events, requests = [], []
                update = {"update_id": 42, "message": {"text": "/help", "chat": {"id": 7, "type": "private"}}}

                def fetch(db, method, payload, timeout):
                    offset = payload.get("offset")
                    requests.append(offset)
                    # Telegram удаляет сообщение, когда текущее время строго больше expires_at.
                    available = (offset or 0) <= 42 and moment <= start + timedelta(days=1)
                    return {"result": [update] if available else []}

                ctx = replace(fixtures.TelegramPollerTests().make_context({}, [], events), curl_json=fetch,
                              load_db=partial(settings.load_db, db_path=path),
                              claim_user_updates=partial(settings.claim_user_updates, db_path=path),
                              utc_stamp=lambda: local_time.strftime("%Y-%m-%dT%H:%M:%SZ"))
                for moment in (start, start + timedelta(seconds=second)):
                    local_time = moment if moment == start else moment + timedelta(seconds=clock_step)
                    poller.poll_user_subscriptions(ctx, quiet=True)
                self.assertEqual(requests, [None, expected_offset])
                self.assertEqual(len([event for event in events if event[0] == "send"]), 1)

    def test_future_cursor_timestamp_cannot_discard_new_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manager.db"
            database.initialize(path).close()
            settings.save_db({"enabled": True, "chatId": "99", "token": "fixture"}, db_path=path)
            events, requests = [], []
            pending = []
            def fetch(db, method, payload, timeout):
                offset = payload.get("offset", 0)
                requests.append(offset)
                pending[:] = [u for u in pending if u["update_id"] >= offset]
                return {"result": pending[:]}
            ctx = replace(fixtures.TelegramPollerTests().make_context({}, [], events), curl_json=fetch,
                          load_db=partial(settings.load_db, db_path=path),
                          claim_user_updates=partial(settings.claim_user_updates, db_path=path),
                          utc_stamp=lambda: stamp)
            for stamp, chat in (("2026-07-10T10:00:00Z", 7), ("2026-07-09T10:00:00Z", 8)):
                # Telegram уже удалил старую команду и повторно выдал 9000 другому чату.
                pending[:] = [{"update_id": 9000, "message": {"chat": {"id": chat, "type": "private"}, "text": "/help"}}]
                poller.poll_user_subscriptions(ctx, quiet=True)
                if chat == 7:
                    stale = settings.load_db(db_path=path)
            settings.save_db_sections(stale, ("clientSubscriptionState",), db_path=path)
            self.assertEqual(settings.load_db(db_path=path)["clientSubscriptionState"]["lastUserPoll"], stamp)
            poller.poll_user_subscriptions(ctx, quiet=True)
            self.assertEqual(requests, [0, 0, 9001])
            self.assertEqual([e[1] for e in events if e[0] == "send"], ["7", "8"])

    def test_idle_poll_does_not_confirm_new_sequence_with_previous_offset(self):
        for confirmed, last_poll in ((True, "2026-06-12T21:59:00Z"), (False, "2026-06-05T12:00:00Z")):
            with self.subTest(confirmed=confirmed), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "manager.db"
                database.initialize(path).close()
                settings.save_db({"enabled": True, "chatId": "99", "token": "fixture",
                                  "clientSubscriptionState": {"userUpdateOffset": 9001, "lastUserPoll": last_poll,
                                                              "userUpdateOffsetConfirmed": confirmed}}, db_path=path)
                events, requests = [], []
                def fetch(db, method, payload, timeout):
                    requests.append(payload.get("offset"))
                    update = {"update_id": 8999, "message": {"text": "/help", "chat": {"id": 7, "type": "private"}}}
                    return {"result": [] if payload.get("offset") else [update]}
                ctx = replace(fixtures.TelegramPollerTests().make_context({}, [], events), curl_json=fetch,
                              load_db=partial(settings.load_db, db_path=path),
                              claim_user_updates=partial(settings.claim_user_updates, db_path=path))
                poller.poll_user_subscriptions(ctx, quiet=True)
                poller.poll_user_subscriptions(ctx, quiet=True)
                self.assertEqual(requests, [None, 9000])
                self.assertEqual(len([e for e in events if e[0] == "send"]), 1)

    def test_readonly_reservation_fails_before_reply_and_next_poll_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manager.db"
            database.initialize(path).close()
            settings.save_db({"enabled": True, "chatId": "99", "token": "fixture",
                              "clientSubscriptionState": {"userUpdateOffset": 41}}, db_path=path)
            events = []
            requests = []
            update = {"update_id": 41, "message": {"text": "/traffic", "chat": {"type": "private", "id": 7}}}
            def fetch(db, method, payload, timeout):
                requests.append(payload["offset"])
                return {"result": [update] if payload["offset"] == 41 else []}

            connect = database.connect
            def readonly(*args, **kwargs):
                conn = connect(*args, **kwargs)
                conn.execute("PRAGMA query_only=ON")
                return conn

            def claim(state, updates, stamp):
                if len(requests) == 1:
                    with mock.patch.object(database, "connect", readonly):
                        return settings.claim_user_updates(state, updates, stamp, db_path=path)
                return settings.claim_user_updates(state, updates, stamp, db_path=path)

            ctx = replace(fixtures.TelegramPollerTests().make_context({}, [], events), curl_json=fetch,
                          load_db=partial(settings.load_db, db_path=path), claim_user_updates=claim,
                          save_db_sections=partial(settings.save_db_sections, db_path=path))
            with self.assertRaises(sqlite3.OperationalError) as failed:
                poller.poll_user_subscriptions(ctx, quiet=True)
            self.assertEqual(failed.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)
            self.assertFalse(events)
            self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)
            self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)
            self.assertEqual(requests, [41, 41, 42])
            self.assertEqual(len([e for e in events if e[0] == "send"]), 1)

    def test_cancel_and_days_are_serialized_between_processes(self):
        with tempfile.TemporaryDirectory() as tmp, core_paths.use(core_paths.Paths.under(Path(tmp))):
            path = core_paths.paths().manager_db
            database.initialize(path).close()
            settings.save_db({
                "enabled": True, "chatId": "99", "token": "fixture",
                "clientSubscriptionState": {"userUpdateOffset": 41},
                "adminState": {"99": {"action": "extend-subscription-days", "client": "fixture"}},
            }, db_path=path)
            events = []
            contenders = []
            requests = []
            run = mock.Mock(side_effect=AssertionError("Продление после отмены"))
            def manual():
                result = subprocess.run(
                    [sys.executable, "-c", "from tests.test_telegram_poller_busy import competing_manual_poll; "
                     "import sys; sys.exit(competing_manual_poll(sys.argv[1]))", tmp],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
                )
                contenders.append(result)

            def fetch(db, method, payload, timeout):
                offset = payload["offset"]
                requests.append(offset)
                if offset == 41:
                    manual()
                text = "/cancel" if offset == 41 else "14"
                return {"result": [{"update_id": offset, "message": {"text": text, "chat": {"type": "private", "id": 99}}}]}

            def send(db, chat, text, **kwargs):
                events.append(text)
                if "отменено" in text.lower():
                    manual()

            ctx = fixtures.TelegramPollerTests().make_context({}, [], [], client_db={"clients": {"fixture": {}}})
            save = partial(settings.save_db_sections, db_path=path)
            ctx = replace(ctx, consumer_lock=telegram_command.poller_context().consumer_lock,
                          load_db=partial(settings.load_db, db_path=path), curl_json=fetch,
                          claim_user_updates=partial(settings.claim_user_updates, db_path=path),
                          save_db_sections=save, send_chat_message=send, admin_context=replace(
                              ctx.admin_context, save_db_sections=save, send_chat_message=send, run_capture=run))
            self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)
            self.assertEqual(poller.poll_user_subscriptions(ctx, quiet=True), 0)
            self.assertEqual(len(contenders), 2)
            for result in contenders:
                self.assertEqual(result.returncode, 1)
                self.assertIn("Опрос Telegram уже выполняется", result.stdout)
                self.assertEqual(result.stderr, "")
            self.assertIn("отменено", events[0].lower())
            self.assertEqual(len(events), 2)
            run.assert_not_called()
            self.assertEqual(requests, [41, 42])
            self.assertEqual(settings.load_db(db_path=path)["clientSubscriptionState"]["userUpdateOffset"], 43)

    def test_other_operational_errors_are_not_retried(self):
        with database.wait_for_writer(), mock.patch.object(database, "time") as timer:
            with closing(sqlite3.connect(":memory:")) as connection:
                with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                    database._wait_if_busy(connection.execute, "SELECT * FROM missing")
            timer.sleep.assert_not_called()
