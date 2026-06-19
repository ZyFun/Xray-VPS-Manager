"""VLESS Reality link generation."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from xray_vps_manager.clients.connections import connection_fingerprint, ensure_connections
from xray_vps_manager.clients.repository import db_clients, db_connections
from xray_vps_manager.clients.settings import server_addr, server_name
from xray_vps_manager.xray.config import (
    client_flow_for_transport,
    connection_transport_settings_from_inbound,
    default_connection_tag,
    find_inbound_by_tag,
    reality_transport_settings_from_inbound,
)
from xray_vps_manager.xray.crypto import reality_public_key


def link_for(
    config: dict[str, Any],
    client_id: str,
    name: str,
    connection_tag: str | None = None,
    db: dict[str, Any] | None = None,
    db_loader=None,
) -> str:
    if db is None:
        if db_loader is None:
            from xray_vps_manager.clients.repository import load_db

            db_loader = load_db
        db = db_loader()
    ensure_connections(config, db)
    connection_tag = connection_tag or db_clients(db).get(name, {}).get("connection") or default_connection_tag(config)
    inbound = find_inbound_by_tag(config, connection_tag)
    stream = inbound.get("streamSettings", {})
    entry = db_connections(db).get(connection_tag, {})
    security = entry.get("security") or stream.get("security") or "reality"
    if security == "tls":
        transport_settings = connection_transport_settings_from_inbound(inbound)
        transport = transport_settings["transport"]
        if transport != "xhttp":
            raise ValueError("TLS connections support only xhttp links.")
        host = entry.get("publicHost") or entry.get("sni")
        if not host:
            raise ValueError("TLS publicHost/SNI not found in connection.")
        port = int(entry.get("publicPort") or entry.get("port") or 443)
        params = {
            "security": "tls",
            "encryption": "none",
            "type": "xhttp",
            "sni": host,
            "path": transport_settings["xhttpPath"],
            "mode": transport_settings["xhttpMode"],
        }
        fingerprint = (entry.get("fingerprint") or "").strip()
        if fingerprint:
            params["fp"] = fingerprint
        query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
        return f"vless://{client_id}@{host}:{port}?{query}#{quote(server_name(), safe='')}"

    reality = stream.get("realitySettings", {})
    port = inbound.get("port", 443)
    sni = reality.get("serverNames", [""])[0]
    private_key = reality.get("privateKey")
    short_ids = reality.get("shortIds", [""])
    short_id = short_ids[0] if short_ids else ""
    if not private_key or not sni:
        raise ValueError("Reality privateKey/serverNames not found in inbound.")
    public_key = reality_public_key(private_key)

    transport_settings = reality_transport_settings_from_inbound(inbound)
    transport = transport_settings["transport"]
    params = {
        "security": "reality",
        "encryption": "none",
        "pbk": public_key,
        "fp": connection_fingerprint(config, db, connection_tag),
        "type": transport,
        "sni": sni,
        "sid": short_id,
        "spx": "/",
    }
    flow = client_flow_for_transport(transport)
    if flow:
        params["flow"] = flow
    if transport == "grpc":
        params["serviceName"] = transport_settings["grpcServiceName"]
    elif transport == "xhttp":
        params["path"] = transport_settings["xhttpPath"]
        params["mode"] = transport_settings["xhttpMode"]
    query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
    return f"vless://{client_id}@{server_addr()}:{port}?{query}#{quote(server_name(), safe='')}"
