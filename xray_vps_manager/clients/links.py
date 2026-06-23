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
    xhttp_extra_json,
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
    protocol = entry.get("protocol") or inbound.get("protocol") or "vless"
    security = entry.get("security") or stream.get("security") or "reality"
    if protocol == "trojan":
        client_entry = db_clients(db).get(name, {})
        client = client_entry.get("client") if isinstance(client_entry.get("client"), dict) else {}
        password = str(client.get("password") or "").strip()
        if not password:
            raise ValueError(f"Trojan password not found for client: {name}")
        host = entry.get("publicHost") or entry.get("sni") or server_addr()
        port = int(entry.get("publicPort") or entry.get("port") or inbound.get("port") or 443)
        sni = entry.get("sni") or host
        transport_settings = connection_transport_settings_from_inbound(inbound)
        transport = str(entry.get("transport") or transport_settings.get("transport") or "tcp").strip().lower()
        params = {
            "security": "tls" if security == "none" else security,
            "type": transport,
        }
        if sni:
            params["sni"] = sni
        if transport == "ws":
            params["path"] = entry.get("wsPath") or transport_settings.get("wsPath") or "/trojan"
            params["host"] = host
        fingerprint = (entry.get("fingerprint") or "").strip()
        if fingerprint:
            params["fp"] = fingerprint
        query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
        return f"trojan://{quote(password, safe='')}@{host}:{port}?{query}#{quote(server_name(), safe='')}"

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
        extra = xhttp_extra_json(entry.get("xhttpExtra"))
        if extra:
            params["extra"] = extra
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
        extra = xhttp_extra_json(entry.get("xhttpExtra"))
        if extra:
            params["extra"] = extra
    query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
    return f"vless://{client_id}@{server_addr()}:{port}?{query}#{quote(server_name(), safe='')}"
