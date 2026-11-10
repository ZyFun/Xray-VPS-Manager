"""Чистые правила блокировки BitTorrent в конфигурации Xray."""


def ensure_blocked_outbound(config: dict) -> bool:
    outbounds = config.setdefault("outbounds", [])
    if not any(outbound.get("tag") == "blocked" for outbound in outbounds):
        outbounds.append({"tag": "blocked", "protocol": "blackhole"})
        return True
    return False


def routing_rules(config: dict) -> list[dict]:
    routing = config.setdefault("routing", {})
    routing.setdefault("domainStrategy", "IPIfNonMatch")
    return routing.setdefault("rules", [])


def rule_values(rule: dict, key: str) -> list:
    value = rule.get(key, [])
    return [value] if isinstance(value, str) else value if isinstance(value, list) else []


def is_api_rule(rule: dict) -> bool:
    return rule.get("outboundTag") == "api" or "api" in rule_values(rule, "inboundTag")


def is_bittorrent_rule(rule: dict) -> bool:
    return "bittorrent" in rule_values(rule, "protocol")


def torrent_block_rule() -> dict:
    return {"type": "field", "protocol": ["bittorrent"], "outboundTag": "blocked"}


def torrent_block_enabled(config: dict) -> bool:
    return any(rule.get("outboundTag") == "blocked" and is_bittorrent_rule(rule)
               for rule in routing_rules(config))


def insert_torrent_rule(rules: list[dict]) -> None:
    index = 0
    while index < len(rules) and is_api_rule(rules[index]):
        index += 1
    rules.insert(index, torrent_block_rule())


def set_blocked(config: dict, blocked: bool) -> bool:
    changed = ensure_blocked_outbound(config)
    rules = routing_rules(config)
    updated = [rule for rule in rules if not is_bittorrent_rule(rule)]
    if blocked:
        insert_torrent_rule(updated)
    if updated != rules:
        changed = True
    if changed:
        config["routing"]["rules"] = updated
    return changed
