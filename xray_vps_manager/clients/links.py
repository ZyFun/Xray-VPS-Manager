"""VLESS Reality link generation."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from xray_vps_manager.clients.connections import connection_fingerprint, ensure_connections
from xray_vps_manager.clients.repository import db_clients
from xray_vps_manager.clients.settings import server_addr, server_name
from xray_vps_manager.xray.config import default_connection_tag, find_inbound_by_tag
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
    reality = stream.get("realitySettings", {})
    port = inbound.get("port", 443)
    sni = reality.get("serverNames", [""])[0]
    private_key = reality.get("privateKey")
    short_ids = reality.get("shortIds", [""])
    short_id = short_ids[0] if short_ids else ""
    if not private_key or not sni:
        raise ValueError("Reality privateKey/serverNames not found in inbound.")
    public_key = reality_public_key(private_key)

    params = {
        "security": "reality",
        "encryption": "none",
        "pbk": public_key,
        "fp": connection_fingerprint(config, db, connection_tag),
        "type": stream.get("network", "tcp"),
        "flow": "xtls-rprx-vision",
        "sni": sni,
        "sid": short_id,
        "spx": "/",
    }
    query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
    return f"vless://{client_id}@{server_addr()}:{port}?{query}#{quote(server_name(), safe='')}"
