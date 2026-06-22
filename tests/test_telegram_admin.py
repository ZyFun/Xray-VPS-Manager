import unittest
from pathlib import Path
from types import SimpleNamespace

from xray_vps_manager.telegram import admin


class TelegramAdminTests(unittest.TestCase):
    def make_context(
        self,
        events,
        client_db=None,
        send_response=None,
        run_capture=None,
        server_name_fragment=None,
        list_tls_sites=None,
        set_tls_site_version=None,
    ):
        if client_db is None:
            client_db = {"clients": {}}
        if run_capture is None:
            run_capture = lambda *_args, **_kwargs: None
        if server_name_fragment is None:
            server_name_fragment = lambda: "Xray"
        if list_tls_sites is None:
            list_tls_sites = lambda: []
        if set_tls_site_version is None:
            set_tls_site_version = lambda *_args, **_kwargs: None

        def send_chat_message(_db, chat_id, text, reply_markup=None, parse_mode=None):
            events.append(
                {
                    "chat_id": str(chat_id),
                    "text": text,
                    "reply_markup": reply_markup,
                    "parse_mode": parse_mode,
                }
            )
            return send_response

        return admin.AdminContext(
            load_client_db=lambda: client_db,
            save_db_sections=lambda _db, sections: events.append({"save": tuple(sections)}),
            format_access_until=lambda value: value or "бессрочно",
            run_capture=run_capture,
            send_chat_message=send_chat_message,
            bot_name=lambda current_db=None: "Vireika",
            notification_context=None,
            xray_client=Path("/usr/local/sbin/xray-client"),
            server_name_fragment=server_name_fragment,
            list_tls_sites=list_tls_sites,
            set_tls_site_version=set_tls_site_version,
        )

    def test_payment_share_callback_sends_payments_submenu(self) -> None:
        db = {
            "paymentTotalAmount": "500",
            "paymentCurrency": "₽",
            "paymentRoundingMode": "none",
            "paymentRoundingStep": "10",
        }
        client_db = {
            "clients": {
                "alice": {"paymentType": "paid"},
                "bob": {"paymentType": "paid"},
                "carol": {"paymentType": "free"},
            }
        }
        events = []
        ctx = self.make_context(events, client_db=client_db)

        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:payment-share"))

        self.assertEqual(len(events), 1)
        self.assertIn("Сумма на клиента: 250 ₽", events[0]["text"])
        buttons = [button for row in events[0]["reply_markup"]["inline_keyboard"] for button in row]
        self.assertIn({"text": "Текущая сумма", "callback_data": "admin:payment-total"}, buttons)
        self.assertIn({"text": "Назад", "callback_data": "admin:menu"}, buttons)

    def test_settings_status_callback_sends_settings_submenu(self) -> None:
        db = {
            "enabled": True,
            "chatId": "111",
            "chatLabel": "owner",
            "routeMode": "cascade",
        }
        events = []
        ctx = self.make_context(events)

        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:settings-status"))

        self.assertEqual(len(events), 1)
        self.assertIn("Имя бота: Vireika", events[0]["text"])
        self.assertIn("Маршрут Telegram: cascade", events[0]["text"])
        buttons = [button for row in events[0]["reply_markup"]["inline_keyboard"] for button in row]
        self.assertIn({"text": "Статус бота", "callback_data": "admin:settings-status"}, buttons)

    def test_server_tls_menu_shows_current_profile_and_modified_time(self) -> None:
        db = {"adminState": {}}
        events = []
        sites = [
            {
                "domain": "api.example.com",
                "localPort": 10300,
                "tlsChoice": "tls12",
                "tlsLabel": "TLS 1.2",
                "modifiedAt": "2026-06-21 10:00 UTC",
            }
        ]
        ctx = self.make_context(events, list_tls_sites=lambda: sites)

        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:server-tls"))

        message = [event for event in events if "text" in event][-1]
        self.assertIn("Текущее шифрование:", message["text"])
        self.assertIn("- api.example.com: TLS 1.2", message["text"])
        self.assertIn("Изменено: 2026-06-21 10:00 UTC", message["text"])
        self.assertEqual(db["adminState"]["serverSettings"]["111"]["tlsSites"], sites)

    def test_server_tls_set_updates_site_from_saved_selection(self) -> None:
        db = {"adminState": {}}
        events = []
        calls = []
        sites = [
            {
                "domain": "api.example.com",
                "localPort": 10300,
                "tlsChoice": "tls12",
                "tlsLabel": "TLS 1.2",
                "modifiedAt": "2026-06-21 10:00 UTC",
            }
        ]

        def set_tls_site_version(domain, local_port, choice_key, site_path=None):
            calls.append((domain, local_port, choice_key, site_path))

        ctx = self.make_context(
            events,
            list_tls_sites=lambda: sites,
            set_tls_site_version=set_tls_site_version,
        )

        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:server-tls"))
        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:server-tls-site:0"))
        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:server-tls-set:0:tls13"))

        self.assertEqual(calls, [("api.example.com", 10300, "tls13", None)])
        sent = [event for event in events if "text" in event][-1]
        self.assertIn("TLS обновлён для api.example.com: TLS 1.3", sent["text"])
        self.assertIn("Caddy config проверен и применён.", sent["text"])

    def test_server_tls_set_updates_static_site_by_config_path(self) -> None:
        db = {"adminState": {}}
        events = []
        calls = []
        sites = [
            {
                "domain": "site.example.com",
                "path": "/etc/caddy/conf.d/site.example.com.caddy",
                "localPort": None,
                "tlsChoice": "default",
                "tlsLabel": "Caddy default",
                "modifiedAt": "2026-06-21 10:00 UTC",
            }
        ]

        def set_tls_site_version(domain, local_port, choice_key, site_path=None):
            calls.append((domain, local_port, choice_key, site_path))

        ctx = self.make_context(
            events,
            list_tls_sites=lambda: sites,
            set_tls_site_version=set_tls_site_version,
        )

        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:server-tls"))
        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:server-tls-site:0"))
        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:server-tls-set:0:tls13"))

        self.assertEqual(calls, [("site.example.com", 0, "tls13", "/etc/caddy/conf.d/site.example.com.caddy")])
        sent = [event for event in events if "text" in event][-1]
        self.assertIn("TLS обновлён для site.example.com: TLS 1.3", sent["text"])

    def test_admin_menu_registers_latest_callback_message(self) -> None:
        db = {"adminState": {}}
        events = []
        ctx = self.make_context(events, send_response={"ok": True, "result": {"message_id": 42}})

        admin.send_admin_menu(ctx, db, "111")

        guard = db["adminState"]["callbackGuards"]["111"]
        self.assertEqual(guard["activeMessageId"], 42)

    def test_admin_callback_guard_blocks_duplicate_message_click(self) -> None:
        db = {"adminState": {}}
        events = []
        ctx = self.make_context(events)

        self.assertTrue(admin.accept_admin_callback(ctx, db, "111", "admin:backup", 42))
        self.assertFalse(admin.accept_admin_callback(ctx, db, "111", "admin:backup", 42))

    def test_admin_callback_guard_blocks_old_message_after_new_admin_message(self) -> None:
        db = {"adminState": {}}
        events = []
        ctx = self.make_context(events)

        self.assertTrue(admin.accept_admin_callback(ctx, db, "111", "admin:backup", 42))
        admin.register_admin_message(ctx, db, "111", {"ok": True, "result": {"message_id": 43}})

        self.assertFalse(admin.accept_admin_callback(ctx, db, "111", "admin:backup", 42))
        self.assertTrue(admin.accept_admin_callback(ctx, db, "111", "admin:backup", 43))

    def test_admin_menu_callback_from_client_menu_can_open_admin_panel(self) -> None:
        db = {"adminState": {}}
        events = []
        ctx = self.make_context(events)
        admin.register_admin_message(ctx, db, "111", {"ok": True, "result": {"message_id": 43}})

        self.assertTrue(admin.accept_admin_callback(ctx, db, "111", "admin:menu", 42))

    def test_admin_client_link_selection_sends_current_link_as_html_code_block(self) -> None:
        db = {"adminState": {}}
        client_db = {
            "clients": {
                "alice": {},
                "bob": {},
            }
        }
        events = []

        def run_capture(command, timeout=20, **_kwargs):
            events.append({"run": command, "timeout": timeout})
            return SimpleNamespace(
                returncode=0,
                stdout="vless://bob@example.com:443?type=tcp&security=reality#InternalBob",
                stderr="",
            )

        ctx = self.make_context(events, client_db=client_db, run_capture=run_capture, server_name_fragment=lambda: "Demo")

        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:client-link"))
        self.assertTrue(admin.handle_callback(ctx, db, "111", "admin:client-link:1"))

        self.assertIn({"run": ["/usr/local/sbin/xray-client", "link", "bob"], "timeout": 20}, events)
        self.assertNotIn("111", db.get("adminState", {}))
        message = [event for event in events if "text" in event][-1]
        self.assertEqual(message["parse_mode"], "HTML")
        self.assertIn("Можно переслать это сообщение пользователю:", message["text"])
        self.assertIn(
            "<pre><code>vless://bob@example.com:443?type=tcp&amp;security=reality#Demo</code></pre>",
            message["text"],
        )
        self.assertNotIn("InternalBob", message["text"])
        self.assertNotIn("Клиент: bob", message["text"])

    def test_add_client_success_sends_key_as_html_code_block(self) -> None:
        db = {"botUsername": "ExampleVpnBot", "adminState": {}}
        client_db = {
            "clients": {
                "alice": {
                    "expiresAt": "2026-07-14T00:00:00+03:00",
                    "paymentType": "paid",
                }
            }
        }
        events = []

        def run_capture(command, timeout=20, **_kwargs):
            events.append({"run": command, "timeout": timeout})
            return SimpleNamespace(
                returncode=0,
                stdout="vless://alice@example.com:443?type=tcp&security=reality#Xray",
                stderr="",
            )

        ctx = self.make_context(events, client_db=client_db, run_capture=run_capture)

        admin.run_add_client_from_pending(
            ctx,
            db,
            "111",
            {"client": "alice", "accessDays": "30", "paymentType": "paid"},
            "vless-reality",
        )

        message = [event for event in events if "text" in event][-1]
        self.assertEqual(message["parse_mode"], "HTML")
        self.assertIn(
            "<pre><code>vless://alice@example.com:443?type=tcp&amp;security=reality#Xray</code></pre>",
            message["text"],
        )


if __name__ == "__main__":
    unittest.main()
