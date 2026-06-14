"""Client listing helpers."""

from __future__ import annotations

from typing import Any

from xray_vps_manager.clients.connections import ensure_connections
from xray_vps_manager.clients.models import split_email
from xray_vps_manager.clients.payments import payment_type_label
from xray_vps_manager.clients.repository import db_clients
from xray_vps_manager.xray import client_routes
from xray_vps_manager.xray.config import clients, default_connection_tag, inbound_tag, reality_inbounds


def client_rows(config: dict[str, Any], db: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    ensure_connections(config, db)
    for inbound in reality_inbounds(config):
        tag = inbound_tag(inbound)
        for item in clients(inbound):
            raw_email = item.get("email", "")
            name, created = split_email(raw_email)
            entry = db_clients(db).get(name, {})
            if name in db_clients(db):
                created = entry.get("created", created)
            rows.append(
                {
                    "name": name or "(no-name)",
                    "created": created or "unknown",
                    "id": item.get("id", ""),
                    "email": raw_email,
                    "status": "enabled",
                    "paymentType": payment_type_label(entry),
                    "expiresAt": entry.get("expiresAt", ""),
                    "connection": entry.get("connection") or tag,
                    "cascade": client_routes.selected_route_label(db, entry),
                }
            )
            seen.add(name)

    for name, entry in db_clients(db).items():
        if name in seen:
            continue
        rows.append(
            {
                "name": name,
                "created": entry.get("created") or "unknown",
                "id": entry.get("id", ""),
                "email": entry.get("client", {}).get("email", name),
                "status": "disabled" if entry.get("enabled") is False else "missing",
                "paymentType": payment_type_label(entry),
                "expiresAt": entry.get("expiresAt", ""),
                "connection": entry.get("connection") or default_connection_tag(config),
                "cascade": client_routes.selected_route_label(db, entry),
            }
        )
    return rows
