"""Клиентские ссылки и формат файла стартового подключения."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients import listing
from xray_vps_manager.clients.connections import connection_fingerprint, ensure_connections
from xray_vps_manager.clients.repository import db_clients, db_managed_connections
from xray_vps_manager.clients.settings import server_addr, server_name
from xray_vps_manager.xray.config import (
    INBOUND_TAG,
    TROJAN_INBOUND_TAG,
    client_flow_for_transport,
    clients as inbound_clients,
    connection_settings_from_inbound,
    connection_transport_settings_from_inbound,
    default_connection_tag,
    find_inbound_by_tag,
    inbound_tag,
    reality_transport_settings_from_inbound,
    trojan_connection_inbounds,
    xhttp_extra_json,
)
from xray_vps_manager.xray.crypto import is_x25519_key, reality_public_key
from xray_vps_manager.xray.email import client_name


def client_link_lines(config, db, name, connection_tag=None, *, default_fingerprint=None):
    ensure_connections(config, db, default_fingerprint=default_fingerprint)
    rows = [
        row for row in listing.credential_rows(config, db)
        if row["name"] == name and (not connection_tag or row["connection"] == connection_tag)
    ]
    if not rows:
        raise ValueError(f"Client not found: {name}")
    for row in rows:
        if len(rows) > 1:
            yield f"[{row['protocol']} {row['connection']}]"
        yield link_for(config, row["id"], name, row["connection"], db, default_fingerprint=default_fingerprint)


INITIAL_PROTOCOLS = {"vless": ("vless",), "trojan": ("trojan",), "both": ("vless", "trojan")}
_INITIAL_TAGS = {"vless": INBOUND_TAG, "trojan": TROJAN_INBOUND_TAG}
_INITIAL_FIELDS = (
    "INITIAL_PROTOCOL", "CLIENT_URI", "VLESS_CLIENT_URI", "TROJAN_CLIENT_URI",
    "SERVER", "PORT", "PROTOCOL", "SECURITY", "TRANSPORT", "FLOW", "UUID", "PUBLIC_KEY",
    "SHORT_ID", "SNI", "DEST", "FINGERPRINT", "GRPC_SERVICE_NAME", "XHTTP_PATH", "XHTTP_MODE",
    "TROJAN_DOMAIN", "TROJAN_LOCAL_PORT", "TROJAN_PUBLIC_PORT", "TROJAN_WS_PATH", "TROJAN_TLS", "CREATED",
)


def read_initial_link_content(text):
    """Читает формат установщика и URI, вынесенные на отдельные строки XR-REV-029."""
    fields = {}
    identities = {"vless": set(), "trojan": set()}
    legacy_multiline = False
    for line in text.splitlines():
        if not line:
            continue
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            if key in fields:
                raise ValueError("Повторяющееся поле стартового файла.")
            fields[key] = value
        elif line.startswith(("vless://", "trojan://")):
            value = line
            key = "CLIENT_URI"
            legacy_multiline = True
        elif re.fullmatch(r"\[(?:vless|trojan) [^\]]+\]", line):
            continue
        else:
            raise ValueError("Неизвестная строка стартового файла.")

        if key in ("CLIENT_URI", "VLESS_CLIENT_URI", "TROJAN_CLIENT_URI"):
            if value.startswith(("vless://", "trojan://")):
                uri = urlsplit(value)
                if uri.username:
                    identities[uri.scheme].add(unquote(uri.username))
    return fields, identities, legacy_multiline


def initial_link_credentials(
    config, db, mode, *, name=None, identities=None, trojan_uuid=None, default_fingerprint=None,
):
    """Выбирает одну точную запись на исходный протокол для одного клиента."""
    selected = {}
    ensure_connections(config, db, default_fingerprint=default_fingerprint)
    rows = listing.credential_rows(config, db)
    protocols = INITIAL_PROTOCOLS[mode]
    active_passwords = {}
    if name is None and "trojan" in protocols:
        for inbound in trojan_connection_inbounds(config):
            tag = inbound_tag(inbound)
            for item in inbound_clients(inbound):
                active_passwords.setdefault((client_name(item), tag), item.get("password"))
    for protocol in protocols:
        candidates = []
        for row in rows:
            if row["protocol"] != protocol:
                continue
            tag = row["connection"]
            if name is not None:
                matches = row["name"] == name and tag == _INITIAL_TAGS[protocol]
            elif protocol == "vless":
                matches = row["id"] in identities[protocol]
            else:
                entry = db_clients(db).get(row["name"])
                credential = client_credentials.credential_for_connection(entry, tag) if entry else None
                if credential is None:
                    password = active_passwords.get((row["name"], tag))
                else:
                    password = credential["client"].get("password")
                matches = password in identities[protocol]
                if trojan_uuid:
                    if matches and row["id"] and row["id"] != trojan_uuid:
                        raise ValueError("UUID расходится со стартовой Trojan-ссылкой.")
                    matches = matches or row["id"] == trojan_uuid
            if matches:
                candidates.append(row)
        if len(candidates) != 1:
            raise ValueError("Стартовое подключение не определяется однозначно.")
        selected[protocol] = candidates[0]
    if len({row["name"] for row in selected.values()}) != 1:
        raise ValueError("Стартовые ссылки относятся к разным клиентам.")
    return selected


def initial_link_content(config, db, mode, selected, fields, *, default_fingerprint=None):
    """Формирует поля установщика по текущим ссылкам и метаданным."""
    values = dict(fields, INITIAL_PROTOCOL=mode, VLESS_CLIENT_URI="", TROJAN_CLIENT_URI="")
    public_server = None
    for protocol, row in selected.items():
        tag = row["connection"]
        inbound = find_inbound_by_tag(config, tag)
        uri = link_for(config, row["id"], row["name"], tag, db, default_fingerprint=default_fingerprint)
        parts = urlsplit(uri)
        params = {key: value[0] for key, value in parse_qs(parts.query, keep_blank_values=True).items()}
        values[f"{protocol.upper()}_CLIENT_URI"] = uri
        if protocol == "vless":
            transport = params["type"]
            previous_transport = fields.get("TRANSPORT")
            if previous_transport and previous_transport != transport:
                values.update(GRPC_SERVICE_NAME="", XHTTP_PATH="", XHTTP_MODE="")
            values.update(
                CLIENT_URI=uri, PROTOCOL="vless+trojan" if mode == "both" else "vless",
                SECURITY=params["security"].upper(), TRANSPORT=transport,
                FLOW=params.get("flow", ""), UUID=row["id"], SNI=params["sni"],
                FINGERPRINT=params.get("fp", ""),
            )
            if params["security"] == "tls":
                public_server, port = parts.hostname, parts.port
                if not public_server or port is None:
                    raise ValueError("Публичный адрес или порт VLESS TLS не определён.")
                values.update(PORT=port, PUBLIC_KEY="", SHORT_ID="", DEST="")
            else:
                reality = connection_settings_from_inbound(inbound)
                values.update(PORT=reality["port"], PUBLIC_KEY=params["pbk"], SHORT_ID=params["sid"], DEST=reality["dest"])
            if transport == "grpc":
                values["GRPC_SERVICE_NAME"] = params["serviceName"]
            elif transport == "xhttp":
                values.update(XHTTP_PATH=params["path"], XHTTP_MODE=params["mode"])
        else:
            if params["security"] != "tls" or params["type"] != "ws":
                raise ValueError("Стартовое Trojan-подключение должно использовать TLS и WebSocket.")
            record = db_managed_connections(db).get(tag, {})
            if not (record.get("publicHost") or record.get("sni")) or not record.get("publicPort"):
                raise ValueError("Публичный адрес или порт Trojan-WS не подтверждён.")
            values.update(
                TROJAN_DOMAIN=params["host"], TROJAN_LOCAL_PORT=inbound["port"],
                TROJAN_PUBLIC_PORT=parts.port, TROJAN_WS_PATH=params["path"],
            )
            if record.get("tlsMinVersion") and record.get("tlsMaxVersion"):
                values["TROJAN_TLS"] = f"{record['tlsMinVersion']}..{record['tlsMaxVersion']}"
            if mode == "trojan":
                values.update(CLIENT_URI=uri, PROTOCOL="trojan", SECURITY="TLS",
                              TRANSPORT="ws", FLOW="", FINGERPRINT=params.get("fp", ""))
                if not values.get("UUID"):
                    entry = db_clients(db).get(row["name"], {})
                    credential = client_credentials.credential_for_connection(entry, tag)
                    if credential and credential.get("id"):
                        values["UUID"] = credential["id"]

    values["SERVER"] = public_server or server_addr()
    fields_in_order = (*_INITIAL_FIELDS, *(key for key in fields if key not in _INITIAL_FIELDS))
    lines = [f"{key}={values.get(key, '')}" for key in fields_in_order]
    if any(line.splitlines() != [line] for line in lines):
        raise ValueError("Значение поля стартового файла должно занимать одну строку.")
    return "\n".join(lines) + "\n"


def link_for(
    config: dict[str, Any],
    client_id: str,
    name: str,
    connection_tag: str | None = None,
    db: dict[str, Any] | None = None,
    db_loader=None,
    *,
    default_fingerprint: str | None = None,
) -> str:
    if db is None:
        if db_loader is None:
            from xray_vps_manager.clients.repository import load_db

            db_loader = load_db
        db = db_loader()
    ensure_connections(config, db, default_fingerprint=default_fingerprint)
    client_entry = db_clients(db).get(name, {})
    if client_entry:
        client_credentials.normalize_entry_credentials(client_entry)
    connection_tag = connection_tag or client_entry.get("connection") or default_connection_tag(config)
    credential = client_credentials.credential_for_connection(client_entry, connection_tag) if client_entry else None
    inbound = find_inbound_by_tag(config, connection_tag)
    stream = inbound.get("streamSettings", {})
    entry = db_managed_connections(db).get(connection_tag, {})
    protocol = entry.get("protocol") or inbound.get("protocol") or "vless"
    security = entry.get("security") or stream.get("security") or "reality"
    if protocol == "trojan":
        if credential is None:
            _, active = client_credentials.active_item_for_connection(config, name, connection_tag)
            password = (active or {}).get("password")
        else:
            password = credential["client"].get("password")
        if not isinstance(password, str) or not password:
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

    if credential:
        client_id = str(credential.get("id") or (credential.get("client") or {}).get("id") or client_id)

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
    sni = (reality.get("serverNames") or [""])[0]
    private_key = reality.get("privateKey")
    short_ids = reality.get("shortIds", [""])
    short_id = short_ids[0] if short_ids else ""
    if not private_key or not sni:
        raise ValueError("Reality privateKey/serverNames not found in inbound.")
    # Ключ из записи подключения: ссылка не зависит от бинарника Xray и формата его вывода (XR-REV-051).
    public_key = str(entry.get("publicKey") or "").strip()
    if not is_x25519_key(public_key):
        public_key = reality_public_key(private_key)

    transport_settings = reality_transport_settings_from_inbound(inbound)
    transport = transport_settings["transport"]
    params = {
        "security": "reality",
        "encryption": "none",
        "pbk": public_key,
        "fp": connection_fingerprint(config, db, connection_tag, default_fingerprint=default_fingerprint),
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
