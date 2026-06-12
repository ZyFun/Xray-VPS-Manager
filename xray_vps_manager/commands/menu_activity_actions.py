"""Activity report and settings actions used by the interactive menu."""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from xray_vps_manager.core.paths import CONFIG_PATH, XRAY_BIN
from xray_vps_manager.core.server_env import ORDERED_ENV_KEYS, read_server_env, write_server_env
from xray_vps_manager.core.terminal import table_border, table_row
from xray_vps_manager.xray.config import load_config as load_xray_config
from xray_vps_manager.xray.config import save_config

CASCADE_UPSTREAM_TAG = "cascade-upstream"
WARP_OUTBOUND_TAG = "warp-out"
DIRECT_OUTBOUND_TAG = "direct"
XRAY_GEOIP_OUTBOUND_PREFIX = "geoip-warning-"
XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV = "ACTIVITY_XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY"
MENU_ENV_REQUIRED_KEYS = [
    "SERVER_ADDR",
    "SERVER_NAME",
    "PORT",
    "REALITY_SNI",
    "REALITY_DEST",
    "FINGERPRINT",
    "MANAGER_TIMEZONE",
]
GEOIP_REGION_PRESETS = [
    ("RU", "Россия"),
    ("US", "США"),
    ("CN", "Китай"),
    ("KZ", "Казахстан"),
    ("BY", "Беларусь"),
    ("UA", "Украина"),
    ("TR", "Турция"),
    ("DE", "Германия"),
    ("NL", "Нидерланды"),
    ("FI", "Финляндия"),
    ("EE", "Эстония"),
    ("GB", "Великобритания"),
]

CommandRunner = Callable[[list[str]], None]
ClientChooser = Callable[[str, str], str]


def show_activity_status(call: CommandRunner) -> None:
    call(["xray-activity", "status"])


def enable_activity_parser(call: CommandRunner) -> None:
    call(["xray-activity", "enable"])


def disable_activity_parser(call: CommandRunner) -> None:
    call(["xray-activity", "disable"])


def sync_activity_now(call: CommandRunner) -> None:
    call(["xray-activity", "sync"])


def die(message: str) -> None:
    raise SystemExit(message)


def run(command: list[str], **kwargs) -> None:
    subprocess.run(command, check=True, **kwargs)


def load_config() -> dict:
    try:
        return load_xray_config()
    except FileNotFoundError as exc:
        die(str(exc))


def apply_config(config: dict) -> Path:
    backup = save_config(config)
    try:
        run([str(XRAY_BIN), "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")
    return backup


def write_server_env_values(values: dict[str, str]) -> None:
    updated = dict(values)
    for key in MENU_ENV_REQUIRED_KEYS:
        updated.setdefault(key, "")
    write_server_env(updated, ordered_keys=ORDERED_ENV_KEYS)


def routing_rules(config: dict) -> list[dict]:
    routing = config.setdefault("routing", {})
    routing.setdefault("domainStrategy", "IPIfNonMatch")
    return routing.setdefault("rules", [])


def ensure_direct_outbound(config: dict) -> dict:
    outbounds = config.setdefault("outbounds", [])
    for outbound in outbounds:
        if outbound.get("tag") == DIRECT_OUTBOUND_TAG:
            return outbound
    outbound = {"tag": DIRECT_OUTBOUND_TAG, "protocol": "freedom"}
    outbounds.append(outbound)
    return outbound


def is_xray_geoip_warning_tag(tag: str | None) -> bool:
    return str(tag or "").startswith(XRAY_GEOIP_OUTBOUND_PREFIX)


def xray_geoip_warning_tag(code: str) -> str:
    return f"{XRAY_GEOIP_OUTBOUND_PREFIX}{code.upper()}"


def xray_geoip_warning_source_outbound(config: dict) -> dict:
    outbounds = config.setdefault("outbounds", [])
    for outbound in outbounds:
        if outbound.get("tag") == WARP_OUTBOUND_TAG and any(
            rule.get("outboundTag") == WARP_OUTBOUND_TAG for rule in routing_rules(config)
        ):
            return outbound
    for outbound in outbounds:
        if outbound.get("tag") == CASCADE_UPSTREAM_TAG:
            return outbound
    return ensure_direct_outbound(config)


def remove_xray_geoip_warning_config(config: dict) -> bool:
    changed = False
    old_outbounds = config.setdefault("outbounds", [])
    new_outbounds = [outbound for outbound in old_outbounds if not is_xray_geoip_warning_tag(outbound.get("tag"))]
    if new_outbounds != old_outbounds:
        changed = True
        config["outbounds"] = new_outbounds

    rules = routing_rules(config)
    new_rules = [rule for rule in rules if not is_xray_geoip_warning_tag(rule.get("outboundTag"))]
    if new_rules != rules:
        changed = True
        config["routing"]["rules"] = new_rules
    return changed


def insert_before_catchall_route(rules: list[dict], rule: dict) -> None:
    insert_index = len(rules)
    for index, existing in enumerate(rules):
        if existing.get("outboundTag") in (WARP_OUTBOUND_TAG, CASCADE_UPSTREAM_TAG) and existing.get("network") == "tcp,udp":
            insert_index = index
            break
    rules.insert(insert_index, rule)


def apply_xray_geoip_warning_config(config: dict, code: str) -> None:
    code = code.upper()
    remove_xray_geoip_warning_config(config)
    routing = config.setdefault("routing", {})
    routing["domainStrategy"] = "IPOnDemand"
    source = xray_geoip_warning_source_outbound(config)
    outbound = copy.deepcopy(source)
    tag = xray_geoip_warning_tag(code)
    outbound["tag"] = tag
    config.setdefault("outbounds", []).append(outbound)
    rule = {
        "type": "field",
        "ip": [f"geoip:{code.lower()}"],
        "outboundTag": tag,
    }
    insert_before_catchall_route(routing_rules(config), rule)


def restore_xray_geoip_domain_strategy(config: dict, values: dict[str, str]) -> None:
    previous = values.pop(XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV, "")
    routing = config.setdefault("routing", {})
    if previous:
        routing["domainStrategy"] = previous
    elif routing.get("domainStrategy") == "IPOnDemand":
        routing["domainStrategy"] = "IPIfNonMatch"


def ask_activity_days(default: int = 7) -> str:
    value = input(f"Период в днях [{default}]: ").strip() or str(default)
    if not re.fullmatch(r"[0-9]+", value) or int(value, 10) < 1:
        print(f"Некорректный период, использую {default} дней.")
        return str(default)
    return value


def activity_client_report(choose_client: ClientChooser, call: CommandRunner) -> None:
    name = choose_client("просмотра журнала активности", "all")
    if not name:
        print("Действие отменено.")
        return
    call(["xray-activity", "client", name, ask_activity_days(7)])


def activity_suspicious_report(call: CommandRunner) -> None:
    call(["xray-activity", "suspicious", ask_activity_days(7)])


def activity_geoip_risk_details(call: CommandRunner) -> None:
    call(["xray-activity", "geoip-risks", ask_activity_days(7)])


def activity_retention_value() -> str:
    result = subprocess.run(
        ["xray-activity", "retention"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = result.stdout + result.stderr
    match = re.search(r"(\d+)\s+days", output)
    return match.group(1) if match else "365"


def update_activity_retention(call: CommandRunner) -> None:
    current = activity_retention_value()
    print("ACTIVITY_RETENTION_DAYS: сколько дней хранить детальные события журнала активности.")
    print("По умолчанию 365 дней. Допустимый диапазон: 1-3650 дней.")
    print("Старые события старше нового срока будут удалены сразу после изменения.")
    value = input(f"ACTIVITY_RETENTION_DAYS [{current}] (Enter - оставить без изменений): ").strip()
    if not value:
        print("Изменение отменено.")
        return
    call(["xray-activity", "retention", value])


def activity_risk_limit_values() -> dict[str, str]:
    defaults = {
        "burst_events": "1000",
        "burst_window": "15",
        "unique_hosts": "500",
        "unique_ports": "20",
    }
    result = subprocess.run(
        ["xray-activity", "risk-limits"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = result.stdout + result.stderr
    patterns = {
        "burst_events": r"\|\s*Burst events\s*\|\s*([0-9]+)\s*\|",
        "burst_window": r"\|\s*Burst window\s*\|\s*([0-9]+)\s+minutes\s*\|",
        "unique_hosts": r"\|\s*Unique hosts\s*\|\s*([0-9]+)\s*\|",
        "unique_ports": r"\|\s*Unique ports\s*\|\s*([0-9]+)\s*\|",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        if match:
            defaults[key] = match.group(1)
    return defaults


def update_activity_risk_limits(call: CommandRunner) -> None:
    current = activity_risk_limit_values()
    print("Лимиты suspicious определяют, когда клиент попадёт в отчёт подозрительной активности.")
    print("По умолчанию burst = 1000 событий за 15 минут, чтобы обычный стриминг не попадал в false positive.")
    print("Нажми Enter на любом пункте, чтобы оставить текущее значение.")
    burst_events = input(f"BURST_EVENTS [{current['burst_events']}]: ").strip() or current["burst_events"]
    burst_window = input(f"BURST_WINDOW_MINUTES [{current['burst_window']}]: ").strip() or current["burst_window"]
    unique_hosts = input(f"UNIQUE_HOSTS [{current['unique_hosts']}]: ").strip() or current["unique_hosts"]
    unique_ports = input(f"UNIQUE_PORTS [{current['unique_ports']}]: ").strip() or current["unique_ports"]
    call(["xray-activity", "risk-limits", "set", burst_events, burst_window, unique_hosts, unique_ports])


def geoip_codes(query: str = "") -> list[str]:
    command = ["xray-activity", "geo-list"]
    if query:
        command.append(query)
    result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        print(result.stderr.strip() or "Не удалось получить список GeoIP-регионов.")
        return []
    return [line.strip().upper() for line in result.stdout.splitlines() if line.strip()]


def print_geoip_region_table(rows: list[tuple[str, str]], include_search: bool = False) -> None:
    headers = ("№", "CODE", "ОПИСАНИЕ")
    values = [(str(index), code, label) for index, (code, label) in enumerate(rows, start=1)]
    if include_search:
        values.append(("S", "Поиск", "найти другой код региона в geoip.dat"))
    values.append(("0", "Назад", ""))
    widths = [
        max(len(headers[column]), *(len(str(row[column])) for row in values))
        for column in range(len(headers))
    ]
    border = table_border(widths)
    print(border)
    print(table_row(headers, widths))
    print(border)
    for row in values:
        print(table_row(row, widths))
    print(border)


def choose_geoip_region_from_rows(rows: list[tuple[str, str]], prompt: str) -> str:
    while True:
        choice = input(prompt).strip().lower()
        if choice in ("", "0"):
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1][0]
        print("Неизвестный регион. Выбери номер из списка или 0 для возврата.")


def search_geoip_region() -> str:
    while True:
        query = input("Фильтр региона или GeoIP code, например Россия, RU или U (Enter - назад): ").strip()
        if not query:
            return ""
        query_code = query.upper()
        query_text = query.lower()
        rows = []
        seen = set()
        for code, label in GEOIP_REGION_PRESETS:
            if query_code in code or query_text in label.lower():
                rows.append((code, label))
                seen.add(code)
        for code in geoip_codes(query_code):
            if code not in seen:
                rows.append((code, "geoip.dat"))
                seen.add(code)
        if not rows:
            print("По этому фильтру ничего не найдено.")
            continue
        displayed = rows[:30]
        print_geoip_region_table(displayed)
        selected = choose_geoip_region_from_rows(displayed, "GeoIP region: ")
        return selected


def choose_geoip_region() -> str:
    while True:
        print("Выбери GeoIP-регион. Если IP назначения попадает в этот регион, отчёты покажут предупреждение о split tunneling.")
        print_geoip_region_table(GEOIP_REGION_PRESETS, include_search=True)
        choice = input("GeoIP region: ").strip().lower()
        if choice in ("", "0"):
            return ""
        if choice in ("s", "search", "поиск"):
            selected = search_geoip_region()
            if selected:
                return selected
            continue
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(GEOIP_REGION_PRESETS):
                return GEOIP_REGION_PRESETS[index - 1][0]
        print("Неизвестный регион. Выбери номер, S для поиска или 0 для возврата.")


def set_xray_geoip_routing_region() -> None:
    print("Эта настройка добавит Xray routing rule вида geoip:CODE -> отдельный outbound tag.")
    print("Маршрут трафика не меняется: outbound дублирует текущий cascade-upstream или direct, но access log получит отдельную метку.")
    print("Для доменных целей routing будет временно переключён в IPOnDemand, иначе catch-all может сработать до GeoIP-проверки.")
    code = choose_geoip_region()
    if not code:
        print("Действие отменено.")
        return
    config = load_config()
    previous_strategy = config.setdefault("routing", {}).get("domainStrategy", "")
    apply_xray_geoip_warning_config(config, code)
    backup = apply_config(config)
    values = read_server_env()
    if not values.get(XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV) and previous_strategy != "IPOnDemand":
        values[XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV] = previous_strategy
    values["ACTIVITY_XRAY_GEOIP_WARNING_CODE"] = code
    write_server_env_values(values)
    print(f"Xray routing GeoIP-предупреждения включены для региона: {code}")
    print(f"Outbound tag: {xray_geoip_warning_tag(code)}")
    print("Routing domainStrategy: IPOnDemand")
    print(f"Backup: {backup}")


def disable_xray_geoip_routing_region() -> None:
    config = load_config()
    remove_xray_geoip_warning_config(config)
    values = read_server_env()
    restore_xray_geoip_domain_strategy(config, values)
    backup = apply_config(config)
    values["ACTIVITY_XRAY_GEOIP_WARNING_CODE"] = ""
    write_server_env_values(values)
    print("Xray routing GeoIP-предупреждения отключены.")
    print(f"Backup: {backup}")
