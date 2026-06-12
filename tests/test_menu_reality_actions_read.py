import unittest
from unittest import mock

from xray_vps_manager.commands import menu_reality_actions


class MenuRealityActionsReadTests(unittest.TestCase):
    def test_update_connection_db_uses_runtime_read_layer(self) -> None:
        db = {"connections": {}}
        with mock.patch.object(menu_reality_actions, "load_db_sql", return_value=db) as load_db_sql, \
            mock.patch.object(menu_reality_actions, "save_db") as save_db:
            menu_reality_actions.update_connection_db(
                "vless-reality",
                port=443,
                sni="example.com",
                dest="example.com:443",
                fingerprint="chrome",
            )

        load_db_sql.assert_called_once_with()
        save_db.assert_called_once_with(db)
        self.assertEqual(
            db["connections"]["vless-reality"],
            {
                "tag": "vless-reality",
                "name": "default",
                "port": 443,
                "sni": "example.com",
                "dest": "example.com:443",
                "fingerprint": "chrome",
            },
        )


if __name__ == "__main__":
    unittest.main()
