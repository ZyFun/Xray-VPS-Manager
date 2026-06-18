"""Helpers for Xray global domain/IP blocklist routing rules."""

from __future__ import annotations

import re
from typing import Any, Iterable

from xray_vps_manager.xray import cascade as cascade_config
from xray_vps_manager.xray import client_routes

BLOCKED_TAG = "blocked"
GEOIP_WARNING_OUTBOUND_PREFIX = "geoip-warning-"

FIELD_CONDITION_KEYS = {
    "domain",
    "ip",
    "protocol",
    "inboundTag",
    "port",
    "network",
    "source",
    "sourcePort",
    "user",
    "attrs",
}


def wildcard_to_regexp(value: str) -> str:
    pattern = re.escape(value).replace(r"\*", ".*").replace(r"\?", ".")
    return f"regexp:^{pattern}$"


def domain_rule_value(item: dict[str, Any]) -> str:
    value = str(item.get("value") or "").strip().lower()
    kind = item.get("kind")
    if kind == "mask":
        return wildcard_to_regexp(value)
    return f"domain:{value}"


def ip_rule_value(item: dict[str, Any]) -> str:
    return str(item.get("value") or "").strip().lower()


def split_rule_values(items: Iterable[dict[str, Any]]) -> tuple[list[str], list[str]]:
    domains = set()
    ips = set()
    for item in items:
        value = str(item.get("value") or "").strip()
        kind = item.get("kind")
        if not value:
            continue
        if kind in ("ip", "cidr"):
            ips.add(ip_rule_value(item))
        elif kind in ("domain", "mask"):
            domains.add(domain_rule_value(item))
    return sorted(domains), sorted(ips)


def rule_values(rule: dict[str, Any], key: str) -> list[str]:
    value = rule.get(key, [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def rule_has_conditions(rule: dict[str, Any]) -> bool:
    return any(key in rule and rule_values(rule, key) for key in FIELD_CONDITION_KEYS)


def remove_values_from_blocked_rules(config: dict[str, Any], items: Iterable[dict[str, Any]]) -> bool:
    domains, ips = split_rule_values(items)
    domain_set = set(domains)
    ip_set = set(ips)
    if not domain_set and not ip_set:
        return False

    rules = cascade_config.routing_rules(config)
    kept_rules: list[dict[str, Any]] = []
    changed = False
    for rule in rules:
        if rule.get("outboundTag") != BLOCKED_TAG:
            kept_rules.append(rule)
            continue
        updated = dict(rule)
        for key, blocked_values in (("domain", domain_set), ("ip", ip_set)):
            values = rule_values(updated, key)
            if not values:
                continue
            kept_values = [value for value in values if value not in blocked_values]
            if kept_values != values:
                changed = True
                if kept_values:
                    updated[key] = kept_values
                else:
                    updated.pop(key, None)
        if not rule_has_conditions(updated):
            changed = True
            continue
        kept_rules.append(updated)

    if changed:
        config.setdefault("routing", {})["rules"] = kept_rules
    return changed


def is_geoip_warning_rule(rule: dict[str, Any]) -> bool:
    return str(rule.get("outboundTag") or "").startswith(GEOIP_WARNING_OUTBOUND_PREFIX)


def is_client_route_rule(rule: dict[str, Any]) -> bool:
    return str(rule.get("balancerTag") or "").startswith(client_routes.CLIENT_BALANCER_PREFIX)


def is_managed_catchall_rule(rule: dict[str, Any]) -> bool:
    tag = rule.get("outboundTag")
    return cascade_config.is_managed_catchall_tag(tag) and cascade_config.is_catchall_rule(rule)


def insert_before_geoip_or_catchall(config: dict[str, Any], rule: dict[str, Any]) -> None:
    rules = cascade_config.routing_rules(config)
    insert_index = len(rules)
    for index, existing in enumerate(rules):
        if is_geoip_warning_rule(existing) or is_client_route_rule(existing) or is_managed_catchall_rule(existing):
            insert_index = index
            break
    rules.insert(insert_index, rule)


def sync_blocklist_rules(
    config: dict[str, Any],
    active_items: Iterable[dict[str, Any]],
    *,
    known_items: Iterable[dict[str, Any]] | None = None,
    removed_items: Iterable[dict[str, Any]] | None = None,
) -> bool:
    active = list(active_items)
    known = list(known_items if known_items is not None else active)
    removed = list(removed_items or [])
    before = repr(config.get("outbounds", [])) + repr(config.get("routing", {}))

    remove_values_from_blocked_rules(config, [*known, *removed])

    domains, ips = split_rule_values(active)
    if domains or ips:
        cascade_config.ensure_blocked_outbound(config)
    if domains:
        insert_before_geoip_or_catchall(
            config,
            {
                "type": "field",
                "domain": domains,
                "outboundTag": BLOCKED_TAG,
            },
        )
    if ips:
        insert_before_geoip_or_catchall(
            config,
            {
                "type": "field",
                "ip": ips,
                "outboundTag": BLOCKED_TAG,
            },
        )

    after = repr(config.get("outbounds", [])) + repr(config.get("routing", {}))
    return before != after
