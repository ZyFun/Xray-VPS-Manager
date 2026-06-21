import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from xray_vps_manager.commands import menu_caddy_actions, menu_reality_actions


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

    def test_choose_xhttp_mode_selects_mode_by_number(self) -> None:
        inputs = iter(["3"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            mode = menu_reality_actions.choose_xhttp_mode("auto")

        self.assertEqual(mode, "stream-up")

    def test_choose_transport_uses_xhttp_mode_list(self) -> None:
        inputs = iter(["3", "/private-xhttp", "4"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            settings = menu_reality_actions.choose_transport("tcp")

        self.assertEqual(
            settings,
            {
                "transport": "xhttp",
                "xhttp_path": "/private-xhttp",
                "xhttp_mode": "stream-one",
            },
        )

    def test_choose_tls_versions_selects_profile_by_number(self) -> None:
        inputs = iter(["3"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            tls_min, tls_max = menu_reality_actions.choose_tls_versions("tls1.2", "tls1.2")

        self.assertEqual((tls_min, tls_max), ("tls1.2", "tls1.3"))

    def test_caddy_tls_prompt_uses_same_profile_list(self) -> None:
        inputs = iter(["4"])

        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), redirect_stdout(StringIO()):
            tls_min, tls_max = menu_caddy_actions.prompt_tls_versions("tls1.2", "tls1.2")

        self.assertEqual((tls_min, tls_max), ("tls1.3", "tls1.3"))


if __name__ == "__main__":
    unittest.main()
