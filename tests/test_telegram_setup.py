import unittest
from unittest import mock

from xray_vps_manager.telegram import setup


class TelegramSetupTests(unittest.TestCase):
    def test_configure_owner_saves_bot_username_from_get_me(self) -> None:
        db = {"token": "token", "routeMode": "direct"}
        saved = []

        with mock.patch.object(setup.settings, "load_db_sql", return_value=db):
            with mock.patch.object(setup, "maybe_adopt_existing_cascade_route", side_effect=lambda current_db: current_db):
                with mock.patch.object(setup.api, "curl_json", return_value={"ok": True, "result": {"username": "ExampleVpnBot"}}):
                    with mock.patch.object(setup, "choose_private_chat", return_value={"id": "111", "label": "owner"}):
                        with mock.patch.object(setup, "initialize_geoip_offsets"):
                            with mock.patch.object(setup, "refresh_user_update_offset"):
                                with mock.patch.object(setup.settings, "save_db", side_effect=lambda current_db: saved.append(dict(current_db))):
                                    with mock.patch.object(setup, "configure_bot_commands"):
                                        with mock.patch.object(setup.api, "send_message"):
                                            with mock.patch("builtins.print"):
                                                setup.configure_owner()

        self.assertEqual(saved[-1]["botUsername"], "ExampleVpnBot")
        self.assertEqual(saved[-1]["chatId"], "111")

    def test_configure_bot_commands_sets_default_menu_for_unsubscribed_users(self) -> None:
        db = {"token": "token"}
        calls = []

        def fake_curl_json(current_db, method, payload=None, timeout=30):
            calls.append((current_db, method, payload, timeout))
            return {"ok": True}

        with mock.patch.object(setup.settings, "load_db_sql", return_value=db):
            with mock.patch.object(setup.api, "curl_json", side_effect=fake_curl_json):
                with mock.patch("builtins.print"):
                    setup.configure_bot_commands()

        self.assertEqual(calls[0][1], "setMyCommands")
        self.assertEqual(
            calls[0][2]["commands"],
            [
                {"command": "start", "description": "Открыть меню"},
                {"command": "help", "description": "Помощь"},
            ],
        )
        self.assertNotIn("scope", calls[0][2])

    def test_configure_bot_commands_sets_subscribed_chat_menu(self) -> None:
        db = {
            "token": "token",
            "clientSubscriptions": {
                "222": {"client": "alice"},
            },
        }
        calls = []

        def fake_curl_json(current_db, method, payload=None, timeout=30):
            calls.append((current_db, method, payload, timeout))
            return {"ok": True}

        with mock.patch.object(setup.settings, "load_db_sql", return_value=db):
            with mock.patch.object(setup.api, "curl_json", side_effect=fake_curl_json):
                with mock.patch("builtins.print"):
                    setup.configure_bot_commands()

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1], "setMyCommands")
        self.assertEqual(calls[1][2]["scope"], {"type": "chat", "chat_id": "222"})
        commands = calls[1][2]["commands"]
        self.assertIn({"command": "status", "description": "Показать подписку"}, commands)
        self.assertIn({"command": "link", "description": "Получить VPN-ссылку"}, commands)
        self.assertIn({"command": "traffic", "description": "Показать статистику трафика"}, commands)
        self.assertIn({"command": "unsubscribe", "description": "Отписаться от бота"}, commands)


if __name__ == "__main__":
    unittest.main()
