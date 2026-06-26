"""Helpers for GeoIP bypass routing through named VLESS outbounds."""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Iterable

from xray_vps_manager.xray import cascade as cascade_config
from xray_vps_manager.xray import client_routes

BYPASS_TAG_PREFIX = "bypass-"
GEOIP_WARNING_OUTBOUND_PREFIX = "geoip-warning-"
GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV = "ACTIVITY_XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY"
NAME_RE = cascade_config.NAME_RE
REGION_CODE_RE = re.compile(r"[A-Za-z0-9_-]{2,64}")


def normalize_bypass_name(value: str | None) -> str:
    name = (value or "").strip()
    if name.startswith(BYPASS_TAG_PREFIX):
        name = name[len(BYPASS_TAG_PREFIX):]
    name = name.lower()
    if not NAME_RE.fullmatch(name):
        raise ValueError("Bypass name must use 1-32 chars: a-z, 0-9, dash, underscore; first char must be a letter or digit.")
    return name


def bypass_tag(name: str | None) -> str:
    return f"{BYPASS_TAG_PREFIX}{normalize_bypass_name(name)}"


def is_bypass_tag(tag: str | None) -> bool:
    return str(tag or "").startswith(BYPASS_TAG_PREFIX)


def bypass_name_from_tag(tag: str | None) -> str:
    text = str(tag or "")
    if is_bypass_tag(text):
        return text[len(BYPASS_TAG_PREFIX):]
    return ""


def normalize_region_code(value: str | None) -> str:
    code = (value or "").strip().upper()
    if not REGION_CODE_RE.fullmatch(code):
        raise ValueError("GeoIP region code must use 2-64 chars: A-Z, 0-9, dash, underscore.")
    return code


def geoip_rule_value(region_code: str) -> str:
    return f"geoip:{normalize_region_code(region_code).lower()}"


def geoip_warning_tag(region_code: str) -> str:
    return f"{GEOIP_WARNING_OUTBOUND_PREFIX}{normalize_region_code(region_code)}"


def is_geoip_warning_tag(tag: str | None) -> bool:
    return str(tag or "").startswith(GEOIP_WARNING_OUTBOUND_PREFIX)


def region_from_geoip_warning_tag(tag: str | None) -> str:
    text = str(tag or "")
    if not is_geoip_warning_tag(text):
        return ""
    return normalize_region_code(text[len(GEOIP_WARNING_OUTBOUND_PREFIX):])


def routing_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    return cascade_config.routing_rules(config)


def outbound_by_tag(config: dict[str, Any], tag: str) -> dict[str, Any] | None:
    for outbound in config.get("outbounds", []):
        if outbound.get("tag") == tag:
            return outbound
    return None


def bypass_outbounds(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        outbound
        for outbound in config.get("outbounds", [])
        if is_bypass_tag(outbound.get("tag"))
    ]


def bypass_outbound(config: dict[str, Any], tag: str) -> dict[str, Any] | None:
    for outbound in bypass_outbounds(config):
        if outbound.get("tag") == tag:
            return outbound
    return None


def remove_tagged_outbounds(config: dict[str, Any], tag: str) -> bool:
    outbounds = config.setdefault("outbounds", [])
    kept = [outbound for outbound in outbounds if outbound.get("tag") != tag]
    if len(kept) == len(outbounds):
        return False
    config["outbounds"] = kept
    return True


def upsert_bypass_outbound(config: dict[str, Any], outbound: dict[str, Any]) -> None:
    tag = str(outbound.get("tag") or "")
    if not is_bypass_tag(tag):
        raise ValueError(f"Bypass outbound tag must start with {BYPASS_TAG_PREFIX}: {tag}")
    previous_index = next(
        (
            index
            for index, item in enumerate(config.get("outbounds", []))
            if item.get("tag") == tag
        ),
        None,
    )
    cascade_config.ensure_base_outbounds(config)
    config["outbounds"] = [item for item in config.get("outbounds", []) if item.get("tag") != tag]
    if previous_index is not None:
        config["outbounds"].insert(min(previous_index, len(config["outbounds"])), outbound)
        return

    insert_index = 0
    for index, item in enumerate(config["outbounds"]):
        if is_bypass_tag(item.get("tag")):
            insert_index = index + 1
    config["outbounds"].insert(insert_index, outbound)


def rule_values(rule: dict[str, Any], key: str) -> list[str]:
    return [str(value) for value in cascade_config.rule_values(rule, key)]


def is_api_rule(rule: dict[str, Any]) -> bool:
    return rule.get("outboundTag") == cascade_config.API_TAG or cascade_config.API_TAG in rule_values(rule, "inboundTag")


def ensure_private_block_rule(config: dict[str, Any]) -> None:
    rules = routing_rules(config)
    for rule in rules:
        if (
            rule.get("type") == "field"
            and rule.get("outboundTag") == cascade_config.BLOCKED_TAG
            and "geoip:private" in rule_values(rule, "ip")
        ):
            return
    insert_index = 0
    while insert_index < len(rules) and is_api_rule(rules[insert_index]):
        insert_index += 1
    rules.insert(
        insert_index,
        {
            "type": "field",
            "ip": ["geoip:private"],
            "outboundTag": cascade_config.BLOCKED_TAG,
        },
    )


def rule_targets_region(rule: dict[str, Any], region_code: str) -> bool:
    return geoip_rule_value(region_code) in {value.lower() for value in rule_values(rule, "ip")}


def is_client_route_rule(rule: dict[str, Any]) -> bool:
    return str(rule.get("balancerTag") or "").startswith(client_routes.CLIENT_BALANCER_PREFIX)


def is_managed_catchall_rule(rule: dict[str, Any]) -> bool:
    tag = rule.get("outboundTag")
    return cascade_config.is_managed_catchall_tag(tag) and cascade_config.is_catchall_rule(rule)


def insert_before_client_or_catchall(config: dict[str, Any], rule: dict[str, Any]) -> None:
    rules = routing_rules(config)
    insert_index = len(rules)
    for index, existing in enumerate(rules):
        if is_client_route_rule(existing) or is_managed_catchall_rule(existing):
            insert_index = index
            break
    rules.insert(insert_index, rule)


def is_geoip_warning_rule_for_region(rule: dict[str, Any], region_code: str) -> bool:
    return rule.get("outboundTag") == geoip_warning_tag(region_code) and rule_targets_region(rule, region_code)


def is_direct_bypass_region_rule(rule: dict[str, Any], region_code: str) -> bool:
    return is_bypass_tag(rule.get("outboundTag")) and rule_targets_region(rule, region_code)


def remove_direct_bypass_region_rules(config: dict[str, Any], region_code: str) -> bool:
    rules = routing_rules(config)
    kept = [rule for rule in rules if not is_direct_bypass_region_rule(rule, region_code)]
    if len(kept) == len(rules):
        return False
    config["routing"]["rules"] = kept
    return True


def ensure_geoip_warning_rule(config: dict[str, Any], region_code: str) -> None:
    tag = geoip_warning_tag(region_code)
    rules = routing_rules(config)
    kept: list[dict[str, Any]] = []
    found = False
    for rule in rules:
        if is_geoip_warning_rule_for_region(rule, region_code):
            if found:
                continue
            updated = dict(rule)
            updated["outboundTag"] = tag
            kept.append(updated)
            found = True
            continue
        kept.append(rule)
    config["routing"]["rules"] = kept
    if found:
        return
    insert_before_client_or_catchall(
        config,
        {
            "type": "field",
            "ip": [geoip_rule_value(region_code)],
            "outboundTag": tag,
        },
    )


def remove_geoip_warning_route(config: dict[str, Any], region_code: str) -> bool:
    tag = geoip_warning_tag(region_code)
    changed = remove_tagged_outbounds(config, tag)
    rules = routing_rules(config)
    kept = [
        rule
        for rule in rules
        if not (rule.get("outboundTag") == tag and rule_targets_region(rule, region_code))
    ]
    if len(kept) != len(rules):
        config["routing"]["rules"] = kept
        changed = True
    return changed


def outbound_without_tag(outbound: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(outbound)
    result.pop("tag", None)
    return result


def outbounds_equivalent(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if not left or not right:
        return False
    return json.dumps(outbound_without_tag(left), sort_keys=True) == json.dumps(outbound_without_tag(right), sort_keys=True)


def clone_outbound_with_tag(outbound: dict[str, Any], tag: str) -> dict[str, Any]:
    result = copy.deepcopy(outbound)
    result["tag"] = tag
    return result


def ensure_geoip_warning_outbound(config: dict[str, Any], region_code: str, source_outbound: dict[str, Any]) -> None:
    tag = geoip_warning_tag(region_code)
    remove_tagged_outbounds(config, tag)
    config.setdefault("outbounds", []).append(clone_outbound_with_tag(source_outbound, tag))


def current_catchall_source_outbound(config: dict[str, Any], fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    catchall = cascade_config.current_catchall_tag(config)
    if catchall:
        outbound = outbound_by_tag(config, catchall)
        if outbound:
            return outbound
    if fallback:
        return fallback
    active = cascade_config.active_cascade_outbound(config)
    if active:
        return active
    return cascade_config.ensure_direct_outbound(config)


def enabled_route_for_region(
    enabled_routes: Iterable[dict[str, Any]] | dict[str, dict[str, Any]] | None,
    region_code: str,
) -> dict[str, Any] | None:
    if enabled_routes is None:
        return None
    records = enabled_routes.values() if isinstance(enabled_routes, dict) else enabled_routes
    code = normalize_region_code(region_code)
    for record in records:
        record_code = str(record.get("regionCode") or "").strip()
        if not record_code:
            continue
        if normalize_region_code(record_code) == code and record.get("enabled") is True:
            return record
    return None


def matching_bypass_outbound_for_warning(config: dict[str, Any], warning_outbound: dict[str, Any] | None) -> dict[str, Any] | None:
    for outbound in bypass_outbounds(config):
        if outbounds_equivalent(outbound, warning_outbound):
            return outbound
    return None


def warning_tags(config: dict[str, Any]) -> list[str]:
    tags = {
        item.get("tag")
        for item in config.get("outbounds", [])
        if is_geoip_warning_tag(item.get("tag"))
    }
    tags.update(
        rule.get("outboundTag")
        for rule in routing_rules(config)
        if is_geoip_warning_tag(rule.get("outboundTag"))
    )
    return sorted(str(tag) for tag in tags if tag)


def warning_source_outbound(
    config: dict[str, Any],
    region_code: str,
    *,
    source_outbound: dict[str, Any] | None = None,
    enabled_routes: Iterable[dict[str, Any]] | dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    route = enabled_route_for_region(enabled_routes, region_code)
    if route:
        outbound = bypass_outbound(config, str(route.get("tag") or ""))
        if outbound:
            return outbound
    existing = outbound_by_tag(config, geoip_warning_tag(region_code))
    matched_bypass = matching_bypass_outbound_for_warning(config, existing)
    if matched_bypass:
        return matched_bypass
    return current_catchall_source_outbound(config, fallback=source_outbound)


def sync_geoip_warning_outbounds(
    config: dict[str, Any],
    *,
    source_outbound: dict[str, Any] | None = None,
    enabled_routes: Iterable[dict[str, Any]] | dict[str, dict[str, Any]] | None = None,
) -> None:
    tags = warning_tags(config)
    if not tags:
        return
    sources = {
        tag: warning_source_outbound(
            config,
            region_from_geoip_warning_tag(tag),
            source_outbound=source_outbound,
            enabled_routes=enabled_routes,
        )
        for tag in tags
    }
    config["outbounds"] = [
        outbound
        for outbound in config.get("outbounds", [])
        if not is_geoip_warning_tag(outbound.get("tag"))
    ]
    for tag in tags:
        config.setdefault("outbounds", []).append(clone_outbound_with_tag(sources[tag], tag))


def apply_bypass_route(config: dict[str, Any], tag: str, region_code: str) -> None:
    region = normalize_region_code(region_code)
    source = bypass_outbound(config, tag)
    if not source:
        raise ValueError(f"Bypass outbound is not configured: {tag}")
    ensure_private_block_rule(config)
    remove_direct_bypass_region_rules(config, region)
    ensure_geoip_warning_outbound(config, region, source)
    ensure_geoip_warning_rule(config, region)
    config.setdefault("routing", {})["domainStrategy"] = "IPOnDemand"


def disable_bypass_route(
    config: dict[str, Any],
    tag: str,
    region_code: str,
    *,
    remove_outbound: bool = False,
    env_values: dict[str, str] | None = None,
) -> None:
    region = normalize_region_code(region_code)
    remove_geoip_warning_route(config, region)
    remove_direct_bypass_region_rules(config, region)
    if remove_outbound:
        remove_tagged_outbounds(config, tag)
    restore_geoip_domain_strategy_if_unused(config, env_values)


def has_geoip_dependent_rules(config: dict[str, Any]) -> bool:
    for rule in routing_rules(config):
        if is_geoip_warning_tag(rule.get("outboundTag")):
            return True
        if is_bypass_tag(rule.get("outboundTag")) and any(str(value).lower().startswith("geoip:") for value in rule_values(rule, "ip")):
            return True
    return False


def ensure_geoip_domain_strategy(config: dict[str, Any], env_values: dict[str, str] | None = None) -> None:
    routing = config.setdefault("routing", {})
    previous = str(routing.get("domainStrategy") or "")
    if env_values is not None and not env_values.get(GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV) and previous != "IPOnDemand":
        env_values[GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV] = previous
    routing["domainStrategy"] = "IPOnDemand"


def restore_geoip_domain_strategy_if_unused(config: dict[str, Any], env_values: dict[str, str] | None = None) -> bool:
    if has_geoip_dependent_rules(config):
        return False
    routing = config.setdefault("routing", {})
    previous = ""
    if env_values is not None:
        previous = env_values.pop(GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV, "")
    if previous:
        routing["domainStrategy"] = previous
        return True
    if routing.get("domainStrategy") == "IPOnDemand":
        routing["domainStrategy"] = "IPIfNonMatch"
        return True
    return False


def configured_bypass_for_warning(config: dict[str, Any], region_code: str) -> str:
    warning = outbound_by_tag(config, geoip_warning_tag(region_code))
    matched = matching_bypass_outbound_for_warning(config, warning)
    return str(matched.get("tag") or "") if matched else ""


def bypass_event_risk(config: dict[str, Any], outbound_tag: str) -> str:
    if not is_geoip_warning_tag(outbound_tag):
        return ""
    region = region_from_geoip_warning_tag(outbound_tag)
    if configured_bypass_for_warning(config, region):
        return f"xray-bypass:{region}"
    return ""
