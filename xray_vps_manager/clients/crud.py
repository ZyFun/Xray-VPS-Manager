"""Client create/update/delete domain helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Callable

from xray_vps_manager.clients import access
from xray_vps_manager.clients import connections
from xray_vps_manager.clients import limits as client_limits
from xray_vps_manager.clients import status as client_status
from xray_vps_manager.clients.models import client_from_db_entry, client_name, db_entry_from_client, normalize_payment_type, split_email
from xray_vps_manager.clients.repository import db_clients, db_connections
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
    created: str
    connection_tag: str
    entry: dict[str, Any]


@dataclass
class RemoveClientResult:
    name: str
    found_active: bool
    found_in_db: bool


@dataclass
class DisableClientResult:
    name: str
    connection_tag: str
    disabled_at: str
    entry: dict[str, Any]


@dataclass
class EnableClientResult:
    name: str
    client_id: str
    connection_tag: str
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


def resolve_connection_for_add(config: dict[str, Any], db: dict[str, Any], connection_tag: str | None = None) -> str:
    connections.ensure_connections(config, db)
    connection_tags = list(db_connections(db))
    if connection_tag:
        if connection_tag not in db_connections(db):
            raise ValueError(f"Connection not found: {connection_tag}")
        return connection_tag
    if len(connection_tags) == 1:
        return connection_tags[0]
    raise ValueError("Multiple connections found. Use --connection TAG.")


def all_client_names(config: dict[str, Any], db: dict[str, Any]) -> set[str]:
    names = set(db_clients(db))
    for inbound in managed_connection_inbounds(config):
        names.update(client_name(item) for item in clients(inbound) if client_name(item))
    return names


def db_entry_for_existing_client(config: dict[str, Any], db: dict[str, Any], name: str) -> dict[str, Any]:
    entry = db_clients(db).get(name)
    if entry:
        return entry

    inbound, item = active_client_any(config, name)
    if item is None:
        raise ValueError(f"Client not found: {name}")
    _, created = split_email(item.get("email", ""))
    entry = db_entry_from_client(item, created=created, enabled=True)
    entry["connection"] = inbound_tag(inbound)
    db_clients(db)[name] = entry
    return entry


def prepare_add_client(config: dict[str, Any], db: dict[str, Any], name: str, connection_tag: str | None = None) -> str:
    selected_tag = resolve_connection_for_add(config, db, connection_tag)
    if name in all_client_names(config, db):
        raise ValueError(f"Client already exists: {name}")
    return selected_tag


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
    current = clients(inbound)

    created = utc_stamp()
    client_id = uuid_factory()
    protocol = inbound.get("protocol") or "vless"
    if protocol == "trojan":
        password = password_factory()
        client = {
            "password": password,
            "level": 0,
            "email": f"{name}|created={created}",
        }
        stored_client = dict(client)
        stored_client["id"] = client_id
        stored_client["protocol"] = "trojan"
    else:
        client = {
            "id": client_id,
            "level": 0,
            "email": f"{name}|created={created}",
        }
        apply_client_transport(client, connection_transport_settings_from_inbound(inbound)["transport"])
        stored_client = dict(client)
    current.append(client)
    entry = db_entry_from_client(stored_client, created=created, enabled=True)
    entry["connection"] = selected_tag
    entry["paymentType"] = normalize_payment_type(payment_type)
    access.set_entry_expiry(entry, access_days)
    db_clients(db)[name] = entry

    return AddClientResult(
        name=name,
        client_id=client_id,
        created=created,
        connection_tag=selected_tag,
        entry=entry,
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


def disable_client(config: dict[str, Any], db: dict[str, Any], name: str) -> DisableClientResult:
    connections.ensure_connections(config, db)
    inbound, item = active_client_any(config, name)
    if item is None:
        if name in db_clients(db) and db_clients(db)[name].get("enabled") is False:
            raise ValueError(f"Client already disabled: {name}")
        raise ValueError(f"Enabled client not found: {name}")

    _, created = split_email(item.get("email", ""))
    previous = db_clients(db).get(name, {})
    if name in db_clients(db):
        created = previous.get("created", created)
    entry = db_entry_from_client(item, created=created, enabled=False, previous=previous)
    connection_tag = previous.get("connection") or inbound_tag(inbound)
    entry["connection"] = connection_tag
    disabled_at = utc_stamp()
    entry["disabledAt"] = disabled_at
    db_clients(db)[name] = entry
    set_clients(inbound, [client for client in clients(inbound) if client_name(client) != name])

    return DisableClientResult(name=name, connection_tag=connection_tag, disabled_at=disabled_at, entry=entry)


def enable_client(config: dict[str, Any], db: dict[str, Any], traffic_db: dict[str, Any], name: str) -> EnableClientResult:
    connections.ensure_connections(config, db)
    if active_client_any(config, name)[1] is not None:
        raise ValueError(f"Client already enabled: {name}")

    entry = db_clients(db).get(name)
    if not entry:
        raise ValueError(f"Client not found: {name}")
    if access.access_expired(entry):
        raise ValueError(f"Access expired for client: {name}. Extend it first: xray-client extend-days {name} DAYS")

    traffic_status = client_limits.traffic_limit_status(entry, traffic_entry(traffic_db, name))
    if traffic_status and traffic_status["exceeded"]:
        raise EnableTrafficLimitExceeded(traffic_status)

    client_status.enable_db_client(config, name, entry)
    client = entry["client"]
    connection_tag = entry["connection"]
    entry["enabled"] = True
    client_status.clear_disabled_state(entry)
    db_clients(db)[name] = entry

    return EnableClientResult(
        name=name,
        client_id=str(entry.get("id") or client.get("id") or ""),
        connection_tag=connection_tag,
        entry=entry,
    )


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
    if active_item is not None:
        if any(client_name(item) == name for item in clients(target_inbound)):
            raise ValueError(f"Target connection already has client: {name}")
        moved_client = dict(active_item)
        if target_protocol == "vless":
            apply_client_transport(moved_client, target_transport)
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

    entry["id"] = moved_client.get("id") or entry.get("id", "")
    entry["client"] = dict(moved_client)
    if target_protocol == "trojan":
        entry["client"]["id"] = entry["id"]
        entry["client"]["protocol"] = "trojan"
        entry["protocol"] = "trojan"
    entry["connection"] = target_tag
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
