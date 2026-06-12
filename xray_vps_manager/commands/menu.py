#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from xray_vps_manager.commands import (
    menu_activity_actions,
    menu_activity_exception_actions,
    menu_activity_export_actions,
    menu_backup_actions,
    menu_client_actions,
    menu_reality_actions,
    menu_security_actions,
    menu_telegram_actions,
    menu_traffic_actions,
)
from xray_vps_manager.core.server_env import ORDERED_ENV_KEYS, read_server_env, write_server_env as write_server_env_file
from xray_vps_manager.core.terminal import table_border, table_row

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
CLIENT_LINK_PATH = Path("/root/xray-reality-client.txt")
XRAY_ASSET_DIR = Path("/usr/local/share/xray")
MENU_VERSION = "v1.0.0"
MENU_UPDATED = "2026-06-12 16:25 UTC"
SECURITY_AUDIT_ENV_KEY = "SECURITY_AUDIT_LAST_RUN"
SECURITY_AUDIT_STALE_DAYS = 30
MENU_ENV_REQUIRED_KEYS = [
    "SERVER_ADDR",
    "SERVER_NAME",
    "PORT",
    "REALITY_SNI",
    "REALITY_DEST",
    "FINGERPRINT",
    "MANAGER_TIMEZONE",
]
GREEN = "\033[92m"
RED = "\033[31m"
RESET = "\033[0m"
TIMEZONE_PRESETS = [
    ("", "Системное время сервера"),
    ("Europe/Moscow", "Москва"),
    ("Europe/Kaliningrad", "Калининград"),
    ("Europe/Samara", "Самара"),
    ("Asia/Yekaterinburg", "Екатеринбург"),
    ("Asia/Omsk", "Омск"),
    ("Asia/Novosibirsk", "Новосибирск"),
    ("Asia/Krasnoyarsk", "Красноярск"),
    ("Asia/Irkutsk", "Иркутск"),
    ("Asia/Yakutsk", "Якутск"),
    ("Asia/Vladivostok", "Владивосток"),
    ("Asia/Magadan", "Магадан"),
    ("Asia/Sakhalin", "Сахалин"),
    ("Asia/Kamchatka", "Камчатка"),
    ("UTC", "UTC"),
]
TIMEZONE_SEARCH_LIMIT = 30
def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def current_xray_version():
    try:
        result = subprocess.run(
            ["/usr/local/bin/xray", "version"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return "unknown"
    if result.returncode != 0 or not result.stdout:
        return "unknown"
    match = re.search(r"(\d+(?:\.\d+){1,3})", result.stdout.splitlines()[0])
    return match.group(1) if match else result.stdout.splitlines()[0]


def print_menu_header():
    rows = [
        ("Xray Version", current_xray_version()),
        ("Manager Version", MENU_VERSION),
        ("Manager Updated", MENU_UPDATED),
        ("Geo Assets", geo_assets_header_value()),
        ("Security Audit", security_audit_header_value()),
    ]
    label_width = max(len(row[0]) for row in rows)
    value_width = max(len(row[1]) for row in rows)
    total_width = label_width + value_width + 3
    title_border = "+" + "-" * (total_width + 2) + "+"
    row_border = table_border([label_width, value_width])

    print(title_border)
    print(f"| {'Xray VPS Manager'.ljust(total_width)} |")
    print(row_border)
    for row in rows:
        print(table_row(row, [label_width, value_width]))
    print(row_border)
    warning = security_audit_header_warning()
    if warning:
        print(red(f"! {warning}"))


def main_menu_actions():
    return [
        ("1", "Клиенты"),
        ("2", "Настройки Xray"),
        ("3", "Безопасность"),
        ("4", "Резервные копии"),
        ("5", "Telegram бот"),
        ("0", "Выход"),
    ]


def telegram_menu_actions():
    return [
        ("1", "Статус бота"),
        ("2", "Первичная настройка"),
        ("3", "Донастроить владельца/чат"),
        ("4", "Включить уведомления"),
        ("5", "Отключить уведомления"),
        ("6", "Изменить маршрут"),
        ("7", "Отправить тестовое сообщение"),
        ("8", "Проверить GeoIP-уведомления сейчас"),
        ("9", "Показать подписки клиентов"),
        ("10", "Обработать сообщения пользователей"),
        ("11", "Проверить напоминания об оплате"),
        ("12", "Настроить оплату и округление"),
        ("13", "Изменить имя бота"),
        ("14", "Уведомить о работах на сервере"),
        ("15", "Обновить меню команд Telegram"),
        ("0", "Назад"),
    ]


def client_menu_actions():
    return [
        ("1", "Показать клиентов"),
        ("2", "Добавить клиента"),
        ("3", "Изменить срок доступа"),
        ("4", "Изменить статус оплаты"),
        ("5", "Отключить клиента"),
        ("6", "Включить клиента"),
        ("7", "Удалить клиента"),
        ("8", "Вывести ссылку клиента"),
        ("9", "Проверить просроченных клиентов"),
        ("10", "Трафик"),
        ("11", "Журнал активности"),
        ("0", "Назад"),
    ]


def reality_menu_actions():
    return [
        ("1", "Показать подключения"),
        ("2", "Создать подключение"),
        ("3", "Обновить PORT"),
        ("4", "Обновить REALITY_SNI и REALITY_DEST"),
        ("5", "Обновить PORT, REALITY_SNI и REALITY_DEST"),
        ("6", "Обновить FINGERPRINT"),
        ("7", "Удалить подключение"),
        ("0", "Назад"),
    ]


def cascade_menu_actions():
    return [
        ("1", "Добавить/заменить каскад"),
        ("2", "Проверить каскад"),
        ("3", "Отключить каскад"),
        ("0", "Назад"),
    ]


def xray_settings_menu_actions():
    return [
        ("1", "Статус Xray"),
        ("2", "Перезапустить Xray"),
        ("3", "Проверить config.json"),
        ("4", "Проверить timers"),
        ("5", "Прогнать все тесты сервера"),
        ("6", "Настройки Reality"),
        ("7", "Каскад"),
        ("8", "Обновление Xray"),
        ("9", "Стартовая ссылка"),
        ("10", "WARP"),
        ("11", "Показать доступ к торрентам"),
        ("12", "Запретить торренты"),
        ("13", "Разрешить торренты"),
        ("14", "Показать часовой пояс"),
        ("15", "Изменить часовой пояс"),
        ("0", "Назад"),
    ]


def security_menu_actions():
    return [
        ("1", "Проверить безопасность сервера"),
        ("2", "Показать SSH-доступ"),
        ("3", "Отключить вход по паролю SSH"),
        ("4", "Включить вход по паролю SSH"),
        ("0", "Назад"),
    ]


def update_menu_actions():
    return [
        ("1", "Проверить доступность обновления"),
        ("2", "Проверить latest с текущим config.json"),
        ("3", "Обновить Xray"),
        ("4", "Показать бэкапы Xray"),
        ("5", "Откатить Xray к предыдущей версии"),
        ("6", "Обновить geoip/geosite из Xray release"),
        ("7", "Обновить geoip/geosite из Loyalsoldier"),
        ("8", "Обновить geoip.dat из v2fly"),
        ("0", "Назад"),
    ]


def warp_menu_actions():
    return [
        ("1", "Статус WARP"),
        ("2", "Создать WARP outbound"),
        ("3", "Пересоздать WARP профиль"),
        ("4", "Включить WARP для Xray"),
        ("5", "Отключить WARP"),
        ("6", "Проверить WARP"),
        ("7", "Удалить WARP из config.json"),
        ("8", "Проверить, что WARP отключен"),
        ("0", "Назад"),
    ]


def traffic_menu_actions():
    return [
        ("1", "Просмотр трафика"),
        ("2", "Показать лимиты трафика"),
        ("3", "Установить лимит трафика"),
        ("4", "Убрать лимит трафика"),
        ("5", "Проверить лимиты трафика"),
        ("0", "Назад"),
    ]


def traffic_report_actions():
    return [
        ("1", "За день по часам"),
        ("2", "За неделю по дням"),
        ("3", "За месяц по дням"),
        ("4", "За период по дням"),
        ("0", "Назад"),
    ]


def suspicious_menu_actions():
    return [
        ("1", "Сводка suspicious"),
        ("2", "GeoIP-риски подробно"),
        ("3", "Настройки исключений"),
        ("0", "Назад"),
    ]


def activity_exception_menu_actions():
    return [
        ("1", "Показать исключения"),
        ("2", "Добавить из suspicious"),
        ("3", "Добавить вручную"),
        ("4", "Удалить исключение"),
        ("5", "Удалить все исключения"),
        ("0", "Назад"),
    ]


def backup_menu_actions():
    return [
        ("1", "Создать бэкап на сервере"),
        ("2", "Создать бэкап и показать команду скачивания"),
        ("3", "Показать бэкапы на сервере"),
        ("4", "Восстановить из бэкапа на сервере"),
        ("5", "Показать команду загрузки бэкапа на сервер"),
        ("6", "Удалить бэкап"),
        ("0", "Назад"),
    ]


def activity_menu_actions():
    return [
        ("1", "Статус журнала активности"),
        ("2", "Включить парсинг activity log"),
        ("3", "Отключить парсинг activity log"),
        ("4", "Синхронизировать сейчас"),
        ("5", "Отчёт по клиенту"),
        ("6", "Подозрительная активность"),
        ("7", "Экспорт отчёта по клиенту"),
        ("8", "Показать архивы экспорта"),
        ("9", "Удалить архив экспорта"),
        ("10", "Удалить все архивы экспорта"),
        ("11", "Изменить срок хранения журнала"),
        ("12", "Настроить лимиты suspicious"),
        ("13", "GeoIP routing: выбрать регион"),
        ("14", "GeoIP routing: отключить"),
        ("0", "Назад"),
    ]


def print_menu_table(rows):
    headers = ("№", "Действие")
    widths = [
        max(len(headers[0]), *(len(row[0]) for row in rows)),
        max(len(headers[1]), *(len(row[1]) for row in rows)),
    ]
    border = table_border(widths)

    print(border)
    print(table_row(headers, widths))
    print(border)
    for row in rows:
        print(table_row(row, widths))
    print(border)


def action_separator(title):
    line_width = max(60, len(title) + 10)
    side = max(2, (line_width - len(title) - 2) // 2)
    line = "=" * side + f" {title} "
    line += "=" * max(2, line_width - len(line))
    return line


def begin_action(title):
    print()
    print(action_separator(title))
    print()
    sys.stdout.flush()


def end_action(title):
    print()
    print("=" * len(action_separator(title)))
    sys.stdout.flush()


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def load_config():
    if not CONFIG_PATH.exists():
        die(f"Config not found: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text())


def color(text, code):
    if os.environ.get("NO_COLOR"):
        return text
    return f"{code}{text}{RESET}"


def green(text):
    return color(text, GREEN)


def red(text):
    return color(text, RED)


def server_env():
    return read_server_env(SERVER_ENV_PATH)


def write_server_env_values(values):
    updated = dict(values)
    for key in MENU_ENV_REQUIRED_KEYS:
        updated.setdefault(key, "")
    write_server_env_file(updated, path=SERVER_ENV_PATH, ordered_keys=ORDERED_ENV_KEYS)


def normalize_timezone(value):
    raw = (value or "").strip()
    if raw.lower() in ("", "server", "local", "default", "system", "сервер", "локально", "по умолчанию"):
        return ""
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        die("MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time.")
    return raw


def configured_timezone_name():
    return normalize_timezone(server_env().get("MANAGER_TIMEZONE", ""))


def manager_timezone():
    name = configured_timezone_name()
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


def manager_timezone_label():
    name = configured_timezone_name()
    if name:
        return name
    current = datetime.now().astimezone()
    suffix = current.tzname() or "server local time"
    return f"server local time ({suffix})"


def parse_utc_timestamp(value):
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_manager_time(moment):
    local = moment.astimezone(manager_timezone())
    tz_name = local.tzname() or manager_timezone_label()
    return local.strftime("%Y-%m-%d %H:%M ") + tz_name


def asset_mtime_label(name):
    path = XRAY_ASSET_DIR / name
    if not path.exists():
        return f"{name}: missing"
    moment = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return f"{name}: {format_manager_time(moment)}"


def geo_assets_header_value():
    return "; ".join(asset_mtime_label(name) for name in ("geoip.dat", "geosite.dat"))


def last_security_audit_time():
    return parse_utc_timestamp(server_env().get(SECURITY_AUDIT_ENV_KEY, ""))


def security_audit_header_value():
    last_run = last_security_audit_time()
    if not last_run:
        return "не выполнялась"
    return format_manager_time(last_run)


def security_audit_is_stale():
    last_run = last_security_audit_time()
    if not last_run:
        return True
    return datetime.now(timezone.utc) - last_run >= timedelta(days=SECURITY_AUDIT_STALE_DAYS)


def security_audit_header_warning():
    if not security_audit_is_stale():
        return ""
    return "Рекомендуется запустить: Безопасность -> Проверить безопасность сервера."


def record_security_audit_run():
    values = server_env()
    stamp = utc_stamp()
    values[SECURITY_AUDIT_ENV_KEY] = stamp
    write_server_env_values(values)
    return parse_utc_timestamp(stamp)


def timezone_value_label(value):
    return value or "server"


def print_timezone_selection_table(rows, include_search=False):
    headers = ("№", "TIMEZONE", "ОПИСАНИЕ")
    values = [
        (str(index), timezone_value_label(value), label)
        for index, (value, label) in enumerate(rows, start=1)
    ]
    if include_search:
        values.append(("S", "Поиск", "найти другой часовой пояс"))
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


def timezone_search_matches(query):
    needle = query.strip().lower()
    if not needle:
        return []
    try:
        zones = sorted(available_timezones())
    except Exception:
        zones = sorted(value for value, _ in TIMEZONE_PRESETS if value)
    return [(zone, "") for zone in zones if needle in zone.lower()][:TIMEZONE_SEARCH_LIMIT]


def choose_timezone_from_rows(rows, prompt):
    while True:
        choice = input(prompt).strip()
        if choice in ("", "0"):
            return None
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1][0]
        print("Неизвестный часовой пояс. Выбери номер из списка или 0 для возврата.")


def search_timezone():
    while True:
        query = input("Фильтр timezone, например Moscow или Europe (Enter - назад): ").strip()
        if not query:
            return None
        matches = timezone_search_matches(query)
        if not matches:
            print("По этому фильтру ничего не найдено.")
            continue
        print_timezone_selection_table(matches)
        selected = choose_timezone_from_rows(matches, "Часовой пояс: ")
        if selected is not None:
            return selected


def choose_timezone():
    current = configured_timezone_name() or "server"
    print(f"Текущее значение: {current}")
    while True:
        print_timezone_selection_table(TIMEZONE_PRESETS, include_search=True)
        choice = input("Часовой пояс: ").strip().lower()
        if choice in ("", "0"):
            return None
        if choice in ("s", "search", "поиск"):
            selected = search_timezone()
            if selected is not None:
                return selected
            continue
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(TIMEZONE_PRESETS):
                return TIMEZONE_PRESETS[index - 1][0]
        print("Неизвестный часовой пояс. Выбери номер из списка, S для поиска или 0 для возврата.")


def write_config(config):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    backup = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.bak.{timestamp}")
    shutil.copy2(CONFIG_PATH, backup)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(CONFIG_PATH)
    return backup


def apply_config(config):
    backup = write_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")
    return backup


def ensure_blocked_outbound(config):
    outbounds = config.setdefault("outbounds", [])
    if not any(outbound.get("tag") == "blocked" for outbound in outbounds):
        outbounds.append({"tag": "blocked", "protocol": "blackhole"})
        return True
    return False


def routing_rules(config):
    routing = config.setdefault("routing", {})
    routing.setdefault("domainStrategy", "IPIfNonMatch")
    return routing.setdefault("rules", [])


def rule_values(rule, key):
    value = rule.get(key, [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return []


def is_api_rule(rule):
    return rule.get("outboundTag") == "api" or "api" in rule_values(rule, "inboundTag")


def is_bittorrent_rule(rule):
    return "bittorrent" in rule_values(rule, "protocol")


def torrent_block_rule():
    return {
        "type": "field",
        "protocol": ["bittorrent"],
        "outboundTag": "blocked",
    }


def torrent_block_enabled(config):
    return any(rule.get("outboundTag") == "blocked" and is_bittorrent_rule(rule) for rule in routing_rules(config))


def print_torrent_status():
    config = load_config()
    if torrent_block_enabled(config):
        print(f"Торренты: {green('запрещены')}")
    else:
        print(f"Торренты: {red('разрешены')}")
        print("Рекомендуемое состояние для сервера: запрещены.")


def insert_torrent_rule(rules):
    insert_index = 0
    while insert_index < len(rules) and is_api_rule(rules[insert_index]):
        insert_index += 1
    rules.insert(insert_index, torrent_block_rule())


def set_torrent_block(blocked):
    config = load_config()
    changed = ensure_blocked_outbound(config)
    rules = routing_rules(config)
    rules_without_torrent = [rule for rule in rules if not is_bittorrent_rule(rule)]

    if blocked:
        insert_torrent_rule(rules_without_torrent)

    if rules_without_torrent != rules:
        changed = True

    if not changed:
        print_torrent_status()
        print("Изменения не требуются.")
        return

    config["routing"]["rules"] = rules_without_torrent
    backup = apply_config(config)
    print_torrent_status()
    print(f"Backup: {backup}")


def block_torrents():
    set_torrent_block(True)


def allow_torrents():
    set_torrent_block(False)


def call(command):
    subprocess.run(command, check=False)


def print_initial_link():
    if CLIENT_LINK_PATH.exists():
        print(CLIENT_LINK_PATH.read_text())
    else:
        print("Файл /root/xray-reality-client.txt не найден. Можно вывести ссылку через xray-client link NAME.")


def confirm(message):
    answer = input(f"{message} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def rollback_xray():
    if confirm("Откатить Xray к последней сохранённой предыдущей версии?"):
        call(["xray-update", "--rollback"])
    else:
        print("Откат отменён.")


def check_config():
    call(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])


def check_timers():
    call(["systemctl", "status", "xray-traffic-sync.timer", "xray-client-expire.timer", "xray-telegram-poller.service", "--no-pager"])


def show_timezone():
    call(["xray-client", "timezone"])


def update_timezone():
    print("MANAGER_TIMEZONE: часовой пояс для сроков доступа, лимитов трафика, отчётов и отображения времени.")
    print("Выбери значение из списка, чтобы не ошибиться при ручном вводе.")
    value = choose_timezone()
    if value is None:
        print("Изменение отменено.")
        return
    call(["xray-client", "set-timezone", value])
    print("Новая настройка будет использоваться в следующих расчётах и выводе времени.")


def execute_action(title, func):
    begin_action(title)
    try:
        func()
    finally:
        end_action(title)


def client_menu_handlers():
    return {
        "1": ("Показать клиентов", lambda: call(["xray-client", "list"])),
        "2": ("Добавить клиента", lambda: menu_client_actions.add_client_from_menu(call)),
        "3": ("Изменить срок доступа", lambda: menu_client_actions.update_selected_client_days(call)),
        "4": ("Изменить статус оплаты", lambda: menu_client_actions.update_selected_client_payment(call)),
        "5": (
            "Отключить клиента",
            lambda: menu_client_actions.call_client_command(call, "disable", "отключения", "enabled"),
        ),
        "6": (
            "Включить клиента",
            lambda: menu_client_actions.call_client_command(call, "enable", "включения", "disabled"),
        ),
        "7": (
            "Удалить клиента",
            lambda: menu_client_actions.call_client_command(call, "remove", "удаления", "all"),
        ),
        "8": (
            "Вывести ссылку клиента",
            lambda: menu_client_actions.call_client_command(call, "link", "вывода ссылки", "all"),
        ),
        "9": ("Проверить просроченных клиентов", lambda: call(["xray-client", "expire-due"])),
        "10": ("Трафик", open_traffic_tools_menu),
        "11": ("Журнал активности", open_activity_menu),
    }


def reality_menu_handlers():
    return {
        "1": ("Показать подключения", lambda: menu_reality_actions.show_settings(call)),
        "2": ("Создать подключение", lambda: menu_reality_actions.create_connection(call)),
        "3": ("Обновить PORT", menu_reality_actions.update_port),
        "4": ("Обновить REALITY_SNI и REALITY_DEST", menu_reality_actions.update_sni),
        "5": ("Обновить PORT, REALITY_SNI и REALITY_DEST", menu_reality_actions.update_port_and_sni),
        "6": ("Обновить FINGERPRINT", menu_reality_actions.update_fingerprint),
        "7": ("Удалить подключение", lambda: menu_reality_actions.delete_connection(call, confirm)),
    }


def cascade_menu_handlers():
    return {
        "1": ("Добавить/заменить каскад", lambda: call(["xray-set-cascade"])),
        "2": ("Проверить каскад", lambda: call(["xray-set-cascade", "--test"])),
        "3": ("Отключить каскад", lambda: call(["xray-set-cascade", "--disable"])),
    }


def recreate_warp_profile():
    print("Будет создан новый WARP-аккаунт и новый WireGuard profile.")
    print("Старые файлы wgcf-account.toml и wgcf-profile.conf будут заменены.")
    if not confirm("Пересоздать WARP профиль"):
        print("Действие отменено.")
        return
    call(["xray-warp", "create", "--force"])


def warp_menu_handlers():
    return {
        "1": ("Статус WARP", lambda: call(["xray-warp", "status"])),
        "2": ("Создать WARP outbound", lambda: call(["xray-warp", "create"])),
        "3": ("Пересоздать WARP профиль", recreate_warp_profile),
        "4": ("Включить WARP для Xray", lambda: call(["xray-warp", "enable"])),
        "5": ("Отключить WARP", lambda: call(["xray-warp", "disable"])),
        "6": ("Проверить WARP", lambda: call(["xray-warp", "test"])),
        "7": ("Удалить WARP из config.json", lambda: call(["xray-warp", "remove"])),
        "8": ("Проверить, что WARP отключен", lambda: call(["xray-warp", "verify-disabled"])),
    }


def xray_settings_menu_handlers():
    return {
        "1": ("Статус Xray", lambda: call(["systemctl", "status", "xray", "--no-pager"])),
        "2": ("Перезапустить Xray", lambda: (call(["systemctl", "restart", "xray"]), call(["systemctl", "is-active", "xray"]))),
        "3": ("Проверить config.json", check_config),
        "4": ("Проверить timers", check_timers),
        "5": ("Прогнать все тесты сервера", lambda: call(["xray-test"])),
        "6": ("Настройки Reality", open_reality_menu),
        "7": ("Каскад", open_cascade_menu),
        "8": ("Обновление Xray", open_update_menu),
        "9": ("Вывести стартовую ссылку", print_initial_link),
        "10": ("WARP", open_warp_menu),
        "11": ("Показать доступ к торрентам", print_torrent_status),
        "12": ("Запретить торренты", block_torrents),
        "13": ("Разрешить торренты", allow_torrents),
        "14": ("Показать часовой пояс", show_timezone),
        "15": ("Изменить часовой пояс", update_timezone),
    }


def security_menu_handlers():
    return {
        "1": (
            "Проверить безопасность сервера",
            lambda: menu_security_actions.run_security_audit(record_security_audit_run, format_manager_time),
        ),
        "2": ("Показать SSH-доступ", menu_security_actions.show_ssh_access),
        "3": ("Отключить вход по паролю SSH", lambda: menu_security_actions.disable_ssh_password_login(confirm)),
        "4": ("Включить вход по паролю SSH", lambda: menu_security_actions.enable_ssh_password_login(confirm)),
    }


def update_menu_handlers():
    return {
        "1": ("Проверить доступность обновления", lambda: call(["xray-update", "--check"])),
        "2": ("Проверить latest с текущим config.json", lambda: call(["xray-update", "--test-latest"])),
        "3": ("Обновить Xray", lambda: call(["xray-update", "--update"])),
        "4": ("Показать бэкапы Xray", lambda: call(["xray-update", "--backups"])),
        "5": ("Откатить Xray к предыдущей версии", rollback_xray),
        "6": ("Обновить geoip/geosite из Xray release", lambda: call(["xray-update", "--update-assets", "xray"])),
        "7": ("Обновить geoip/geosite из Loyalsoldier", lambda: call(["xray-update", "--update-assets", "loyalsoldier"])),
        "8": ("Обновить geoip.dat из v2fly", lambda: call(["xray-update", "--update-assets", "v2fly"])),
    }


def traffic_menu_handlers():
    return {
        "1": ("Просмотр трафика", open_traffic_menu),
        "2": ("Показать лимиты трафика", lambda: call(["xray-client", "limit-list"])),
        "3": ("Установить лимит трафика", lambda: menu_client_actions.update_selected_client_limit(call)),
        "4": ("Убрать лимит трафика", lambda: menu_client_actions.clear_selected_client_limit(call)),
        "5": ("Проверить лимиты трафика", lambda: call(["xray-client", "enforce-limits", "--sync"])),
    }


def traffic_report_handlers(name):
    return {
        "1": ("Трафик за день по часам", lambda: menu_traffic_actions.show_traffic_day(call, name)),
        "2": ("Трафик за неделю по дням", lambda: menu_traffic_actions.show_traffic_week(call, name)),
        "3": ("Трафик за месяц по дням", lambda: menu_traffic_actions.show_traffic_month(call, name)),
        "4": ("Трафик за период по дням", lambda: menu_traffic_actions.show_traffic_period(call, name)),
    }


def backup_menu_handlers():
    return {
        "1": ("Создать бэкап на сервере", lambda: menu_backup_actions.create_backup_server(call)),
        "2": (
            "Создать бэкап и показать команду скачивания",
            lambda: menu_backup_actions.create_backup_download_command(call),
        ),
        "3": ("Показать бэкапы на сервере", lambda: call(["xray-backup", "list"])),
        "4": ("Восстановить из бэкапа на сервере", lambda: menu_backup_actions.restore_backup_from_menu(call, confirm)),
        "5": (
            "Показать команду загрузки бэкапа на сервер",
            lambda: menu_backup_actions.show_backup_upload_command(call),
        ),
        "6": ("Удалить бэкап", lambda: menu_backup_actions.delete_backup_from_menu(call, confirm)),
    }


def telegram_menu_handlers():
    return {
        "1": ("Статус бота", lambda: call(["xray-telegram", "status"])),
        "2": ("Первичная настройка", lambda: call(["xray-telegram", "setup"])),
        "3": ("Донастроить владельца/чат", lambda: call(["xray-telegram", "owner"])),
        "4": ("Включить уведомления", lambda: call(["xray-telegram", "enable"])),
        "5": ("Отключить уведомления", lambda: call(["xray-telegram", "disable"])),
        "6": ("Изменить маршрут", lambda: menu_telegram_actions.update_route_mode(call)),
        "7": ("Отправить тестовое сообщение", lambda: call(["xray-telegram", "test"])),
        "8": ("Проверить GeoIP-уведомления сейчас", lambda: call(["xray-telegram", "notify-geoip"])),
        "9": ("Показать подписки клиентов", lambda: call(["xray-telegram", "subscribers"])),
        "10": ("Обработать сообщения пользователей", lambda: call(["xray-telegram", "poll-users"])),
        "11": ("Проверить напоминания об оплате", lambda: call(["xray-telegram", "notify-expiry"])),
        "12": ("Настроить оплату и округление", lambda: menu_telegram_actions.update_payment_amount(call)),
        "13": ("Изменить имя бота", lambda: menu_telegram_actions.update_bot_name(call)),
        "14": ("Уведомить о работах на сервере", lambda: menu_telegram_actions.send_maintenance_notice(call, confirm)),
        "15": ("Обновить меню команд Telegram", lambda: call(["xray-telegram", "commands"])),
    }


def suspicious_menu_handlers():
    return {
        "1": ("Сводка suspicious", lambda: menu_activity_actions.activity_suspicious_report(call)),
        "2": ("GeoIP-риски подробно", lambda: menu_activity_actions.activity_geoip_risk_details(call)),
        "3": ("Настройки исключений", open_activity_exception_menu),
    }


def activity_exception_menu_handlers():
    return {
        "1": ("Показать исключения", lambda: call(["xray-activity", "exceptions"])),
        "2": (
            "Добавить из suspicious",
            lambda: menu_activity_exception_actions.activity_exception_add_from_suspicious(
                call,
                menu_activity_actions.ask_activity_days,
            ),
        ),
        "3": ("Добавить вручную", lambda: menu_activity_exception_actions.activity_exception_add_manual(call)),
        "4": (
            "Удалить исключение",
            lambda: menu_activity_exception_actions.activity_exception_delete_from_menu(call),
        ),
        "5": (
            "Удалить все исключения",
            lambda: menu_activity_exception_actions.activity_exception_delete_all_from_menu(call, confirm),
        ),
    }


def activity_menu_handlers():
    return {
        "1": ("Статус журнала активности", lambda: call(["xray-activity", "status"])),
        "2": ("Включить парсинг activity log", lambda: call(["xray-activity", "enable"])),
        "3": ("Отключить парсинг activity log", lambda: call(["xray-activity", "disable"])),
        "4": ("Синхронизировать сейчас", lambda: call(["xray-activity", "sync"])),
        "5": (
            "Отчёт по клиенту",
            lambda: menu_activity_actions.activity_client_report(menu_client_actions.choose_client, call),
        ),
        "6": ("Подозрительная активность", open_activity_suspicious_menu),
        "7": (
            "Экспорт отчёта по клиенту",
            lambda: menu_activity_export_actions.activity_export_report(menu_client_actions.choose_client, call),
        ),
        "8": ("Показать архивы экспорта", lambda: call(["xray-activity", "export-list"])),
        "9": (
            "Удалить архив экспорта",
            lambda: menu_activity_export_actions.delete_activity_export_from_menu(call, confirm),
        ),
        "10": (
            "Удалить все архивы экспорта",
            lambda: menu_activity_export_actions.delete_all_activity_exports_from_menu(call, confirm),
        ),
        "11": ("Изменить срок хранения журнала", lambda: menu_activity_actions.update_activity_retention(call)),
        "12": ("Настроить лимиты suspicious", lambda: menu_activity_actions.update_activity_risk_limits(call)),
        "13": ("GeoIP routing: выбрать регион", menu_activity_actions.set_xray_geoip_routing_region),
        "14": ("GeoIP routing: отключить", menu_activity_actions.disable_xray_geoip_routing_region),
    }


def print_section_title(title):
    print(f"Раздел: {title}")


def menu_loop(title, rows, handlers, back_label="Назад"):
    while True:
        print()
        print_menu_header()
        print()
        print_section_title(title)
        print()
        print_menu_table(rows)
        choice = input("Выбор: ").strip()
        if not sys.stdin.isatty():
            print()

        if choice == "0":
            return
        if choice in handlers:
            action_title, handler = handlers[choice]
            execute_action(action_title, handler)
        else:
            print(f"Неизвестный пункт меню. 0 - {back_label}.")


def open_clients_menu():
    menu_loop("Клиенты", client_menu_actions(), client_menu_handlers())


def open_reality_menu():
    menu_loop("Настройки Reality", reality_menu_actions(), reality_menu_handlers())


def open_cascade_menu():
    menu_loop("Каскад", cascade_menu_actions(), cascade_menu_handlers())


def open_warp_menu():
    menu_loop("WARP", warp_menu_actions(), warp_menu_handlers())


def open_xray_settings_menu():
    menu_loop("Настройки Xray", xray_settings_menu_actions(), xray_settings_menu_handlers())


def open_security_menu():
    menu_loop("Безопасность", security_menu_actions(), security_menu_handlers())


def open_update_menu():
    menu_loop("Обновление Xray", update_menu_actions(), update_menu_handlers())


def open_backup_menu():
    menu_loop("Резервные копии", backup_menu_actions(), backup_menu_handlers())


def open_telegram_menu():
    menu_loop("Telegram бот", telegram_menu_actions(), telegram_menu_handlers())


def open_activity_suspicious_menu():
    menu_loop("Подозрительная активность", suspicious_menu_actions(), suspicious_menu_handlers())


def open_activity_exception_menu():
    menu_loop("Настройки исключений", activity_exception_menu_actions(), activity_exception_menu_handlers())


def open_activity_menu():
    menu_loop("Журнал активности", activity_menu_actions(), activity_menu_handlers())


def open_client_traffic_menu(name):
    menu_loop(f"Просмотр трафика: {name}", traffic_report_actions(), traffic_report_handlers(name))


def open_traffic_tools_menu():
    menu_loop("Трафик", traffic_menu_actions(), traffic_menu_handlers())


def open_traffic_menu():
    while True:
        print()
        print_menu_header()
        print()
        print_section_title("Просмотр трафика")
        print()
        name = menu_traffic_actions.choose_traffic_client()
        if not name:
            return
        open_client_traffic_menu(name)


def main_menu_handlers():
    return {
        "1": ("Клиенты", open_clients_menu),
        "2": ("Настройки Xray", open_xray_settings_menu),
        "3": ("Безопасность", open_security_menu),
        "4": ("Резервные копии", open_backup_menu),
        "5": ("Telegram бот", open_telegram_menu),
    }


def menu():
    if os.geteuid() != 0:
        die("Run this script as root.")

    handlers = main_menu_handlers()
    while True:
        print()
        print_menu_header()
        print()
        print_section_title("Главное меню")
        print()
        print_menu_table(main_menu_actions())
        choice = input("Выбор: ").strip()
        if not sys.stdin.isatty():
            print()

        if choice == "0":
            return
        if choice in handlers:
            title, handler = handlers[choice]
            if choice == "7":
                execute_action(title, handler)
            else:
                handler()
        else:
            print("Неизвестный пункт меню.")


if __name__ == "__main__":
    menu()
