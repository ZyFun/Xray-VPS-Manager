"""Credential helpers for clients with multiple protocol keys."""

from __future__ import annotations

import re
from typing import Any

from xray_vps_manager.clients.models import client_name, email_for_client
from xray_vps_manager.xray.config import (
    apply_client_transport,
    clients,
    connection_transport_settings_from_inbound,
    find_inbound_by_tag,
    inbound_tag,
    managed_connection_inbounds,
)

TROJAN_PASSWORD_MIN_LENGTH = 32
TROJAN_PASSWORD_MAX_LENGTH = 128
TROJAN_PASSWORD_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def db_credentials(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    credentials = entry.setdefault("credentials", {})
    if not isinstance(credentials, dict):
        credentials = {}
        entry["credentials"] = credentials
    return credentials


def credential_key(connection_tag: str) -> str:
    return str(connection_tag or "").strip()


def trojan_password_policy_issues(password: str) -> list[str]:
    value = str(password or "")
    issues = []
    if len(value) < TROJAN_PASSWORD_MIN_LENGTH:
        issues.append(f"shorter than {TROJAN_PASSWORD_MIN_LENGTH} chars")
    if len(value) > TROJAN_PASSWORD_MAX_LENGTH:
        issues.append(f"longer than {TROJAN_PASSWORD_MAX_LENGTH} chars")
    if value.strip() != value or any(char.isspace() for char in value):
        issues.append("contains whitespace")
    if value and not TROJAN_PASSWORD_RE.fullmatch(value):
        issues.append("contains non URL-safe chars")
    return issues


def validate_trojan_password(password: str) -> str:
    value = str(password or "")
    issues = trojan_password_policy_issues(value)
    if issues:
        raise ValueError("Trojan password policy violation: " + ", ".join(issues))
    return value


def protocol_from_inbound(inbound: dict[str, Any] | None) -> str:
    if not inbound:
        return "vless"
    return str(inbound.get("protocol") or "vless").strip().lower()


def security_from_inbound(inbound: dict[str, Any] | None) -> str:
    if not inbound:
        return ""
    return str(inbound.get("streamSettings", {}).get("security") or "").strip().lower()


def display_security(
    security: str,
    inbound: dict[str, Any] | None,
    connection_entry: dict[str, Any] | None = None,
) -> str:
    raw_security = str(security or "").strip().lower() or security_from_inbound(inbound)
    connection = connection_entry if isinstance(connection_entry, dict) else {}
    connection_security = str(connection.get("security") or "").strip().lower()
    if connection.get("caddy") and connection_security == "tls":
        return "tls/caddy"
    if raw_security in ("", "none") and connection_security:
        return connection_security
    return raw_security


def transport_from_inbound(inbound: dict[str, Any] | None) -> str:
    if not inbound:
        return ""
    return str(connection_transport_settings_from_inbound(inbound).get("transport") or "").strip().lower()


def legacy_credential_from_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    connection_tag = credential_key(entry.get("connection", ""))
    client = entry.get("client") if isinstance(entry.get("client"), dict) else {}
    if not connection_tag or not client:
        return None
    protocol = str(entry.get("protocol") or client.get("protocol") or "").strip().lower()
    if not protocol and client.get("password"):
        protocol = "trojan"
    if not protocol:
        protocol = "vless"
    return {
        "connection": connection_tag,
        "id": client.get("id") or entry.get("id", ""),
        "protocol": protocol,
        "enabled": entry.get("enabled") is not False,
        "created": entry.get("created", ""),
        "client": dict(client),
        "linkMetadata": {},
    }


def normalize_entry_credentials(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    credentials = db_credentials(entry)
    if not credentials:
        legacy = legacy_credential_from_entry(entry)
        if legacy:
            credentials[credential_key(legacy["connection"])] = legacy
    for key, credential in list(credentials.items()):
        if not isinstance(credential, dict):
            credentials.pop(key, None)
            continue
        connection_tag = credential_key(credential.get("connection") or key)
        if connection_tag != key:
            credentials.pop(key, None)
            credentials[connection_tag] = credential
        credential["connection"] = connection_tag
        credential.setdefault("id", credential.get("client", {}).get("id") or "")
        credential.setdefault("protocol", "trojan" if credential.get("client", {}).get("password") else "vless")
        credential.setdefault("enabled", entry.get("enabled") is not False)
        credential.setdefault("created", entry.get("created", ""))
        credential.setdefault("client", {})
        credential.setdefault("linkMetadata", {})
    sync_legacy_fields(entry)
    return credentials


def sorted_credentials(entry: dict[str, Any]) -> list[dict[str, Any]]:
    credentials = normalize_entry_credentials(entry)
    return [credentials[key] for key in sorted(credentials)]


def credential_for_connection(entry: dict[str, Any], connection_tag: str) -> dict[str, Any] | None:
    return normalize_entry_credentials(entry).get(credential_key(connection_tag))


def active_credential_count(entry: dict[str, Any]) -> int:
    return sum(1 for credential in normalize_entry_credentials(entry).values() if credential.get("enabled") is not False)


def total_credential_count(entry: dict[str, Any]) -> int:
    return len(normalize_entry_credentials(entry))


def sync_legacy_fields(entry: dict[str, Any]) -> None:
    credentials = db_credentials(entry)
    primary = None
    if entry.get("connection") in credentials:
        primary = credentials[entry["connection"]]
    if primary is None and credentials:
        primary = credentials[sorted(credentials)[0]]
    if primary is None:
        return
    entry["connection"] = primary.get("connection", "")
    entry["client"] = dict(primary.get("client") or {})
    if primary.get("id"):
        entry["client"].setdefault("id", primary.get("id"))
    entry["protocol"] = primary.get("protocol", "vless")
    entry["enabled"] = any(credential.get("enabled") is not False for credential in credentials.values())


def credential_email(name: str, entry: dict[str, Any], credential: dict[str, Any]) -> str:
    client = credential.get("client") if isinstance(credential.get("client"), dict) else {}
    current = str(client.get("email") or "").strip()
    if current:
        return current
    return email_for_client(
        name,
        str(credential.get("created") or entry.get("created") or ""),
        connection_tag=str(credential.get("connection") or ""),
    )


def xray_client_for_credential(name: str, entry: dict[str, Any], credential: dict[str, Any]) -> dict[str, Any]:
    client = dict(credential.get("client") or {})
    protocol = str(credential.get("protocol") or client.get("protocol") or "").strip().lower()
    if not protocol and client.get("password"):
        protocol = "trojan"
    email = credential_email(name, entry, credential)
    if protocol == "trojan":
        password = str(client.get("password") or "").strip()
        try:
            validate_trojan_password(password)
        except ValueError as exc:
            raise ValueError(f"Trojan credential has invalid password in database: {name}: {exc}") from exc
        return {
            "password": password,
            "email": email,
            "level": int(client.get("level", 0) or 0),
        }
    client.setdefault("id", credential.get("id") or client.get("id") or entry.get("id", ""))
    client.setdefault("level", 0)
    client["email"] = email
    client.pop("protocol", None)
    if not client.get("id"):
        raise ValueError(f"VLESS credential has no UUID in database: {name}")
    return client


def active_items_for_client(config: dict[str, Any], name: str) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for inbound in managed_connection_inbounds(config):
        tag = inbound_tag(inbound)
        for item in clients(inbound):
            if client_name(item) == name:
                rows.append((tag, inbound, item))
    return rows


def active_item_for_connection(
    config: dict[str, Any],
    name: str,
    connection_tag: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    tag = credential_key(connection_tag)
    try:
        inbound = find_inbound_by_tag(config, tag)
    except ValueError:
        return None, None
    for item in clients(inbound):
        if client_name(item) == name:
            return inbound, item
    return inbound, None


def upsert_active_credential(
    config: dict[str, Any],
    name: str,
    entry: dict[str, Any],
    credential: dict[str, Any],
) -> bool:
    connection_tag = credential_key(credential.get("connection", ""))
    inbound = find_inbound_by_tag(config, connection_tag)
    next_client = xray_client_for_credential(name, entry, credential)
    if protocol_from_inbound(inbound) == "vless":
        apply_client_transport(next_client, connection_transport_settings_from_inbound(inbound)["transport"])
    current = clients(inbound)
    replaced = False
    for index, item in enumerate(current):
        if client_name(item) == name:
            current[index] = next_client
            replaced = True
            break
    if not replaced:
        current.append(next_client)
    credential["client"] = dict(next_client)
    if protocol_from_inbound(inbound) == "trojan":
        credential["client"]["id"] = credential.get("id") or entry.get("id", "")
        credential["client"]["protocol"] = "trojan"
    credential["id"] = credential.get("id") or credential["client"].get("id") or entry.get("id", "")
    credential["protocol"] = protocol_from_inbound(inbound)
    credential["security"] = security_from_inbound(inbound)
    credential["transport"] = transport_from_inbound(inbound)
    credential["enabled"] = True
    sync_legacy_fields(entry)
    return True


def remove_active_credential(config: dict[str, Any], name: str, connection_tag: str) -> tuple[bool, dict[str, Any] | None]:
    inbound, item = active_item_for_connection(config, name, connection_tag)
    if inbound is None:
        return False, None
    before = clients(inbound)
    after = [candidate for candidate in before if client_name(candidate) != name]
    if len(after) == len(before):
        return False, None
    from xray_vps_manager.xray.config import set_clients

    set_clients(inbound, after)
    return True, item
