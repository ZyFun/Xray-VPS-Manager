"""Client listing helpers."""

from __future__ import annotations

from typing import Any

from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients.connections import ensure_connections
from xray_vps_manager.clients.models import db_entry_from_client, split_email
from xray_vps_manager.clients.payments import payment_type_label
from xray_vps_manager.clients.repository import db_clients
from xray_vps_manager.xray import client_routes
from xray_vps_manager.xray.config import clients, default_connection_tag, inbound_tag, managed_connection_inbounds


def client_rows(config: dict[str, Any], db: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ensure_connections(config, db)
    active_by_name: dict[str, set[str]] = {}
    for inbound in managed_connection_inbounds(config):
        tag = inbound_tag(inbound)
        for item in clients(inbound):
            raw_email = item.get("email", "")
            name, created = split_email(raw_email)
            if not name:
                continue
            active_by_name.setdefault(name, set()).add(tag)
            if name not in db_clients(db):
                entry = db_entry_from_client(item, created=created, enabled=True)
                entry["connection"] = tag
                db_clients(db)[name] = entry

    for name, entry in db_clients(db).items():
        credentials = client_credentials.normalize_entry_credentials(entry)
        active_tags = active_by_name.get(name, set())
        total = client_credentials.total_credential_count(entry)
        active = len([tag for tag in credentials if tag in active_tags or credentials[tag].get("enabled") is not False])
        status = "enabled" if active_tags else ("disabled" if entry.get("enabled") is False else "missing")
        rows.append(
            {
                "name": name,
                "created": entry.get("created") or "unknown",
                "id": entry.get("id", ""),
                "email": entry.get("client", {}).get("email", name),
                "status": status,
                "paymentType": payment_type_label(entry),
                "expiresAt": entry.get("expiresAt", ""),
                "connection": entry.get("connection") or default_connection_tag(config),
                "cascade": client_routes.selected_route_label(db, entry),
                "credentialsActive": active,
                "credentialsTotal": total,
            }
        )
    return rows


def credential_rows(config: dict[str, Any], db: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ensure_connections(config, db)
    active: dict[tuple[str, str], dict[str, Any]] = {}
    inbound_by_tag = {inbound_tag(inbound): inbound for inbound in managed_connection_inbounds(config)}
    for tag, inbound in inbound_by_tag.items():
        for item in clients(inbound):
            name, _created = split_email(item.get("email", ""))
            if name:
                active[(name, tag)] = item

    for name, entry in db_clients(db).items():
        credentials = client_credentials.normalize_entry_credentials(entry)
        for tag, credential in credentials.items():
            inbound = inbound_by_tag.get(tag)
            active_item = active.get((name, tag))
            protocol = credential.get("protocol") or client_credentials.protocol_from_inbound(inbound)
            status = "enabled" if active_item is not None else ("disabled" if credential.get("enabled") is False else "missing")
            rows.append(
                {
                    "name": name,
                    "client": name,
                    "id": credential.get("id") or (credential.get("client") or {}).get("id") or entry.get("id", ""),
                    "email": client_credentials.credential_email(name, entry, credential),
                    "protocol": str(protocol or "vless").lower(),
                    "security": credential.get("security") or client_credentials.security_from_inbound(inbound),
                    "transport": credential.get("transport") or client_credentials.transport_from_inbound(inbound),
                    "connection": tag,
                    "status": status,
                    "created": credential.get("created") or entry.get("created") or "unknown",
                    "expiresAt": entry.get("expiresAt", ""),
                }
            )

    for (name, tag), item in active.items():
        if name in db_clients(db) and tag in client_credentials.normalize_entry_credentials(db_clients(db)[name]):
            continue
        inbound = inbound_by_tag.get(tag)
        raw_email = item.get("email", "")
        _name, created = split_email(raw_email)
        rows.append(
            {
                "name": name,
                "client": name,
                "id": item.get("id", ""),
                "email": raw_email,
                "protocol": client_credentials.protocol_from_inbound(inbound),
                "security": client_credentials.security_from_inbound(inbound),
                "transport": client_credentials.transport_from_inbound(inbound),
                "connection": tag,
                "status": "enabled",
                "created": created or "unknown",
                "expiresAt": "",
            }
        )
    return sorted(rows, key=lambda row: (row["client"], row["connection"]))
