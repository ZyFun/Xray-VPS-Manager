"""Client enabled/disabled status helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from xray_vps_manager.clients import connections
from xray_vps_manager.clients.access import access_expired, local_now, set_entry_expiry
from xray_vps_manager.clients.limits import traffic_limit_status
from xray_vps_manager.clients.models import client_from_db_entry, client_name, db_entry_from_client, split_email
from xray_vps_manager.clients.repository import db_clients
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.traffic.repository import traffic_entry
from xray_vps_manager.xray.config import (
    active_client_any,
    clients,
    default_connection_tag,
    find_inbound_by_tag,
    inbound_tag,
    reality_inbounds,
)


@dataclass
class AccessUpdateResult:
    entry: dict[str, Any]
    config_changed: bool
    status: str
    traffic_status: dict[str, Any] | None


def clear_disabled_state(entry: dict[str, Any]) -> None:
    entry.pop("disabledAt", None)
    entry.pop("disabledReason", None)
    entry.pop("expiredAt", None)
    entry.pop("trafficLimitExceededAt", None)
    entry.pop("trafficLimitExceededPeriod", None)
    entry.pop("trafficLimitExceededBytes", None)
    entry.pop("trafficLimitResetAt", None)


def clear_traffic_limit_exceeded_state(entry: dict[str, Any]) -> None:
    entry.pop("trafficLimitExceededAt", None)
    entry.pop("trafficLimitExceededPeriod", None)
    entry.pop("trafficLimitExceededBytes", None)
    entry.pop("trafficLimitResetAt", None)


def enable_db_client(config: dict[str, Any], name: str, entry: dict[str, Any]) -> bool:
    if active_client_any(config, name)[1] is not None:
        return False

    client = client_from_db_entry(name, entry)
    connection_tag = entry.get("connection") or default_connection_tag(config)
    inbound = find_inbound_by_tag(config, connection_tag)
    clients(inbound).append(client)
    entry["client"] = client
    entry["connection"] = connection_tag
    return True


def remove_active_client(config: dict[str, Any], name: str) -> tuple[bool, str, dict[str, Any] | None]:
    removed_tag = ""
    removed_item = None
    changed = False
    for inbound in reality_inbounds(config):
        before = clients(inbound)
        after = []
        for item in before:
            if client_name(item) == name:
                if removed_item is None:
                    removed_tag = inbound_tag(inbound)
                    removed_item = item
                changed = True
                continue
            after.append(item)
        if len(after) != len(before):
            inbound["settings"]["clients"] = after
    return changed, removed_tag, removed_item


def disabled_entry_for_policy(
    config: dict[str, Any],
    name: str,
    entry: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    changed, tag, item = remove_active_client(config, name)
    if item is None:
        disabled = dict(entry)
        disabled["enabled"] = False
        return disabled, changed

    _, created = split_email(item.get("email", ""))
    created = entry.get("created", created)
    disabled = db_entry_from_client(item, created=created, enabled=False, previous=entry)
    disabled["connection"] = entry.get("connection") or tag
    return disabled, changed


def reconcile_client_access_status(
    config: dict[str, Any],
    db: dict[str, Any],
    traffic_db: dict[str, Any],
    name: str,
    entry: dict[str, Any],
    now: datetime | None = None,
):
    now = now or local_now()
    stamp = utc_stamp()
    status = traffic_limit_status(entry, traffic_entry(traffic_db, name), now)

    if status and status["exceeded"]:
        disabled, changed = disabled_entry_for_policy(config, name, entry)
        disabled["disabledAt"] = stamp
        disabled["disabledReason"] = "traffic-limit"
        disabled["trafficLimitExceededAt"] = stamp
        disabled["trafficLimitExceededPeriod"] = status["periodKey"]
        disabled["trafficLimitExceededBytes"] = status["usedBytes"]
        disabled["trafficLimitResetAt"] = status["resetAt"]
        disabled.pop("expiredAt", None)
        db_clients(db)[name] = disabled
        return disabled, changed, "disabled-traffic-limit", status

    if access_expired(entry, now):
        disabled, changed = disabled_entry_for_policy(config, name, entry)
        disabled["disabledAt"] = stamp
        disabled["expiredAt"] = stamp
        disabled["disabledReason"] = "expired"
        clear_traffic_limit_exceeded_state(disabled)
        db_clients(db)[name] = disabled
        return disabled, changed, "disabled-expired", None

    changed = enable_db_client(config, name, entry)
    entry["enabled"] = True
    clear_disabled_state(entry)
    db_clients(db)[name] = entry
    return entry, changed, "enabled", None


def apply_access_update(
    config: dict[str, Any],
    db: dict[str, Any],
    traffic_db: dict[str, Any],
    name: str,
    update_entry: Callable[[dict[str, Any]], None],
) -> AccessUpdateResult:
    connections.ensure_connections(config, db)
    entry = db_clients(db).get(name)

    if not entry:
        inbound, item = active_client_any(config, name)
        if item is None:
            raise ValueError(f"Client not found: {name}")
        _, created = split_email(item.get("email", ""))
        entry = db_entry_from_client(item, created=created, enabled=True)
        entry["connection"] = inbound_tag(inbound)

    update_entry(entry)
    entry, config_changed, status, traffic_status = reconcile_client_access_status(config, db, traffic_db, name, entry)
    return AccessUpdateResult(
        entry=entry,
        config_changed=config_changed,
        status=status,
        traffic_status=traffic_status,
    )


def set_access_days(
    config: dict[str, Any],
    db: dict[str, Any],
    traffic_db: dict[str, Any],
    name: str,
    days: int | None,
) -> AccessUpdateResult:
    return apply_access_update(config, db, traffic_db, name, lambda entry: set_entry_expiry(entry, days))
