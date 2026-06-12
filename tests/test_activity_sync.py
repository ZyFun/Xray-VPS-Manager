import unittest
from unittest import mock

from xray_vps_manager.activity import sync as activity_sync


class ActivitySyncTests(unittest.TestCase):
    def test_known_clients_uses_client_read_switch(self) -> None:
        client_db = {
            "clients": {
                "sqlite_client": {
                    "connection": "sqlite-connection",
                    "client": {"email": "sqlite_client|created=2026-06-12T07:01:00Z"},
                }
            }
        }

        with mock.patch.object(activity_sync.repository, "load_json", return_value={}), \
            mock.patch.object(activity_sync.client_repository, "load_db_for_read", return_value=client_db) as load_db:
            clients = activity_sync.known_clients()

        load_db.assert_called_once_with(activity_sync.CLIENT_DB_PATH)
        self.assertEqual(
            clients,
            {
                "sqlite_client": {
                    "email": "sqlite_client|created=2026-06-12T07:01:00Z",
                    "connection": "sqlite-connection",
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
