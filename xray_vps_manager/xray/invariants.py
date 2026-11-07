"""Чистая проверка инвариантов менеджера без исправления кандидата."""

from xray_vps_manager.core.errors import ConfigInvariantError
from xray_vps_manager.xray import cascade
from xray_vps_manager.xray.config import managed_connection_inbounds


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigInvariantError(message, stage="invariants")


def _objects(value: object, label: str) -> list[dict]:
    _require(isinstance(value, list) and all(isinstance(item, dict) for item in value),
             f"{label}: ожидается список объектов.")
    return value


def _tags(items: list[dict], label: str) -> set[str]:
    # Xray допускает анонимные объекты; уникальны именно заданные теги.
    tags = [item["tag"] for item in items if item.get("tag") not in (None, "")]
    _require(all(isinstance(tag, str) and tag.strip() for tag in tags),
             f"{label}: теги должны быть непустыми строками.")
    _require(len(tags) == len(set(tags)), f"{label}: теги должны быть уникальны.")
    return set(tags)


def validate(config: dict) -> None:
    """Проверить пять групп инвариантов; не читать БД и не нормализовать config.

    Email проверяется только на непустую строку у управляемых подключений.
    Разбор формата и метаданных остаётся у владельца email (VIREI-123).
    """
    _require(isinstance(config, dict), "Конфигурация должна быть объектом.")
    inbounds = _objects(config.get("inbounds", []), "inbounds")
    outbounds = _objects(config.get("outbounds", []), "outbounds")
    routing = config.get("routing", {})
    _require(isinstance(routing, dict), "routing должен быть объектом.")
    rules = _objects(routing.get("rules", []), "routing.rules")
    balancers = _objects(routing.get("balancers", []), "routing.balancers")

    # 1. Неявный маршрут всегда direct; blocked нужен управляемым запретам.
    _require(bool(outbounds) and outbounds[0].get("tag") == "direct",
             "Первый outbound должен иметь тег direct.")
    inbound_tags = _tags(inbounds, "inbounds")
    outbound_tags = _tags(outbounds, "outbounds")
    balancer_tags = _tags(balancers, "routing.balancers")
    _require("blocked" in outbound_tags, "Отсутствует outbound blocked.")

    # 2. Допускается отсутствие catch-all (неявный direct), но не два маршрута.
    catchalls = [rule for rule in rules if cascade.is_catchall_rule(rule)
                and (rule.get("outboundTag") in ("direct", "warp-out")
                     or cascade.is_cascade_tag(rule.get("outboundTag")))]
    _require(len(catchalls) <= 1, "Допускается не более одного управляемого catch-all.")
    catchall_tag = catchalls[0].get("outboundTag", "") if catchalls else ""

    # 3. API — виртуальный outbound Xray; остальные ссылки должны разрешаться.
    api = config.get("api", {})
    _require(isinstance(api, dict), "api должен быть объектом.")
    api_tag = api.get("tag")
    _require(isinstance(api_tag, str) and bool(api_tag.strip()), "Отсутствует тег api.")
    _require(bool(rules) and rules[0].get("inboundTag") == [api_tag]
             and rules[0].get("outboundTag") == api_tag,
             "Правило api должно быть первым.")
    for rule in rules:
        if "inboundTag" in rule:
            values = rule.get("inboundTag")
            _require(isinstance(values, list) and all(isinstance(tag, str) and tag in inbound_tags for tag in values),
                     "Правило ссылается на неизвестный inbound.")
        if "outboundTag" in rule:
            tag = rule.get("outboundTag")
            _require(isinstance(tag, str) and tag in outbound_tags | {api_tag},
                     "Правило ссылается на неизвестный outbound.")
        if "balancerTag" in rule:
            tag = rule.get("balancerTag")
            _require(isinstance(tag, str) and tag in balancer_tags,
                     "Правило ссылается на неизвестный балансер.")
            if tag.startswith("client-route-"):
                _require(cascade.is_cascade_tag(catchall_tag),
                         "Клиентские маршруты разрешены только при каскадном catch-all.")
    for balancer in balancers:
        if str(balancer.get("tag", "")).startswith("client-route-"):
            selectors = balancer.get("selector")
            _require(isinstance(selectors, list) and bool(selectors)
                     and all(isinstance(tag, str) and tag in outbound_tags for tag in selectors),
                     "Клиентский балансер должен ссылаться на существующие outbounds.")
            _require(cascade.is_cascade_tag(catchall_tag),
                     "Клиентский балансер разрешён только при каскадном catch-all.")

    # 4. Включённые запреты должны срабатывать раньше GeoIP-предупреждений.
    warning_indices = [index for index, rule in enumerate(rules)
                       if str(rule.get("outboundTag", "")).startswith("geoip-warning-")]
    if warning_indices:
        first_warning = min(warning_indices)
        for index, rule in enumerate(rules):
            if rule.get("outboundTag") == "blocked" and (
                "bittorrent" in cascade.rule_values(rule, "protocol")
                or "geoip:private" in cascade.rule_values(rule, "ip")
            ):
                _require(index < first_warning,
                         "Правила BitTorrent/private должны находиться выше GeoIP warning.")

    # 5. Не запрещаем старые email без метаданных и не переписываем их.
    for inbound in managed_connection_inbounds(config):
        settings = inbound.get("settings", {})
        _require(isinstance(settings, dict), "settings подключения должен быть объектом.")
        clients = _objects(settings.get("clients", settings.get("users", [])), "Клиенты подключения")
        for client in clients:
            email = client.get("email")
            _require(isinstance(email, str) and bool(email.strip()),
                     "Email клиента управляемого подключения должен быть непустой строкой.")
