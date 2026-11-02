from pathlib import Path
import unittest

from tests.support.sandbox import Sandbox

from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import cascades as sqlite_cascades
from xray_vps_manager.db.repositories import connections as sqlite_connections


def client_db() -> dict:
    return {
        "connections": {
            "vless-reality": {
                "tag": "vless-reality",
                "name": "main",
                "created": "2026-06-12T08:00:00Z",
                "port": 443,
                "sni": "example.com",
                "dest": "example.com:443",
                "fingerprint": "chrome",
            }
        },
        "clients": {
            "alice": {
                "id": "00000000-0000-0000-0000-000000000001",
                "created": "2026-06-12T08:01:00Z",
                "enabled": True,
                "connection": "vless-reality",
                "client": {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "email": "alice|created=2026-06-12T08:01:00Z",
                },
                "paymentType": " Да ",
                "selectedCascadeTag": "cascade-de",
            }
        },
        "cascadeRoutes": {
            "cascade-de": {"tag": "cascade-de", "country": "Германия", "label": "de"},
        },
    }


class ClientRepositoryWriteTests(unittest.TestCase):
    def seed_client(self, path: Path) -> None:
        connection = database.open_database(path)
        try:
            sqlite_connections.upsert_connection(
                connection,
                "old-connection",
                {
                    "tag": "old-connection",
                    "name": "old",
                    "created": "2026-06-12T07:00:00Z",
                    "port": 8443,
                    "sni": "old.example.com",
                    "dest": "old.example.com:443",
                    "fingerprint": "safari",
                },
            )
            client_repository.save_client(
                connection,
                "old_client",
                {
                    "id": "00000000-0000-0000-0000-000000000009",
                    "created": "2026-06-12T07:01:00Z",
                    "enabled": True,
                    "connection": "old-connection",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000009",
                        "email": "old_client|created=2026-06-12T07:01:00Z",
                    },
                },
            )
        finally:
            connection.close()

    def test_save_writes_clients_and_connections_to_sqlite(self) -> None:
        """XR-REV-001: save_db сохраняет пропущенные записи и прежний порядок вставок."""
        with Sandbox(db="fixtures/db/empty-ready.sql") as box:
            db_path = box.paths.manager_db
            json_path = box.root / "clients.json"
            self.seed_client(db_path)

            connection = database.open_database(db_path)
            try:
                sqlite_cascades.upsert_route(connection, "old-route", {"label": "old"})
                # Триггеры наблюдают реальные вставки и любое неожиданное удаление.
                connection.execute("CREATE TABLE write_log(operation TEXT)")
                for table in ("reality_connections", "cascade_routes", "clients"):
                    for action in ("INSERT", "DELETE"):
                        connection.execute(f"""
                            CREATE TRIGGER observe_{action}_{table} BEFORE {action} ON {table}
                            BEGIN INSERT INTO write_log VALUES ('{action} {table}'); END
                        """)
            finally:
                connection.close()

            client_repository.save_db(client_db(), json_path, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                clients = sqlite_clients.list_clients(connection)
                connections = sqlite_connections.list_connections(connection)
                routes = sqlite_cascades.list_routes(connection)
                operations = connection.execute("SELECT operation FROM write_log ORDER BY rowid")
                self.assertEqual([row[0] for row in operations], [
                    "INSERT reality_connections", "INSERT cascade_routes", "INSERT clients",
                ])
                credentials = connection.execute(
                    "SELECT client_name, connection_tag, credential_uuid, protocol FROM client_credentials"
                )
                self.assertCountEqual([tuple(row) for row in credentials], [
                    ("old_client", "old-connection", "00000000-0000-0000-0000-000000000009", "vless"),
                    ("alice", "vless-reality", "00000000-0000-0000-0000-000000000001", "vless"),
                ])
            finally:
                connection.close()
            self.assertEqual(set(clients), {"old_client", "alice"})
            self.assertEqual(set(connections), {"old-connection", "vless-reality"})
            self.assertEqual(clients["alice"]["paymentType"], "paid")
            self.assertEqual(clients["alice"]["selectedCascadeTag"], "cascade-de")
            self.assertEqual(connections["vless-reality"]["fingerprint"], "chrome")
            self.assertEqual(set(routes), {"old-route", "cascade-de"})
            self.assertEqual(routes["cascade-de"]["country"], "Германия")
            self.assertFalse(json_path.exists())

    def test_save_fails_when_sqlite_database_is_missing(self) -> None:
        with Sandbox(db="fixtures/db/empty-ready.sql") as box:
            db_path = box.root / "missing.db"
            json_path = box.root / "clients.json"

            with self.assertRaisesRegex(RuntimeError, "manager database is missing"):
                client_repository.save_db(client_db(), json_path, db_path=db_path)

            self.assertFalse(json_path.exists())
            self.assertFalse(db_path.exists())

    def test_save_fails_when_sqlite_database_is_not_marked_ready(self) -> None:
        with Sandbox(db=None) as box:
            db_path = box.paths.manager_db
            json_path = box.root / "clients.json"
            self.seed_client(db_path)

            with self.assertRaisesRegex(RuntimeError, "database is not marked ready"):
                client_repository.save_db(client_db(), json_path, db_path=db_path)

            connection = database.open_database(db_path)
            try:
                clients = sqlite_clients.list_clients(connection)
                connections = sqlite_connections.list_connections(connection)
            finally:
                connection.close()
            self.assertEqual(set(clients), {"old_client"})
            self.assertEqual(set(connections), {"old-connection"})
            self.assertFalse(json_path.exists())


if __name__ == "__main__":
    unittest.main()
