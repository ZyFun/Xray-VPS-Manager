"""Reality connection records stored in clients.json."""

from __future__ import annotations

from typing import Any

from xray_vps_manager.clients.repository import db_clients, db_connections
from xray_vps_manager.clients.settings import FINGERPRINTS, fingerprint, server_env_values
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.xray.config import (
    connection_name_from_tag,
    connection_settings_from_inbound,
    default_connection_tag,
    inbound_tag,
    reality_inbounds,
)
from xray_vps_manager.clients.models import client_name


def ensure_connections(config: dict[str, Any], db: dict[str, Any]) -> None:
    connections = db_connections(db)
    env = server_env_values()
    now_stamp = utc_stamp()
    for inbound in reality_inbounds(config):
        settings = connection_settings_from_inbound(inbound)
        tag = settings["tag"]
        entry = connections.setdefault(
            tag,
            {
                "tag": tag,
                "name": connection_name_from_tag(tag),
                "created": now_stamp,
            },
        )
        entry.setdefault("tag", tag)
        entry.setdefault("name", connection_name_from_tag(tag))
        entry["port"] = settings["port"]
        entry["sni"] = settings["sni"]
        entry["dest"] = settings["dest"]
        entry.setdefault("fingerprint", env.get("FINGERPRINT") or "chrome")

    default_tag = default_connection_tag(config)
    for inbound in reality_inbounds(config):
        tag = inbound_tag(inbound)
        for item in inbound.setdefault("settings", {}).setdefault("clients", []):
            name = client_name(item)
            if name in db_clients(db):
                db_clients(db)[name].setdefault("connection", tag)

    for entry in db_clients(db).values():
        entry.setdefault("connection", default_tag)


def connection_entry(config: dict[str, Any], db: dict[str, Any], tag: str) -> dict[str, Any]:
    ensure_connections(config, db)
    entry = db_connections(db).get(tag)
    if not entry:
        raise ValueError(f"Connection not found: {tag}")
    return entry


def connection_display_name(config: dict[str, Any], db: dict[str, Any], tag: str) -> str:
    return connection_entry(config, db, tag).get("name") or connection_name_from_tag(tag)


def connection_fingerprint(config: dict[str, Any], db: dict[str, Any], tag: str) -> str:
    value = (connection_entry(config, db, tag).get("fingerprint") or fingerprint()).strip().lower()
    if value not in FINGERPRINTS:
        raise ValueError("FINGERPRINT must be one of: " + ", ".join(sorted(FINGERPRINTS)))
    return value


def resolve_connection_identifier(config: dict[str, Any], db: dict[str, Any], value: str) -> str:
    identifier = (value or "").strip()
    if not identifier:
        raise ValueError("Connection name or tag is required.")
    ensure_connections(config, db)
    connections = db_connections(db)
    if identifier in connections:
        return identifier

    matches = [
        tag
        for tag, entry in connections.items()
        if (entry.get("name") or connection_name_from_tag(tag)) == identifier
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Connection name is ambiguous: {identifier}. Use TAG instead.")
    raise ValueError(f"Connection not found: {identifier}")
