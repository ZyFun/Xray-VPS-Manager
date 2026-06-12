"""Reality connection records stored in clients.json."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Callable

from xray_vps_manager.clients.repository import db_clients, db_connections
from xray_vps_manager.clients.settings import FINGERPRINTS, fingerprint, server_env_values
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.xray import crypto as xray_crypto
from xray_vps_manager.xray.config import (
    INBOUND_TAG,
    clients,
    connection_name_from_tag,
    connection_settings_from_inbound,
    default_connection_tag,
    find_inbound_by_tag,
    inbound_tag,
    reality_dest,
    reality_inbounds,
)
from xray_vps_manager.clients.models import client_name


@dataclass
class AddConnectionResult:
    tag: str
    name: str
    port: int
    sni: str
    dest: str
    fingerprint: str
    public_key: str
    short_id: str


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


def connection_rows(config: dict[str, Any], db: dict[str, Any]) -> list[list[Any]]:
    ensure_connections(config, db)
    fallback_fingerprint = fingerprint()
    rows = []
    for tag, entry in db_connections(db).items():
        rows.append(
            [
                entry.get("name", connection_name_from_tag(tag)),
                tag,
                entry.get("port", ""),
                entry.get("sni", ""),
                entry.get("fingerprint", fallback_fingerprint),
                entry.get("created", "unknown"),
            ]
        )
    return rows


def used_ports(config: dict[str, Any]) -> set[int]:
    ports = set()
    for inbound in config.get("inbounds", []):
        port = inbound.get("port")
        if isinstance(port, int):
            ports.add(port)
    return ports


def next_connection_tag(config: dict[str, Any]) -> str:
    existing = {inbound_tag(inbound) for inbound in reality_inbounds(config)}
    if INBOUND_TAG not in existing:
        return INBOUND_TAG
    index = 2
    while True:
        tag = f"{INBOUND_TAG}-{index}"
        if tag not in existing:
            return tag
        index += 1


def make_reality_inbound(tag: str, port: int, sni: str, private_key: str, short_id: str) -> dict[str, Any]:
    return {
        "tag": tag,
        "listen": "0.0.0.0",
        "port": port,
        "protocol": "vless",
        "settings": {
            "clients": [],
            "decryption": "none",
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": reality_dest(sni),
                "xver": 0,
                "serverNames": [sni],
                "privateKey": private_key,
                "shortIds": [short_id],
            },
        },
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls", "quic"],
        },
    }


def add_connection(
    config: dict[str, Any],
    db: dict[str, Any],
    name: str,
    port: int,
    sni: str,
    fingerprint_value: str = "chrome",
    key_pair_factory: Callable[[], tuple[str, str]] = xray_crypto.xray_x25519_keys,
    short_id_factory: Callable[[], str] = xray_crypto.random_short_id,
) -> AddConnectionResult:
    ensure_connections(config, db)

    existing_connection_names = {entry.get("name") for entry in db_connections(db).values()}
    if name in existing_connection_names:
        raise ValueError(f"Connection already exists: {name}")

    if port in used_ports(config):
        raise ValueError(f"PORT is already used by another inbound: {port}")

    fp = (fingerprint_value or "chrome").strip().lower()
    if fp not in FINGERPRINTS:
        raise ValueError("FINGERPRINT must be one of: " + ", ".join(sorted(FINGERPRINTS)))

    tag = next_connection_tag(config)
    private_key, public_key = key_pair_factory()
    short_id = short_id_factory()
    inbound = make_reality_inbound(tag, port, sni, private_key, short_id)
    config.setdefault("inbounds", []).append(inbound)

    dest = reality_dest(sni)
    created = utc_stamp()
    db_connections(db)[tag] = {
        "tag": tag,
        "name": name,
        "created": created,
        "port": port,
        "sni": sni,
        "dest": dest,
        "fingerprint": fp,
        "publicKey": public_key,
        "shortId": short_id,
    }

    return AddConnectionResult(
        tag=tag,
        name=name,
        port=port,
        sni=sni,
        dest=dest,
        fingerprint=fp,
        public_key=public_key,
        short_id=short_id,
    )


def connection_client_names(config: dict[str, Any], db: dict[str, Any], tag: str) -> list[str]:
    names = set()
    inbound = find_inbound_by_tag(config, tag)
    for item in clients(inbound):
        name = client_name(item)
        if name:
            names.add(name)
    for name, entry in db_clients(db).items():
        if entry.get("connection") == tag:
            names.add(name)
    return sorted(names)


def server_env_values_for_connection(config: dict[str, Any], db: dict[str, Any], tag: str) -> dict[str, str]:
    inbound = find_inbound_by_tag(config, tag)
    settings = connection_settings_from_inbound(inbound)
    values = server_env_values()
    values.setdefault("SERVER_ADDR", "")
    values["PORT"] = str(settings["port"])
    values["REALITY_SNI"] = settings["sni"]
    values["REALITY_DEST"] = settings["dest"]
    values["FINGERPRINT"] = connection_fingerprint(config, db, tag)
    return values


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
