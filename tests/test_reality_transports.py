from urllib.parse import parse_qs, urlsplit
import unittest
from unittest import mock

from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.clients import links as client_links
from xray_vps_manager.commands import set_cascade


CLIENT_ID = "00000000-0000-0000-0000-000000000001"


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


class RealityTransportTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
