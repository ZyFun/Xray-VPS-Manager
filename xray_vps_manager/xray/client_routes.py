"""Per-client cascade route helpers."""

from __future__ import annotations

import re
from typing import Any

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


def selected_route_tag(entry: dict[str, Any] | None) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("selectedCascadeTag") or "").strip()


def client_balancer_tag(name: str, entry: dict[str, Any]) -> str:
    uuid = re.sub(r"[^A-Za-z0-9]+", "", str(entry.get("id") or ""))
    if uuid:
        return CLIENT_BALANCER_PREFIX + uuid[:24].lower()
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "client"
    return (CLIENT_BALANCER_PREFIX + safe_name)[:63]


def is_client_balancer_tag(tag: str | None) -> bool:
    return str(tag or "").startswith(CLIENT_BALANCER_PREFIX)


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


def client_routes_allowed(config: dict[str, Any]) -> bool:
    """Client rules sit above the catch-all, so they may only refine a cascade catch-all.

    With catch-all warp-out they would send clients past WARP (XR-REV-055), without a catch-all
    (`xray-set-cascade --disable`) past direct. selectedCascadeTag stays in manager.db and
    restores the rules once a cascade is the catch-all again.
    """
    return bool(cascade_config.active_cascade_tag(config))


def client_route_rule_count(config: dict[str, Any]) -> int:
    rules = config.get("routing", {}).get("rules", [])
    return sum(1 for rule in rules if is_client_balancer_tag(rule.get("balancerTag")))


def remove_client_route_config(config: dict[str, Any], balancer_tag: str) -> bool:
    routing = config.setdefault("routing", {})
    rules = cascade_config.routing_rules(config)
    balancers = routing_balancers(config)
    routing["rules"] = [rule for rule in rules if rule.get("balancerTag") != balancer_tag]
    routing["balancers"] = [balancer for balancer in balancers if balancer.get("tag") != balancer_tag]
    return len(routing["rules"]) != len(rules) or len(routing["balancers"]) != len(balancers)


def insert_client_rule_before_catchall(config: dict[str, Any], emails: list[str], balancer_tag: str) -> None:
    rules = cascade_config.routing_rules(config)
    rule = {"type": "field", "user": emails, "balancerTag": balancer_tag}
    index = len(rules)
    for current_index, current in enumerate(rules):
        if cascade_config.is_catchall_rule(current):
            index = current_index
            break
    rules.insert(index, rule)


def prepare_client_route_config(
    config: dict[str, Any], name: str, entry: dict[str, Any], selected_tag: str | None = None
) -> bool | tuple[str, str, str]:
    """Подготовить правило; bool означает завершённое удаление или пропуск.

    Email разрешаются сценарием после этой фазы, как и до разделения модуля.
    Снимок нужен для прежнего результата changed, включая подготовительные изменения.
    """
    if not client_routes_allowed(config):
        return remove_client_route_config(config, client_balancer_tag(name, entry))
    selected = selected_tag or selected_route_tag(entry) or cascade_config.active_cascade_tag(config)
    if not selected or not cascade_config.cascade_outbound(config, selected):
        return False
    before = repr(config.get("api", {})) + repr(config.get("routing", {}))
    ensure_routing_service(config)
    balancer_tag = client_balancer_tag(name, entry)
    remove_existing_client_rule(config, balancer_tag)
    upsert_balancer(config, balancer_tag, selected)
    return before, balancer_tag, selected


def complete_client_route_config(
    config: dict[str, Any], entry: dict[str, Any], prepared: tuple[str, str, str], emails: list[str]
) -> bool:
    """Завершить чистое преобразование с email, переданными сценарием."""
    before, balancer_tag, selected = prepared
    insert_client_rule_before_catchall(config, emails, balancer_tag)
    entry["selectedCascadeTag"] = selected
    after = repr(config.get("api", {})) + repr(config.get("routing", {}))
    return before != after


def ensure_client_route_config(
    config: dict[str, Any], name: str, entry: dict[str, Any], selected_tag: str | None = None,
    *, emails: list[str],
) -> bool:
    """Построить правило только по переданным данным, без разрешения credentials."""
    prepared = prepare_client_route_config(config, name, entry, selected_tag)
    if isinstance(prepared, bool):
        return prepared
    return complete_client_route_config(config, entry, prepared, emails)


def remove_stale_client_route_config(config: dict[str, Any], expected_balancers: set[str]) -> bool:
    routing = config.setdefault("routing", {})
    before = repr(routing)
    routing["rules"] = [
        rule
        for rule in cascade_config.routing_rules(config)
        if not (
            is_client_balancer_tag(rule.get("balancerTag"))
            and str(rule.get("balancerTag") or "") not in expected_balancers
        )
    ]
    routing["balancers"] = [
        balancer
        for balancer in routing_balancers(config)
        if not (
            is_client_balancer_tag(balancer.get("tag"))
            and str(balancer.get("tag") or "") not in expected_balancers
        )
    ]
    return before != repr(routing)


def remove_all_client_route_config(config: dict[str, Any]) -> bool:
    routing = config.setdefault("routing", {})
    before = repr(routing)
    routing["rules"] = [
        rule
        for rule in cascade_config.routing_rules(config)
        if not is_client_balancer_tag(rule.get("balancerTag"))
    ]
    routing["balancers"] = [
        balancer
        for balancer in routing_balancers(config)
        if not is_client_balancer_tag(balancer.get("tag"))
    ]
    return before != repr(routing)


def remove_unavailable_client_route_config(config: dict[str, Any]) -> bool:
    routing = config.setdefault("routing", {})
    configured = set(cascade_config.cascade_tags(config))
    before = repr(routing)
    kept_balancer_tags = set()
    kept_balancers = []
    for balancer in routing_balancers(config):
        tag = str(balancer.get("tag") or "")
        if not is_client_balancer_tag(tag):
            kept_balancers.append(balancer)
            continue
        selector = [
            str(item)
            for item in (balancer.get("selector") or [])
            if str(item) in configured
        ]
        if not selector:
            continue
        next_balancer = dict(balancer)
        next_balancer["selector"] = selector
        kept_balancer_tags.add(tag)
        kept_balancers.append(next_balancer)
    routing["balancers"] = kept_balancers
    routing["rules"] = [
        rule
        for rule in cascade_config.routing_rules(config)
        if not (
            is_client_balancer_tag(rule.get("balancerTag"))
            and str(rule.get("balancerTag") or "") not in kept_balancer_tags
        )
    ]
    return before != repr(routing)


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
