"""Контракты точечной записи в существующую базу схемы 8."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import mock

import sqlite3
import unittest

from tests.support.sandbox import Sandbox
from xray_vps_manager.activity import exception_reports, exceptions
from xray_vps_manager.core.errors import ConflictError
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import telegram as sqlite_telegram
from xray_vps_manager.telegram import settings, subscriptions
from xray_vps_manager.traffic import repository as traffic


class PointWriteTests(unittest.TestCase):
    def test_poller_saves_only_changed_chats_and_does_not_replay_previous_save(self):
        with Sandbox():
            with database.write_session() as conn:
                for chat in ("1", "2"):
                    settings.save_subscription(conn, chat, {"clientId": chat, "subscribedAt": "initial"})
            poller = settings.load_db()
            with database.write_session() as conn:
                settings.save_subscription(conn, "2", {"clientId": "2", "chatLabel": "new", "subscribedAt": "initial"})
                settings.save_subscription(conn, "3", {"clientId": "3"})
                untouched = sqlite_telegram.subscriptions_for_chat(conn, "2")
            del poller["clientSubscriptions"]["1"]
            settings.save_db_sections(poller, ("clientSubscriptions", "clientSubscriptionState"))
            with database.write_session() as conn:
                self.assertIsNone(settings.get_subscription(conn, "1"))
                settings.save_subscription(conn, "1", {"clientId": "new"})
            settings.save_db_sections(poller, ("clientSubscriptions",))
            with database.session() as conn:
                self.assertEqual(set(settings.list_subscriptions(conn)), {"1", "2", "3"})
                self.assertEqual(sqlite_telegram.subscriptions_for_chat(conn, "2"), untouched)
                self.assertEqual(settings.get_subscription(conn, "1")["clientId"], "new")
                self.assertNotIn("clientSubscriptions", settings.load_settings(conn))

    def test_stale_callback_or_unsubscribe_cannot_replace_a_new_binding(self):
        with Sandbox():
            settings.save_db({"clientSubscriptions": {"42": {"clientId": "first"}}})
            stale = settings.load_db()
            with database.write_session() as conn:
                settings.save_subscription(conn, "42", {"clientId": "next"})
            subscriptions.set_activity_notifications(stale, "42", True)
            for action in ("toggle", "unsubscribe"):
                with self.subTest(action):
                    if action == "unsubscribe":
                        subscriptions.unsubscribe_chat(stale, "42")
                    with self.assertRaises(ConflictError):
                        settings.save_db_sections(stale, ("clientSubscriptions",))
                    entry = settings.load_db()["clientSubscriptions"]["42"]
                    self.assertEqual(entry["clientId"], "next")
                    self.assertFalse(entry["activityNotificationsEnabled"])

    def test_legacy_chat_ids_share_lookup_update_and_delete_with_list(self):
        with Sandbox():
            with database.write_session() as conn:
                for raw, uuid in ((" 42 ", "old"), ("42\t", "next"), ("142", "other")):
                    sqlite_telegram.upsert_subscription(conn, {"chatId": raw, "clientUuid": uuid})
                self.assertEqual(settings.get_subscription(conn, "42"), settings.list_subscriptions(conn)["42"])
            snapshot = settings.load_db()
            subscriptions.set_activity_notifications(snapshot, "42", True, "now")
            settings.save_db_sections(snapshot, ("clientSubscriptions",))
            with database.session() as conn:
                self.assertEqual([r["chatId"] for r in sqlite_telegram.list_subscriptions(conn)], ["142", "42"])
            subscriptions.unsubscribe_chat(snapshot, "42")
            settings.save_db_sections(snapshot, ("clientSubscriptions",))
            self.assertEqual(set(settings.load_db()["clientSubscriptions"]), {"142"})

    def test_shallow_copy_keeps_baseline_after_a_section_commit(self):
        with Sandbox():
            snapshot = settings.load_db()
            # Поля poller и поля SQLite различаются, в том числе после первой успешной записи.
            snapshot["clientSubscriptions"]["42"] = {"clientId": "key", "clientKey": "local", "subscribedAt": "start"}
            settings.save_db_sections(snapshot, ("clientSubscriptions",))
            updated = snapshot.copy()
            subscriptions.unsubscribe_chat(updated, "42")
            settings.save_db_sections(updated, ("clientSubscriptions",))
            self.assertEqual(settings.load_db()["clientSubscriptions"], {})

    def test_resubscribe_changes_uuid_then_created_at(self):
        with Sandbox():
            with database.write_session() as conn:
                for uuid in ("first", "next"):
                    settings.save_subscription(conn, "7", {"clientId": uuid, "createdAt": "start"})
                settings.save_subscription(conn, "7", {"clientId": "next", "subscribedAt": "again"})
                rows = sqlite_telegram.subscriptions_for_chat(conn, "7")
                self.assertEqual([(r["clientUuid"], r["createdAt"]) for r in rows], [("next", "again")])

    def test_failed_subscription_write_rolls_back_state_and_can_be_retried(self):
        with Sandbox():
            db = settings.load_db()
            db["clientSubscriptions"]["1"] = {"clientId": "abc"}
            db["clientSubscriptionState"]["userUpdateOffset"] = 10
            with database.write_session() as conn:
                conn.execute("""CREATE TRIGGER refuse BEFORE INSERT ON telegram_subscriptions
                                BEGIN SELECT RAISE(ABORT, 'refuse'); END""")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "refuse"):
                settings.save_db_sections(db, ("clientSubscriptionState", "clientSubscriptions"))
            with database.write_session() as conn:
                self.assertEqual(sqlite_telegram.get_state(conn, "clientSubscriptionState"), {})
                conn.execute("DROP TRIGGER refuse")
            settings.save_db_sections(db, ("clientSubscriptionState", "clientSubscriptions"))
            with database.session() as conn:
                self.assertEqual(settings.get_subscription(conn, "1")["clientId"], "abc")
                self.assertEqual(sqlite_telegram.get_state(conn, "clientSubscriptionState")["userUpdateOffset"], 10)

    def test_exception_unique_conflict_and_snapshot_rollback(self):
        """B2.4: повтор успешен, но отказ UNIQUE из SQLite становится ConflictError."""
        with Sandbox() as box:
            with database.write_session() as conn:
                conn.execute("CREATE UNIQUE INDEX test_unique_source ON activity_exceptions(source)")
            exceptions.add_exception("first.example")
            exceptions.add_exception("keep.example", "imported")
            initial = exceptions.exception_items()
            repeat = box.run_cli("activity", "exception-add", "FIRST.example", "changed")
            self.assertEqual((repeat.exit_code, repeat.stdout, repeat.stderr),
                             (0, "Exception already exists: first.example\n", ""))
            with self.assertRaises(ConflictError) as caught:
                exceptions.add_exception("other.example")
            self.assertEqual(caught.exception.__cause__.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_UNIQUE)
            result = box.run_cli("activity", "exception-add", "other.example")
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.stderr, "ERROR: Activity exception conflicts with an existing entry: other.example\n")
            self.assertEqual(result.stdout, "")
            snapshot = {"items": [initial[0], {"value": "other.example", "source": "manual"}]}
            with self.assertRaises(ConflictError):
                exceptions.save_activity_exceptions(snapshot)
            self.assertFalse(exceptions.write_activity_exceptions_to_sqlite_for_write(snapshot))
            self.assertEqual(exceptions.exception_items(), initial)

    def test_exception_unrelated_constraint_is_not_a_conflict(self):
        with Sandbox():
            with database.write_session() as conn:
                conn.execute("""CREATE TRIGGER refuse BEFORE INSERT ON activity_exceptions
                                BEGIN SELECT RAISE(ABORT, 'unrelated'); END""")
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                exceptions.add_exception("example.com")
            self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_TRIGGER)

    def test_exception_replacement_only_writes_changed_values(self):
        fixed = {"value": "EXAMPLE.com", "createdAt": "first", "source": "manual"}
        with Sandbox():
            exceptions.save_activity_exceptions({"items": [fixed, "old.example"]})
            with database.write_session() as conn:
                for op in ("UPDATE", "DELETE"):
                    conn.execute(f"""CREATE TRIGGER protect_{op} BEFORE {op} ON activity_exceptions
                                     WHEN OLD.value = 'example.com' BEGIN SELECT RAISE(ABORT, 'unchanged'); END""")
            exceptions.save_activity_exceptions({"items": [fixed, "*.new.example", fixed]})
            items = exceptions.exception_items()
            self.assertEqual([item["value"] for item in items], ["*.new.example", "example.com"])
            self.assertEqual(items[1]["createdAt"], "first")

    def test_exception_commands_keep_the_other_writers_committed_row(self):
        for operation in ("add", "duplicate", "delete"):
            with self.subTest(operation), Sandbox() as box, ThreadPoolExecutor(max_workers=1) as workers:
                exception_reports.add_exception("old.example")
                waiting = Event()
                connect = database.connect

                def traced_connect(*args, **kwargs):
                    connection = connect(*args, **kwargs)
                    connection.set_trace_callback(lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None)
                    return connection

                with database.write_session(box.paths.manager_db) as first:
                    sqlite_activity.upsert_exception(first, {
                        "value": "other.example", "kind": "domain", "source": "first", "createdAt": "original",
                    })
                    # Вторая команда уже дошла до BEGIN IMMEDIATE, но ещё не видит незавершённой вставки.
                    with mock.patch.object(database, "connect", traced_connect):
                        if operation == "delete":
                            result = workers.submit(exception_reports.delete_exception, "OLD.example")
                        else:
                            value = "OTHER.example" if operation == "duplicate" else "NEW.example"
                            result = workers.submit(exception_reports.add_exception, value, "second")
                        self.assertTrue(waiting.wait(timeout=5), "Вторая команда не запросила запись")
                outcome = result.result(timeout=5)
                stored = {item["value"]: item for item in exceptions.exception_items()}
                self.assertEqual(stored["other.example"]["source"], "first")
                self.assertEqual(stored["other.example"]["createdAt"], "original")
                if operation == "delete":
                    self.assertEqual(outcome, "old.example")
                    self.assertEqual(set(stored), {"other.example"})
                elif operation == "duplicate":
                    self.assertFalse(outcome["added"])
                    self.assertEqual(set(stored), {"other.example", "old.example"})
                else:
                    self.assertTrue(outcome["added"])
                    self.assertEqual(set(stored), {"other.example", "old.example", "new.example"})
                self.assertEqual(exception_reports.delete_all_exceptions(), len(stored))
                self.assertEqual(exception_reports.delete_all_exceptions(), 0)
                with self.assertRaises(KeyError):
                    exception_reports.delete_exception("old.example")

    def test_partial_credential_save_leaves_client_totals_and_other_hours_intact(self):
        date = "2026-10-06"
        tag = "vless-reality"
        bucket = {"incoming": 3, "outgoing": 7}
        initial = {"incoming": 9, "outgoing": 21, "history": {date: dict.fromkeys(("01", "02", "03"), bucket)}}
        with Sandbox():
            traffic.save_traffic_db({"clients": {"alice": initial}, "credentials": {"alice": {tag: initial}}})
            with database.write_session() as conn:
                for op in ("DELETE", "UPDATE"):
                    conn.execute(f"""CREATE TRIGGER protect_{op} BEFORE {op} ON credential_traffic_history
                                     WHEN OLD.bucket_hour = 1 BEGIN SELECT RAISE(ABORT, 'unchanged'); END""")
            updated = {"incoming": 10, "history": {date: {"01": bucket, "02": {"incoming": 6}, "04": bucket}}}
            traffic.save_traffic_db({"credentials": {"alice": {tag: updated}}})
            traffic.save_traffic_db({"credentials": {"alice": {tag: {"history": {}}}}})
            saved = traffic.load_traffic_db()
            self.assertEqual(saved["clients"]["alice"]["history"], initial["history"])
            self.assertEqual(saved["clients"]["alice"]["incoming"], 9)
            self.assertEqual(saved["credentials"]["alice"][tag]["history"][date], {
                "01": bucket, "02": {"incoming": 6, "outgoing": 0}, "04": bucket,
            })
