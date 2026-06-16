"""Reality connection records."""

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
    apply_client_transport,
    apply_reality_transport,
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
    transport: str
    grpc_service_name: str = ""
    xhttp_path: str = ""
    xhttp_mode: str = ""


@dataclass
class RemoveConnectionResult:
    tag: str
    display_name: str
    removed_client_names: list[str]
    env_switch_tag: str = ""
    env_switch_name: str = ""
    env_update: dict[str, str] | None = None


@dataclass
class UpdateConnectionTransportResult:
    tag: str
    display_name: str
    transport: str
    grpc_service_name: str = ""
    xhttp_path: str = ""
    xhttp_mode: str = ""
    updated_clients: list[str] | None = None
    env_update: dict[str, str] | None = None


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
        for key in ("transport", "grpcServiceName", "xhttpPath", "xhttpMode"):
            entry.pop(key, None)
        entry.update(
            {
                key: value
                for key, value in settings.items()
                if key in ("transport", "grpcServiceName", "xhttpPath", "xhttpMode")
            }
        )
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
                entry.get("transport", "tcp"),
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


def make_reality_inbound(
    tag: str,
    port: int,
    sni: str,
    private_key: str,
    short_id: str,
    *,
    transport: str = "tcp",
    grpc_service_name: str = "",
    xhttp_path: str = "",
    xhttp_mode: str = "",
) -> dict[str, Any]:
    stream_settings = {
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
    }
    apply_reality_transport(
        stream_settings,
        transport,
        grpc_service_name=grpc_service_name,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
    )
    return {
        "tag": tag,
        "listen": "0.0.0.0",
        "port": port,
        "protocol": "vless",
        "settings": {
            "clients": [],
            "decryption": "none",
        },
        "streamSettings": stream_settings,
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
    transport: str = "tcp",
    grpc_service_name: str = "",
    xhttp_path: str = "",
    xhttp_mode: str = "",
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
    inbound = make_reality_inbound(
        tag,
        port,
        sni,
        private_key,
        short_id,
        transport=transport,
        grpc_service_name=grpc_service_name,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
    )
    config.setdefault("inbounds", []).append(inbound)

    dest = reality_dest(sni)
    transport_settings = connection_settings_from_inbound(inbound)
    created = utc_stamp()
    record = {
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
    record.update(
        {
            key: value
            for key, value in transport_settings.items()
            if key in ("transport", "grpcServiceName", "xhttpPath", "xhttpMode")
        }
    )
    db_connections(db)[tag] = record

    return AddConnectionResult(
        tag=tag,
        name=name,
        port=port,
        sni=sni,
        dest=dest,
        fingerprint=fp,
        public_key=public_key,
        short_id=short_id,
        transport=record.get("transport", "tcp"),
        grpc_service_name=record.get("grpcServiceName", ""),
        xhttp_path=record.get("xhttpPath", ""),
        xhttp_mode=record.get("xhttpMode", ""),
    )


def remove_connection(config: dict[str, Any], db: dict[str, Any], identifier: str) -> RemoveConnectionResult:
    ensure_connections(config, db)
    tag = resolve_connection_identifier(config, db, identifier)

    if len(reality_inbounds(config)) <= 1:
        raise ValueError("Cannot remove the last Reality connection.")

    display_name = connection_display_name(config, db, tag)
    removed_client_names = connection_client_names(config, db, tag)
    config["inbounds"] = [
        inbound
        for inbound in config.get("inbounds", [])
        if not (
            inbound.get("protocol") == "vless"
            and inbound.get("streamSettings", {}).get("security") == "reality"
            and inbound_tag(inbound) == tag
        )
    ]

    db_connections(db).pop(tag, None)
    for name in removed_client_names:
        db_clients(db).pop(name, None)

    env_update = None
    env_switch_tag = ""
    env_switch_name = ""
    if tag == INBOUND_TAG:
        remaining = reality_inbounds(config)
        if remaining:
            env_switch_tag = inbound_tag(remaining[0])
            env_switch_name = connection_display_name(config, db, env_switch_tag)
            env_update = server_env_values_for_connection(config, db, env_switch_tag)

    return RemoveConnectionResult(
        tag=tag,
        display_name=display_name,
        removed_client_names=removed_client_names,
        env_switch_tag=env_switch_tag,
        env_switch_name=env_switch_name,
        env_update=env_update,
    )


def update_connection_transport(
    config: dict[str, Any],
    db: dict[str, Any],
    identifier: str,
    transport: str,
    *,
    grpc_service_name: str = "",
    xhttp_path: str = "",
    xhttp_mode: str = "",
) -> UpdateConnectionTransportResult:
    ensure_connections(config, db)
    tag = resolve_connection_identifier(config, db, identifier)
    inbound = find_inbound_by_tag(config, tag)
    stream = inbound.setdefault("streamSettings", {})
    settings = apply_reality_transport(
        stream,
        transport,
        grpc_service_name=grpc_service_name,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
    )
    updated_clients = []
    for item in clients(inbound):
        apply_client_transport(item, settings["transport"])
        name = client_name(item)
        if name:
            updated_clients.append(name)

    connections = db_connections(db)
    entry = connections.setdefault(tag, {"tag": tag, "name": connection_name_from_tag(tag)})
    for key in ("transport", "grpcServiceName", "xhttpPath", "xhttpMode"):
        entry.pop(key, None)
    entry.update(settings)
    for name, client_entry in db_clients(db).items():
        if client_entry.get("connection") == tag:
            client = dict(client_entry.get("client") or {})
            if client:
                apply_client_transport(client, settings["transport"])
                client_entry["client"] = client

    env_update = server_env_values_for_connection(config, db, tag) if tag == INBOUND_TAG else None
    return UpdateConnectionTransportResult(
        tag=tag,
        display_name=connection_display_name(config, db, tag),
        transport=settings["transport"],
        grpc_service_name=settings.get("grpcServiceName", ""),
        xhttp_path=settings.get("xhttpPath", ""),
        xhttp_mode=settings.get("xhttpMode", ""),
        updated_clients=sorted(updated_clients),
        env_update=env_update,
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
    values["REALITY_TRANSPORT"] = settings["transport"]
    values.pop("GRPC_SERVICE_NAME", None)
    values.pop("XHTTP_PATH", None)
    values.pop("XHTTP_MODE", None)
    if settings["transport"] == "grpc":
        values["GRPC_SERVICE_NAME"] = settings["grpcServiceName"]
    elif settings["transport"] == "xhttp":
        values["XHTTP_PATH"] = settings["xhttpPath"]
        values["XHTTP_MODE"] = settings["xhttpMode"]
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
