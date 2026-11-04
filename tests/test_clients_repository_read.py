from pathlib import Path
import unittest

from tests.support.sandbox import Sandbox

from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.core.errors import StateError
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import cascades as sqlite_cascades
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.storage import SQLiteReadUnavailable


class ClientRepositoryReadTests(unittest.TestCase):
    def seed_client(self, path: Path) -> None:
        connection = database.initialize(path)
        try:
            sqlite_connections.upsert_connection(
                connection,
                "sqlite-connection",
                {
                    "tag": "sqlite-connection",
                    "name": "sqlite",
                    "created": "2026-06-12T09:00:00Z",
                    "port": 8443,
                    "sni": "sqlite.example.com",
                    "dest": "sqlite.example.com:443",
                    "fingerprint": "safari",
                },
            )
            sqlite_clients.upsert_client(
                connection,
                "sqlite_client",
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "created": "2026-06-12T09:01:00Z",
                    "enabled": True,
                    "connection": "sqlite-connection",
                    "client": {
                        "id": "00000000-0000-0000-0000-000000000002",
                        "email": "sqlite_client|created=2026-06-12T09:01:00Z",
                    },
                    "selectedCascadeTag": "cascade-us",
                },
            )
            sqlite_cascades.upsert_route(
                connection,
                "cascade-us",
                {"tag": "cascade-us", "country": "США", "label": "us"},
            )
        finally:
            connection.close()

    def test_read_uses_sqlite_database(self) -> None:
        with Sandbox(db=None) as box:
            db_path = box.paths.manager_db
            self.seed_client(db_path)

            result = client_repository.load_db_sql(db_path=db_path)

            self.assertIn("sqlite_client", result["clients"])
            self.assertEqual(result["connections"]["sqlite-connection"]["fingerprint"], "safari")
            self.assertEqual(result["clients"]["sqlite_client"]["paymentType"], "free")
            self.assertEqual(set(result["clients"]["sqlite_client"]["credentials"]), {"sqlite-connection"})
            self.assertEqual(result["clients"]["sqlite_client"]["selectedCascadeTag"], "cascade-us")
            self.assertEqual(result["cascadeRoutes"]["cascade-us"]["country"], "США")

    def test_read_fails_when_sqlite_database_is_missing(self) -> None:
        self.assertIs(SQLiteReadUnavailable, StateError)
        with Sandbox(db=None) as box:
            missing_db_path = box.root / "missing.db"

            with self.assertRaisesRegex(SQLiteReadUnavailable, "SQLite manager database is missing"):
                client_repository.load_db_sql(db_path=missing_db_path)

            self.assertFalse(missing_db_path.exists())

    def test_facade_accepts_initialized_database_without_ready_flag(self) -> None:
        """B2.1 (XR-REV-021): явной инициализации достаточно для чтения, ключ старого установщика не нужен."""
        with Sandbox(db=None) as box:
            self.seed_client(box.paths.manager_db)
            self.assertIn("sqlite_client", client_repository.load_db_sql()["clients"])


if __name__ == "__main__":
    unittest.main()
