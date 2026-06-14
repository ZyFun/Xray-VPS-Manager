from pathlib import Path
import tempfile
import unittest

from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import cascades as sqlite_cascades
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.storage import SQLiteReadUnavailable


class ClientRepositoryReadTests(unittest.TestCase):
    def make_sqlite_db(self, path: Path, *, ready: bool = True) -> None:
        connection = database.open_database(path)
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
            if ready:
                sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()

    def test_read_uses_sqlite_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path)

            result = client_repository.load_db_sql_result(db_path=db_path)

            self.assertEqual(result.source, "sqlite")
            self.assertIn("sqlite_client", result.db["clients"])
            self.assertEqual(result.db["connections"]["sqlite-connection"]["fingerprint"], "safari")
            self.assertEqual(result.db["clients"]["sqlite_client"]["paymentType"], "free")
            self.assertEqual(result.db["clients"]["sqlite_client"]["selectedCascadeTag"], "cascade-us")
            self.assertEqual(result.db["cascadeRoutes"]["cascade-us"]["country"], "США")

    def test_read_fails_when_sqlite_database_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_db_path = Path(tmp_dir) / "missing.db"

            with self.assertRaisesRegex(SQLiteReadUnavailable, "manager database is missing"):
                client_repository.load_db_sql_result(db_path=missing_db_path)

            self.assertFalse(missing_db_path.exists())

    def test_read_fails_when_sqlite_database_is_not_marked_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "manager.db"
            self.make_sqlite_db(db_path, ready=False)

            with self.assertRaisesRegex(SQLiteReadUnavailable, "database is not marked ready"):
                client_repository.load_db_sql_result(db_path=db_path)


if __name__ == "__main__":
    unittest.main()
