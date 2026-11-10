"""Конфигурация локальных SOCKS-входов для проб WARP."""

from collections.abc import Iterator
from copy import deepcopy
import re

from xray_vps_manager.core.errors import ConflictError

SOCKS_HOST = "127.0.0.1"
TEST_SOCKS_PORT = 10809
VERIFY_SOCKS_PORT = 10819
VERIFY_INBOUND_TAG = "warp-verify-disabled-socks"


def _socks_inbound(tag: str, port: int, udp: bool) -> dict:
    return {
        "tag": tag, "listen": SOCKS_HOST, "port": port, "protocol": "socks",
        "settings": {"auth": "noauth", "udp": udp},
        "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
    }


def add_socks_inbound(config: dict, tag: str, port: int, *, udp: bool = True) -> None:
    inbounds = [item for item in config.get("inbounds", []) if item.get("tag") != tag]
    inbounds.append(_socks_inbound(tag, port, udp))
    config["inbounds"] = inbounds


def outbound_at_endpoint(outbound: dict, endpoint: str) -> dict:
    candidate = deepcopy(outbound)
    candidate["settings"]["peers"][0]["endpoint"] = endpoint
    return candidate


def disabled_probe_config(config: dict) -> dict:
    """Добавить SOCKS для обычного маршрута без специального правила для него."""
    probe = deepcopy(config)
    add_socks_inbound(probe, VERIFY_INBOUND_TAG, VERIFY_SOCKS_PORT)
    routing = probe.setdefault("routing", {})
    routing["rules"] = [
        rule for rule in routing.get("rules", [])
        if rule.get("inboundTag") != VERIFY_INBOUND_TAG
        and VERIFY_INBOUND_TAG not in (rule.get("inboundTag") if isinstance(rule.get("inboundTag"), list) else ())
    ]
    return probe


def endpoint_probe_ports(config: dict) -> Iterator[int]:
    """Порты вне текущих inbound (включая диапазоны) и порта verify-disabled."""
    ranges = [(VERIFY_SOCKS_PORT, VERIFY_SOCKS_PORT)]
    for inbound in config.get("inbounds", []):
        for part in str(inbound.get("port", "")).split(","):
            start, _, end = part.strip().partition("-")
            if start.isdecimal() and (not end or end.isdecimal()):
                ranges.append((int(start), int(end or start)))
    for port in range(TEST_SOCKS_PORT, 65536):
        if not any(start <= port <= end for start, end in ranges):
            yield port


def _endpoint_rule(inbound_tag: str, outbound_tag: str) -> dict:
    return {"type": "field", "inboundTag": [inbound_tag], "outboundTag": outbound_tag}


def _balancer_selectors(config: dict) -> tuple[str, ...]:
    return tuple(
        selector
        for balancer in (config.get("routing", {}).get("balancers") or ())
        for selector in (balancer.get("selector") or ())
        if isinstance(selector, str)
    )


def _referenced_outbound_tags(config: dict) -> set[str]:
    tags = {item.get("fallbackTag") for item in (config.get("routing", {}).get("balancers") or ())}
    for outbound in config.get("outbounds", []):
        tags.add((outbound.get("proxySettings") or {}).get("tag"))
        sockopt = (outbound.get("streamSettings") or {}).get("sockopt") or {}
        tags.add(sockopt.get("dialerProxy"))
    return {tag for tag in tags if isinstance(tag, str)}


def remove_endpoint_probe_artifacts(config: dict) -> bool:
    """Удалить только узнаваемые тройки от прерванной пакетной пробы."""
    inbounds = config.get("inbounds", [])
    outbounds = config.get("outbounds", [])
    rules = config.get("routing", {}).get("rules", [])
    socks = {item.get("tag"): item for item in inbounds}
    wireguard = {item.get("tag") for item in outbounds if item.get("protocol") == "wireguard"}
    selectors = _balancer_selectors(config)
    referenced_tags = _referenced_outbound_tags(config)
    if outbounds:
        referenced_tags.add(outbounds[0].get("tag"))
    removed_inbounds = set()
    removed_outbounds = set()
    removed_rules = set()
    for index, rule in enumerate(rules):
        outbound_tag = rule.get("outboundTag", "")
        if not isinstance(outbound_tag, str) or outbound_tag not in wireguard:
            continue
        if not re.fullmatch(r"warp-test-[1-9][0-9]*", outbound_tag):
            continue
        if outbound_tag in referenced_tags or outbound_tag.startswith(selectors):
            continue
        inbound_tag = f"{outbound_tag}-socks"
        inbound = socks.get(inbound_tag)
        if not inbound or inbound != _socks_inbound(inbound_tag, inbound.get("port"), False):
            continue
        if rule != _endpoint_rule(inbound_tag, outbound_tag):
            continue
        if any(index != other and (
            candidate.get("outboundTag") == outbound_tag
            or inbound_tag in (candidate.get("inboundTag") or [])
        ) for other, candidate in enumerate(rules)):
            continue
        removed_inbounds.add(inbound_tag)
        removed_outbounds.add(outbound_tag)
        removed_rules.add(index)
    if not removed_rules:
        return False
    config["inbounds"] = [item for item in inbounds if item.get("tag") not in removed_inbounds]
    config["outbounds"] = [item for item in outbounds if item.get("tag") not in removed_outbounds]
    config["routing"]["rules"] = [rule for index, rule in enumerate(rules) if index not in removed_rules]
    return True


def endpoint_probe_config(config: dict, outbound: dict, endpoints: list[str], ports: list[int]) -> dict:
    """Дополнить действующий маршрут входом и выходом для каждого кандидата."""
    probe = deepcopy(config)
    inbounds = probe.get("inbounds", [])
    outbounds = probe.setdefault("outbounds", [])
    if not outbounds:
        raise ConflictError("Cannot probe WARP without an existing default outbound.")
    tags = {item.get("tag") for item in (*inbounds, *outbounds)} | _referenced_outbound_tags(probe)
    selectors = _balancer_selectors(probe)
    if "warp-test-".startswith(selectors) or all(
        f"warp-test-{digit}".startswith(selectors) for digit in range(1, 10)
    ):
        raise ConflictError("An existing balancer selects all WARP probe tags.")
    rules = []
    number = 0
    for endpoint, port in zip(endpoints, ports, strict=True):
        while True:
            number += 1
            outbound_tag = f"warp-test-{number}"
            inbound_tag = f"{outbound_tag}-socks"
            if outbound_tag not in tags and inbound_tag not in tags and not outbound_tag.startswith(selectors):
                break
        candidate = outbound_at_endpoint(outbound, endpoint)
        candidate["tag"] = outbound_tag
        outbounds.append(candidate)
        add_socks_inbound(probe, inbound_tag, port, udp=False)
        rules.append(_endpoint_rule(inbound_tag, outbound_tag))
    probe.setdefault("routing", {}).setdefault("rules", [])[0:0] = rules
    return probe
