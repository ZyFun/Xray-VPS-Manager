"""Per-client cascade route helpers."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.clients.models import client_from_db_entry
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.xray import cascade as cascade_config

ROUTING_SERVICE = "RoutingService"
CLIENT_BALANCER_PREFIX = "client-route-"
XRAY_API_SERVER = "127.0.0.1:10085"
COUNTRY_MAX_LENGTH = 64
DEFAULT_ACTIVE_COUNTRY = "Германия"
DEFAULT_INACTIVE_COUNTRY = "США"


def normalize_country(value: str | None) -> str:
    country = str(value or "").strip()
    if any(char in country for char in "\r\n\t"):
        raise ValueError("Cascade country must not contain control characters.")
    if len(country) > COUNTRY_MAX_LENGTH:
        raise ValueError(f"Cascade country must be {COUNTRY_MAX_LENGTH} characters or shorter.")
    return country


def route_label(record: dict[str, Any] | None, tag: str = "") -> str:
    record = record if isinstance(record, dict) else {}
    country = str(record.get("country") or "").strip()
    label = str(record.get("label") or "").strip()
    if country:
        return country
    if label:
        return label
    if tag:
        return cascade_config.cascade_name_from_tag(tag) or tag
    return "-"


def sync_routes_from_config(config: dict[str, Any], db: dict[str, Any]) -> bool:
    """Mirror configured cascade outbounds into SQLite metadata and default client choices."""

    changed = False
    routes = client_repository.db_cascade_routes(db)
    stamp = utc_stamp()
    active_tag = cascade_config.active_cascade_tag(config)
    for tag in cascade_config.cascade_tags(config):
        existing = routes.get(tag, {})
        if not isinstance(existing, dict):
            existing = {}
        record = dict(existing)
        changed_record = False
        if record.get("tag") != tag:
            record["tag"] = tag
            changed_record = True
        if not record.get("label"):
            record["label"] = cascade_config.cascade_name_from_tag(tag) or tag
            changed_record = True
        if not record.get("country"):
            record["country"] = DEFAULT_ACTIVE_COUNTRY if tag == active_tag else DEFAULT_INACTIVE_COUNTRY
            changed_record = True
        if not record.get("created"):
            record["created"] = stamp
            changed_record = True
        if changed_record:
            record["updated"] = stamp
        if routes.get(tag) != record:
            routes[tag] = record
            changed = True

    changed = assign_default_client_routes(config, db) or changed
    return changed


def assign_default_client_routes(config: dict[str, Any], db: dict[str, Any]) -> bool:
    active = cascade_config.active_cascade_tag(config)
    if not active:
        return False
    configured = set(cascade_config.cascade_tags(config))
    changed = False
    for entry in client_repository.db_clients(db).values():
        if not isinstance(entry, dict):
            continue
        selected = str(entry.get("selectedCascadeTag") or "").strip()
        if selected and selected in configured:
            continue
        entry["selectedCascadeTag"] = active
        changed = True
    return changed


def upsert_route_country(db: dict[str, Any], tag: str, country: str, *, label: str = "") -> None:
    routes = client_repository.db_cascade_routes(db)
    existing = routes.get(tag, {})
    if not isinstance(existing, dict):
        existing = {}
    stamp = utc_stamp()
    record = dict(existing)
    record["tag"] = tag
    record["country"] = normalize_country(country)
    record["label"] = label or record.get("label") or cascade_config.cascade_name_from_tag(tag) or tag
    record.setdefault("created", stamp)
    record["updated"] = stamp
    routes[tag] = record


def route_options(db: dict[str, Any], config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    configured = set(cascade_config.cascade_tags(config)) if config is not None else None
    options = []
    for tag, record in client_repository.db_cascade_routes(db).items():
        if configured is not None and tag not in configured:
            continue
        if not isinstance(record, dict):
            continue
        item = dict(record)
        item["tag"] = tag
        item["display"] = route_label(item, tag)
        options.append(item)
    return sorted(options, key=lambda item: (str(item.get("display") or "").lower(), str(item.get("tag") or "")))


def selected_route_tag(entry: dict[str, Any] | None) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("selectedCascadeTag") or "").strip()


def selected_route_label(db: dict[str, Any], entry: dict[str, Any] | None) -> str:
    tag = selected_route_tag(entry)
    if not tag:
        return "не выбрана"
    record = client_repository.db_cascade_routes(db).get(tag, {})
    return route_label(record, tag)


def client_balancer_tag(name: str, entry: dict[str, Any]) -> str:
    uuid = re.sub(r"[^A-Za-z0-9]+", "", str(entry.get("id") or ""))
    if uuid:
        return CLIENT_BALANCER_PREFIX + uuid[:24].lower()
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "client"
    return (CLIENT_BALANCER_PREFIX + safe_name)[:63]


def client_email(name: str, entry: dict[str, Any]) -> str:
    client = entry.get("client") if isinstance(entry.get("client"), dict) else {}
    email = str(client.get("email") or "").strip()
    if email:
        return email
    return client_from_db_entry(name, entry).get("email", name)


def ensure_routing_service(config: dict[str, Any]) -> bool:
    api = config.setdefault("api", {})
    services = api.setdefault("services", [])
    if ROUTING_SERVICE in services:
        return False
    services.append(ROUTING_SERVICE)
    return True


def routing_balancers(config: dict[str, Any]) -> list[dict[str, Any]]:
    routing = config.setdefault("routing", {})
    return routing.setdefault("balancers", [])


def remove_existing_client_rule(config: dict[str, Any], balancer_tag: str) -> None:
    rules = cascade_config.routing_rules(config)
    config["routing"]["rules"] = [rule for rule in rules if rule.get("balancerTag") != balancer_tag]


def upsert_balancer(config: dict[str, Any], balancer_tag: str, selected_tag: str) -> None:
    balancers = routing_balancers(config)
    selected = None
    for balancer in balancers:
        if balancer.get("tag") == balancer_tag:
            selected = balancer
            break
    if selected is None:
        selected = {"tag": balancer_tag}
        balancers.append(selected)
    # Keep the persisted config deterministic: the balancer has a single
    # selected candidate, while xray api bo can override it at runtime.
    selected["selector"] = [selected_tag]
    selected.pop("fallbackTag", None)
    selected.pop("strategy", None)


def insert_client_rule_before_catchall(config: dict[str, Any], email: str, balancer_tag: str) -> None:
    rules = cascade_config.routing_rules(config)
    rule = {"type": "field", "user": [email], "balancerTag": balancer_tag}
    index = len(rules)
    for current_index, current in enumerate(rules):
        if cascade_config.is_catchall_rule(current):
            index = current_index
            break
    rules.insert(index, rule)


def ensure_client_route_config(config: dict[str, Any], name: str, entry: dict[str, Any], selected_tag: str | None = None) -> bool:
    selected = selected_tag or selected_route_tag(entry) or cascade_config.active_cascade_tag(config)
    if not selected or not cascade_config.cascade_outbound(config, selected):
        return False
    before = repr(config.get("api", {})) + repr(config.get("routing", {}))
    ensure_routing_service(config)
    balancer_tag = client_balancer_tag(name, entry)
    remove_existing_client_rule(config, balancer_tag)
    upsert_balancer(config, balancer_tag, selected)
    insert_client_rule_before_catchall(config, client_email(name, entry), balancer_tag)
    entry["selectedCascadeTag"] = selected
    after = repr(config.get("api", {})) + repr(config.get("routing", {}))
    return before != after


def remove_stale_client_route_config(config: dict[str, Any], expected_balancers: set[str]) -> bool:
    routing = config.setdefault("routing", {})
    before = repr(routing)
    routing["rules"] = [
        rule
        for rule in cascade_config.routing_rules(config)
        if not (
            str(rule.get("balancerTag") or "").startswith(CLIENT_BALANCER_PREFIX)
            and str(rule.get("balancerTag") or "") not in expected_balancers
        )
    ]
    routing["balancers"] = [
        balancer
        for balancer in routing_balancers(config)
        if not (
            str(balancer.get("tag") or "").startswith(CLIENT_BALANCER_PREFIX)
            and str(balancer.get("tag") or "") not in expected_balancers
        )
    ]
    return before != repr(routing)


def ensure_all_client_route_config(config: dict[str, Any], db: dict[str, Any]) -> bool:
    changed = sync_routes_from_config(config, db)
    expected_balancers = {
        client_balancer_tag(name, entry)
        for name, entry in client_repository.db_clients(db).items()
        if isinstance(entry, dict)
    }
    changed = remove_stale_client_route_config(config, expected_balancers) or changed
    for name, entry in client_repository.db_clients(db).items():
        if isinstance(entry, dict):
            changed = ensure_client_route_config(config, name, entry) or changed
    return changed


def runtime_override_commands(
    name: str,
    entry: dict[str, Any],
    outbound_tag: str,
    *,
    server: str = XRAY_API_SERVER,
) -> list[list[str]]:
    balancer_tag = client_balancer_tag(name, entry)
    return [
        ["/usr/local/bin/xray", "api", "bo", f"--server={server}", "-b", balancer_tag, outbound_tag],
        ["/usr/local/bin/xray", "api", "bo", "-s", server, "-b", balancer_tag, outbound_tag],
    ]


def apply_runtime_override(
    name: str,
    entry: dict[str, Any],
    outbound_tag: str,
    run_capture: Callable[..., Any],
    *,
    timeout: int = 8,
) -> tuple[bool, str]:
    errors = []
    for command in runtime_override_commands(name, entry, outbound_tag):
        try:
            result = run_capture(command, timeout=timeout)
        except Exception as exc:  # pragma: no cover - defensive wrapper around system command
            errors.append(str(exc))
            continue
        if getattr(result, "returncode", 1) == 0:
            return True, ""
        detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or f"exit {getattr(result, 'returncode', '?')}").strip()
        if detail:
            errors.append(detail)
    return False, "; ".join(errors[-2:]) or "xray api bo failed"
