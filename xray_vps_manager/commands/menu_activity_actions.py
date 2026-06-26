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
from xray_vps_manager.xray import cascade as cascade_config
from xray_vps_manager.xray import bypass as bypass_config
from xray_vps_manager.xray import client_routes
from xray_vps_manager.xray.config import load_config as load_xray_config
from xray_vps_manager.xray.config import save_config

WARP_OUTBOUND_TAG = "warp-out"
DIRECT_OUTBOUND_TAG = "direct"
XRAY_GEOIP_OUTBOUND_PREFIX = "geoip-warning-"
XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV = bypass_config.GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV
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


def update_alert_detection(call: CommandRunner) -> None:
    print("ACTIVITY_ALERTS_ENABLED: отдельный alert-log и Telegram GeoIP/suspicious уведомления.")
    print("Значения: on/off. Подробный detailed log этим не меняется.")
    value = input("Alert detection [on/off] (Enter - отмена): ").strip().lower()
    if not value:
        print("Изменение отменено.")
        return
    call(["xray-activity", "alert-detection", value])


def show_geoip_status(call: CommandRunner) -> None:
    call(["xray-activity", "geoip-status"])


def show_retention_overview(call: CommandRunner) -> None:
    call(["xray-activity", "retention-overview"])


def show_detail_mode(call: CommandRunner) -> None:
    call(["xray-activity", "detail-mode"])


def update_detail_mode(call: CommandRunner) -> None:
    print("Режим detailed activity:")
    print("1 - выключено")
    print("2 - все клиенты")
    print("3 - выбранные клиенты")
    choice = input("Выбор режима (Enter - отмена): ").strip()
    mapping = {
        "1": "off",
        "2": "all",
        "3": "selected",
    }
    if not choice:
        print("Изменение отменено.")
        return
    mode = mapping.get(choice)
    if not mode:
        print("Неизвестный режим.")
        return
    call(["xray-activity", "detail-mode", mode])


def set_detail_mode_all(call: CommandRunner) -> None:
    call(["xray-activity", "detail-mode", "all"])


def set_detail_mode_off(call: CommandRunner) -> None:
    call(["xray-activity", "detail-mode", "off"])


def set_detail_mode_selected(call: CommandRunner) -> None:
    call(["xray-activity", "detail-mode", "selected"])


def choose_detail_clients(choose_client: ClientChooser, call: CommandRunner) -> None:
    selected: list[str] = []
    while True:
        name = choose_client("подробной записи активности", "all")
        if not name:
            break
        if name not in selected:
            selected.append(name)
        answer = input("Добавить ещё клиента? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            break
    if not selected:
        print("Список выбранных клиентов не изменён.")
        return
    call(["xray-activity", "detail-clients", "set", *selected])
    call(["xray-activity", "detail-mode", "selected"])


def clear_detail_clients(call: CommandRunner) -> None:
    call(["xray-activity", "detail-clients", "clear"])


def show_alert_log(call: CommandRunner) -> None:
    call(["xray-activity", "alerts", "50"])


def show_geoip_alert_log(call: CommandRunner) -> None:
    call(["xray-activity", "alerts", "50", "geoip"])


def show_activity_counters_day(call: CommandRunner) -> None:
    call(["xray-activity", "counters", "day", "50"])


def show_activity_counters_hour(call: CommandRunner) -> None:
    call(["xray-activity", "counters", "hour", "50"])


def show_activity_counters_today(call: CommandRunner) -> None:
    call(["xray-activity", "counters-today", "50"])


def show_activity_counters_week(call: CommandRunner) -> None:
    call(["xray-activity", "counters-week", "50"])


def show_activity_counters_hour_client(choose_client: ClientChooser, call: CommandRunner) -> None:
    name = choose_client("почасовой статистики activity", "all")
    if not name:
        return
    call(["xray-activity", "counters", "hour", "50", name])


def show_activity_counters_day_client(choose_client: ClientChooser, call: CommandRunner) -> None:
    name = choose_client("дневной статистики activity", "all")
    if not name:
        return
    call(["xray-activity", "counters", "day", "50", name])


def show_activity_counter_growth(call: CommandRunner) -> None:
    call(["xray-activity", "counters-growth", "50"])


def show_xray_errors(call: CommandRunner) -> None:
    call(["xray-activity", "errors", "50"])


def show_xray_error_warnings(call: CommandRunner) -> None:
    call(["xray-activity", "errors", "50", "warning"])


def show_xray_error_summary(call: CommandRunner) -> None:
    call(["xray-activity", "errors-summary", "7"])


def show_xray_errors_7_days(call: CommandRunner) -> None:
    call(["xray-activity", "errors-days", "7", "50"])


def show_xray_error_warning_errors(call: CommandRunner) -> None:
    call(["xray-activity", "errors-days", "7", "50", "warning,error"])


def show_xray_error_detail(call: CommandRunner) -> None:
    value = input("ID ошибки (Enter - отмена): ").strip()
    if not value:
        print("Действие отменено.")
        return
    call(["xray-activity", "error-detail", value])


def show_raw_logs(call: CommandRunner) -> None:
    call(["xray-activity", "raw-logs"])


def show_raw_log_archives(call: CommandRunner) -> None:
    call(["xray-activity", "raw-log-archives"])


def rotate_raw_logs_now(call: CommandRunner) -> None:
    call(["xray-activity", "rotate-raw-logs"])


def sync_raw_log_timer(call: CommandRunner) -> None:
    call(["xray-activity", "raw-log-timer-sync"])


def update_activity_alert_retention(call: CommandRunner) -> None:
    print("ACTIVITY_ALERT_RETENTION_DAYS: сколько дней хранить отдельный alert-log.")
    print("По умолчанию 90 дней.")
    value = input("ACTIVITY_ALERT_RETENTION_DAYS (Enter - отмена): ").strip()
    if not value:
        print("Изменение отменено.")
        return
    call(["xray-activity", "alert-retention", value])


def update_xray_error_retention(call: CommandRunner) -> None:
    print("XRAY_ERROR_EVENT_RETENTION_DAYS: сколько дней хранить нормализованные ошибки Xray/manager.")
    print("По умолчанию 180 дней.")
    value = input("XRAY_ERROR_EVENT_RETENTION_DAYS (Enter - отмена): ").strip()
    if not value:
        print("Изменение отменено.")
        return
    call(["xray-activity", "error-retention", value])


def update_raw_access_log_retention(call: CommandRunner) -> None:
    print("XRAY_ACCESS_LOG_RETENTION_DAYS: сколько дней хранить сырой access.log и архивы.")
    print("По умолчанию 180 дней.")
    value = input("XRAY_ACCESS_LOG_RETENTION_DAYS (Enter - отмена): ").strip()
    if not value:
        print("Изменение отменено.")
        return
    call(["xray-activity", "raw-log-retention", "access", value])


def update_raw_error_log_retention(call: CommandRunner) -> None:
    print("XRAY_ERROR_LOG_RETENTION_DAYS: сколько дней хранить сырой error.log и архивы.")
    print("По умолчанию 180 дней.")
    value = input("XRAY_ERROR_LOG_RETENTION_DAYS (Enter - отмена): ").strip()
    if not value:
        print("Изменение отменено.")
        return
    call(["xray-activity", "raw-log-retention", "error", value])


def update_raw_log_rotate_time(call: CommandRunner) -> None:
    print("XRAY_RAW_LOG_ROTATE_TIME: время ротации в MANAGER_TIMEZONE, формат HH:MM.")
    print("По умолчанию 03:00.")
    value = input("XRAY_RAW_LOG_ROTATE_TIME (Enter - отмена): ").strip()
    if not value:
        print("Изменение отменено.")
        return
    call(["xray-activity", "raw-log-rotate-time", value])


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
    return bypass_config.routing_rules(config)


def ensure_direct_outbound(config: dict) -> dict:
    return cascade_config.ensure_direct_outbound(config)


def is_xray_geoip_warning_tag(tag: str | None) -> bool:
    return bypass_config.is_geoip_warning_tag(tag)


def xray_geoip_warning_tag(code: str) -> str:
    return bypass_config.geoip_warning_tag(code)


def xray_geoip_warning_source_outbound(config: dict) -> dict:
    return bypass_config.warning_source_outbound(config, "RU")


def remove_xray_geoip_warning_config(config: dict) -> bool:
    changed = False
    for tag in bypass_config.warning_tags(config):
        try:
            region = bypass_config.region_from_geoip_warning_tag(tag)
        except ValueError:
            continue
        if bypass_config.configured_bypass_for_warning(config, region):
            continue
        changed = bypass_config.remove_geoip_warning_route(config, region) or changed
    return changed


def insert_before_catchall_route(rules: list[dict], rule: dict) -> None:
    config = {"routing": {"rules": rules}}
    bypass_config.insert_before_client_or_catchall(config, rule)


def apply_xray_geoip_warning_config(config: dict, code: str) -> None:
    code = bypass_config.normalize_region_code(code)
    source = bypass_config.warning_source_outbound(config, code)
    bypass_config.ensure_geoip_warning_outbound(config, code, source)
    bypass_config.ensure_geoip_warning_rule(config, code)
    bypass_config.ensure_geoip_domain_strategy(config)


def restore_xray_geoip_domain_strategy(config: dict, values: dict[str, str]) -> None:
    bypass_config.restore_geoip_domain_strategy_if_unused(config, values)


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


def activity_backfill_from_menu(choose_client: ClientChooser, call: CommandRunner, confirm: Callable[[str], bool]) -> None:
    target = choose_client("backfill detailed activity", "all")
    if not target:
        print("Действие отменено.")
        return
    start = input("START_DATE YYYY-MM-DD: ").strip()
    end = input("END_DATE YYYY-MM-DD: ").strip()
    if not start or not end:
        print("Действие отменено.")
        return
    call(["xray-activity", "backfill", target, start, end, "--dry-run"])
    if confirm("Импортировать найденные события в detailed activity?"):
        call(["xray-activity", "backfill", target, start, end, "--apply", "--yes"])


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
    print("Маршрут трафика не меняется: outbound дублирует текущий активный cascade-*, WARP или direct, но access log получит отдельную метку.")
    print("Для доменных целей routing будет временно переключён в IPOnDemand, иначе catch-all может сработать до GeoIP-проверки.")
    code = choose_geoip_region()
    if not code:
        print("Действие отменено.")
        return
    config = load_config()
    values = read_server_env()
    previous_code = values.get("ACTIVITY_XRAY_GEOIP_WARNING_CODE", "")
    if previous_code and previous_code.upper() != code.upper() and not bypass_config.configured_bypass_for_warning(config, previous_code):
        bypass_config.remove_geoip_warning_route(config, previous_code)
    bypass_config.ensure_geoip_domain_strategy(config, values)
    apply_xray_geoip_warning_config(config, code)
    backup = apply_config(config)
    values["ACTIVITY_XRAY_GEOIP_WARNING_CODE"] = code
    write_server_env_values(values)
    print(f"Xray routing GeoIP-предупреждения включены для региона: {code}")
    print(f"Outbound tag: {xray_geoip_warning_tag(code)}")
    print("Routing domainStrategy: IPOnDemand")
    print(f"Backup: {backup}")


def disable_xray_geoip_routing_region() -> None:
    config = load_config()
    values = read_server_env()
    code = values.get("ACTIVITY_XRAY_GEOIP_WARNING_CODE", "")
    if code and not bypass_config.configured_bypass_for_warning(config, code):
        bypass_config.remove_geoip_warning_route(config, code)
    elif not code:
        remove_xray_geoip_warning_config(config)
    restore_xray_geoip_domain_strategy(config, values)
    backup = apply_config(config)
    values["ACTIVITY_XRAY_GEOIP_WARNING_CODE"] = ""
    write_server_env_values(values)
    print("Xray routing GeoIP-предупреждения отключены.")
    print(f"Backup: {backup}")
