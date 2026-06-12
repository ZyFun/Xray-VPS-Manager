"""Client create/update/delete domain helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Callable

from xray_vps_manager.clients import access
from xray_vps_manager.clients import connections
from xray_vps_manager.clients.models import client_name, db_entry_from_client, normalize_payment_type, split_email
from xray_vps_manager.clients.repository import db_clients, db_connections
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.xray import crypto as xray_crypto
from xray_vps_manager.xray.config import active_client_any, clients, find_inbound_by_tag, inbound_tag, reality_inbounds


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
    for inbound in reality_inbounds(config):
        names.update(client_name(item) for item in clients(inbound) if client_name(item))
    return names


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
) -> AddClientResult:
    connections.ensure_connections(config, db)
    selected_tag = prepare_add_client(config, db, name, connection_tag)
    inbound = find_inbound_by_tag(config, selected_tag)
    current = clients(inbound)

    created = utc_stamp()
    client_id = uuid_factory()
    client = {
        "id": client_id,
        "flow": "xtls-rprx-vision",
        "level": 0,
        "email": f"{name}|created={created}",
    }
    current.append(client)
    entry = db_entry_from_client(client, created=created, enabled=True)
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
    for inbound in reality_inbounds(config):
        before = clients(inbound)
        after = [item for item in before if client_name(item) != name]
        if len(after) != len(before):
            found_active = True
            inbound["settings"]["clients"] = after

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
    inbound["settings"]["clients"] = [client for client in clients(inbound) if client_name(client) != name]

    return DisableClientResult(name=name, connection_tag=connection_tag, disabled_at=disabled_at, entry=entry)
