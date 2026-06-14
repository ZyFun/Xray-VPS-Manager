"""Helpers for named Xray cascade outbounds."""

from __future__ import annotations

import re
from typing import Any

CASCADE_TAG_PREFIX = "cascade-"
DEFAULT_CASCADE_NAME = "upstream"
LEGACY_CASCADE_TAG = "cascade-upstream"
DIRECT_TAG = "direct"
BLOCKED_TAG = "blocked"
API_TAG = "api"
WARP_OUTBOUND_TAG = "warp-out"
TELEGRAM_SOCKS_TAG = "telegram-bot-socks"

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
MANAGED_CATCHALL_FIXED_TAGS = {DIRECT_TAG, WARP_OUTBOUND_TAG}


def normalize_cascade_name(value: str | None, default: str = DEFAULT_CASCADE_NAME) -> str:
    name = (value or default).strip()
    if name.startswith(CASCADE_TAG_PREFIX):
        name = name[len(CASCADE_TAG_PREFIX):]
    name = name.lower()
    if not NAME_RE.fullmatch(name):
        raise ValueError("Cascade name must use 1-32 chars: a-z, 0-9, dash, underscore; first char must be a letter or digit.")
    return name


def cascade_tag(name: str | None = None) -> str:
    return f"{CASCADE_TAG_PREFIX}{normalize_cascade_name(name)}"


def is_cascade_tag(tag: str | None) -> bool:
    return str(tag or "").startswith(CASCADE_TAG_PREFIX)


def cascade_name_from_tag(tag: str | None) -> str:
    text = str(tag or "")
    if is_cascade_tag(text):
        return text[len(CASCADE_TAG_PREFIX):]
    return ""


def routing_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    routing = config.setdefault("routing", {})
    routing.setdefault("domainStrategy", "IPIfNonMatch")
    return routing.setdefault("rules", [])


def rule_values(rule: dict[str, Any], key: str) -> list[Any]:
    value = rule.get(key, [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return []


def is_catchall_rule(rule: dict[str, Any], tag: str | None = None) -> bool:
    if rule.get("type") != "field":
        return False
    if tag and rule.get("outboundTag") != tag:
        return False
    if rule.get("network") != "tcp,udp":
        return False
    for key in ("domain", "ip", "protocol", "inboundTag", "port", "source", "sourcePort", "attrs"):
        if key in rule:
            return False
    return True


def current_catchall_tag(config: dict[str, Any]) -> str:
    for rule in reversed(routing_rules(config)):
        if is_catchall_rule(rule):
            return str(rule.get("outboundTag") or "")
    return ""


def cascade_outbounds(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        outbound
        for outbound in config.get("outbounds", [])
        if is_cascade_tag(outbound.get("tag"))
    ]


def cascade_tags(config: dict[str, Any]) -> list[str]:
    return [str(outbound.get("tag")) for outbound in cascade_outbounds(config) if outbound.get("tag")]


def cascade_outbound(config: dict[str, Any], tag: str) -> dict[str, Any] | None:
    for outbound in cascade_outbounds(config):
        if outbound.get("tag") == tag:
            return outbound
    return None


def first_cascade_tag(config: dict[str, Any]) -> str:
    tags = cascade_tags(config)
    return tags[0] if tags else ""


def active_cascade_tag(config: dict[str, Any]) -> str:
    tag = current_catchall_tag(config)
    if is_cascade_tag(tag) and cascade_outbound(config, tag):
        return tag
    return ""


def selected_cascade_tag(config: dict[str, Any]) -> str:
    return active_cascade_tag(config) or first_cascade_tag(config)


def active_cascade_outbound(config: dict[str, Any]) -> dict[str, Any] | None:
    tag = active_cascade_tag(config)
    return cascade_outbound(config, tag) if tag else None


def ensure_direct_outbound(config: dict[str, Any]) -> dict[str, Any]:
    outbounds = config.setdefault("outbounds", [])
    for outbound in outbounds:
        if outbound.get("tag") == DIRECT_TAG:
            return outbound
    outbound = {"tag": DIRECT_TAG, "protocol": "freedom"}
    outbounds.append(outbound)
    return outbound


def ensure_blocked_outbound(config: dict[str, Any]) -> dict[str, Any]:
    outbounds = config.setdefault("outbounds", [])
    for outbound in outbounds:
        if outbound.get("tag") == BLOCKED_TAG:
            return outbound
    outbound = {"tag": BLOCKED_TAG, "protocol": "blackhole"}
    outbounds.append(outbound)
    return outbound


def ensure_base_outbounds(config: dict[str, Any]) -> list[dict[str, Any]]:
    ensure_direct_outbound(config)
    ensure_blocked_outbound(config)
    return config.setdefault("outbounds", [])


def is_managed_catchall_tag(tag: str | None) -> bool:
    return str(tag or "") in MANAGED_CATCHALL_FIXED_TAGS or is_cascade_tag(tag)


def remove_managed_catchall_routes(config: dict[str, Any]) -> bool:
    rules = routing_rules(config)
    kept = [
        rule
        for rule in rules
        if not (is_managed_catchall_tag(rule.get("outboundTag")) and is_catchall_rule(rule))
    ]
    config["routing"]["rules"] = kept
    return len(kept) != len(rules)


def append_catchall_route(config: dict[str, Any], tag: str) -> None:
    routing_rules(config).append(
        {
            "type": "field",
            "network": "tcp,udp",
            "outboundTag": tag,
        }
    )


def activate_cascade_route(config: dict[str, Any], tag: str) -> None:
    if not cascade_outbound(config, tag):
        raise ValueError(f"Cascade outbound is not configured: {tag}")
    remove_managed_catchall_routes(config)
    append_catchall_route(config, tag)


def move_outbound_to_front(config: dict[str, Any], tag: str) -> None:
    outbounds = config.setdefault("outbounds", [])
    selected = [outbound for outbound in outbounds if outbound.get("tag") == tag]
    if not selected:
        return
    rest = [outbound for outbound in outbounds if outbound.get("tag") != tag]
    config["outbounds"] = [selected[0], *rest]


def sync_telegram_cascade_rules(config: dict[str, Any], tag: str) -> bool:
    changed = False
    for rule in routing_rules(config):
        if TELEGRAM_SOCKS_TAG in rule_values(rule, "inboundTag") and is_cascade_tag(rule.get("outboundTag")):
            if rule.get("outboundTag") != tag:
                rule["outboundTag"] = tag
                changed = True
    return changed
