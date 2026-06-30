"""Client enabled/disabled status helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients import connections
from xray_vps_manager.clients.access import access_expired, extend_entry_expiry, local_now, set_entry_expiry
from xray_vps_manager.clients.limits import traffic_limit_status
from xray_vps_manager.clients.models import client_from_db_entry, client_name, db_entry_from_client, split_email
from xray_vps_manager.clients.repository import db_clients
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.traffic.repository import traffic_entry
from xray_vps_manager.xray.config import (
    active_client_any,
    apply_client_transport,
    clients,
    connection_transport_settings_from_inbound,
    default_connection_tag,
    find_inbound_by_tag,
    inbound_tag,
    managed_connection_inbounds,
    set_clients,
)


@dataclass
class AccessUpdateResult:
    entry: dict[str, Any]
    config_changed: bool
    status: str
    traffic_status: dict[str, Any] | None


@dataclass
class TrafficLimitEnforcementResult:
    reactivated_names: list[str]
    due_names: list[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.reactivated_names or self.due_names)


@dataclass
class ExpireDueResult:
    due_names: list[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.due_names)


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
    changed = False
    for credential in client_credentials.sorted_credentials(entry):
        tag = credential.get("connection") or entry.get("connection") or default_connection_tag(config)
        _inbound, active_item = client_credentials.active_item_for_connection(config, name, tag)
        if active_item is not None and credential.get("enabled") is not False:
            continue
        client_credentials.upsert_active_credential(config, name, entry, credential)
        changed = True
    if changed:
        entry["enabled"] = True
        client_credentials.sync_legacy_fields(entry)
    return changed


def remove_active_client(config: dict[str, Any], name: str) -> tuple[bool, str, dict[str, Any] | None]:
    removed_tag = ""
    removed_item = None
    changed = False
    for inbound in managed_connection_inbounds(config):
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
            set_clients(inbound, after)
    return changed, removed_tag, removed_item


def disabled_entry_for_policy(
    config: dict[str, Any],
    name: str,
    entry: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    disabled = dict(entry)
    disabled["credentials"] = {
        tag: dict(credential)
        for tag, credential in client_credentials.normalize_entry_credentials(entry).items()
    }
    changed = False
    for tag, credential in disabled["credentials"].items():
        removed, item = client_credentials.remove_active_credential(config, name, tag)
        changed = changed or removed
        if item is not None:
            stored = dict(item)
            if credential.get("protocol") == "trojan":
                stored["id"] = credential.get("id") or disabled.get("id", "")
                stored["protocol"] = "trojan"
            credential["client"] = stored
        credential["enabled"] = False
    disabled["enabled"] = False
    client_credentials.sync_legacy_fields(disabled)
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


def extend_access_days(
    config: dict[str, Any],
    db: dict[str, Any],
    traffic_db: dict[str, Any],
    name: str,
    days: int,
) -> AccessUpdateResult:
    return apply_access_update(config, db, traffic_db, name, lambda entry: extend_entry_expiry(entry, days))


def enforce_traffic_limits(
    config: dict[str, Any],
    db: dict[str, Any],
    traffic_db: dict[str, Any],
    now: datetime | None = None,
    stamp: str | None = None,
) -> TrafficLimitEnforcementResult:
    now = now or local_now()
    stamp = stamp or utc_stamp()
    reactivated_names: list[str] = []
    due_names: list[str] = []
    due_clients: dict[str, tuple[str, dict[str, Any]]] = {}
    due_statuses: dict[str, dict[str, Any]] = {}

    for name, entry in db_clients(db).items():
        if entry.get("enabled") is not False or entry.get("disabledReason") != "traffic-limit":
            continue
        status = traffic_limit_status(entry, traffic_entry(traffic_db, name), now)
        if not status or status["exceeded"]:
            continue
        exceeded_period = entry.get("trafficLimitExceededPeriod", "")
        if exceeded_period and exceeded_period == status["periodKey"]:
            continue
        if access_expired(entry, now):
            continue

        enable_db_client(config, name, entry)
        entry["enabled"] = True
        clear_disabled_state(entry)
        db_clients(db)[name] = entry
        reactivated_names.append(name)

    for inbound in managed_connection_inbounds(config):
        for item in clients(inbound):
            name = client_name(item)
            if name in due_statuses:
                continue
            entry = db_clients(db).get(name, {})
            status = traffic_limit_status(entry, traffic_entry(traffic_db, name), now)
            if status and status["exceeded"]:
                due_names.append(name)
                due_clients[name] = (inbound_tag(inbound), item)
                due_statuses[name] = status

    for name in due_names:
        status = due_statuses[name]
        previous = db_clients(db).get(name, {})
        if not previous:
            tag, item = due_clients[name]
            _, created = split_email(item.get("email", ""))
            previous = db_entry_from_client(item, created=created, enabled=True)
            previous["connection"] = tag
        entry, _changed = disabled_entry_for_policy(config, name, previous)
        entry["disabledAt"] = stamp
        entry["disabledReason"] = "traffic-limit"
        entry["trafficLimitExceededAt"] = stamp
        entry["trafficLimitExceededPeriod"] = status["periodKey"]
        entry["trafficLimitExceededBytes"] = status["usedBytes"]
        entry["trafficLimitResetAt"] = status["resetAt"]
        db_clients(db)[name] = entry

    return TrafficLimitEnforcementResult(
        reactivated_names=reactivated_names,
        due_names=due_names,
    )


def expire_due_clients(
    config: dict[str, Any],
    db: dict[str, Any],
    now: datetime | None = None,
    stamp: str | None = None,
) -> ExpireDueResult:
    now = now or local_now()
    stamp = stamp or utc_stamp()
    due_names: list[str] = []
    due_clients: dict[str, tuple[str, dict[str, Any]]] = {}

    for inbound in managed_connection_inbounds(config):
        for item in clients(inbound):
            name = client_name(item)
            if name in due_clients:
                continue
            entry = db_clients(db).get(name, {})
            if access_expired(entry, now):
                due_names.append(name)
                due_clients[name] = (inbound_tag(inbound), item)

    for name in due_names:
        previous = db_clients(db).get(name, {})
        if not previous:
            tag, item = due_clients[name]
            _, created = split_email(item.get("email", ""))
            previous = db_entry_from_client(item, created=created, enabled=True)
            previous["connection"] = tag
        entry, _changed = disabled_entry_for_policy(config, name, previous)
        entry["disabledAt"] = stamp
        entry["expiredAt"] = stamp
        entry["disabledReason"] = "expired"
        db_clients(db)[name] = entry

    return ExpireDueResult(due_names=due_names)
