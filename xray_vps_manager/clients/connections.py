"""Managed connection records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Callable

from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients.repository import db_clients, db_managed_connections
from xray_vps_manager.clients.settings import FINGERPRINTS, fingerprint, server_env_values
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.xray import crypto as xray_crypto
from xray_vps_manager.xray.config import (
    DEFAULT_XHTTP_MODE,
    DEFAULT_XHTTP_PATH,
    DEFAULT_XHTTP_TLS_LOCAL_PORT,
    DEFAULT_XHTTP_TLS_PUBLIC_PORT,
    DEFAULT_TROJAN_TLS_PUBLIC_PORT,
    DEFAULT_TROJAN_TLS_MIN_VERSION,
    DEFAULT_TROJAN_TLS_MAX_VERSION,
    DEFAULT_TROJAN_WS_PATH,
    INBOUND_TAG,
    TLS_INBOUND_TAG,
    TROJAN_INBOUND_TAG,
    apply_client_transport,
    apply_reality_transport,
    clients,
    connection_name_from_tag,
    connection_settings_from_inbound,
    connection_transport_settings_from_inbound,
    default_connection_tag,
    find_inbound_by_tag,
    inbound_tag,
    managed_connection_inbounds,
    normalize_xhttp_extra,
    normalize_trojan_ws_path,
    reality_dest,
    reality_inbounds,
    tls_xhttp_inbounds,
    trojan_connection_inbounds,
    vless_connection_inbounds,
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
    security: str = "reality"
    public_host: str = ""
    public_port: int = 0
    local_port: int = 0
    caddy_enabled: bool = False
    tls_min_version: str = ""
    tls_max_version: str = ""
    grpc_service_name: str = ""
    xhttp_path: str = ""
    xhttp_mode: str = ""
    xhttp_extra: dict[str, Any] | None = None
    cert_file: str = ""
    key_file: str = ""
    ws_path: str = ""


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
    xhttp_extra: dict[str, Any] | None = None
    updated_clients: list[str] | None = None
    env_update: dict[str, str] | None = None


@dataclass
class UpdateConnectionXhttpExtraResult:
    tag: str
    display_name: str
    xhttp_extra: dict[str, Any]


@dataclass
class RenameConnectionResult:
    tag: str
    old_name: str
    new_name: str


def ensure_connections(config: dict[str, Any], db: dict[str, Any]) -> None:
    connections = db_managed_connections(db)
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
        if settings.get("transport") != "xhttp":
            entry.pop("xhttpExtra", None)
        elif isinstance(settings.get("xhttpExtra"), dict):
            merged_extra = normalize_xhttp_extra(entry.get("xhttpExtra") if isinstance(entry.get("xhttpExtra"), dict) else {})
            merged_extra.update(settings["xhttpExtra"])
            settings["xhttpExtra"] = normalize_xhttp_extra(merged_extra)
        entry.update(
            {
                key: value
                for key, value in settings.items()
                if key in ("transport", "grpcServiceName", "xhttpPath", "xhttpMode", "xhttpExtra")
            }
        )
        entry.setdefault("fingerprint", env.get("FINGERPRINT") or "chrome")
        entry["security"] = "reality"

    for inbound in tls_xhttp_inbounds(config):
        tag = inbound_tag(inbound)
        entry = connections.get(tag)
        if not isinstance(entry, dict):
            entry = {
                "tag": tag,
                "name": connection_name_from_tag(tag),
                "created": now_stamp,
                "security": "tls",
                "port": DEFAULT_XHTTP_TLS_PUBLIC_PORT,
                "sni": "",
                "dest": "",
                "transport": "xhttp",
                "xhttpPath": DEFAULT_XHTTP_PATH,
                "xhttpMode": DEFAULT_XHTTP_MODE,
                "localPort": int(inbound.get("port", 0) or 0),
                "publicHost": "",
                "publicPort": DEFAULT_XHTTP_TLS_PUBLIC_PORT,
                "caddy": True,
            }
            connections[tag] = entry
        entry.setdefault("tag", tag)
        entry.setdefault("name", connection_name_from_tag(tag))
        entry.setdefault("created", now_stamp)
        entry["security"] = "tls"
        entry["localPort"] = int(inbound.get("port", 0) or 0)
        entry.setdefault("publicHost", entry.get("sni") or "")
        entry.setdefault("publicPort", int(entry.get("port") or DEFAULT_XHTTP_TLS_PUBLIC_PORT))
        entry["port"] = int(entry.get("publicPort") or DEFAULT_XHTTP_TLS_PUBLIC_PORT)
        entry["sni"] = entry.get("publicHost") or entry.get("sni") or ""
        entry.setdefault("dest", "")
        entry.setdefault("fingerprint", env.get("FINGERPRINT") or "chrome")
        settings = connection_transport_settings_from_inbound(inbound)
        if isinstance(settings.get("xhttpExtra"), dict):
            merged_extra = normalize_xhttp_extra(entry.get("xhttpExtra") if isinstance(entry.get("xhttpExtra"), dict) else {})
            merged_extra.update(settings["xhttpExtra"])
            settings["xhttpExtra"] = normalize_xhttp_extra(merged_extra)
        entry.update(settings)

    for inbound in trojan_connection_inbounds(config):
        tag = inbound_tag(inbound)
        entry = connections.get(tag)
        stream = inbound.get("streamSettings", {})
        network = str(stream.get("network") or "tcp").strip().lower()
        tls = stream.get("tlsSettings") if isinstance(stream.get("tlsSettings"), dict) else {}
        certificates = tls.get("certificates") if isinstance(tls.get("certificates"), list) else []
        certificate = certificates[0] if certificates and isinstance(certificates[0], dict) else {}
        transport_settings = connection_transport_settings_from_inbound(inbound)
        if not isinstance(entry, dict):
            entry = {
                "tag": tag,
                "name": connection_name_from_tag(tag),
                "created": now_stamp,
                "protocol": "trojan",
                "security": "tls",
                "transport": transport_settings.get("transport") or network,
                "port": int(inbound.get("port", 0) or 0),
                "sni": "",
                "dest": "",
                "fingerprint": env.get("FINGERPRINT") or "chrome",
                "publicKey": "",
                "shortId": "",
            }
            connections[tag] = entry
        entry.setdefault("tag", tag)
        entry.setdefault("name", connection_name_from_tag(tag))
        entry.setdefault("created", now_stamp)
        entry["protocol"] = "trojan"
        entry["security"] = "tls"
        entry["transport"] = transport_settings.get("transport") or network
        if entry["transport"] == "ws":
            entry["localPort"] = int(inbound.get("port", 0) or 0)
            entry.setdefault("publicHost", entry.get("sni") or "")
            entry.setdefault("publicPort", int(entry.get("port") or DEFAULT_TROJAN_TLS_PUBLIC_PORT))
            entry["port"] = int(entry.get("publicPort") or DEFAULT_TROJAN_TLS_PUBLIC_PORT)
            entry["sni"] = entry.get("publicHost") or entry.get("sni") or ""
            entry["caddy"] = bool(entry.get("caddy", True))
            entry["wsPath"] = transport_settings.get("wsPath") or entry.get("wsPath") or DEFAULT_TROJAN_WS_PATH
        else:
            entry["transport"] = "tcp"
            entry["port"] = int(inbound.get("port", 0) or 0)
            entry.pop("localPort", None)
            entry.setdefault("publicPort", entry["port"])
            entry.setdefault("publicHost", entry.get("sni") or "")
            entry["caddy"] = bool(entry.get("caddy", False))
            entry.pop("wsPath", None)
        entry.setdefault("sni", "")
        entry.setdefault("dest", "")
        entry.setdefault("fingerprint", env.get("FINGERPRINT") or "chrome")
        entry.setdefault("publicKey", "")
        entry.setdefault("shortId", "")
        if certificate.get("certificateFile"):
            entry["certFile"] = certificate["certificateFile"]
        if certificate.get("keyFile"):
            entry["keyFile"] = certificate["keyFile"]

    default_tag = default_connection_tag(config)
    for inbound in managed_connection_inbounds(config):
        tag = inbound_tag(inbound)
        for item in clients(inbound):
            name = client_name(item)
            if name in db_clients(db):
                db_clients(db)[name].setdefault("connection", tag)

    for entry in db_clients(db).values():
        entry.setdefault("connection", default_tag)


def connection_entry(config: dict[str, Any], db: dict[str, Any], tag: str) -> dict[str, Any]:
    ensure_connections(config, db)
    entry = db_managed_connections(db).get(tag)
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
    for tag, entry in db_managed_connections(db).items():
        protocol = entry.get("protocol") or "vless"
        security = entry.get("security") or "reality"
        security_label = f"{protocol}/{security}" if protocol != "vless" else security
        rows.append(
            [
                entry.get("name", connection_name_from_tag(tag)),
                tag,
                security_label,
                entry.get("port", ""),
                entry.get("sni", ""),
                entry.get("transport", "tcp"),
                entry.get("fingerprint", fallback_fingerprint) if security == "reality" else (entry.get("fingerprint") or "-"),
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


def next_local_port(config: dict[str, Any], start: int = DEFAULT_XHTTP_TLS_LOCAL_PORT) -> int:
    ports = used_ports(config)
    port = start
    while port in ports:
        port += 1
        if port > 65535:
            raise ValueError("No free local port found.")
    return port


def public_port_conflicts(config: dict[str, Any], public_port: int) -> list[dict[str, Any]]:
    conflicts = []
    for inbound in config.get("inbounds", []):
        if inbound.get("port") != public_port:
            continue
        listen = str(inbound.get("listen") or "0.0.0.0").strip().lower()
        if listen in ("127.0.0.1", "localhost", "::1"):
            continue
        conflicts.append(inbound)
    return conflicts


def next_connection_tag(config: dict[str, Any]) -> str:
    existing = {inbound_tag(inbound) for inbound in vless_connection_inbounds(config)}
    if INBOUND_TAG not in existing:
        return INBOUND_TAG
    index = 2
    while True:
        tag = f"{INBOUND_TAG}-{index}"
        if tag not in existing:
            return tag
        index += 1


def next_tls_connection_tag(config: dict[str, Any]) -> str:
    existing = {inbound_tag(inbound) for inbound in managed_connection_inbounds(config)}
    if TLS_INBOUND_TAG not in existing:
        return TLS_INBOUND_TAG
    index = 2
    while True:
        tag = f"{TLS_INBOUND_TAG}-{index}"
        if tag not in existing:
            return tag
        index += 1


def next_trojan_connection_tag(config: dict[str, Any]) -> str:
    existing = {inbound_tag(inbound) for inbound in managed_connection_inbounds(config)}
    if TROJAN_INBOUND_TAG not in existing:
        return TROJAN_INBOUND_TAG
    index = 2
    while True:
        tag = f"{TROJAN_INBOUND_TAG}-{index}"
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
    xhttp_extra: dict[str, Any] | None = None,
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
        xhttp_extra=xhttp_extra,
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


def make_trojan_tls_inbound(tag: str, port: int, cert_file: str, key_file: str) -> dict[str, Any]:
    return {
        "tag": tag,
        "listen": "0.0.0.0",
        "port": port,
        "protocol": "trojan",
        "settings": {
            "clients": [],
        },
        "streamSettings": {
            "network": "tcp",
            "security": "tls",
            "tlsSettings": {
                "certificates": [
                    {
                        "certificateFile": cert_file,
                        "keyFile": key_file,
                    }
                ]
            },
        },
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls", "quic"],
        },
    }


def make_trojan_ws_inbound(tag: str, local_port: int, ws_path: str = "") -> dict[str, Any]:
    return {
        "tag": tag,
        "listen": "127.0.0.1",
        "port": local_port,
        "protocol": "trojan",
        "settings": {
            "clients": [],
        },
        "streamSettings": {
            "network": "ws",
            "security": "none",
            "wsSettings": {
                "path": normalize_trojan_ws_path(ws_path),
            },
        },
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls", "quic"],
        },
    }


def make_tls_xhttp_inbound(
    tag: str,
    local_port: int,
    *,
    xhttp_path: str = "",
    xhttp_mode: str = "",
    xhttp_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stream_settings = {
        "network": "xhttp",
        "security": "none",
    }
    apply_reality_transport(
        stream_settings,
        "xhttp",
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
        xhttp_extra=xhttp_extra,
    )
    return {
        "tag": tag,
        "listen": "127.0.0.1",
        "port": local_port,
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
    xhttp_extra: dict[str, Any] | None = None,
    key_pair_factory: Callable[[], tuple[str, str]] = xray_crypto.xray_x25519_keys,
    short_id_factory: Callable[[], str] = xray_crypto.random_short_id,
) -> AddConnectionResult:
    ensure_connections(config, db)

    existing_connection_names = {entry.get("name") for entry in db_managed_connections(db).values()}
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
        xhttp_extra=xhttp_extra,
    )
    config.setdefault("inbounds", []).append(inbound)

    dest = reality_dest(sni)
    transport_settings = connection_settings_from_inbound(inbound)
    if transport_settings.get("transport") == "xhttp" and xhttp_extra:
        transport_settings["xhttpExtra"] = normalize_xhttp_extra(xhttp_extra)
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
            if key in ("transport", "grpcServiceName", "xhttpPath", "xhttpMode", "xhttpExtra")
        }
    )
    db_managed_connections(db)[tag] = record

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
        xhttp_extra=record.get("xhttpExtra"),
    )


def add_tls_xhttp_connection(
    config: dict[str, Any],
    db: dict[str, Any],
    name: str,
    domain: str,
    *,
    local_port: int,
    public_port: int = DEFAULT_XHTTP_TLS_PUBLIC_PORT,
    fingerprint_value: str = "",
    xhttp_path: str = "",
    xhttp_mode: str = "",
    xhttp_extra: dict[str, Any] | None = None,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
    caddy_enabled: bool = True,
) -> AddConnectionResult:
    ensure_connections(config, db)

    existing_connection_names = {entry.get("name") for entry in db_managed_connections(db).values()}
    if name in existing_connection_names:
        raise ValueError(f"Connection already exists: {name}")

    if local_port in used_ports(config):
        raise ValueError(f"LOCAL_PORT is already used by another inbound: {local_port}")

    fp = (fingerprint_value or "").strip().lower()
    if fp and fp not in FINGERPRINTS:
        raise ValueError("FINGERPRINT must be one of: " + ", ".join(sorted(FINGERPRINTS)))

    tag = next_tls_connection_tag(config)
    inbound = make_tls_xhttp_inbound(
        tag,
        local_port,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
        xhttp_extra=xhttp_extra,
    )
    config.setdefault("inbounds", []).append(inbound)

    transport_settings = connection_transport_settings_from_inbound(inbound)
    if transport_settings.get("transport") == "xhttp" and xhttp_extra:
        transport_settings["xhttpExtra"] = normalize_xhttp_extra(xhttp_extra)
    created = utc_stamp()
    record = {
        "tag": tag,
        "name": name,
        "created": created,
        "security": "tls",
        "port": public_port,
        "publicPort": public_port,
        "localPort": local_port,
        "publicHost": domain,
        "sni": domain,
        "dest": "",
        "fingerprint": fp,
        "publicKey": "",
        "shortId": "",
        "caddy": bool(caddy_enabled),
        "tlsMinVersion": tls_min_version,
        "tlsMaxVersion": tls_max_version,
    }
    record.update(
        {
            key: value
            for key, value in transport_settings.items()
            if key in ("transport", "grpcServiceName", "xhttpPath", "xhttpMode", "xhttpExtra")
        }
    )
    db_managed_connections(db)[tag] = record

    return AddConnectionResult(
        tag=tag,
        name=name,
        port=public_port,
        sni=domain,
        dest="",
        fingerprint=fp,
        public_key="",
        short_id="",
        transport=record.get("transport", "xhttp"),
        security="tls",
        public_host=domain,
        public_port=public_port,
        local_port=local_port,
        caddy_enabled=bool(caddy_enabled),
        tls_min_version=tls_min_version,
        tls_max_version=tls_max_version,
        xhttp_path=record.get("xhttpPath", ""),
        xhttp_mode=record.get("xhttpMode", ""),
        xhttp_extra=record.get("xhttpExtra"),
    )


def add_trojan_tls_connection(
    config: dict[str, Any],
    db: dict[str, Any],
    name: str,
    port: int,
    domain: str,
    cert_file: str,
    key_file: str,
    fingerprint_value: str = "chrome",
) -> AddConnectionResult:
    ensure_connections(config, db)

    existing_connection_names = {entry.get("name") for entry in db_managed_connections(db).values()}
    if name in existing_connection_names:
        raise ValueError(f"Connection already exists: {name}")

    if port in used_ports(config):
        raise ValueError(f"PORT is already used by another inbound: {port}")

    fp = (fingerprint_value or "chrome").strip().lower()
    if fp not in FINGERPRINTS:
        raise ValueError("FINGERPRINT must be one of: " + ", ".join(sorted(FINGERPRINTS)))

    tag = next_trojan_connection_tag(config)
    inbound = make_trojan_tls_inbound(tag, port, cert_file, key_file)
    config.setdefault("inbounds", []).append(inbound)

    created = utc_stamp()
    record = {
        "tag": tag,
        "name": name,
        "created": created,
        "protocol": "trojan",
        "security": "tls",
        "transport": "tcp",
        "port": port,
        "sni": domain,
        "dest": "",
        "fingerprint": fp,
        "publicKey": "",
        "shortId": "",
        "certFile": cert_file,
        "keyFile": key_file,
    }
    db_managed_connections(db)[tag] = record

    return AddConnectionResult(
        tag=tag,
        name=name,
        port=port,
        sni=domain,
        dest="",
        fingerprint=fp,
        public_key="",
        short_id="",
        transport="tcp",
        security="tls",
        cert_file=cert_file,
        key_file=key_file,
    )


def add_trojan_caddy_connection(
    config: dict[str, Any],
    db: dict[str, Any],
    name: str,
    domain: str,
    *,
    local_port: int,
    public_port: int = DEFAULT_TROJAN_TLS_PUBLIC_PORT,
    fingerprint_value: str = "chrome",
    ws_path: str = "",
    tls_min_version: str = DEFAULT_TROJAN_TLS_MIN_VERSION,
    tls_max_version: str = DEFAULT_TROJAN_TLS_MAX_VERSION,
    caddy_enabled: bool = True,
) -> AddConnectionResult:
    ensure_connections(config, db)

    existing_connection_names = {entry.get("name") for entry in db_managed_connections(db).values()}
    if name in existing_connection_names:
        raise ValueError(f"Connection already exists: {name}")

    if local_port in used_ports(config):
        raise ValueError(f"LOCAL_PORT is already used by another inbound: {local_port}")

    fp = (fingerprint_value or "chrome").strip().lower()
    if fp not in FINGERPRINTS:
        raise ValueError("FINGERPRINT must be one of: " + ", ".join(sorted(FINGERPRINTS)))

    path = normalize_trojan_ws_path(ws_path)
    tag = next_trojan_connection_tag(config)
    inbound = make_trojan_ws_inbound(tag, local_port, path)
    config.setdefault("inbounds", []).append(inbound)

    created = utc_stamp()
    record = {
        "tag": tag,
        "name": name,
        "created": created,
        "protocol": "trojan",
        "security": "tls",
        "transport": "ws",
        "port": public_port,
        "publicPort": public_port,
        "localPort": local_port,
        "publicHost": domain,
        "sni": domain,
        "dest": "",
        "fingerprint": fp,
        "publicKey": "",
        "shortId": "",
        "caddy": bool(caddy_enabled),
        "wsPath": path,
        "tlsMinVersion": tls_min_version,
        "tlsMaxVersion": tls_max_version,
    }
    db_managed_connections(db)[tag] = record

    return AddConnectionResult(
        tag=tag,
        name=name,
        port=public_port,
        sni=domain,
        dest="",
        fingerprint=fp,
        public_key="",
        short_id="",
        transport="ws",
        security="tls",
        public_host=domain,
        public_port=public_port,
        local_port=local_port,
        caddy_enabled=bool(caddy_enabled),
        tls_min_version=tls_min_version,
        tls_max_version=tls_max_version,
        ws_path=path,
    )


def remove_connection(config: dict[str, Any], db: dict[str, Any], identifier: str) -> RemoveConnectionResult:
    ensure_connections(config, db)
    tag = resolve_connection_identifier(config, db, identifier)
    inbound = find_inbound_by_tag(config, tag)
    protocol = inbound.get("protocol") or "vless"
    security = db_managed_connections(db).get(tag, {}).get("security") or inbound.get("streamSettings", {}).get("security") or "reality"

    if protocol == "vless" and len(vless_connection_inbounds(config)) <= 1:
        raise ValueError("Cannot remove the last VLESS connection.")
    if protocol == "vless" and security == "reality" and len(reality_inbounds(config)) <= 1:
        raise ValueError("Cannot remove the last Reality connection.")

    display_name = connection_display_name(config, db, tag)
    removed_client_names = connection_client_names(config, db, tag)
    config["inbounds"] = [
        inbound
        for inbound in config.get("inbounds", [])
        if not (inbound_tag(inbound) == tag and inbound.get("protocol") in ("vless", "trojan"))
    ]

    db_managed_connections(db).pop(tag, None)
    for name in removed_client_names:
        entry = db_clients(db).get(name)
        if not isinstance(entry, dict):
            continue
        credentials = client_credentials.normalize_entry_credentials(entry)
        credentials.pop(tag, None)
        if credentials:
            client_credentials.sync_legacy_fields(entry)
        else:
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
    xhttp_extra: dict[str, Any] | None = None,
) -> UpdateConnectionTransportResult:
    ensure_connections(config, db)
    tag = resolve_connection_identifier(config, db, identifier)
    inbound = find_inbound_by_tag(config, tag)
    security = db_managed_connections(db).get(tag, {}).get("security") or inbound.get("streamSettings", {}).get("security") or "reality"
    if inbound.get("protocol") != "vless":
        raise ValueError("Transport updates are currently supported only for VLESS connections.")
    if security == "tls" and transport != "xhttp":
        raise ValueError("TLS connections support only xhttp transport.")
    current_entry = db_managed_connections(db).get(tag, {})
    effective_xhttp_extra = xhttp_extra
    if transport == "xhttp" and xhttp_extra is None and isinstance(current_entry.get("xhttpExtra"), dict):
        effective_xhttp_extra = current_entry["xhttpExtra"]
    stream = inbound.setdefault("streamSettings", {})
    settings = apply_reality_transport(
        stream,
        transport,
        grpc_service_name=grpc_service_name,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
        xhttp_extra=effective_xhttp_extra,
    )
    updated_clients = []
    for item in clients(inbound):
        apply_client_transport(item, settings["transport"])
        name = client_name(item)
        if name:
            updated_clients.append(name)

    connections = db_managed_connections(db)
    entry = connections.setdefault(tag, {"tag": tag, "name": connection_name_from_tag(tag)})
    for key in ("transport", "grpcServiceName", "xhttpPath", "xhttpMode", "xhttpExtra"):
        entry.pop(key, None)
    entry.update(settings)
    for name, client_entry in db_clients(db).items():
        credentials = client_credentials.normalize_entry_credentials(client_entry)
        credential = credentials.get(tag)
        if credential:
            client = dict(credential.get("client") or {})
            if client:
                apply_client_transport(client, settings["transport"])
                credential["client"] = client
                credential["transport"] = settings["transport"]
                client_credentials.sync_legacy_fields(client_entry)

    env_update = server_env_values_for_connection(config, db, tag) if tag == INBOUND_TAG else None
    return UpdateConnectionTransportResult(
        tag=tag,
        display_name=connection_display_name(config, db, tag),
        transport=settings["transport"],
        grpc_service_name=settings.get("grpcServiceName", ""),
        xhttp_path=settings.get("xhttpPath", ""),
        xhttp_mode=settings.get("xhttpMode", ""),
        xhttp_extra=settings.get("xhttpExtra"),
        updated_clients=sorted(updated_clients),
        env_update=env_update,
    )


def update_connection_xhttp_extra(
    config: dict[str, Any],
    db: dict[str, Any],
    identifier: str,
    xhttp_extra: dict[str, Any] | None,
) -> UpdateConnectionXhttpExtraResult:
    ensure_connections(config, db)
    tag = resolve_connection_identifier(config, db, identifier)
    inbound = find_inbound_by_tag(config, tag)
    settings = connection_transport_settings_from_inbound(inbound)
    if settings.get("transport") != "xhttp":
        raise ValueError("XHTTP advanced settings are available only for xhttp connections.")
    normalized_extra = normalize_xhttp_extra(xhttp_extra)
    updated = apply_reality_transport(
        inbound.setdefault("streamSettings", {}),
        "xhttp",
        xhttp_path=settings.get("xhttpPath") or DEFAULT_XHTTP_PATH,
        xhttp_mode=settings.get("xhttpMode") or DEFAULT_XHTTP_MODE,
        xhttp_extra=normalized_extra,
    )
    entry = db_managed_connections(db).setdefault(tag, {"tag": tag, "name": connection_name_from_tag(tag)})
    if updated.get("xhttpExtra"):
        entry["xhttpExtra"] = updated["xhttpExtra"]
    else:
        entry.pop("xhttpExtra", None)
    return UpdateConnectionXhttpExtraResult(
        tag=tag,
        display_name=connection_display_name(config, db, tag),
        xhttp_extra=entry.get("xhttpExtra", {}),
    )


def rename_connection(config: dict[str, Any], db: dict[str, Any], identifier: str, new_name: str) -> RenameConnectionResult:
    ensure_connections(config, db)
    name = (new_name or "").strip()
    if not name:
        raise ValueError("Connection name is required.")
    tag = resolve_connection_identifier(config, db, identifier)
    connections = db_managed_connections(db)
    for existing_tag, entry in connections.items():
        if existing_tag != tag and (entry.get("name") or connection_name_from_tag(existing_tag)) == name:
            raise ValueError(f"Connection already exists: {name}")
    entry = connections.setdefault(tag, {"tag": tag, "name": connection_name_from_tag(tag)})
    old_name = entry.get("name") or connection_name_from_tag(tag)
    entry["name"] = name
    return RenameConnectionResult(tag=tag, old_name=old_name, new_name=name)


def connection_client_names(config: dict[str, Any], db: dict[str, Any], tag: str) -> list[str]:
    names = set()
    inbound = find_inbound_by_tag(config, tag)
    for item in clients(inbound):
        name = client_name(item)
        if name:
            names.add(name)
    for name, entry in db_clients(db).items():
        credentials = client_credentials.normalize_entry_credentials(entry)
        if tag in credentials:
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
    connections = db_managed_connections(db)
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
