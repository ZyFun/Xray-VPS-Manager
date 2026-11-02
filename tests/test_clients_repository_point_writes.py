"""R2.2: точечные операции, явное удаление и запоздалая запись снимка."""

import multiprocessing
import unittest

from tests.support.sandbox import Sandbox
from xray_vps_manager.clients import repository as repo
from xray_vps_manager.core.errors import ConflictError
from xray_vps_manager.db import database


def replace_client_in_worker(channel):
    with channel:
        path = channel.recv()
        snapshot = repo.load_db(db_path=path)
        previous = snapshot["clients"].pop("alice")
        previous["id"] = previous["client"]["id"] = "00000000-0000-4000-8000-000000000002"
        previous["client"]["email"] = "bob"
        previous.pop("credentials")
        snapshot["clients"]["bob"] = previous
        repo.save_db(snapshot, db_path=path, deletions=repo.Deletions(clients=("alice",)))


class ClientPointWriteTests(unittest.TestCase):
    def test_delayed_snapshot_keeps_other_process_changes_and_commits_its_own_route(self):
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        worker = context.Process(target=replace_client_in_worker, args=(receiver,))
        worker.start()
        receiver.close()
        try:
            with Sandbox() as box:
                stale = repo.load_db().copy()
                sender.send(str(box.paths.manager_db))
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
                stale["cascadeRoutes"]["cascade-test"] = {"country": "test"}
                repo.save_db(stale)
                current = repo.load_db()
                self.assertEqual(set(current["clients"]), {"bob"})
                self.assertEqual(current["cascadeRoutes"]["cascade-test"]["country"], "test")
                self.assertEqual(set(current["clients"]["bob"]["credentials"]), {"vless-reality"})
        finally:
            sender.close()
            if worker.is_alive():
                worker.terminate()
            worker.join()

    def test_conflict_rolls_back_earlier_writes_in_batch(self):
        with Sandbox():
            stale = repo.load_db()
            with database.write_session() as conn:
                repo.save_connection(conn, "second", {"port": 9443})
                repo.save_credential(conn, "alice", "second", {"id": "another-key"})
            stale["clients"]["alice"]["paymentType"] = "paid"
            stale["cascadeRoutes"]["uncommitted"] = {"country": "test"}
            with self.assertRaises(ConflictError):
                repo.save_db(stale)
            with database.session() as conn:
                self.assertIsNone(repo.get_route(conn, "uncommitted"))
                self.assertEqual(repo.get_client(conn, "alice")["paymentType"], "free")
                self.assertEqual(repo.get_credential(conn, "alice", "second")["id"], "another-key")

    def test_stale_snapshot_does_not_restore_deleted_connection_route_or_credential(self):
        with Sandbox():
            with database.write_session() as conn:
                repo.save_route(conn, "old", {"country": "old"})
                repo.save_connection(conn, "second", {"port": 9443})
                repo.save_credential(conn, "alice", "second", {"id": "secondary"})
            stale = repo.load_db()
            with database.write_session() as conn:
                self.assertTrue(repo.delete_credential(conn, "alice", "second"))
                self.assertTrue(repo.delete_connection(conn, "second"))
                self.assertTrue(repo.delete_route(conn, "old"))
            repo.save_db(stale)
            with database.session() as conn:
                self.assertIsNone(repo.get_route(conn, "old"))
                self.assertIsNone(repo.get_connection(conn, "second"))
                self.assertEqual(set(repo.list_credentials(conn, "alice")), {"vless-reality"})

    def test_read_normalizes_legacy_fields_without_writing(self):
        with Sandbox():
            with database.write_session() as conn:
                conn.execute("DELETE FROM client_credentials")
                # Имитация старой записи до нормализации paymentType и материализации ключа.
                conn.execute("PRAGMA ignore_check_constraints = ON")
                conn.execute("UPDATE clients SET payment_type = ' Да ' WHERE name = 'alice'")
                conn.execute("PRAGMA ignore_check_constraints = OFF")
                before = conn.total_changes
                self.assertEqual(repo.get_client(conn, "alice")["paymentType"], "paid")
                self.assertEqual(repo.get_credential(conn, "alice", "vless-reality")["protocol"], "vless")
                self.assertEqual(conn.total_changes, before)
                repo.save_connection(conn, "second", {"port": 9443})
                repo.save_credential(conn, "alice", "second", {"id": "secondary"})
                self.assertEqual(set(repo.list_credentials(conn, "alice")), {"vless-reality", "second"})
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM client_credentials").fetchone()[0], 2)
                repo.delete_credential(conn, "alice", "vless-reality")
                self.assertEqual(repo.get_client(conn, "alice")["connection"], "second")
                with self.assertRaises(ConflictError):
                    repo.delete_credential(conn, "alice", "second")

    def test_reuse_snapshot_after_save_and_compensate_update(self):
        with Sandbox():
            original = repo.load_db()
            db = repo.load_db()
            db["connections"]["vless-reality"]["name"] = "first"
            db["cascadeRoutes"]["temporary"] = {"label": "new"}
            repo.save_db(db)
            db["connections"]["vless-reality"]["name"] = "second"
            repo.save_db(db)
            repo.restore_after_update(db, original)
            self.assertEqual(repo.load_db(), original)
