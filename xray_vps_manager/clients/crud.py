"""Client create/update/delete domain helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Callable

from xray_vps_manager.clients import access
from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients import connections
from xray_vps_manager.clients import limits as client_limits
from xray_vps_manager.clients import status as client_status
from xray_vps_manager.clients.models import (
    client_from_db_entry,
    client_name,
    db_entry_from_client,
    email_for_client,
    normalize_payment_type,
    split_email,
)
from xray_vps_manager.clients.repository import db_clients, db_managed_connections
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.traffic.repository import traffic_entry
from xray_vps_manager.xray import crypto as xray_crypto
from xray_vps_manager.xray.config import (
    active_client_any,
    apply_client_transport,
    clients,
    find_inbound_by_tag,
    inbound_tag,
    connection_transport_settings_from_inbound,
    managed_connection_inbounds,
    set_clients,
)


class EnableTrafficLimitExceeded(ValueError):
    def __init__(self, traffic_status: dict[str, Any]) -> None:
        super().__init__("Traffic limit is exhausted for the current period.")
        self.traffic_status = traffic_status


@dataclass
class AddClientResult:
    name: str
    client_id: str
    credential_id: str
    created: str
    connection_tag: str
    entry: dict[str, Any]
    added_client: bool


@dataclass
class RemoveClientResult:
    name: str
    found_active: bool
    found_in_db: bool


@dataclass
class DisableClientResult:
    name: str
    connection_tag: str
    connection_tags: list[str]
    disabled_at: str
    entry: dict[str, Any]


@dataclass
class EnableClientResult:
    name: str
    client_id: str
    credential_id: str
    connection_tag: str
    connection_tags: list[str]
    entry: dict[str, Any]


@dataclass
class MoveClientResult:
    name: str
    client_id: str
    source_connection_tag: str
    target_connection_tag: str
    enabled: bool
    config_changed: bool
    entry: dict[str, Any]


@dataclass
class RotateTrojanPasswordResult:
    name: str
    client_id: str
    credential_id: str
    connection_tag: str
    entry: dict[str, Any]
    config_changed: bool


def normalize_connection_protocol(protocol: str | None) -> str:
    value = str(protocol or "").strip().lower()
    if not value:
        return ""
    if value not in ("vless", "trojan"):
        raise ValueError("PROTOCOL must be vless or trojan.")
    return value


def resolve_connection_for_add(
    config: dict[str, Any],
    db: dict[str, Any],
    connection_tag: str | None = None,
    *,
    protocol: str | None = None,
) -> str:
    connections.ensure_connections(config, db)
    connection_entries = db_managed_connections(db)
    connection_tags = list(connection_entries)
    requested_protocol = normalize_connection_protocol(protocol)
    if connection_tag and requested_protocol:
        raise ValueError("Use either --connection TAG or --protocol vless|trojan.")
    if connection_tag:
        if connection_tag not in connection_entries:
            raise ValueError(f"Connection not found: {connection_tag}")
        return connection_tag
    if requested_protocol:
        matches = [
            tag
            for tag, entry in connection_entries.items()
            if str(entry.get("protocol") or "vless").strip().lower() == requested_protocol
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"No {requested_protocol} connections found.")
        raise ValueError(f"Multiple {requested_protocol} connections found. Use --connection TAG.")
    if len(connection_tags) == 1:
        return connection_tags[0]
    raise ValueError("Multiple connections found. Use --connection TAG.")


def all_client_names(config: dict[str, Any], db: dict[str, Any]) -> set[str]:
    names = set(db_clients(db))
    for inbound in managed_connection_inbounds(config):
        names.update(client_name(item) for item in clients(inbound) if client_name(item))
    return names


def client_exists(config: dict[str, Any], db: dict[str, Any], name: str) -> bool:
    return name in all_client_names(config, db)


def db_entry_for_existing_client(config: dict[str, Any], db: dict[str, Any], name: str) -> dict[str, Any]:
    entry = db_clients(db).get(name)
    if entry:
        client_credentials.normalize_entry_credentials(entry)
        return entry

    inbound, item = active_client_any(config, name)
    if item is None:
        raise ValueError(f"Client not found: {name}")
    _, created = split_email(item.get("email", ""))
    entry = db_entry_from_client(item, created=created, enabled=True)
    entry["connection"] = inbound_tag(inbound)
    client_credentials.normalize_entry_credentials(entry)
    db_clients(db)[name] = entry
    return entry


def prepare_add_client(
    config: dict[str, Any],
    db: dict[str, Any],
    name: str,
    connection_tag: str | None = None,
    *,
    protocol: str | None = None,
) -> str:
    selected_tag = resolve_connection_for_add(config, db, connection_tag, protocol=protocol)
    if name in all_client_names(config, db):
        entry = db_entry_for_existing_client(config, db, name)
        credentials = client_credentials.normalize_entry_credentials(entry)
        if selected_tag in credentials:
            raise ValueError(f"Client already has credential for connection: {selected_tag}")
        _inbound, active_item = client_credentials.active_item_for_connection(config, name, selected_tag)
        if active_item is not None:
            raise ValueError(f"Client already has active credential for connection: {selected_tag}")
    return selected_tag


def credential_record(
    *,
    name: str,
    inbound: dict[str, Any],
    connection_tag: str,
    client_id: str,
    created: str,
    password: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    protocol = str(inbound.get("protocol") or "vless").strip().lower()
    email = email_for_client(name, created, connection_tag=connection_tag)
    if protocol == "trojan":
        password = client_credentials.validate_trojan_password(password)
        client = {
            "password": password,
            "level": 0,
            "email": email,
        }
        stored_client = dict(client)
        stored_client["id"] = client_id
        stored_client["protocol"] = "trojan"
    else:
        client = {
            "id": client_id,
            "level": 0,
            "email": email,
        }
        apply_client_transport(client, connection_transport_settings_from_inbound(inbound)["transport"])
        stored_client = dict(client)
    credential = {
        "id": client_id,
        "connection": connection_tag,
        "protocol": protocol,
        "security": client_credentials.security_from_inbound(inbound),
        "transport": client_credentials.transport_from_inbound(inbound),
        "enabled": True,
        "created": created,
        "client": stored_client,
        "linkMetadata": {},
    }
    return client, credential


def add_client(
    config: dict[str, Any],
    db: dict[str, Any],
    name: str,
    access_days: int | None = None,
    connection_tag: str | None = None,
    payment_type: str = "free",
    uuid_factory: Callable[[], str] = xray_crypto.xray_uuid,
    password_factory: Callable[[], str] = xray_crypto.random_trojan_password,
) -> AddClientResult:
    connections.ensure_connections(config, db)
    selected_tag = prepare_add_client(config, db, name, connection_tag)
    inbound = find_inbound_by_tag(config, selected_tag)
    created = utc_stamp()
    added_client = name not in all_client_names(config, db)

    if added_client:
        client_id = uuid_factory()
        entry = {
            "id": client_id,
            "created": created,
            "enabled": True,
            "paymentType": normalize_payment_type(payment_type),
            "credentials": {},
        }
        access.set_entry_expiry(entry, access_days)
    else:
        entry = db_entry_for_existing_client(config, db, name)
        client_id = str(entry.get("id") or uuid_factory())
        entry["id"] = client_id
        if payment_type:
            entry["paymentType"] = normalize_payment_type(payment_type)

    credential_id = client_id if added_client else uuid_factory()
    password = password_factory() if str(inbound.get("protocol") or "vless").strip().lower() == "trojan" else ""
    xray_client, credential = credential_record(
        name=name,
        inbound=inbound,
        connection_tag=selected_tag,
        client_id=credential_id,
        created=created,
        password=password,
    )
    should_enable = entry.get("enabled") is not False and not access.access_expired(entry)
    credential["enabled"] = should_enable
    client_credentials.normalize_entry_credentials(entry)
    client_credentials.db_credentials(entry)[selected_tag] = credential
    client_credentials.sync_legacy_fields(entry)
    if should_enable:
        current = clients(inbound)
        current.append(xray_client)
    db_clients(db)[name] = entry

    return AddClientResult(
        name=name,
        client_id=client_id,
        credential_id=credential_id,
        created=created,
        connection_tag=selected_tag,
        entry=entry,
        added_client=added_client,
    )


def remove_client(config: dict[str, Any], db: dict[str, Any], name: str) -> RemoveClientResult:
    connections.ensure_connections(config, db)
    found_active = False
    for inbound in managed_connection_inbounds(config):
        before = clients(inbound)
        after = [item for item in before if client_name(item) != name]
        if len(after) != len(before):
            found_active = True
            set_clients(inbound, after)

    found_in_db = name in db_clients(db)
    if not found_active and not found_in_db:
        raise ValueError(f"Client not found: {name}")
    db_clients(db).pop(name, None)

    return RemoveClientResult(name=name, found_active=found_active, found_in_db=found_in_db)


def disable_client(
    config: dict[str, Any],
    db: dict[str, Any],
    name: str,
    connection_tag: str | None = None,
) -> DisableClientResult:
    connections.ensure_connections(config, db)
    entry = db_entry_for_existing_client(config, db, name)
    credentials = client_credentials.normalize_entry_credentials(entry)
    selected_tags = [connection_tag] if connection_tag else list(credentials)
    if connection_tag and connection_tag not in credentials:
        raise ValueError(f"Client credential not found for connection: {connection_tag}")

    removed_tags: list[str] = []
    for tag in selected_tags:
        removed, item = client_credentials.remove_active_credential(config, name, tag)
        credential = credentials.get(tag)
        if credential is None:
            continue
        if item is not None:
            stored = dict(item)
            if credential.get("protocol") == "trojan":
                stored["id"] = credential.get("id") or entry.get("id", "")
                stored["protocol"] = "trojan"
            credential["client"] = stored
        if removed or credential.get("enabled") is not False:
            removed_tags.append(tag)
        credential["enabled"] = False

    if not removed_tags:
        if entry.get("enabled") is False:
            raise ValueError(f"Client already disabled: {name}")
        raise ValueError(f"Enabled client not found: {name}")

    entry["enabled"] = any(credential.get("enabled") is not False for credential in credentials.values())
    disabled_at = utc_stamp()
    if not entry["enabled"]:
        entry["disabledAt"] = disabled_at
    db_clients(db)[name] = entry
    client_credentials.sync_legacy_fields(entry)

    return DisableClientResult(
        name=name,
        connection_tag=removed_tags[0] if len(removed_tags) == 1 else "",
        connection_tags=removed_tags,
        disabled_at=disabled_at,
        entry=entry,
    )


def enable_client(
    config: dict[str, Any],
    db: dict[str, Any],
    traffic_db: dict[str, Any],
    name: str,
    connection_tag: str | None = None,
) -> EnableClientResult:
    connections.ensure_connections(config, db)
    entry = db_clients(db).get(name)
    if not entry:
        raise ValueError(f"Client not found: {name}")
    if access.access_expired(entry):
        raise ValueError(f"Access expired for client: {name}. Extend it first: xray-client extend-days {name} DAYS")

    traffic_status = client_limits.traffic_limit_status(entry, traffic_entry(traffic_db, name))
    if traffic_status and traffic_status["exceeded"]:
        raise EnableTrafficLimitExceeded(traffic_status)

    credentials = client_credentials.normalize_entry_credentials(entry)
    selected_tags = [connection_tag] if connection_tag else list(credentials)
    if connection_tag and connection_tag not in credentials:
        raise ValueError(f"Client credential not found for connection: {connection_tag}")

    enabled_tags: list[str] = []
    for tag in selected_tags:
        credential = credentials[tag]
        _inbound, active_item = client_credentials.active_item_for_connection(config, name, tag)
        if active_item is not None and credential.get("enabled") is not False:
            continue
        client_credentials.upsert_active_credential(config, name, entry, credential)
        enabled_tags.append(tag)

    if not enabled_tags:
        raise ValueError(f"Client already enabled: {name}")

    entry["enabled"] = True
    client_status.clear_disabled_state(entry)
    client_credentials.sync_legacy_fields(entry)
    db_clients(db)[name] = entry

    return EnableClientResult(
        name=name,
        client_id=str(entry.get("id") or ""),
        credential_id=str(credentials[enabled_tags[0]].get("id") or entry.get("id") or ""),
        connection_tag=enabled_tags[0] if len(enabled_tags) == 1 else "",
        connection_tags=enabled_tags,
        entry=entry,
    )


def trojan_credential_tags(config: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    credentials = client_credentials.normalize_entry_credentials(entry)
    tags = []
    for tag, credential in credentials.items():
        inbound = find_inbound_by_tag(config, tag)
        protocol = str(credential.get("protocol") or inbound.get("protocol") or "").strip().lower()
        if protocol == "trojan":
            tags.append(tag)
    return tags


def resolve_trojan_credential_tag(
    config: dict[str, Any],
    entry: dict[str, Any],
    connection_tag: str | None = None,
) -> str:
    credentials = client_credentials.normalize_entry_credentials(entry)
    if connection_tag:
        if connection_tag not in credentials:
            raise ValueError(f"Client credential not found for connection: {connection_tag}")
        inbound = find_inbound_by_tag(config, connection_tag)
        protocol = str(credentials[connection_tag].get("protocol") or inbound.get("protocol") or "").strip().lower()
        if protocol != "trojan":
            raise ValueError(f"Credential is not Trojan: {connection_tag}")
        return connection_tag

    tags = trojan_credential_tags(config, entry)
    if not tags:
        raise ValueError("Client has no Trojan credentials.")
    if len(tags) > 1:
        raise ValueError("Client has multiple Trojan credentials. Use --connection TAG.")
    return tags[0]


def rotate_trojan_password(
    config: dict[str, Any],
    db: dict[str, Any],
    name: str,
    connection_tag: str | None = None,
    password_factory: Callable[[], str] = xray_crypto.random_trojan_password,
) -> RotateTrojanPasswordResult:
    connections.ensure_connections(config, db)
    entry = db_entry_for_existing_client(config, db, name)
    selected_tag = resolve_trojan_credential_tag(config, entry, connection_tag)
    credentials = client_credentials.normalize_entry_credentials(entry)
    credential = credentials[selected_tag]
    password = client_credentials.validate_trojan_password(password_factory())

    client = dict(credential.get("client") or {})
    client["password"] = password
    client["protocol"] = "trojan"
    client.setdefault("id", credential.get("id") or entry.get("id", ""))
    credential["client"] = client
    credential["protocol"] = "trojan"
    credential["enabled"] = credential.get("enabled") is not False

    _inbound, active_item = client_credentials.active_item_for_connection(config, name, selected_tag)
    config_changed = False
    if active_item is not None:
        active_item["password"] = password
        config_changed = True

    client_credentials.sync_legacy_fields(entry)
    db_clients(db)[name] = entry

    return RotateTrojanPasswordResult(
        name=name,
        client_id=str(entry.get("id") or ""),
        credential_id=str(credential.get("id") or entry.get("id") or ""),
        connection_tag=selected_tag,
        entry=entry,
        config_changed=config_changed,
    )


def trojan_password_policy_rows(config: dict[str, Any], db: dict[str, Any]) -> list[dict[str, str]]:
    connections.ensure_connections(config, db)
    rows: list[dict[str, str]] = []
    for name, entry in db_clients(db).items():
        credentials = client_credentials.normalize_entry_credentials(entry)
        for tag, credential in credentials.items():
            try:
                inbound = find_inbound_by_tag(config, tag)
                inbound_protocol = str(inbound.get("protocol") or "").strip().lower()
            except ValueError:
                inbound = None
                inbound_protocol = ""
            protocol = str(credential.get("protocol") or inbound_protocol or "").strip().lower()
            if protocol != "trojan":
                continue
            client = credential.get("client") if isinstance(credential.get("client"), dict) else {}
            password = str(client.get("password") or "")
            issues = client_credentials.trojan_password_policy_issues(password)
            if inbound is None:
                issues.append("connection inbound is missing")
            else:
                _active_inbound, active_item = client_credentials.active_item_for_connection(config, name, tag)
                if active_item is not None and active_item.get("password") != password:
                    issues.append("active config password differs from SQLite")
            rows.append(
                {
                    "client": name,
                    "connection": tag,
                    "status": "FAIL" if issues else "OK",
                    "issues": ", ".join(issues) if issues else "-",
                }
            )
    return rows


def move_client_to_connection(
    config: dict[str, Any],
    db: dict[str, Any],
    name: str,
    target_connection_identifier: str,
) -> MoveClientResult:
    connections.ensure_connections(config, db)
    target_tag = connections.resolve_connection_identifier(config, db, target_connection_identifier)
    target_inbound = find_inbound_by_tag(config, target_tag)
    target_protocol = target_inbound.get("protocol") or "vless"
    target_transport = connection_transport_settings_from_inbound(target_inbound)["transport"]
    entry = db_entry_for_existing_client(config, db, name)
    entry_credentials = client_credentials.normalize_entry_credentials(entry)
    if len(entry_credentials) > 1:
        raise ValueError("Client has multiple credentials. Add a new credential or use --connection actions instead.")
    if target_tag in entry_credentials:
        raise ValueError(f"Client already has credential for connection: {target_tag}")

    source_inbound, active_item = active_client_any(config, name)
    source_tag = inbound_tag(source_inbound) if source_inbound is not None else str(entry.get("connection") or "")
    if not source_tag:
        raise ValueError(f"Client connection not found: {name}")
    if source_tag == target_tag:
        raise ValueError(f"Client is already in connection: {target_tag}")
    source_inbound = source_inbound or find_inbound_by_tag(config, source_tag)
    source_protocol = source_inbound.get("protocol") or entry.get("protocol") or "vless"
    if source_protocol != target_protocol:
        raise ValueError("Moving clients between VLESS and Trojan connections is not supported yet.")

    entry = dict(entry)
    config_changed = False
    enabled = active_item is not None
    moved_created = entry.get("created", "")
    if active_item is not None:
        if any(client_name(item) == name for item in clients(target_inbound)):
            raise ValueError(f"Target connection already has client: {name}")
        moved_client = dict(active_item)
        if target_protocol == "vless":
            apply_client_transport(moved_client, target_transport)
        moved_client["email"] = email_for_client(name, moved_created, connection_tag=target_tag)
        set_clients(source_inbound, [
            item for item in clients(source_inbound) if client_name(item) != name
        ])
        clients(target_inbound).append(moved_client)
        entry["enabled"] = True
        config_changed = True
    else:
        if entry.get("enabled") is not False:
            raise ValueError(f"Enabled client config not found: {name}")
        moved_client = client_from_db_entry(name, entry)
        if target_protocol == "vless":
            apply_client_transport(moved_client, target_transport)
        moved_client["email"] = email_for_client(name, moved_created, connection_tag=target_tag)

    entry["id"] = moved_client.get("id") or entry.get("id", "")
    entry["client"] = dict(moved_client)
    if target_protocol == "trojan":
        entry["client"]["id"] = entry["id"]
        entry["client"]["protocol"] = "trojan"
        entry["protocol"] = "trojan"
    entry["connection"] = target_tag
    credentials = client_credentials.normalize_entry_credentials(entry)
    credential = credentials.pop(source_tag, None)
    if credential is None:
        credential = client_credentials.legacy_credential_from_entry(entry) or {}
    credential["connection"] = target_tag
    credential["protocol"] = target_protocol
    credential["security"] = client_credentials.security_from_inbound(target_inbound)
    credential["transport"] = client_credentials.transport_from_inbound(target_inbound)
    credential["enabled"] = enabled
    credential["client"] = dict(moved_client)
    if target_protocol == "trojan":
        credential["client"]["id"] = entry.get("id", "")
        credential["client"]["protocol"] = "trojan"
    credential["id"] = entry.get("id") or credential.get("id") or credential["client"].get("id", "")
    credentials[target_tag] = credential
    client_credentials.sync_legacy_fields(entry)
    db_clients(db)[name] = entry

    return MoveClientResult(
        name=name,
        client_id=str(entry.get("id") or moved_client.get("id") or ""),
        source_connection_tag=source_tag,
        target_connection_tag=target_tag,
        enabled=enabled,
        config_changed=config_changed,
        entry=entry,
    )
