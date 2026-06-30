"""Xray access log parsing helpers."""

from __future__ import annotations

from xray_vps_manager.activity.constants import (
    ACCESS_RE,
    ADMIN_PORTS,
    GEOIP_PATHS,
    INBOUND_TAG,
    NETWORK_TARGET_RE,
    ROUTE_RE,
    SMTP_PORTS,
    TARGET_RE,
    XRAY_GEOIP_OUTBOUND_PREFIX,
)
from xray_vps_manager.activity.time import access_time_to_iso
from xray_vps_manager.clients import credentials as client_credentials

_GEOIP_CODES_CACHE: list[str] | None = None


def split_email(email: str) -> str:
    if "|created=" in email:
        name, _created = email.split("|created=", 1)
        return name
    return email


def parse_target(value: str) -> tuple[str, str, str]:
    if not value or ":" not in value:
        return "", "", ""
    network, rest = value.split(":", 1)
    host = rest
    port = ""
    if rest.startswith("[") and "]:" in rest:
        host, port = rest[1:].split("]:", 1)
    elif ":" in rest:
        host, port = rest.rsplit(":", 1)
    return network, host, port


def read_varint(data: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
    raise ValueError("truncated varint")


def parse_proto_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    index = 0
    fields = []
    while index < len(data):
        key, index = read_varint(data, index)
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, index = read_varint(data, index)
            fields.append((field_number, wire_type, value))
        elif wire_type == 1:
            value = data[index:index + 8]
            index += 8
            fields.append((field_number, wire_type, value))
        elif wire_type == 2:
            length, index = read_varint(data, index)
            value = data[index:index + length]
            index += length
            fields.append((field_number, wire_type, value))
        elif wire_type == 5:
            value = data[index:index + 4]
            index += 4
            fields.append((field_number, wire_type, value))
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
    return fields


def geoip_path():
    for path in GEOIP_PATHS:
        if path.exists():
            return path
    return None


def iter_geoip_entries():
    path = geoip_path()
    if not path:
        return
    data = path.read_bytes()
    for field_number, wire_type, geoip_blob in parse_proto_fields(data):
        if field_number != 1 or wire_type != 2:
            continue
        country_code = ""
        for inner_number, inner_type, inner_value in parse_proto_fields(geoip_blob):
            if inner_number == 1 and inner_type == 2:
                country_code = inner_value.decode("utf-8", errors="ignore").upper()
        if country_code:
            yield country_code


def available_geoip_codes() -> list[str]:
    global _GEOIP_CODES_CACHE
    if _GEOIP_CODES_CACHE is None:
        try:
            _GEOIP_CODES_CACHE = sorted(set(iter_geoip_entries()))
        except Exception:
            _GEOIP_CODES_CACHE = []
    return _GEOIP_CODES_CACHE


def reality_inbounds(config: dict) -> list[dict]:
    return [
        inbound
        for inbound in config.get("inbounds", [])
        if inbound.get("protocol") == "vless"
        and inbound.get("streamSettings", {}).get("security") == "reality"
    ]


def managed_inbounds(config: dict) -> list[dict]:
    return [
        inbound
        for inbound in config.get("inbounds", [])
        if inbound.get("protocol") in ("vless", "trojan")
    ]


def parse_route(body: str) -> tuple[str, str]:
    match = ROUTE_RE.search(body)
    if not match:
        return "", ""
    parts = [part.strip() for part in match.group(1).split("->")]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return parts[0], ""


def parse_source(body: str) -> str:
    first = body.split(" ", 1)[0].strip()
    if first and ":" in first and not first.startswith(("accepted", "rejected", "tcp:", "udp:")):
        return first
    return ""


def event_risks(event: dict) -> list[str]:
    risks = []
    port = str(event.get("port") or "")
    outbound = (event.get("outbound") or "").lower()
    target = (event.get("target") or "").lower()
    if port in SMTP_PORTS:
        risks.append("smtp")
    if port in ADMIN_PORTS:
        risks.append("admin-port")
    if "block" in outbound or "blocked" in outbound or "blackhole" in outbound:
        risks.append("blocked")
    if "bittorrent" in target or "torrent" in outbound:
        risks.append("torrent")
    if outbound.startswith(XRAY_GEOIP_OUTBOUND_PREFIX):
        code = outbound[len(XRAY_GEOIP_OUTBOUND_PREFIX):].upper()
        if code:
            risks.append(f"xray-geoip:{code}")
    return risks


def parse_access_line(line: str, clients: dict) -> dict | None:
    match = ACCESS_RE.match(line)
    if not match:
        return None
    email = match.group("email").strip()
    name = split_email(email)
    client_info = clients.get(email) or clients.get(name)
    if not client_info:
        return None
    client_name_value = client_info.get("client") or name

    body = match.group("body")
    target_match = TARGET_RE.search(body)
    status = ""
    target = ""
    network = ""
    if target_match:
        status = target_match.group("status")
        target = target_match.group("target")
        network = target_match.group("network")
    else:
        target_match = NETWORK_TARGET_RE.search(body)
        if target_match:
            target = target_match.group("target")
            network = target_match.group("network")

    if target:
        network, host, port = parse_target(target)
    else:
        host = ""
        port = ""

    inbound, outbound = parse_route(body)
    event = {
        "time": access_time_to_iso(match.group("time")),
        "client": client_name_value,
        "email": email,
        "connection": client_info.get("connection", ""),
        "source": parse_source(body),
        "status": status,
        "network": network,
        "target": target,
        "host": host,
        "port": port,
        "inbound": inbound,
        "outbound": outbound,
    }
    risks = event_risks(event)
    if risks:
        event["risks"] = risks
    return event


def config_clients(config: dict, client_db: dict) -> dict:
    clients = {}
    for inbound in managed_inbounds(config):
        tag = inbound.get("tag") or INBOUND_TAG
        items = inbound.get("settings", {}).get("clients", [])
        if not items and inbound.get("protocol") == "trojan":
            items = inbound.get("settings", {}).get("users", [])
        for item in items:
            email = item.get("email", "")
            if not email:
                continue
            name = split_email(email)
            record = {"client": name, "email": email, "connection": tag}
            clients[email] = record
            clients.setdefault(name, record)

    for name, entry in client_db.get("clients", {}).items():
        credentials = client_credentials.sorted_credentials(entry)
        if not credentials:
            email = entry.get("client", {}).get("email") or name
            record = {"client": name, "email": email, "connection": entry.get("connection") or INBOUND_TAG}
            clients[email] = record
            clients.setdefault(name, record)
            continue
        for credential in credentials:
            email = client_credentials.credential_email(name, entry, credential)
            record = {"client": name, "email": email, "connection": credential.get("connection") or INBOUND_TAG}
            clients[email] = record
            clients.setdefault(name, record)
    return clients
