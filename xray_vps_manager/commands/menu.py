#!/usr/bin/env python3
import copy
import json
import os
import re
import shutil
import subprocess
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from xray_vps_manager.commands import (
    menu_activity_exception_actions,
    menu_activity_export_actions,
    menu_backup_actions,
    menu_reality_actions,
)
from xray_vps_manager.core.server_env import ORDERED_ENV_KEYS, read_server_env, write_server_env as write_server_env_file
from xray_vps_manager.core.terminal import table_border, table_row

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
CLIENT_DB_PATH = Path("/usr/local/etc/xray/clients.json")
SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
CLIENT_LINK_PATH = Path("/root/xray-reality-client.txt")
TRAFFIC_PATH = Path("/usr/local/etc/xray/traffic.json")
XRAY_ASSET_DIR = Path("/usr/local/share/xray")
SSHD_CONFIG_PATH = Path("/etc/ssh/sshd_config")
SSHD_DROPIN_PATH = Path("/etc/ssh/sshd_config.d/00-xray-vps-manager.conf")
SSHD_LEGACY_DROPIN_PATH = Path("/etc/ssh/sshd_config.d/99-xray-vps-manager.conf")
AUTHORIZED_KEYS_PATH = Path("/root/.ssh/authorized_keys")
CASCADE_UPSTREAM_TAG = "cascade-upstream"
WARP_OUTBOUND_TAG = "warp-out"
DIRECT_OUTBOUND_TAG = "direct"
XRAY_GEOIP_OUTBOUND_PREFIX = "geoip-warning-"
XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV = "ACTIVITY_XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY"
MENU_VERSION = "v1.0.0"
MENU_UPDATED = "2026-06-12 15:35 UTC"
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
CLIENT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
GREEN = "\033[92m"
RED = "\033[31m"
GOLD = "\033[93m"
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


def validate_client_name(value):
    if not CLIENT_RE.fullmatch(value or ""):
        die("Client name must be 1-64 chars: A-Z a-z 0-9 _ . @ -")
    return value


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


def gold(text):
    return color(text, GOLD)


def color_payment_status(value):
    text = str(value or "")
    if text == "free":
        return f"{GREEN}{text}{RESET}"
    if text == "paid":
        return f"{GOLD}{text}{RESET}"
    return text


def split_email(email):
    if "|created=" in email:
        name, created = email.split("|created=", 1)
        return name, created
    return email, ""


def db_clients(db):
    return db.setdefault("clients", {})


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_access_until(value):
    parsed = parse_time(value)
    if parsed is None:
        return "бессрочно"
    return parsed.astimezone(manager_timezone()).strftime("%Y-%m-%d %H:%M")


def local_today():
    return datetime.now(manager_timezone()).date()


def parse_date_value(value, label="DATE"):
    try:
        return date.fromisoformat(value)
    except ValueError:
        die(f"{label} must be in YYYY-MM-DD format.")


def parse_month_value(value):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value or ""):
        die("MONTH must be in YYYY-MM format.")
    year, month = (int(part, 10) for part in value.split("-", 1))
    if month < 1 or month > 12:
        die("MONTH must be in YYYY-MM format.")
    return f"{year:04d}-{month:02d}"


def current_month_key():
    today = local_today()
    return f"{today.year:04d}-{today.month:02d}"


def month_bounds(month_key):
    year, month = (int(part, 10) for part in month_key.split("-", 1))
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    today = local_today()
    if start <= today <= end:
        end = today
    return start, end


def iter_dates(start, end):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def format_traffic(value):
    value = int(value or 0)
    if value < 1024:
        return "0.00KB"
    units = [
        ("KB", 1024),
        ("MB", 1024 ** 2),
        ("GB", 1024 ** 3),
    ]
    for suffix, size in units:
        next_size = size * 1024
        if value < next_size or suffix == "GB":
            return f"{value / size:.2f}{suffix}"
    return "0.00KB"


def traffic_bucket_totals(bucket):
    if not isinstance(bucket, dict):
        return 0, 0
    return int(bucket.get("incoming", 0) or 0), int(bucket.get("outgoing", 0) or 0)


def traffic_entry(traffic_db, name):
    return traffic_db.get("clients", {}).get(name, {})


def history_for_entry(entry):
    history = entry.get("history", {})
    return history if isinstance(history, dict) else {}


def day_total(entry, day):
    hours = history_for_entry(entry).get(day.isoformat(), {})
    if not isinstance(hours, dict):
        return 0, 0
    incoming = 0
    outgoing = 0
    for bucket in hours.values():
        bucket_in, bucket_out = traffic_bucket_totals(bucket)
        incoming += bucket_in
        outgoing += bucket_out
    return incoming, outgoing


def month_total(entry, month_key):
    start, end = month_bounds(month_key)
    incoming = 0
    outgoing = 0
    for day in iter_dates(start, end):
        day_in, day_out = day_total(entry, day)
        incoming += day_in
        outgoing += day_out
    return incoming, outgoing


def all_time_total(entry):
    return int(entry.get("incoming", 0) or 0), int(entry.get("outgoing", 0) or 0)


def load_traffic_db():
    return load_json(TRAFFIC_PATH, {"clients": {}})


def sync_traffic_quiet():
    sync = Path("/usr/local/sbin/xray-traffic-sync")
    if sync.exists():
        try:
            subprocess.run([str(sync), "--quiet"], check=False, timeout=10)
        except subprocess.TimeoutExpired:
            pass


def client_rows_for_selection(mode="all"):
    config = load_config()
    db = load_json(CLIENT_DB_PATH, {"clients": {}})
    rows = []
    seen = set()

    connection_names = {row["tag"]: row["name"] for row in menu_reality_actions.connection_rows()}
    for inbound in menu_reality_actions.reality_inbounds(config):
        tag = menu_reality_actions.inbound_tag(inbound)
        for item in inbound.get("settings", {}).get("clients", []):
            name, created = split_email(item.get("email", ""))
            if not name:
                continue
            entry = db_clients(db).get(name, {})
            rows.append({
                "name": name,
                "status": "enabled",
                "paymentType": entry.get("paymentType", "free"),
                "created": entry.get("created") or created or "unknown",
                "expiresAt": entry.get("expiresAt", ""),
                "connection": entry.get("connection") or tag,
                "connectionName": connection_names.get(
                    entry.get("connection") or tag,
                    menu_reality_actions.connection_name_from_tag(tag),
                ),
            })
            seen.add(name)

    for name, entry in db_clients(db).items():
        if name in seen:
            continue
        tag = entry.get("connection") or menu_reality_actions.INBOUND_TAG
        rows.append({
            "name": name,
            "status": "disabled" if entry.get("enabled") is False else "missing",
            "paymentType": entry.get("paymentType", "free"),
            "created": entry.get("created") or "unknown",
            "expiresAt": entry.get("expiresAt", ""),
            "connection": tag,
            "connectionName": connection_names.get(tag, menu_reality_actions.connection_name_from_tag(tag)),
        })

    if mode == "enabled":
        return [row for row in rows if row["status"] == "enabled"]
    if mode == "disabled":
        return [row for row in rows if row["status"] != "enabled"]
    return rows


def print_client_selection_table(rows):
    headers = ("№", "CONNECTION", "NAME", "STATUS", "PAYMENT", "ACCESS UNTIL", "CREATED")
    values = [
        (
            str(index),
            row["connectionName"],
            row["name"],
            row["status"],
            row.get("paymentType", "free"),
            format_access_until(row.get("expiresAt", "")),
            row["created"],
        )
        for index, row in enumerate(rows, start=1)
    ]
    values.append(("0", "Назад", "", "", "", "", ""))
    widths = [
        max(len(headers[column]), *(len(str(row[column])) for row in values))
        for column in range(len(headers))
    ]
    border = table_border(widths)
    print(border)
    print(table_row(headers, widths))
    print(border)
    for row in values:
        row = list(row)
        row[4] = color_payment_status(row[4])
        print(table_row(row, widths))
    print(border)


def choose_client(action, mode="all"):
    rows = client_rows_for_selection(mode)
    if not rows:
        print(f"Нет клиентов для действия: {action}.")
        return ""

    print(f"Выбери клиента для действия: {action}.")
    print_client_selection_table(rows)
    while True:
        choice = input("Клиент: ").strip()
        if choice == "0":
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1]["name"]
        print("Неизвестный клиент. Выбери номер из списка или 0 для возврата.")


def traffic_rows_for_selection(month_key):
    sync_traffic_quiet()
    traffic_db = load_traffic_db()
    rows = []
    for row in client_rows_for_selection("all"):
        entry = traffic_entry(traffic_db, row["name"])
        month_in, month_out = month_total(entry, month_key)
        all_in, all_out = all_time_total(entry)
        rows.append({
            "name": row["name"],
            "status": row["status"],
            "connectionName": row["connectionName"],
            "monthIn": month_in,
            "monthOut": month_out,
            "monthTotal": month_in + month_out,
            "allTimeTotal": all_in + all_out,
        })
    return rows


def print_traffic_selection_table(rows):
    headers = ("№", "CONNECTION", "NAME", "STATUS", "MONTH IN", "MONTH OUT", "MONTH TOTAL", "ALL TIME")
    values = [
        (
            str(index),
            row["connectionName"],
            row["name"],
            row["status"],
            format_traffic(row["monthIn"]),
            format_traffic(row["monthOut"]),
            format_traffic(row["monthTotal"]),
            format_traffic(row["allTimeTotal"]),
        )
        for index, row in enumerate(rows, start=1)
    ]
    values.append(("0", "Назад", "", "", "", "", "", ""))
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


def choose_traffic_client():
    month_key = current_month_key()
    rows = traffic_rows_for_selection(month_key)
    if not rows:
        print("Нет клиентов для просмотра трафика.")
        return ""

    print(f"Трафик за текущий месяц: {month_key}")
    print_traffic_selection_table(rows)
    while True:
        choice = input("Клиент: ").strip()
        if choice == "0":
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1]["name"]
        print("Неизвестный клиент. Выбери номер из списка или 0 для возврата.")


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


def ensure_direct_outbound(config):
    outbounds = config.setdefault("outbounds", [])
    for outbound in outbounds:
        if outbound.get("tag") == DIRECT_OUTBOUND_TAG:
            return outbound
    outbound = {"tag": DIRECT_OUTBOUND_TAG, "protocol": "freedom"}
    outbounds.append(outbound)
    return outbound


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


def is_xray_geoip_warning_tag(tag):
    return str(tag or "").startswith(XRAY_GEOIP_OUTBOUND_PREFIX)


def xray_geoip_warning_tag(code):
    return f"{XRAY_GEOIP_OUTBOUND_PREFIX}{code.upper()}"


def xray_geoip_warning_source_outbound(config):
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


def remove_xray_geoip_warning_config(config):
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


def insert_before_catchall_route(rules, rule):
    insert_index = len(rules)
    for index, existing in enumerate(rules):
        if existing.get("outboundTag") in (WARP_OUTBOUND_TAG, CASCADE_UPSTREAM_TAG) and existing.get("network") == "tcp,udp":
            insert_index = index
            break
    rules.insert(insert_index, rule)


def apply_xray_geoip_warning_config(config, code):
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


def restore_xray_geoip_domain_strategy(config, values):
    previous = values.pop(XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV, "")
    routing = config.setdefault("routing", {})
    if previous:
        routing["domainStrategy"] = previous
    elif routing.get("domainStrategy") == "IPOnDemand":
        routing["domainStrategy"] = "IPIfNonMatch"


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


def prompt_date(default, label, description):
    print(description)
    value = input(f"{label} [{default}]: ").strip() or default
    return parse_date_value(value, label).isoformat()


def prompt_month(default, description):
    print(description)
    value = input(f"MONTH [{default}]: ").strip() or default
    return parse_month_value(value)


def show_traffic_day(name):
    today = local_today().isoformat()
    day = prompt_date(today, "DATE", "DATE: день отчёта в формате YYYY-MM-DD. Вывод будет по часам.")
    call(["xray-client", "traffic-day", name, day])


def show_traffic_week(name):
    default = (local_today() - timedelta(days=6)).isoformat()
    start = prompt_date(default, "START_DATE", "START_DATE: первый день 7-дневного периода в формате YYYY-MM-DD.")
    call(["xray-client", "traffic-week", name, start])


def show_traffic_month(name):
    month = prompt_month(current_month_key(), "MONTH: месяц отчёта в формате YYYY-MM. Вывод будет по дням.")
    call(["xray-client", "traffic-month", name, month])


def show_traffic_period(name):
    today = local_today().isoformat()
    start = prompt_date(today, "START_DATE", "START_DATE: первый день периода в формате YYYY-MM-DD.")
    end = prompt_date(today, "END_DATE", "END_DATE: последний день периода в формате YYYY-MM-DD.")
    call(["xray-client", "traffic-period", name, start, end])


def ask_name(action):
    name = input(f"Имя клиента для {action}: ").strip()
    return validate_client_name(name)


def ask_payment_type():
    print("Статус оплаты клиента.")
    print("1) Бесплатный: не участвует в расчёте общей аренды и не получает напоминания об оплате.")
    print("2) Платный: участвует в расчёте суммы на клиента и получает напоминания.")
    choice = input("Статус оплаты [1-бесплатный]: ").strip() or "1"
    if choice == "1":
        return "free"
    if choice == "2":
        return "paid"
    print("Действие отменено: неизвестный статус оплаты.")
    return ""


def ask_new_client_command():
    print("Введите имя клиента. Можно сразу указать срок через пробел.")
    print("Примеры: data_test2 или data_test2 30. Пустой срок или 0 означает бессрочно.")
    raw = input("Имя клиента [и дни]: ").strip()
    if not raw:
        die("Client name is required.")
    parts = raw.split(maxsplit=1)
    name = validate_client_name(parts[0])
    tag = menu_reality_actions.choose_connection("добавления клиента")
    if not tag:
        return []
    payment_type = ask_payment_type()
    if not payment_type:
        return []
    command = ["xray-client", "add", name]
    if len(parts) == 1:
        return command + ["--connection", tag, "--payment", payment_type]
    return command + [parts[1].strip(), "--connection", tag, "--payment", payment_type]


def add_client_from_menu():
    command = ask_new_client_command()
    if not command:
        print("Действие отменено.")
        return
    call(command)


def ask_access_days():
    print("Количество календарных дней доступа.")
    print("Введите 0 или нажмите Enter, чтобы сделать доступ бессрочным.")
    value = input("ACCESS_DAYS [бессрочно]: ").strip()
    return value or "0"


def ask_extend_days():
    print("Количество дней, которое нужно добавить к текущей дате окончания доступа.")
    print("Если срок уже истёк или не был установлен, продление пойдёт от сегодняшней даты.")
    value = input("EXTEND_DAYS: ").strip()
    if not value:
        die("Extend days is required.")
    return value


def choose_limit_period():
    print("Период лимита трафика.")
    print("1) День: лимит сбрасывается каждый календарный день в 00:00 по времени сервера.")
    print("2) Месяц: лимит сбрасывается в начале следующего календарного месяца.")
    while True:
        value = input("Период [1-день, 2-месяц]: ").strip()
        if value == "1":
            return "daily"
        if value == "2":
            return "monthly"
        print("Выбери 1 для дневного лимита или 2 для месячного лимита.")


def update_selected_client_limit():
    name = choose_client("установки лимита трафика", "all")
    if not name:
        print("Действие отменено.")
        return
    period = choose_limit_period()
    print("LIMIT_GB: лимит общего трафика IN+OUT в гигабайтах.")
    print("Пример: 15. Пустой ввод отменяет изменение, 0 убирает лимит.")
    value = input("LIMIT_GB: ").strip()
    if not value:
        print("Действие отменено.")
        return
    call(["xray-client", "set-limit", name, period, value])


def clear_selected_client_limit():
    name = choose_client("снятия лимита трафика", "all")
    if not name:
        print("Действие отменено.")
        return
    call(["xray-client", "clear-limit", name])


def call_client_command(command, action, mode="all"):
    name = choose_client(action, mode)
    if not name:
        print("Действие отменено.")
        return
    call(["xray-client", command, name])


def update_selected_client_days():
    name = choose_client("изменения срока", "all")
    if not name:
        print("Действие отменено.")
        return
    print("Что сделать со сроком доступа?")
    print("1) Продлить на N дней: прибавить дни к текущей дате окончания.")
    print("2) Установить срок N дней от сегодняшней даты.")
    print("3) Сделать доступ бессрочным.")
    choice = input("Действие [1-продлить]: ").strip() or "1"
    if choice == "1":
        call(["xray-client", "extend-days", name, ask_extend_days()])
    elif choice == "2":
        call(["xray-client", "set-days", name, ask_access_days()])
    elif choice == "3":
        call(["xray-client", "set-days", name, "0"])
    else:
        print("Действие отменено: неизвестный выбор.")


def update_selected_client_payment():
    name = choose_client("изменения статуса оплаты", "all")
    if not name:
        print("Действие отменено.")
        return
    payment_type = ask_payment_type()
    if not payment_type:
        return
    call(["xray-client", "set-payment", name, payment_type])


def update_telegram_payment_rounding():
    print("Округление суммы на платного клиента.")
    print("1) Оставить текущую настройку.")
    print("2) Без округления.")
    print("3) Округлять вверх до выбранного шага. Пример: шаг 10 превратит 223.10 в 230.")
    choice = input("Округление [1-оставить]: ").strip() or "1"
    if choice == "1":
        print("Округление не изменено.")
        return
    if choice == "2":
        call(["xray-telegram", "payment-rounding", "none"])
        return
    if choice != "3":
        print("Действие отменено: неизвестный выбор.")
        return
    print("Введите шаг округления. Сумма на клиента будет округляться вверх до кратного шага.")
    print("Примеры: 10, 50, 100.")
    step = input("Шаг округления [10]: ").strip().replace(",", ".") or "10"
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", step):
        die("Payment rounding step must be a positive number.")
    if float(step) <= 0:
        die("Payment rounding step must be greater than zero.")
    call(["xray-telegram", "payment-rounding", "step", step])


def update_telegram_payment_amount():
    call(["xray-telegram", "payment-amount"])
    print("Введите общую сумму оплаты для всех клиентов.")
    print("Введите только число. Пример: 500. Введите 0, чтобы очистить значение.")
    print("Нажмите Enter, чтобы оставить текущую сумму.")
    amount = input("Сумма оплаты: ").strip().replace(",", ".")
    if not amount:
        print("Сумма оплаты не изменена.")
    elif amount == "0":
        call(["xray-telegram", "payment-amount", "0"])
    elif not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", amount):
        die("Payment amount must be a number.")
    else:
        print("Выберите валюту:")
        print("1) Рубли: ₽")
        print("2) Доллары: $")
        print("3) Евро: €")
        choice = input("Валюта [1-рубли]: ").strip() or "1"
        currencies = {
            "1": "₽",
            "2": "$",
            "3": "€",
        }
        symbol = currencies.get(choice)
        if not symbol:
            print("Действие отменено: неизвестная валюта.")
            return
        call(["xray-telegram", "payment-amount", f"{amount} {symbol}"])
    update_telegram_payment_rounding()


def update_telegram_bot_name():
    print("Имя бота используется в сообщениях пользователям.")
    print("Например: Vireika. Оставь пустым, чтобы не менять.")
    value = input("Имя бота: ").strip()
    if not value:
        print("Имя бота не изменено.")
        return
    call(["xray-telegram", "bot-name", value])


def send_telegram_maintenance_notice():
    print("Выбери уведомление для подписанных клиентов.")
    call(["xray-telegram", "maintenance-notice", "templates"])
    print("3) Своё сообщение через Telegram админ-панель")
    choice = input("Уведомление [1]: ").strip() or "1"
    if choice == "3":
        print("Для своего сообщения открой Telegram-бота владельцем: /admin -> Уведомления -> Своё сообщение.")
        return
    notices = {
        "1": "start",
        "2": "done",
        "start": "start",
        "done": "done",
    }
    notice = notices.get(choice.lower())
    if not notice:
        print("Действие отменено: неизвестное уведомление.")
        return
    print()
    print("Предпросмотр:")
    call(["xray-telegram", "maintenance-notice", notice, "--dry-run"])
    if not confirm("Отправить это уведомление всем подписанным клиентам?"):
        print("Рассылка отменена.")
        return
    call(["xray-telegram", "maintenance-notice", notice, "--yes"])


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


def ask_activity_days(default=7):
    value = input(f"Период в днях [{default}]: ").strip() or str(default)
    if not re.fullmatch(r"[0-9]+", value) or int(value, 10) < 1:
        print(f"Некорректный период, использую {default} дней.")
        return str(default)
    return value


def activity_client_report():
    name = choose_client("просмотра журнала активности", "all")
    if not name:
        print("Действие отменено.")
        return
    call(["xray-activity", "client", name, ask_activity_days(7)])


def activity_suspicious_report():
    call(["xray-activity", "suspicious", ask_activity_days(7)])


def activity_geoip_risk_details():
    call(["xray-activity", "geoip-risks", ask_activity_days(7)])


def update_telegram_route_mode():
    print("Как Telegram-боту выходить в интернет?")
    print("1) direct: напрямую с этого сервера")
    print("2) cascade: через исходящий сервер, настроенный в каскаде")
    print("Cascade-режим добавит локальный SOCKS inbound 127.0.0.1:10810 только для Telegram Bot API.")
    choice = input("Маршрут [1-direct, 2-cascade]: ").strip() or "1"
    if choice == "1":
        call(["xray-telegram", "mode", "direct"])
    elif choice == "2":
        call(["xray-telegram", "mode", "cascade"])
    else:
        print("Действие отменено: неизвестный маршрут.")


def activity_retention_value():
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


def update_activity_retention():
    current = activity_retention_value()
    print("ACTIVITY_RETENTION_DAYS: сколько дней хранить детальные события журнала активности.")
    print("По умолчанию 365 дней. Допустимый диапазон: 1-3650 дней.")
    print("Старые события старше нового срока будут удалены сразу после изменения.")
    value = input(f"ACTIVITY_RETENTION_DAYS [{current}] (Enter - оставить без изменений): ").strip()
    if not value:
        print("Изменение отменено.")
        return
    call(["xray-activity", "retention", value])


def activity_risk_limit_values():
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


def update_activity_risk_limits():
    current = activity_risk_limit_values()
    print("Лимиты suspicious определяют, когда клиент попадёт в отчёт подозрительной активности.")
    print("По умолчанию burst = 1000 событий за 15 минут, чтобы обычный стриминг не попадал в false positive.")
    print("Нажми Enter на любом пункте, чтобы оставить текущее значение.")
    burst_events = input(f"BURST_EVENTS [{current['burst_events']}]: ").strip() or current["burst_events"]
    burst_window = input(f"BURST_WINDOW_MINUTES [{current['burst_window']}]: ").strip() or current["burst_window"]
    unique_hosts = input(f"UNIQUE_HOSTS [{current['unique_hosts']}]: ").strip() or current["unique_hosts"]
    unique_ports = input(f"UNIQUE_PORTS [{current['unique_ports']}]: ").strip() or current["unique_ports"]
    call(["xray-activity", "risk-limits", "set", burst_events, burst_window, unique_hosts, unique_ports])


def geoip_codes(query=""):
    command = ["xray-activity", "geo-list"]
    if query:
        command.append(query)
    result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        print(result.stderr.strip() or "Не удалось получить список GeoIP-регионов.")
        return []
    return [line.strip().upper() for line in result.stdout.splitlines() if line.strip()]


def print_geoip_region_table(rows, include_search=False):
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


def choose_geoip_region_from_rows(rows, prompt):
    while True:
        choice = input(prompt).strip().lower()
        if choice in ("", "0"):
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1][0]
        print("Неизвестный регион. Выбери номер из списка или 0 для возврата.")


def search_geoip_region():
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


def choose_geoip_region():
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


def set_xray_geoip_routing_region():
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
    values = server_env()
    if not values.get(XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV) and previous_strategy != "IPOnDemand":
        values[XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY_ENV] = previous_strategy
    values["ACTIVITY_XRAY_GEOIP_WARNING_CODE"] = code
    write_server_env_values(values)
    print(f"Xray routing GeoIP-предупреждения включены для региона: {code}")
    print(f"Outbound tag: {xray_geoip_warning_tag(code)}")
    print("Routing domainStrategy: IPOnDemand")
    print(f"Backup: {backup}")


def disable_xray_geoip_routing_region():
    config = load_config()
    changed = remove_xray_geoip_warning_config(config)
    values = server_env()
    restore_xray_geoip_domain_strategy(config, values)
    changed = True
    backup = None
    if changed:
        backup = apply_config(config)
    values["ACTIVITY_XRAY_GEOIP_WARNING_CODE"] = ""
    write_server_env_values(values)
    print("Xray routing GeoIP-предупреждения отключены.")
    if backup:
        print(f"Backup: {backup}")
    else:
        print("Изменения config.json не требуются.")


def sshd_binary():
    for candidate in ("/usr/sbin/sshd", "/usr/local/sbin/sshd", shutil.which("sshd")):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def sshd_effective_config():
    binary = sshd_binary()
    if not binary:
        return None, "sshd не найден."
    result = subprocess.run(
        [binary, "-T"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "sshd -T завершился с ошибкой."
        return None, message
    settings = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            settings[parts[0].lower()] = parts[1].strip()
    return settings, ""


def validate_sshd_config():
    binary = sshd_binary()
    if not binary:
        return False, "sshd не найден."
    result = subprocess.run(
        [binary, "-t"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    message = result.stderr.strip() or result.stdout.strip()
    return result.returncode == 0, message


def root_authorized_key_count():
    if not AUTHORIZED_KEYS_PATH.exists():
        return 0
    count = 0
    for line in AUTHORIZED_KEYS_PATH.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


def sshd_config_includes_manager_dropin():
    if not SSHD_CONFIG_PATH.exists():
        return False
    include_line = f"include {SSHD_DROPIN_PATH}".lower()
    for line in SSHD_CONFIG_PATH.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.lower() == include_line:
            return True
    return False


def ensure_sshd_dropin_include():
    if sshd_config_includes_manager_dropin():
        return False
    lines = SSHD_CONFIG_PATH.read_text(errors="ignore").splitlines()
    include_block = [
        "# Added by Xray VPS Manager",
        f"Include {SSHD_DROPIN_PATH}",
        "",
    ]
    lines = include_block + lines
    SSHD_CONFIG_PATH.write_text("\n".join(lines) + "\n")
    return True


def remove_sshd_dropin_include():
    if not SSHD_CONFIG_PATH.exists():
        return False
    target = f"include {SSHD_DROPIN_PATH}".lower()
    lines = SSHD_CONFIG_PATH.read_text(errors="ignore").splitlines()
    new_lines = []
    removed = False
    skip_next_blank = False

    for line in lines:
        stripped = line.strip()
        if stripped.lower() == target:
            removed = True
            if new_lines and new_lines[-1].strip() == "# Added by Xray VPS Manager":
                new_lines.pop()
            skip_next_blank = True
            continue
        if skip_next_blank and not stripped:
            skip_next_blank = False
            continue
        skip_next_blank = False
        new_lines.append(line)

    if removed:
        SSHD_CONFIG_PATH.write_text("\n".join(new_lines) + "\n")
    return removed


def write_sshd_password_dropin():
    SSHD_DROPIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SSHD_DROPIN_PATH.write_text(
        "\n".join(
            [
                "# Managed by Xray VPS Manager.",
                "# Password SSH logins are disabled; public-key SSH remains enabled.",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "ChallengeResponseAuthentication no",
                "PubkeyAuthentication yes",
                "",
            ]
        )
    )
    SSHD_DROPIN_PATH.chmod(0o644)


def remove_legacy_sshd_dropin():
    if SSHD_LEGACY_DROPIN_PATH.exists():
        SSHD_LEGACY_DROPIN_PATH.unlink()
        return True
    return False


def backup_existing_file(path):
    if not path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = Path(f"{path}.bak.{timestamp}")
    shutil.copy2(path, backup)
    return backup


def restore_text_file(path, text):
    if text is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if path in (SSHD_DROPIN_PATH, SSHD_LEGACY_DROPIN_PATH):
        path.chmod(0o644)


def reload_sshd_service():
    attempts = [
        ("reload", "ssh"),
        ("reload", "sshd"),
        ("restart", "ssh"),
        ("restart", "sshd"),
    ]
    errors = []
    for action, unit in attempts:
        result = subprocess.run(
            ["systemctl", action, unit],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            print(green(f"SSH service обновлён через: systemctl {action} {unit}"))
            return True
        errors.append(f"systemctl {action} {unit}: {(result.stderr or result.stdout).strip()}")
    print(red("Не удалось перезагрузить ssh/sshd через systemctl."))
    for error in errors:
        if error.strip():
            print(error)
    return False


def ssh_password_login_disabled(settings):
    if not settings:
        return False
    password = settings.get("passwordauthentication")
    keyboard = settings.get("kbdinteractiveauthentication")
    challenge = settings.get("challengeresponseauthentication")
    return password == "no" and keyboard == "no" and challenge in (None, "no")


def ssh_password_login_enabled(settings):
    if not settings:
        return False
    return settings.get("passwordauthentication") == "yes"


def format_ssh_setting(key, value):
    if value == "unknown":
        return value
    if key == "pubkeyauthentication":
        return green(value) if value == "yes" else red(value)
    if key in ("passwordauthentication", "kbdinteractiveauthentication", "challengeresponseauthentication"):
        return green(value) if value == "no" else red(value)
    return value


def show_ssh_access():
    settings, error = sshd_effective_config()
    if settings:
        print("Effective SSH config по данным sshd -T:")
        for key in (
            "pubkeyauthentication",
            "passwordauthentication",
            "kbdinteractiveauthentication",
            "challengeresponseauthentication",
            "permitrootlogin",
        ):
            value = settings.get(key, "unknown")
            print(f"{key}: {format_ssh_setting(key, value)}")
        print()
        if ssh_password_login_disabled(settings):
            print(green("Парольный вход SSH отключен."))
        else:
            print(red("Парольный вход SSH может быть разрешен."))
    else:
        print(red(f"Не удалось прочитать effective config SSH: {error}"))
    key_count = root_authorized_key_count()
    key_message = f"{AUTHORIZED_KEYS_PATH}: {key_count} ключ(ей)"
    print(green(key_message) if key_count > 0 else red(key_message))
    dropin_status = "есть" if SSHD_DROPIN_PATH.exists() else "нет"
    print(f"Managed drop-in: {SSHD_DROPIN_PATH} ({dropin_status})")
    if SSHD_LEGACY_DROPIN_PATH.exists():
        print(f"Legacy drop-in: {SSHD_LEGACY_DROPIN_PATH} (есть, будет убран при следующем применении)")


def run_security_audit():
    print("Проверка безопасности сервера.")
    print("Текущий набор проверок: SSH password login.")
    print()

    findings = []
    settings, error = sshd_effective_config()
    if not settings:
        print(red("FAIL SSH password login: не удалось получить effective config через sshd -T."))
        findings.append(
            {
                "title": "Не удалось проверить SSH password login",
                "details": error or "sshd -T не вернул настройки.",
                "recommendations": [
                    "Проверь, что установлен openssh-server и команда sshd доступна.",
                    "Выполни /usr/sbin/sshd -t и исправь ошибки конфигурации, если они есть.",
                    "Повтори проверку через меню Безопасность -> Проверить безопасность сервера.",
                ],
            }
        )
    else:
        password = settings.get("passwordauthentication", "unknown")
        keyboard = settings.get("kbdinteractiveauthentication", "unknown")
        challenge = settings.get("challengeresponseauthentication", "unknown")
        details = (
            f"PasswordAuthentication={password}, "
            f"KbdInteractiveAuthentication={keyboard}, "
            f"ChallengeResponseAuthentication={challenge}"
        )
        password_available = password == "yes" or keyboard == "yes" or challenge == "yes"
        if password_available:
            print(red(f"FAIL SSH password login: вход по паролю доступен. {details}"))
            findings.append(
                {
                    "title": "SSH password login доступен",
                    "details": details,
                    "recommendations": [
                        "Убедись, что вход по SSH-ключу работает в отдельной сессии.",
                        "Открой Безопасность -> Отключить вход по паролю SSH.",
                        "После применения снова запусти Проверить безопасность сервера и проверь, что PasswordAuthentication=no.",
                    ],
                }
            )
        else:
            print(green(f"OK   SSH password login: вход по паролю отключён. {details}"))

    print()
    if findings:
        print(red("Рекомендации по найденным проблемам:"))
        for index, finding in enumerate(findings, 1):
            print(f"{index}. {finding['title']}")
            print(f"   Детали: {finding['details']}")
            for recommendation in finding["recommendations"]:
                print(f"   - {recommendation}")
    else:
        print(green("Проблем безопасности из текущего набора проверок не найдено."))

    try:
        recorded_at = record_security_audit_run()
    except Exception as exc:
        print(red(f"WARN: не удалось записать время проверки безопасности: {exc}"))
    else:
        print()
        print(f"Последняя проверка безопасности записана: {format_manager_time(recorded_at)}")


def disable_ssh_password_login():
    print("Будет отключён только вход по логину и паролю.")
    print("SSH-сервис и вход по SSH-ключам останутся включены.")
    print("Перед применением скрипт проверит root authorized_keys и валидность sshd config.")
    print()
    show_ssh_access()
    if root_authorized_key_count() < 1:
        print()
        print(red(f"Остановка: в {AUTHORIZED_KEYS_PATH} не найдено ни одного ключа."))
        print("Сначала добавь SSH-ключ, открой вторую сессию по ключу и только потом отключай парольный вход.")
        return
    if not SSHD_CONFIG_PATH.exists():
        print(red(f"Остановка: не найден {SSHD_CONFIG_PATH}."))
        return
    print()
    print("После применения текущая сессия обычно остаётся открытой, но новый вход по паролю будет запрещён.")
    if not confirm("Отключить парольный вход SSH сейчас"):
        print("Изменение отменено.")
        return

    original_config = SSHD_CONFIG_PATH.read_text(errors="ignore")
    original_dropin = SSHD_DROPIN_PATH.read_text(errors="ignore") if SSHD_DROPIN_PATH.exists() else None
    original_legacy_dropin = (
        SSHD_LEGACY_DROPIN_PATH.read_text(errors="ignore") if SSHD_LEGACY_DROPIN_PATH.exists() else None
    )
    config_backup = backup_existing_file(SSHD_CONFIG_PATH)
    dropin_backup = backup_existing_file(SSHD_DROPIN_PATH)
    legacy_backup = backup_existing_file(SSHD_LEGACY_DROPIN_PATH)
    if config_backup:
        print(f"Бэкап SSH config: {config_backup}")
    if dropin_backup:
        print(f"Бэкап managed drop-in: {dropin_backup}")
    if legacy_backup:
        print(f"Бэкап legacy drop-in: {legacy_backup}")

    try:
        write_sshd_password_dropin()
        include_added = ensure_sshd_dropin_include()
        if include_added:
            print(f"Добавлен ранний Include для {SSHD_DROPIN_PATH} в {SSHD_CONFIG_PATH}.")
        if remove_legacy_sshd_dropin():
            print(f"Удалён legacy drop-in: {SSHD_LEGACY_DROPIN_PATH}")
        valid, message = validate_sshd_config()
        if not valid:
            raise RuntimeError(f"sshd -t не прошёл проверку: {message}")
        settings, error = sshd_effective_config()
        if not ssh_password_login_disabled(settings):
            raise RuntimeError(f"sshd -T всё ещё показывает включённый парольный вход: {error or settings}")
        if not reload_sshd_service():
            raise RuntimeError("не удалось применить SSH config через systemctl")
    except Exception as exc:
        restore_text_file(SSHD_CONFIG_PATH, original_config)
        restore_text_file(SSHD_DROPIN_PATH, original_dropin)
        restore_text_file(SSHD_LEGACY_DROPIN_PATH, original_legacy_dropin)
        valid, _message = validate_sshd_config()
        if valid:
            reload_sshd_service()
        print(red(f"Изменения SSH отменены: {exc}"))
        return

    print(green("Готово: вход по паролю SSH отключён, вход по SSH-ключам оставлен."))
    print("Проверь новый вход из отдельного терминала: ssh root@SERVER_HOST")


def enable_ssh_password_login():
    print("Будет убрана managed-настройка Xray VPS Manager, которая отключала вход по паролю.")
    print("После этого парольный вход включится, если он разрешён основным sshd_config или drop-in файлами сервера.")
    print("Вход по SSH-ключам не отключается.")
    print()
    show_ssh_access()
    if not SSHD_CONFIG_PATH.exists():
        print(red(f"Остановка: не найден {SSHD_CONFIG_PATH}."))
        return
    print()
    if not confirm("Включить парольный вход SSH сейчас"):
        print("Изменение отменено.")
        return

    original_config = SSHD_CONFIG_PATH.read_text(errors="ignore")
    original_dropin = SSHD_DROPIN_PATH.read_text(errors="ignore") if SSHD_DROPIN_PATH.exists() else None
    original_legacy_dropin = (
        SSHD_LEGACY_DROPIN_PATH.read_text(errors="ignore") if SSHD_LEGACY_DROPIN_PATH.exists() else None
    )
    config_backup = backup_existing_file(SSHD_CONFIG_PATH)
    dropin_backup = backup_existing_file(SSHD_DROPIN_PATH)
    legacy_backup = backup_existing_file(SSHD_LEGACY_DROPIN_PATH)
    if config_backup:
        print(f"Бэкап SSH config: {config_backup}")
    if dropin_backup:
        print(f"Бэкап managed drop-in: {dropin_backup}")
    if legacy_backup:
        print(f"Бэкап legacy drop-in: {legacy_backup}")

    try:
        if remove_sshd_dropin_include():
            print(f"Удалён ранний Include для {SSHD_DROPIN_PATH} из {SSHD_CONFIG_PATH}.")
        if SSHD_DROPIN_PATH.exists():
            SSHD_DROPIN_PATH.unlink()
            print(f"Удалён managed drop-in: {SSHD_DROPIN_PATH}")
        if remove_legacy_sshd_dropin():
            print(f"Удалён legacy drop-in: {SSHD_LEGACY_DROPIN_PATH}")

        valid, message = validate_sshd_config()
        if not valid:
            raise RuntimeError(f"sshd -t не прошёл проверку: {message}")
        settings, error = sshd_effective_config()
        if not ssh_password_login_enabled(settings):
            raise RuntimeError(f"sshd -T всё ещё показывает отключённый парольный вход: {error or settings}")
        if not reload_sshd_service():
            raise RuntimeError("не удалось применить SSH config через systemctl")
    except Exception as exc:
        restore_text_file(SSHD_CONFIG_PATH, original_config)
        restore_text_file(SSHD_DROPIN_PATH, original_dropin)
        restore_text_file(SSHD_LEGACY_DROPIN_PATH, original_legacy_dropin)
        valid, _message = validate_sshd_config()
        if valid:
            reload_sshd_service()
        print(red(f"Изменения SSH отменены: {exc}"))
        return

    print(green("Готово: вход по паролю SSH включён."))
    print("Проверь новый вход из отдельного терминала перед закрытием текущей SSH-сессии.")


def execute_action(title, func):
    begin_action(title)
    try:
        func()
    finally:
        end_action(title)


def client_menu_handlers():
    return {
        "1": ("Показать клиентов", lambda: call(["xray-client", "list"])),
        "2": ("Добавить клиента", add_client_from_menu),
        "3": ("Изменить срок доступа", update_selected_client_days),
        "4": ("Изменить статус оплаты", update_selected_client_payment),
        "5": ("Отключить клиента", lambda: call_client_command("disable", "отключения", "enabled")),
        "6": ("Включить клиента", lambda: call_client_command("enable", "включения", "disabled")),
        "7": ("Удалить клиента", lambda: call_client_command("remove", "удаления", "all")),
        "8": ("Вывести ссылку клиента", lambda: call_client_command("link", "вывода ссылки", "all")),
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
        "1": ("Проверить безопасность сервера", run_security_audit),
        "2": ("Показать SSH-доступ", show_ssh_access),
        "3": ("Отключить вход по паролю SSH", disable_ssh_password_login),
        "4": ("Включить вход по паролю SSH", enable_ssh_password_login),
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
        "3": ("Установить лимит трафика", update_selected_client_limit),
        "4": ("Убрать лимит трафика", clear_selected_client_limit),
        "5": ("Проверить лимиты трафика", lambda: call(["xray-client", "enforce-limits", "--sync"])),
    }


def traffic_report_handlers(name):
    return {
        "1": ("Трафик за день по часам", lambda: show_traffic_day(name)),
        "2": ("Трафик за неделю по дням", lambda: show_traffic_week(name)),
        "3": ("Трафик за месяц по дням", lambda: show_traffic_month(name)),
        "4": ("Трафик за период по дням", lambda: show_traffic_period(name)),
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
        "6": ("Изменить маршрут", update_telegram_route_mode),
        "7": ("Отправить тестовое сообщение", lambda: call(["xray-telegram", "test"])),
        "8": ("Проверить GeoIP-уведомления сейчас", lambda: call(["xray-telegram", "notify-geoip"])),
        "9": ("Показать подписки клиентов", lambda: call(["xray-telegram", "subscribers"])),
        "10": ("Обработать сообщения пользователей", lambda: call(["xray-telegram", "poll-users"])),
        "11": ("Проверить напоминания об оплате", lambda: call(["xray-telegram", "notify-expiry"])),
        "12": ("Настроить оплату и округление", update_telegram_payment_amount),
        "13": ("Изменить имя бота", update_telegram_bot_name),
        "14": ("Уведомить о работах на сервере", send_telegram_maintenance_notice),
        "15": ("Обновить меню команд Telegram", lambda: call(["xray-telegram", "commands"])),
    }


def suspicious_menu_handlers():
    return {
        "1": ("Сводка suspicious", activity_suspicious_report),
        "2": ("GeoIP-риски подробно", activity_geoip_risk_details),
        "3": ("Настройки исключений", open_activity_exception_menu),
    }


def activity_exception_menu_handlers():
    return {
        "1": ("Показать исключения", lambda: call(["xray-activity", "exceptions"])),
        "2": (
            "Добавить из suspicious",
            lambda: menu_activity_exception_actions.activity_exception_add_from_suspicious(call, ask_activity_days),
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
        "5": ("Отчёт по клиенту", activity_client_report),
        "6": ("Подозрительная активность", open_activity_suspicious_menu),
        "7": (
            "Экспорт отчёта по клиенту",
            lambda: menu_activity_export_actions.activity_export_report(choose_client, call),
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
        "11": ("Изменить срок хранения журнала", update_activity_retention),
        "12": ("Настроить лимиты suspicious", update_activity_risk_limits),
        "13": ("GeoIP routing: выбрать регион", set_xray_geoip_routing_region),
        "14": ("GeoIP routing: отключить", disable_xray_geoip_routing_region),
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
        name = choose_traffic_client()
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
