from urllib.parse import parse_qs, urlsplit
import unittest
from unittest import mock

from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.clients import links as client_links
from xray_vps_manager.commands import set_cascade
from xray_vps_manager.xray import config as xray_config


CLIENT_ID = "00000000-0000-0000-0000-000000000001"
XHTTP_EXTRA = {
    "xPaddingBytes": "120-900",
    "scStreamUpServerSecs": "25-70",
    "xmux": {
        "maxConcurrency": "12-24",
        "maxConnections": 0,
        "cMaxReuseTimes": 0,
        "hMaxRequestTimes": "500-800",
        "hMaxReusableSecs": "1500-2400",
        "hKeepAlivePeriod": 0,
    },
}


def base_inbound(transport: str = "tcp") -> dict:
    return client_connections.make_reality_inbound(
        "vless-reality",
        443,
        "example.com",
        "private-key",
        "abcd",
        transport=transport,
        grpc_service_name="vless-grpc",
        xhttp_path="/vless-xhttp",
        xhttp_mode="auto",
    )


def extra_inbound(tag: str = "vless-reality-2", transport: str = "xhttp") -> dict:
    return client_connections.make_reality_inbound(
        tag,
        8443,
        "backup.example.com",
        "private-key-2",
        "bcde",
        transport=transport,
        grpc_service_name="backup-grpc",
        xhttp_path="/backup-xhttp",
        xhttp_mode="auto",
    )


class RealityTransportTests(unittest.TestCase):
    def test_xhttp_extra_rejects_conflicting_xmux_connection_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "maxConcurrency and maxConnections"):
            xray_config.normalize_xhttp_extra(
                {
                    "xPaddingBytes": "100-1000",
                    "scStreamUpServerSecs": "20-80",
                    "xmux": {
                        "maxConcurrency": "16-32",
                        "maxConnections": 1,
                    },
                }
            )

    def test_xhttp_extra_allows_sc_stream_up_server_secs_minus_one(self) -> None:
        extra = xray_config.normalize_xhttp_extra({"scStreamUpServerSecs": "-1"})

        self.assertEqual(extra, {"scStreamUpServerSecs": -1})

    def test_update_existing_connection_to_grpc_removes_client_flow(self) -> None:
        item = {
            "id": CLIENT_ID,
            "flow": "xtls-rprx-vision",
            "level": 0,
            "email": "alice|created=2026-06-12T08:00:00Z",
        }
        config = {"inbounds": [base_inbound()], "outbounds": []}
        config["inbounds"][0]["settings"]["clients"].append(dict(item))
        db = {
            "connections": {
                "vless-reality": {
                    "tag": "vless-reality",
                    "name": "default",
                    "port": 443,
                    "sni": "example.com",
                    "dest": "example.com:443",
                    "fingerprint": "chrome",
                }
            },
            "clients": {
                "alice": {
                    "id": CLIENT_ID,
                    "connection": "vless-reality",
                    "client": dict(item),
                }
            },
        }

        result = client_connections.update_connection_transport(
            config,
            db,
            "vless-reality",
            "grpc",
            grpc_service_name="svc",
        )

        stream = config["inbounds"][0]["streamSettings"]
        self.assertEqual(result.transport, "grpc")
        self.assertEqual(stream["network"], "grpc")
        self.assertEqual(stream["grpcSettings"], {"serviceName": "svc"})
        self.assertNotIn("flow", config["inbounds"][0]["settings"]["clients"][0])
        self.assertNotIn("flow", db["clients"]["alice"]["client"])
        self.assertEqual(db["connections"]["vless-reality"]["grpcServiceName"], "svc")

    def test_add_client_to_xhttp_connection_has_no_vision_flow(self) -> None:
        config = {"inbounds": [base_inbound("xhttp")], "outbounds": []}
        db = {
            "connections": {},
            "clients": {},
        }

        result = client_crud.add_client(
            config,
            db,
            "alice",
            access_days=None,
            connection_tag="vless-reality",
            uuid_factory=lambda: CLIENT_ID,
        )

        self.assertNotIn("flow", result.entry["client"])
        self.assertNotIn("flow", config["inbounds"][0]["settings"]["clients"][0])

    def test_xhttp_link_contains_path_mode_and_no_flow(self) -> None:
        config = {"inbounds": [base_inbound("xhttp")]}
        db = {
            "connections": {},
            "clients": {
                "alice": {
                    "id": CLIENT_ID,
                    "connection": "vless-reality",
                }
            },
        }

        with mock.patch.object(client_links, "server_addr", return_value="vpn.example"), \
            mock.patch.object(client_links, "server_name", return_value="Xray"), \
            mock.patch.object(client_links, "reality_public_key", return_value="public-key"):
            link = client_links.link_for(config, CLIENT_ID, "alice", db=db)

        parsed = urlsplit(link)
        params = parse_qs(parsed.query)
        self.assertEqual(params["type"], ["xhttp"])
        self.assertEqual(params["path"], ["/vless-xhttp"])
        self.assertEqual(params["mode"], ["auto"])
        self.assertNotIn("flow", params)

    def test_xhttp_extra_is_stored_server_side_and_added_to_link(self) -> None:
        config = {"inbounds": [base_inbound("tcp")], "outbounds": []}
        db = {
            "connections": {},
            "clients": {
                "alice": {
                    "id": CLIENT_ID,
                    "connection": "vless-reality-2",
                }
            },
        }
        connection = client_connections.add_connection(
            config,
            db,
            "stealth",
            8443,
            "backup.example.com",
            "chrome",
            transport="xhttp",
            xhttp_path="/private-xhttp",
            xhttp_mode="auto",
            xhttp_extra=XHTTP_EXTRA,
            key_pair_factory=lambda: ("private-key-2", "public-key-2"),
            short_id_factory=lambda: "bcde",
        )

        inbound = config["inbounds"][1]
        self.assertEqual(
            inbound["streamSettings"]["xhttpSettings"]["extra"],
            {
                "xPaddingBytes": "120-900",
                "scStreamUpServerSecs": "25-70",
            },
        )
        self.assertEqual(db["connections"][connection.tag]["xhttpExtra"], XHTTP_EXTRA)

        with mock.patch.object(client_links, "server_addr", return_value="vpn.example"), \
            mock.patch.object(client_links, "server_name", return_value="Xray"), \
            mock.patch.object(client_links, "reality_public_key", return_value="public-key-2"):
            link = client_links.link_for(config, CLIENT_ID, "alice", connection.tag, db)

        params = parse_qs(urlsplit(link).query)
        self.assertIn("extra", params)
        self.assertIn('"xmux"', params["extra"][0])
        self.assertIn('"xPaddingBytes":"120-900"', params["extra"][0])

    def test_tls_xhttp_connection_uses_local_inbound_and_public_tls_link(self) -> None:
        config = {"inbounds": [base_inbound()], "outbounds": []}
        db = {
            "connections": {},
            "clients": {},
        }

        connection = client_connections.add_tls_xhttp_connection(
            config,
            db,
            "api",
            "api.example.com",
            local_port=10000,
            xhttp_path="/private-xhttp",
            xhttp_mode="auto",
        )
        result = client_crud.add_client(
            config,
            db,
            "alice",
            access_days=None,
            connection_tag=connection.tag,
            uuid_factory=lambda: CLIENT_ID,
        )

        inbound = config["inbounds"][1]
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["port"], 10000)
        self.assertEqual(inbound["streamSettings"]["security"], "none")
        self.assertEqual(inbound["streamSettings"]["network"], "xhttp")
        self.assertNotIn("flow", result.entry["client"])
        self.assertEqual(db["connections"][connection.tag]["security"], "tls")
        self.assertEqual(db["connections"][connection.tag]["publicHost"], "api.example.com")

        with mock.patch.object(client_links, "server_name", return_value="Xray"):
            link = client_links.link_for(config, CLIENT_ID, "alice", connection.tag, db)

        parsed = urlsplit(link)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.hostname, "api.example.com")
        self.assertEqual(parsed.port, 443)
        self.assertEqual(params["security"], ["tls"])
        self.assertEqual(params["type"], ["xhttp"])
        self.assertEqual(params["sni"], ["api.example.com"])
        self.assertEqual(params["path"], ["/private-xhttp"])
        self.assertNotIn("pbk", params)

    def test_update_xhttp_extra_can_clear_existing_profile(self) -> None:
        config = {"inbounds": [base_inbound("xhttp")], "outbounds": []}
        db = {"connections": {}, "clients": {}}
        client_connections.ensure_connections(config, db)

        result = client_connections.update_connection_xhttp_extra(config, db, "vless-reality", XHTTP_EXTRA)

        self.assertEqual(result.xhttp_extra, XHTTP_EXTRA)
        self.assertEqual(
            config["inbounds"][0]["streamSettings"]["xhttpSettings"]["extra"],
            {
                "xPaddingBytes": "120-900",
                "scStreamUpServerSecs": "25-70",
            },
        )

        cleared = client_connections.update_connection_xhttp_extra(config, db, "vless-reality", {})

        self.assertEqual(cleared.xhttp_extra, {})
        self.assertNotIn("xhttpExtra", db["connections"]["vless-reality"])
        self.assertNotIn("extra", config["inbounds"][0]["streamSettings"]["xhttpSettings"])

    def test_cascade_parser_accepts_xhttp_links(self) -> None:
        link = (
            "vless://11111111-1111-1111-1111-111111111111@example.com:443?"
            "security=reality&encryption=none&type=xhttp&pbk=public-key&fp=firefox"
            "&sni=example.com&sid=abcd&path=%2Fvless-xhttp&mode=auto#Example"
        )

        outbound, label = set_cascade.parse_vless(link, "cascade-xhttp")

        self.assertEqual(label, "Example")
        self.assertEqual(outbound["streamSettings"]["network"], "xhttp")
        self.assertEqual(outbound["streamSettings"]["xhttpSettings"], {"path": "/vless-xhttp", "mode": "auto"})
        self.assertNotIn("flow", outbound["settings"]["vnext"][0]["users"][0])

    def test_cascade_parser_accepts_xhttp_extra_from_link(self) -> None:
        extra = (
            "%7B%22scStreamUpServerSecs%22%3A%2225-70%22%2C%22xPaddingBytes%22%3A%22120-900%22%2C"
            "%22xmux%22%3A%7B%22cMaxReuseTimes%22%3A0%2C%22hKeepAlivePeriod%22%3A0%2C"
            "%22hMaxRequestTimes%22%3A%22500-800%22%2C%22hMaxReusableSecs%22%3A%221500-2400%22%2C"
            "%22maxConcurrency%22%3A%2212-24%22%2C%22maxConnections%22%3A0%7D%7D"
        )
        link = (
            "vless://11111111-1111-1111-1111-111111111111@example.com:443?"
            "security=reality&encryption=none&type=xhttp&pbk=public-key&fp=firefox"
            f"&sni=example.com&sid=abcd&path=%2Fvless-xhttp&mode=auto&extra={extra}#Example"
        )

        outbound, _ = set_cascade.parse_vless(link, "cascade-xhttp")

        self.assertEqual(outbound["streamSettings"]["xhttpSettings"]["extra"], XHTTP_EXTRA)

    def test_rename_connection_changes_display_name_only(self) -> None:
        config = {"inbounds": [base_inbound("xhttp")]}
        db = {
            "connections": {},
            "clients": {},
        }
        client_connections.ensure_connections(config, db)

        result = client_connections.rename_connection(config, db, "vless-reality", "Apple")

        self.assertEqual(result.tag, "vless-reality")
        self.assertEqual(result.old_name, "default")
        self.assertEqual(result.new_name, "Apple")
        self.assertEqual(db["connections"]["vless-reality"]["name"], "Apple")
        self.assertEqual(config["inbounds"][0]["tag"], "vless-reality")

    def test_move_enabled_client_to_another_connection_updates_config_and_link_settings(self) -> None:
        config = {"inbounds": [base_inbound("tcp"), extra_inbound("vless-reality-2", "xhttp")], "outbounds": []}
        db = {
            "connections": {},
            "clients": {},
        }
        added = client_crud.add_client(
            config,
            db,
            "alice",
            access_days=None,
            connection_tag="vless-reality",
            uuid_factory=lambda: CLIENT_ID,
        )
        self.assertIn("flow", added.entry["client"])

        result = client_crud.move_client_to_connection(config, db, "alice", "vless-reality-2")

        self.assertTrue(result.config_changed)
        self.assertTrue(result.enabled)
        self.assertEqual(result.source_connection_tag, "vless-reality")
        self.assertEqual(result.target_connection_tag, "vless-reality-2")
        self.assertEqual(config["inbounds"][0]["settings"]["clients"], [])
        self.assertEqual(len(config["inbounds"][1]["settings"]["clients"]), 1)
        self.assertNotIn("flow", config["inbounds"][1]["settings"]["clients"][0])
        self.assertNotIn("flow", db["clients"]["alice"]["client"])
        self.assertEqual(db["clients"]["alice"]["connection"], "vless-reality-2")

        with mock.patch.object(client_links, "server_addr", return_value="vpn.example"), \
            mock.patch.object(client_links, "server_name", return_value="Xray"), \
            mock.patch.object(client_links, "reality_public_key", return_value="public-key"):
            link = client_links.link_for(config, CLIENT_ID, "alice", db=db)

        params = parse_qs(urlsplit(link).query)
        self.assertEqual(params["type"], ["xhttp"])
        self.assertEqual(params["path"], ["/backup-xhttp"])
        self.assertNotIn("flow", params)

    def test_move_disabled_client_updates_saved_connection_without_config_change(self) -> None:
        config = {"inbounds": [base_inbound("tcp"), extra_inbound("vless-reality-2", "xhttp")], "outbounds": []}
        db = {
            "connections": {},
            "clients": {},
        }
        client_crud.add_client(
            config,
            db,
            "alice",
            access_days=None,
            connection_tag="vless-reality",
            uuid_factory=lambda: CLIENT_ID,
        )
        client_crud.disable_client(config, db, "alice")

        result = client_crud.move_client_to_connection(config, db, "alice", "vless-reality-2")

        self.assertFalse(result.config_changed)
        self.assertFalse(result.enabled)
        self.assertEqual(config["inbounds"][0]["settings"]["clients"], [])
        self.assertEqual(config["inbounds"][1]["settings"]["clients"], [])
        self.assertEqual(db["clients"]["alice"]["connection"], "vless-reality-2")
        self.assertNotIn("flow", db["clients"]["alice"]["client"])


if __name__ == "__main__":
    unittest.main()
