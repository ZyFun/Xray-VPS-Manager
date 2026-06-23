import unittest
from urllib.parse import parse_qs, unquote, urlsplit
from unittest import mock

from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.clients import listing as client_listing
from xray_vps_manager.clients import links as client_links
from xray_vps_manager.xray import config as xray_config


CLIENT_ID = "00000000-0000-0000-0000-000000000001"
SECOND_CLIENT_ID = "00000000-0000-0000-0000-000000000002"
PASSWORD = "trojan-secret"


def base_vless_inbound() -> dict:
    return client_connections.make_reality_inbound(
        "vless-reality",
        443,
        "example.com",
        "private-key",
        "abcd",
    )


class TrojanConnectionTests(unittest.TestCase):
    def test_add_trojan_tls_connection_creates_managed_inbound(self) -> None:
        config = {"inbounds": [base_vless_inbound()], "outbounds": []}
        db = {"connections": {}, "clients": {}}

        result = client_connections.add_trojan_tls_connection(
            config,
            db,
            "trojan-main",
            8443,
            "vpn.example.com",
            "/etc/ssl/vpn/fullchain.pem",
            "/etc/ssl/vpn/privkey.pem",
            "chrome",
        )

        self.assertEqual(result.tag, "trojan-tls")
        inbound = config["inbounds"][1]
        self.assertEqual(inbound["protocol"], "trojan")
        self.assertEqual(inbound["settings"], {"clients": []})
        self.assertEqual(inbound["streamSettings"]["security"], "tls")
        self.assertEqual(
            inbound["streamSettings"]["tlsSettings"]["certificates"],
            [
                {
                    "certificateFile": "/etc/ssl/vpn/fullchain.pem",
                    "keyFile": "/etc/ssl/vpn/privkey.pem",
                }
            ],
        )
        self.assertEqual(db["connections"]["trojan-tls"]["protocol"], "trojan")
        self.assertEqual(db["connections"]["trojan-tls"]["sni"], "vpn.example.com")

    def test_add_client_to_trojan_connection_stores_internal_uuid_and_generates_link(self) -> None:
        config = {"inbounds": [base_vless_inbound()], "outbounds": []}
        db = {"connections": {}, "clients": {}}
        connection = client_connections.add_trojan_tls_connection(
            config,
            db,
            "trojan-main",
            8443,
            "vpn.example.com",
            "/etc/ssl/vpn/fullchain.pem",
            "/etc/ssl/vpn/privkey.pem",
            "chrome",
        )

        result = client_crud.add_client(
            config,
            db,
            "alice",
            access_days=None,
            connection_tag=connection.tag,
            uuid_factory=lambda: CLIENT_ID,
            password_factory=lambda: PASSWORD,
        )

        self.assertEqual(result.client_id, CLIENT_ID)
        self.assertEqual(db["clients"]["alice"]["id"], CLIENT_ID)
        self.assertEqual(db["clients"]["alice"]["protocol"], "trojan")
        self.assertEqual(db["clients"]["alice"]["client"]["password"], PASSWORD)
        clients = config["inbounds"][1]["settings"]["clients"]
        self.assertEqual(clients, [{"password": PASSWORD, "level": 0, "email": result.entry["client"]["email"]}])
        self.assertNotIn("id", clients[0])

        with mock.patch.object(client_links, "server_name", return_value="Xray"):
            link = client_links.link_for(config, CLIENT_ID, "alice", connection.tag, db)

        parsed = urlsplit(link)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "trojan")
        self.assertEqual(unquote(parsed.username or ""), PASSWORD)
        self.assertEqual(parsed.hostname, "vpn.example.com")
        self.assertEqual(parsed.port, 8443)
        self.assertEqual(params["security"], ["tls"])
        self.assertEqual(params["type"], ["tcp"])
        self.assertEqual(params["sni"], ["vpn.example.com"])
        self.assertEqual(params["fp"], ["chrome"])

    def test_existing_vless_client_can_receive_trojan_credential(self) -> None:
        config = {"inbounds": [base_vless_inbound()], "outbounds": []}
        db = {"connections": {}, "clients": {}}
        trojan = client_connections.add_trojan_caddy_connection(
            config,
            db,
            "trojan-web",
            "vpn.example.com",
            local_port=10100,
            public_port=443,
            fingerprint_value="chrome",
            ws_path="/trojan-private",
        )

        first = client_crud.add_client(
            config,
            db,
            "alice",
            access_days=None,
            connection_tag="vless-reality",
            uuid_factory=lambda: CLIENT_ID,
        )
        second = client_crud.add_client(
            config,
            db,
            "alice",
            access_days=None,
            connection_tag=trojan.tag,
            uuid_factory=lambda: SECOND_CLIENT_ID,
            password_factory=lambda: PASSWORD,
        )

        self.assertTrue(first.added_client)
        self.assertFalse(second.added_client)
        self.assertEqual(db["clients"]["alice"]["id"], CLIENT_ID)
        self.assertEqual(set(db["clients"]["alice"]["credentials"]), {"vless-reality", trojan.tag})
        self.assertEqual(db["clients"]["alice"]["credentials"][trojan.tag]["client"]["password"], PASSWORD)
        self.assertEqual(config["inbounds"][0]["settings"]["clients"][0]["id"], CLIENT_ID)
        self.assertEqual(config["inbounds"][1]["settings"]["clients"][0]["password"], PASSWORD)

        client_crud.disable_client(config, db, "alice")

        self.assertEqual(config["inbounds"][0]["settings"]["clients"], [])
        self.assertEqual(config["inbounds"][1]["settings"]["clients"], [])
        self.assertFalse(db["clients"]["alice"]["enabled"])

        client_crud.enable_client(config, db, {"clients": {}}, "alice")

        self.assertEqual(config["inbounds"][0]["settings"]["clients"][0]["id"], CLIENT_ID)
        self.assertEqual(config["inbounds"][1]["settings"]["clients"][0]["password"], PASSWORD)

    def test_prepare_add_client_can_resolve_single_trojan_protocol(self) -> None:
        config = {"inbounds": [base_vless_inbound()], "outbounds": []}
        db = {"connections": {}, "clients": {}}
        trojan = client_connections.add_trojan_caddy_connection(
            config,
            db,
            "trojan-web",
            "vpn.example.com",
            local_port=10100,
            public_port=443,
            fingerprint_value="chrome",
            ws_path="/trojan",
        )

        selected = client_crud.prepare_add_client(config, db, "alice", protocol="trojan")

        self.assertEqual(selected, trojan.tag)

    def test_prepare_add_client_rejects_ambiguous_protocol(self) -> None:
        config = {"inbounds": [base_vless_inbound()], "outbounds": []}
        db = {"connections": {}, "clients": {}}
        client_connections.add_trojan_caddy_connection(
            config,
            db,
            "trojan-web",
            "vpn.example.com",
            local_port=10100,
            public_port=443,
            fingerprint_value="chrome",
            ws_path="/trojan",
        )
        client_connections.add_trojan_caddy_connection(
            config,
            db,
            "trojan-backup",
            "backup.example.com",
            local_port=10101,
            public_port=443,
            fingerprint_value="firefox",
            ws_path="/backup-trojan",
        )

        with self.assertRaisesRegex(ValueError, "Multiple trojan connections"):
            client_crud.prepare_add_client(config, db, "alice", protocol="trojan")

    def test_prepare_add_client_rejects_connection_and_protocol_together(self) -> None:
        config = {"inbounds": [base_vless_inbound()], "outbounds": []}
        db = {"connections": {}, "clients": {}}
        client_connections.add_trojan_caddy_connection(
            config,
            db,
            "trojan-web",
            "vpn.example.com",
            local_port=10100,
            public_port=443,
            fingerprint_value="chrome",
            ws_path="/trojan",
        )

        with self.assertRaisesRegex(ValueError, "Use either --connection"):
            client_crud.prepare_add_client(config, db, "alice", "vless-reality", protocol="trojan")

    def test_add_trojan_caddy_connection_uses_local_ws_inbound_and_public_tls_link(self) -> None:
        config = {"inbounds": [base_vless_inbound()], "outbounds": []}
        db = {"connections": {}, "clients": {}}
        connection = client_connections.add_trojan_caddy_connection(
            config,
            db,
            "trojan-web",
            "vpn.example.com",
            local_port=10100,
            public_port=443,
            fingerprint_value="chrome",
            ws_path="/trojan-private",
        )

        result = client_crud.add_client(
            config,
            db,
            "alice",
            access_days=None,
            connection_tag=connection.tag,
            uuid_factory=lambda: CLIENT_ID,
            password_factory=lambda: PASSWORD,
        )

        inbound = config["inbounds"][1]
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["port"], 10100)
        self.assertEqual(inbound["protocol"], "trojan")
        self.assertEqual(inbound["streamSettings"]["security"], "none")
        self.assertEqual(inbound["streamSettings"]["network"], "ws")
        self.assertEqual(inbound["streamSettings"]["wsSettings"], {"path": "/trojan-private"})
        self.assertEqual(db["connections"][connection.tag]["security"], "tls")
        self.assertEqual(db["connections"][connection.tag]["transport"], "ws")
        self.assertEqual(db["connections"][connection.tag]["publicHost"], "vpn.example.com")
        self.assertEqual(db["connections"][connection.tag]["localPort"], 10100)
        self.assertEqual(result.client_id, CLIENT_ID)

        with mock.patch.object(client_links, "server_name", return_value="Xray"):
            link = client_links.link_for(config, CLIENT_ID, "alice", connection.tag, db)

        parsed = urlsplit(link)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "trojan")
        self.assertEqual(unquote(parsed.username or ""), PASSWORD)
        self.assertEqual(parsed.hostname, "vpn.example.com")
        self.assertEqual(parsed.port, 443)
        self.assertEqual(params["security"], ["tls"])
        self.assertEqual(params["type"], ["ws"])
        self.assertEqual(params["path"], ["/trojan-private"])
        self.assertEqual(params["host"], ["vpn.example.com"])
        self.assertEqual(params["sni"], ["vpn.example.com"])

    def test_trojan_caddy_credential_list_shows_tls_caddy_security(self) -> None:
        config = {"inbounds": [base_vless_inbound()], "outbounds": []}
        db = {"connections": {}, "clients": {}}
        connection = client_connections.add_trojan_caddy_connection(
            config,
            db,
            "trojan-web",
            "vpn.example.com",
            local_port=10100,
            public_port=443,
            fingerprint_value="chrome",
            ws_path="/trojan-private",
        )
        client_crud.add_client(
            config,
            db,
            "alice",
            access_days=None,
            connection_tag=connection.tag,
            uuid_factory=lambda: CLIENT_ID,
            password_factory=lambda: PASSWORD,
        )

        rows = client_listing.credential_rows(config, db)

        row = next(item for item in rows if item["client"] == "alice" and item["connection"] == connection.tag)
        self.assertEqual(row["security"], "tls/caddy")

    def test_disable_and_enable_trojan_client_uses_clients_section(self) -> None:
        config = {"inbounds": [base_vless_inbound()], "outbounds": []}
        db = {"connections": {}, "clients": {}}
        connection = client_connections.add_trojan_tls_connection(
            config,
            db,
            "trojan-main",
            8443,
            "vpn.example.com",
            "/etc/ssl/vpn/fullchain.pem",
            "/etc/ssl/vpn/privkey.pem",
        )
        client_crud.add_client(
            config,
            db,
            "alice",
            access_days=None,
            connection_tag=connection.tag,
            uuid_factory=lambda: CLIENT_ID,
            password_factory=lambda: PASSWORD,
        )

        client_crud.disable_client(config, db, "alice")

        self.assertEqual(config["inbounds"][1]["settings"]["clients"], [])
        self.assertEqual(db["clients"]["alice"]["id"], CLIENT_ID)
        self.assertEqual(db["clients"]["alice"]["client"]["password"], PASSWORD)

        client_crud.enable_client(config, db, {"clients": {}}, "alice")

        clients = config["inbounds"][1]["settings"]["clients"]
        self.assertEqual(clients[0]["password"], PASSWORD)
        self.assertEqual(
            clients[0]["email"],
            "alice|created=" + db["clients"]["alice"]["created"] + "|connection=trojan-tls",
        )
        self.assertNotIn("id", clients[0])

    def test_legacy_trojan_users_section_is_migrated_to_clients_section(self) -> None:
        inbound = client_connections.make_trojan_ws_inbound("trojan-tls", 10100, "/trojan")
        inbound["settings"] = {
            "users": [
                {
                    "password": PASSWORD,
                    "level": 0,
                    "email": "alice|created=2026-06-20T00:00:00Z",
                }
            ]
        }

        clients = xray_config.clients(inbound)

        self.assertEqual(clients[0]["password"], PASSWORD)
        self.assertIn("clients", inbound["settings"])
        self.assertNotIn("users", inbound["settings"])

    def test_remove_trojan_connection_keeps_last_vless_connection(self) -> None:
        config = {"inbounds": [base_vless_inbound()], "outbounds": []}
        db = {"connections": {}, "clients": {}}
        connection = client_connections.add_trojan_tls_connection(
            config,
            db,
            "trojan-main",
            8443,
            "vpn.example.com",
            "/etc/ssl/vpn/fullchain.pem",
            "/etc/ssl/vpn/privkey.pem",
        )
        client_crud.add_client(
            config,
            db,
            "alice",
            access_days=None,
            connection_tag=connection.tag,
            uuid_factory=lambda: CLIENT_ID,
            password_factory=lambda: PASSWORD,
        )

        result = client_connections.remove_connection(config, db, connection.tag)

        self.assertEqual(result.removed_client_names, ["alice"])
        self.assertEqual([inbound["protocol"] for inbound in config["inbounds"]], ["vless"])
        self.assertNotIn("trojan-tls", db["connections"])
        self.assertNotIn("alice", db["clients"])


if __name__ == "__main__":
    unittest.main()
