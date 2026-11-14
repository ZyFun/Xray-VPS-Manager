"""Client enabled/disabled status helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import json
from typing import Any

from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients import connections
from xray_vps_manager.clients.access import access_expired, local_now
from xray_vps_manager.clients.models import client_from_db_entry, client_name, db_entry_from_client, split_email
from xray_vps_manager.clients.repository import db_clients
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.db.traffic_snapshot import traffic_entry
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
    expired_during_recovery: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.reactivated_names or self.due_names or self.expired_during_recovery)


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
    traffic_entry: dict[str, Any],
    name: str,
    entry: dict[str, Any],
    now: datetime | None = None,
    *,
    limit_status: Callable,
):
    now = now or local_now()
    stamp = utc_stamp()
    status = limit_status(entry, traffic_entry, now)

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
    traffic_entry: dict[str, Any],
    name: str,
    update_entry: Callable[[dict[str, Any]], None],
    *,
    limit_status: Callable,
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
    entry, config_changed, status, traffic_status = reconcile_client_access_status(
        config, db, traffic_entry, name, entry, limit_status=limit_status,
    )
    return AccessUpdateResult(
        entry=entry,
        config_changed=config_changed,
        status=status,
        traffic_status=traffic_status,
    )


def enforce_traffic_limits(
    config: dict[str, Any],
    db: dict[str, Any],
    traffic_db: dict[str, Any],
    now: datetime | None = None,
    stamp: str | None = None,
    *,
    limit_status: Callable,
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
        status = limit_status(entry, traffic_entry(traffic_db, name), now)
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
            status = limit_status(entry, traffic_entry(traffic_db, name), now)
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


def reactivation_fingerprints(entry, *, tags=None):
    """Сверяет каждое подключение с состоянием на момент записи маркера."""
    client = (entry.get("id"), entry.get("created"))
    return {
        tag: sha256(json.dumps(
            (*client, tag, item.get("id"), item.get("protocol"), item.get("enabled")),
            ensure_ascii=False,
        ).encode()).hexdigest()
        for tag, item in client_credentials.normalize_entry_credentials(entry).items()
        if tags is None or tag in tags
    }


@dataclass
class LimitReactivationRetry:
    enabled: list[str]
    exceeded: list[str]
    expired: list[str]
    config_changed: bool
    credentials: dict[str, set[str]]


def _matches_reactivation_connection(config, name, tag, item):
    inbound, active = client_credentials.active_item_for_connection(config, name, tag)
    if inbound is None or client_credentials.protocol_from_inbound(inbound) != item["protocol"]:
        return False
    field = "password" if item["protocol"] == "trojan" else "id"
    return active is None or active.get(field) == item["client"].get(field)


def retry_limit_reactivations(config, db, traffic_entries, pending, *, limit_status, now):
    """Повторно оценить только зафиксированные попытки вернуть доступ."""
    result = LimitReactivationRetry([], [], [], False, {})
    for name, fingerprints in sorted(pending.items()):
        entry = db_clients(db).get(name)
        if not entry:
            continue
        items = client_credentials.normalize_entry_credentials(entry)
        current = reactivation_fingerprints(entry, tags=fingerprints)
        # Поздняя замена credential или подключения имеет приоритет.
        matched = {
            tag for tag, fingerprint in current.items()
            if fingerprints[tag] == fingerprint and _matches_reactivation_connection(config, name, tag, items[tag])
        }
        if not matched:
            continue
        result.credentials[name] = matched
        if entry.get("enabled") is False:
            # Предыдущая попытка уже отменила реактивацию в БД.
            for tag in sorted(matched):
                removed, _item = client_credentials.remove_active_credential(config, name, tag)
                result.config_changed |= removed
            destination = result.exceeded if entry.get("disabledReason") == "traffic-limit" else result.expired
            destination.append(name)
            continue
        traffic = traffic_entries.get(name, {})
        current_limit = limit_status(entry, traffic, now)
        if access_expired(entry, now) or (current_limit and current_limit["exceeded"]):
            _update, config_changed, state, _traffic = reconcile_client_access_status(
                config, db, traffic, name, entry, now, limit_status=limit_status,
            )
            result.config_changed |= config_changed
            result.credentials[name] = set(items)
            destination = result.exceeded if state == "disabled-traffic-limit" else result.expired
            destination.append(name)
        else:
            for tag in sorted(matched):
                if items[tag].get("enabled") is False:
                    continue
                _inbound, active = client_credentials.active_item_for_connection(config, name, tag)
                if active is None:
                    result.config_changed |= client_credentials.upsert_active_credential(config, name, entry, items[tag])
            result.enabled.append(name)
    return result


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
