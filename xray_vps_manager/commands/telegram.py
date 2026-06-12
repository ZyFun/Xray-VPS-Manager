#!/usr/bin/env python3
import copy
import fnmatch
import hashlib
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.traffic import formatting as traffic_formatting
from xray_vps_manager.traffic import history as traffic_history
from xray_vps_manager.traffic import repository as traffic_repository
from xray_vps_manager.telegram import api as telegram_api
from xray_vps_manager.telegram import keyboards as telegram_keyboards
from xray_vps_manager.telegram import messages as telegram_messages
from xray_vps_manager.telegram import payments as telegram_payments
from xray_vps_manager.telegram import settings as telegram_settings

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
CLIENT_DB_PATH = Path("/usr/local/etc/xray/clients.json")
SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
TRAFFIC_PATH = Path("/usr/local/etc/xray/traffic.json")
TELEGRAM_DB_PATH = Path("/usr/local/etc/xray/telegram-bot.json")
ACTIVITY_DIR = Path("/usr/local/etc/xray/activity")
CLIENT_LOG_DIR = ACTIVITY_DIR / "clients"
ACTIVITY_EXCEPTIONS_PATH = Path("/usr/local/etc/xray/activity-exceptions.json")
XRAY_BIN = Path("/usr/local/bin/xray")
XRAY_CLIENT = Path("/usr/local/sbin/xray-client")
CASCADE_UPSTREAM_TAG = "cascade-upstream"
TELEGRAM_SOCKS_TAG = "telegram-bot-socks"
TELEGRAM_SOCKS_HOST = "127.0.0.1"
TELEGRAM_SOCKS_PORT = 10810
XRAY_GEOIP_OUTBOUND_PREFIX = "geoip-warning-"
EXPIRY_REMINDER_DAYS = (5, 1)
EXPIRY_REMINDER_HOUR = 8
DAILY_SUMMARY_HOUR = 8
ONLINE_WINDOW_SECONDS = 300
USER_POLL_SHORT_TIMEOUT = 2
USER_POLL_LONG_TIMEOUT = 45
USER_POLLER_SLEEP_UNCONFIGURED = 30
USER_POLLER_SLEEP_ERROR = 5
TELEGRAM_MESSAGE_LIMIT = 3900
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
DEFAULT_SERVER_NAME = "Xray"
DEFAULT_BOT_NAME = "Vireika"
MAINTENANCE_NOTICE_TEMPLATES = telegram_messages.MAINTENANCE_NOTICE_TEMPLATES
DEFAULT_DB = telegram_settings.DEFAULT_DB


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def run_capture(command, timeout=20, input_text=None):
    return subprocess.run(
        command,
        check=False,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def require_root():
    if os.geteuid() != 0:
        die("Run this script as root.")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_stamp():
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_time(value):
    raw = str(value or "").strip()
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


def load_json(path, default):
    return telegram_settings.load_json(path, default)


def chown_xray(path):
    telegram_settings.chown_xray(path)


def ensure_config_dir():
    telegram_settings.ensure_config_dir(TELEGRAM_DB_PATH)


def load_db():
    return telegram_settings.load_db(TELEGRAM_DB_PATH)


def save_db(db):
    telegram_settings.save_db(db, TELEGRAM_DB_PATH)


def save_db_sections(db, sections):
    telegram_settings.save_db_sections(db, sections, TELEGRAM_DB_PATH)


def mask_token(token):
    return telegram_settings.mask_token(token)


def normalize_display_name(value, default, label):
    return telegram_settings.normalize_display_name(value, default, label)


def bot_name(db=None):
    return telegram_settings.bot_name(db, loader=load_db)


def set_bot_name(value):
    db = load_db()
    db["botName"] = normalize_display_name(value, DEFAULT_BOT_NAME, "BOT_NAME")
    save_db(db)
    print(f"Bot name: {db['botName']}")


def server_env_values():
    values = {}
    if SERVER_ENV_PATH.exists():
        for line in SERVER_ENV_PATH.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"').strip("'")
    return values


def server_name_fragment():
    value = (server_env_values().get("SERVER_NAME") or DEFAULT_SERVER_NAME).strip()
    if not value or not SERVER_NAME_RE.fullmatch(value):
        value = DEFAULT_SERVER_NAME
    return quote(value, safe="")


def display_timezone():
    configured = (server_env_values().get("MANAGER_TIMEZONE") or "").strip()
    if configured:
        try:
            return ZoneInfo(configured), configured
        except ZoneInfoNotFoundError:
            return timezone.utc, f"UTC (invalid MANAGER_TIMEZONE: {configured})"
    local = datetime.now().astimezone().tzinfo or timezone.utc
    return local, "server local time"


def format_event_time(value):
    moment = parse_time(value)
    if not moment:
        return value or "-"
    tzinfo, label = display_timezone()
    return f"{moment.astimezone(tzinfo).strftime('%Y-%m-%d %H:%M:%S')} {label}"


def load_config():
    if not CONFIG_PATH.exists():
        die(f"Config not found: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text())


def load_client_db():
    return load_json(CLIENT_DB_PATH, {"clients": {}, "connections": {}})


def load_traffic_db():
    return traffic_repository.load_traffic_db(TRAFFIC_PATH)


def client_db_clients(db):
    clients = db.get("clients", {})
    return clients if isinstance(clients, dict) else {}


def client_db_connections(db):
    connections = db.get("connections", {})
    return connections if isinstance(connections, dict) else {}


def save_config(config):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    backup = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.bak.telegram.{timestamp}")
    shutil.copy2(CONFIG_PATH, backup)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    chown_xray(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(CONFIG_PATH)
    return backup


def restart_xray():
    result = run_capture(["systemctl", "restart", "xray"], timeout=30)
    if result.returncode == 0:
        return
    run_capture(["systemctl", "reset-failed", "xray"], timeout=10)
    time.sleep(1.0)
    retry = run_capture(["systemctl", "restart", "xray"], timeout=30)
    if retry.returncode == 0:
        return
    detail = (retry.stderr or retry.stdout or result.stderr or result.stdout or "systemctl restart failed").strip()
    raise subprocess.CalledProcessError(retry.returncode, retry.args, retry.stdout, detail)


def apply_config(config):
    backup = save_config(config)
    try:
        run([str(XRAY_BIN), "run", "-test", "-config", str(CONFIG_PATH)])
        restart_xray()
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        chown_xray(CONFIG_PATH)
        os.chmod(CONFIG_PATH, 0o640)
        restart_xray()
        die(f"New config failed. Restored backup: {backup}")
    return backup


def rule_values(rule, key):
    value = rule.get(key, [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return []


def routing_rules(config):
    routing = config.setdefault("routing", {})
    routing.setdefault("domainStrategy", "IPIfNonMatch")
    return routing.setdefault("rules", [])


def remove_telegram_proxy_config(config):
    old_inbounds = config.get("inbounds", [])
    config["inbounds"] = [item for item in old_inbounds if item.get("tag") != TELEGRAM_SOCKS_TAG]
    rules = routing_rules(config)
    config["routing"]["rules"] = [rule for rule in rules if TELEGRAM_SOCKS_TAG not in rule_values(rule, "inboundTag")]
    return len(config["inbounds"]) != len(old_inbounds) or len(config["routing"]["rules"]) != len(rules)


def telegram_proxy_configured(config):
    has_inbound = any(item.get("tag") == TELEGRAM_SOCKS_TAG for item in config.get("inbounds", []))
    has_route = any(TELEGRAM_SOCKS_TAG in rule_values(rule, "inboundTag") for rule in routing_rules(config))
    return has_inbound and has_route


def ensure_telegram_proxy_config(config):
    if not any(item.get("tag") == CASCADE_UPSTREAM_TAG for item in config.get("outbounds", [])):
        die("Cascade outbound is not configured. Add cascade first or use direct Telegram route mode.")

    remove_telegram_proxy_config(config)
    config.setdefault("inbounds", []).append(
        {
            "tag": TELEGRAM_SOCKS_TAG,
            "listen": TELEGRAM_SOCKS_HOST,
            "port": TELEGRAM_SOCKS_PORT,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        }
    )
    routing_rules(config).insert(
        0,
        {
            "type": "field",
            "inboundTag": [TELEGRAM_SOCKS_TAG],
            "outboundTag": CASCADE_UPSTREAM_TAG,
        },
    )


def wait_for_tcp(host, port, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def set_route_mode(mode):
    if mode not in ("direct", "cascade"):
        die("Route mode must be direct or cascade.")
    db = load_db()
    previous_mode = db.get("routeMode", "direct")
    config = load_config()
    if mode == "cascade":
        db["routeMode"] = mode
        save_db(db)
        ensure_telegram_proxy_config(config)
        try:
            backup = apply_config(config)
        except Exception:
            db["routeMode"] = previous_mode
            save_db(db)
            raise
        if not wait_for_tcp(TELEGRAM_SOCKS_HOST, TELEGRAM_SOCKS_PORT):
            die(f"Telegram SOCKS inbound did not open: {TELEGRAM_SOCKS_HOST}:{TELEGRAM_SOCKS_PORT}")
        print(f"Telegram Bot API traffic will use cascade through SOCKS {TELEGRAM_SOCKS_HOST}:{TELEGRAM_SOCKS_PORT}.")
        print(f"Backup: {backup}")
    else:
        changed = remove_telegram_proxy_config(config)
        if changed:
            backup = apply_config(config)
            print("Telegram Bot API traffic will use direct server internet route.")
            print(f"Backup: {backup}")
        else:
            print("Telegram Bot API traffic already uses direct server internet route.")
    db["routeMode"] = mode
    save_db(db)


def curl_json(db, method, payload=None, timeout=30):
    try:
        return telegram_api.curl_json(db, method, payload=payload, timeout=timeout)
    except ValueError as exc:
        die(str(exc))


def telegram_chat_label(chat):
    parts = []
    if chat.get("username"):
        parts.append("@" + str(chat["username"]))
    full_name = " ".join(str(chat.get(key, "")).strip() for key in ("first_name", "last_name")).strip()
    if full_name:
        parts.append(full_name)
    if not parts:
        parts.append(str(chat.get("id", "")))
    return " / ".join(parts)


def private_chats_from_updates(updates):
    rows = []
    seen = set()
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or "id" not in chat:
            continue
        chat_id = str(chat["id"])
        if chat_id in seen:
            continue
        seen.add(chat_id)
        rows.append({"id": chat_id, "label": telegram_chat_label(chat)})
    return rows


def choose_private_chat(db):
    print("Теперь отправь любое сообщение новому боту в Telegram, например /start.")
    print("После этого нажми Enter здесь, чтобы сервер увидел твой chat_id.")
    while True:
        input("Enter после отправки /start боту: ")
        data = curl_json(db, "getUpdates", {"allowed_updates": ["message"], "timeout": 2}, timeout=15)
        chats = private_chats_from_updates(data.get("result", []))
        if not chats:
            print("Личные чаты не найдены. Проверь, что ты написал именно этому боту, и попробуй ещё раз.")
            continue
        print("Найденные личные чаты:")
        for index, row in enumerate(chats, start=1):
            print(f"{index}. {row['label']} ({row['id']})")
        choice = input("Выбери чат для уведомлений [1]: ").strip() or "1"
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(chats):
                return chats[index - 1]
        print("Неизвестный выбор.")


def initialize_geoip_offsets(db):
    state = db.setdefault("geoipState", {})
    files = {}
    if CLIENT_LOG_DIR.exists():
        for path in CLIENT_LOG_DIR.glob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            files[str(path)] = {"inode": stat.st_ino, "offset": stat.st_size}
    state["files"] = files
    state["sentIds"] = []
    state["updated"] = utc_stamp()


def send_chat_message(db, chat_id, text, reply_markup=None, parse_mode=None):
    try:
        return telegram_api.send_chat_message(db, chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    except ValueError as exc:
        die(str(exc))


def send_message(db, text, parse_mode=None):
    return send_chat_message(db, db.get("chatId"), text, parse_mode=parse_mode)


def answer_callback_query(db, callback_id, text="", show_alert=False):
    return telegram_api.answer_callback_query(db, callback_id, text=text, show_alert=show_alert)


def refresh_user_update_offset(db):
    try:
        data = curl_json(db, "getUpdates", {"allowed_updates": ["message", "callback_query"], "timeout": 1}, timeout=10)
    except Exception:
        return
    updates = data.get("result", [])
    if not updates:
        return
    offset = max(int(update.get("update_id", 0)) + 1 for update in updates)
    state = db.setdefault("clientSubscriptionState", {})
    state["userUpdateOffset"] = max(int(state.get("userUpdateOffset", 0) or 0), offset)


def ask_token_if_missing(db):
    if db.get("token"):
        return db
    print("Token Telegram-бота не настроен.")
    print("Возьми token у @BotFather и вставь его сюда.")
    token = input("BOT_TOKEN: ").strip()
    if not token or ":" not in token:
        die("BOT_TOKEN выглядит некорректно.")
    db["token"] = token
    save_db(db)
    return db


def maybe_adopt_existing_cascade_route(db):
    if db.get("routeMode") == "cascade":
        return db
    try:
        config = load_config()
    except Exception:
        return db
    if not telegram_proxy_configured(config):
        return db
    print("Найден уже настроенный Telegram SOCKS для cascade в Xray config.")
    print("Похоже, первичная настройка оборвалась после применения маршрута, но до выбора владельца бота.")
    answer = input("Использовать cascade для привязки владельца? [Y/n]: ").strip().lower()
    if answer in ("", "y", "yes", "д", "да"):
        db["routeMode"] = "cascade"
        save_db(db)
    return db


def configure_owner(send_test=True):
    db = ask_token_if_missing(load_db())
    db = maybe_adopt_existing_cascade_route(db)
    me = curl_json(db, "getMe")
    bot = me.get("result", {})
    print(f"Бот найден: @{bot.get('username', 'unknown')}")
    chat = choose_private_chat(db)
    db["chatId"] = chat["id"]
    db["chatLabel"] = chat["label"]
    db["enabled"] = True
    initialize_geoip_offsets(db)
    refresh_user_update_offset(db)
    save_db(db)
    configure_bot_commands()
    if send_test:
        send_message(db, "Xray VPS Manager: Telegram-уведомления подключены. GeoIP-уведомления будут отправляться только в этот чат.")
    print("Telegram bot owner/chat configured.")
    print(f"Chat: {db['chatLabel']} ({db['chatId']})")
    print(f"Route mode: {db['routeMode']}")


def setup():
    print("Нужен токен Telegram-бота от @BotFather.")
    print("1. Открой Telegram и найди @BotFather.")
    print("2. Создай бота командой /newbot.")
    print("3. Скопируй token вида 123456:ABC-DEF...")
    token = input("BOT_TOKEN: ").strip()
    if not token or ":" not in token:
        die("BOT_TOKEN выглядит некорректно.")
    db = load_db()
    db["token"] = token

    print()
    print("BOT_NAME: имя бота в сообщениях пользователям.")
    print("Например: Vireika. Если нажать Enter, останется текущее значение.")
    value = input(f"BOT_NAME [{bot_name(db)}]: ").strip()
    db["botName"] = normalize_display_name(value or bot_name(db), DEFAULT_BOT_NAME, "BOT_NAME")

    print()
    print("Как боту выходить в интернет?")
    print("1. direct: напрямую с этого сервера")
    print("2. cascade: через исходящий сервер, настроенный как cascade-upstream")
    mode_choice = input("Route mode [1-direct]: ").strip() or "1"
    mode = "cascade" if mode_choice == "2" else "direct"
    save_db(db)
    if mode == "cascade":
        print()
        print("Сейчас будет применён cascade-маршрут для Telegram Bot API.")
        print("Если SSH-сессия оборвётся на перезапуске Xray, подключись заново и запусти:")
        print("  xray-telegram owner")
        print("или в меню: Telegram бот -> Донастроить владельца/чат")
    set_route_mode(mode)
    configure_owner(send_test=True)


def set_enabled(value):
    db = load_db()
    db["enabled"] = bool(value)
    if value:
        initialize_geoip_offsets(db)
    save_db(db)
    print("Telegram notifications enabled." if value else "Telegram notifications disabled.")


def test_message():
    db = load_db()
    send_message(db, "Xray VPS Manager: тестовое сообщение Telegram-бота.")
    print("Test message sent.")


def configure_bot_commands():
    db = load_db()
    if not db.get("token"):
        die("Telegram bot token is not configured.")
    commands = [
        {"command": "start", "description": "Открыть меню"},
        {"command": "status", "description": "Показать подписку"},
        {"command": "link", "description": "Получить VLESS-ссылку"},
        {"command": "unsubscribe", "description": "Отключить напоминания"},
        {"command": "help", "description": "Помощь"},
    ]
    curl_json(db, "setMyCommands", {"commands": commands}, timeout=30)
    print("Telegram command menu updated.")


def normalize_value(value):
    return str(value or "").strip().lower()


def find_vless_link(text):
    match = re.search(r"vless://[^\s<>()]+", str(text or ""), flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(0).rstrip(".,;")


def parse_vless_link(text):
    link = find_vless_link(text)
    if not link:
        raise ValueError("VLESS-ссылка не найдена.")
    parsed = urlsplit(link)
    if parsed.scheme.lower() != "vless":
        raise ValueError("Это не VLESS-ссылка.")
    client_id = unquote(parsed.username or "").strip().lower()
    if not UUID_RE.fullmatch(client_id):
        raise ValueError("В ссылке не найден корректный UUID клиента.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("В ссылке указан некорректный порт.") from exc
    if not port:
        raise ValueError("В ссылке не указан порт подключения.")
    params = {key.lower(): value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
    for key in ("security", "pbk", "sni", "sid"):
        if not params.get(key):
            raise ValueError(f"В ссылке нет обязательного Reality-параметра: {key}")
    if normalize_value(params.get("security")) != "reality":
        raise ValueError("Поддерживаются только VLESS Reality-ссылки.")
    return {
        "raw": link,
        "id": client_id,
        "host": parsed.hostname or "",
        "port": str(port or ""),
        "params": params,
        "hash": hashlib.sha256(link.encode("utf-8")).hexdigest(),
    }


def client_entry_id(entry):
    return normalize_value(entry.get("id") or (entry.get("client") or {}).get("id"))


def expected_connection_params(client_db, entry):
    connection_tag = entry.get("connection", "")
    connection = client_db_connections(client_db).get(connection_tag, {})
    return {
        "port": str(connection.get("port", "")),
        "pbk": connection.get("publicKey", ""),
        "sni": connection.get("sni", ""),
        "sid": connection.get("shortId", ""),
        "fp": connection.get("fingerprint", ""),
        "security": "reality",
        "encryption": "none",
        "type": "tcp",
        "flow": (entry.get("client") or {}).get("flow", "xtls-rprx-vision"),
    }


def match_vless_to_client(parsed_link):
    client_db = load_client_db()
    clients = client_db_clients(client_db)
    same_uuid = []
    for name, entry in clients.items():
        if client_entry_id(entry) == parsed_link["id"]:
            same_uuid.append((name, entry))
    if not same_uuid:
        return None, "По UUID из ссылки клиент в базе не найден."

    matches = []
    mismatch_notes = []
    for name, entry in same_uuid:
        expected = expected_connection_params(client_db, entry)
        mismatches = []
        for key, expected_value in expected.items():
            if not expected_value:
                continue
            actual_value = parsed_link["port"] if key == "port" else parsed_link["params"].get(key, "")
            if actual_value and normalize_value(actual_value) != normalize_value(expected_value):
                mismatches.append(key)
        if mismatches:
            mismatch_notes.append(f"{name}: " + ", ".join(mismatches))
            continue
        matches.append((name, entry))

    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, "По ссылке найдено несколько клиентов. Обратись к администратору."
    detail = "; ".join(mismatch_notes) if mismatch_notes else "параметры подключения не совпали"
    return None, "UUID найден, но Reality-параметры ссылки не совпадают с текущей базой: " + detail


def format_access_until(value):
    if not value:
        return "бессрочно"
    moment = parse_time(value)
    if not moment:
        return value
    tzinfo, label = display_timezone()
    return f"{moment.astimezone(tzinfo).strftime('%Y-%m-%d %H:%M')} {label}"


def parse_payment_value(value):
    return telegram_payments.parse_payment_value(value)


def decimal_storage_value(value):
    return telegram_payments.decimal_storage_value(value)


def format_decimal_amount(amount):
    return telegram_payments.format_decimal_amount(amount)


def format_payment_amount(amount, currency):
    return telegram_payments.format_payment_amount(amount, currency)


def parse_payment_rounding_step(value):
    return telegram_payments.parse_payment_rounding_step(value)


def normalize_payment_rounding_mode(value):
    return telegram_payments.normalize_payment_rounding_mode(value)


def payment_rounding_settings(db):
    return telegram_payments.payment_rounding_settings(db)


def payment_rounding_label(db):
    return telegram_payments.payment_rounding_label(db)


def paid_client_count(client_db=None):
    client_db = client_db or load_client_db()
    return sum(1 for entry in client_db_clients(client_db).values() if entry.get("paymentType") == "paid")


def payment_share_amount(total_amount, paid_count, rounding_mode="none", rounding_step="10"):
    return telegram_payments.payment_share_amount(total_amount, paid_count, rounding_mode, rounding_step)


def payment_amount_label(db, client_db=None):
    total = str(db.get("paymentTotalAmount") or "").strip()
    currency = db.get("paymentCurrency") or "₽"
    count = paid_client_count(client_db)
    rounding_mode, rounding_step = payment_rounding_settings(db)
    share = payment_share_amount(total, count, rounding_mode, rounding_step)
    if not total:
        return "не указана"
    if count <= 0:
        return f"не рассчитана: нет платных клиентов (общая сумма: {format_payment_amount(total, currency)})"
    return format_payment_amount(share, currency)


def payment_summary(db, client_db=None):
    total = str(db.get("paymentTotalAmount") or "").strip()
    currency = db.get("paymentCurrency") or "₽"
    count = paid_client_count(client_db)
    rounding_mode, rounding_step = payment_rounding_settings(db)
    share = payment_share_amount(total, count, rounding_mode, rounding_step)
    summary = {
        "total": format_payment_amount(total, currency),
        "paidCount": count,
        "rounding": payment_rounding_label(db),
        "share": format_payment_amount(share, currency) if share else "не рассчитана",
    }
    if not total and count > 0:
        summary["warning"] = (
            "Total rent amount is not set. Configure it in "
            "Telegram бот -> Настроить оплату и округление, "
            "or run: xray-telegram payment-amount '500 ₽'"
        )
    return summary


def print_payment_summary(db):
    summary = payment_summary(db)
    print(f"Total rent amount: {summary['total']}")
    print(f"Paid clients: {summary['paidCount']}")
    print(f"Rounding: {summary['rounding']}")
    print(f"Amount per paid client: {summary['share']}")
    if summary.get("warning"):
        print(f"WARN: {summary['warning']}")


def set_payment_amount(value):
    db = load_db()
    amount, currency = parse_payment_value(value)
    db["paymentTotalAmount"] = amount
    db["paymentCurrency"] = currency
    db["paymentAmount"] = format_payment_amount(amount, currency) if amount else ""
    save_db(db)
    if amount:
        print_payment_summary(db)
    else:
        print("Payment amount cleared.")


def set_payment_rounding(mode_value, step_value=None):
    db = load_db()
    mode = normalize_payment_rounding_mode(mode_value)
    if mode == "step":
        if not step_value:
            raise ValueError("Для режима step нужно указать шаг округления.")
        db["paymentRoundingStep"] = parse_payment_rounding_step(step_value)
    db["paymentRoundingMode"] = mode
    if "paymentRoundingStep" not in db:
        db["paymentRoundingStep"] = "10"
    save_db(db)
    print_payment_summary(db)


def show_payment_amount():
    db = load_db()
    print_payment_summary(db)


def format_traffic(value):
    return traffic_formatting.format_traffic(value, none_label="0.00KB")


def traffic_bucket_totals(bucket):
    return traffic_history.traffic_bucket_totals(bucket)


def traffic_day_total(entry, day_key):
    return traffic_history.traffic_day_total(entry, day_key)


def systemd_state(unit):
    result = run_capture(["systemctl", "is-active", unit], timeout=5)
    if result.returncode == 0:
        return result.stdout.strip() or "active"
    return (result.stdout or result.stderr or "unknown").strip() or "unknown"


def disk_usage_label(path="/"):
    usage = shutil.disk_usage(path)
    used = usage.total - usage.free
    percent = int((used / usage.total) * 100) if usage.total else 0
    return f"{percent}% занято, свободно {format_traffic(usage.free)}"


def client_enabled(entry):
    return entry.get("enabled") is not False


def online_clients_count(client_db, traffic_db):
    now = utc_now()
    count = 0
    clients = client_db_clients(client_db)
    for name, entry in clients.items():
        if not client_enabled(entry):
            continue
        traffic_entry = traffic_db.get("clients", {}).get(name, {})
        parsed = parse_time(traffic_entry.get("lastOnline", ""))
        if parsed and (now - parsed).total_seconds() <= ONLINE_WINDOW_SECONDS:
            count += 1
    return count


def daily_traffic_rows(client_db, traffic_db, day_key):
    names = set(client_db_clients(client_db)) | set(traffic_db.get("clients", {}))
    rows = []
    total_in = 0
    total_out = 0
    for name in sorted(names):
        entry = traffic_db.get("clients", {}).get(name, {})
        incoming, outgoing = traffic_day_total(entry, day_key)
        total = incoming + outgoing
        total_in += incoming
        total_out += outgoing
        if total > 0:
            rows.append({"name": name, "incoming": incoming, "outgoing": outgoing, "total": total})
    rows.sort(key=lambda item: item["total"], reverse=True)
    return rows, total_in, total_out


def build_daily_summary_message(target_day=None):
    db = load_db()
    tzinfo, timezone_label = display_timezone()
    now_local = utc_now().astimezone(tzinfo)
    day = target_day or (now_local.date() - timedelta(days=1))
    day_key = day.isoformat()
    client_db = load_client_db()
    traffic_db = load_traffic_db()
    clients = client_db_clients(client_db)
    enabled_count = sum(1 for entry in clients.values() if client_enabled(entry))
    rows, total_in, total_out = daily_traffic_rows(client_db, traffic_db, day_key)
    total = total_in + total_out

    lines = [
        "Xray VPS Manager: ежедневная сводка",
        "",
        f"Период трафика: {day_key} ({timezone_label})",
        f"Xray: {systemd_state('xray.service')}",
        f"Telegram poller: {systemd_state('xray-telegram-poller.service')}",
        f"Клиенты: включено {enabled_count} из {len(clients)}, online сейчас: {online_clients_count(client_db, traffic_db)}",
        f"Диск /: {disk_usage_label('/')}",
        "",
        "Трафик за предыдущий день:",
        f"IN: {format_traffic(total_in)}",
        f"OUT: {format_traffic(total_out)}",
        f"TOTAL: {format_traffic(total)}",
    ]
    if rows:
        lines.extend(["", "Топ клиентов:"])
        for index, row in enumerate(rows[:5], start=1):
            lines.append(
                f"{index}. {row['name']}: {format_traffic(row['total'])} "
                f"(IN {format_traffic(row['incoming'])} / OUT {format_traffic(row['outgoing'])})"
            )
        if len(rows) > 5:
            lines.append(f"...и ещё клиентов с трафиком: {len(rows) - 5}")
    else:
        lines.extend(["", "Трафика за этот день не было."])
    return "\n".join(lines)


def notify_daily_summary(quiet=False, dry_run=False):
    db = load_db()
    if not db.get("enabled") or not db.get("token") or not db.get("chatId"):
        if not quiet:
            print("Telegram bot notifications are not configured or disabled.")
        return 0

    tzinfo, _timezone_label = display_timezone()
    now_local = utc_now().astimezone(tzinfo)
    target_day = now_local.date() - timedelta(days=1)
    target_key = target_day.isoformat()
    state = db.setdefault("dailySummaryState", {})

    if dry_run:
        print(build_daily_summary_message(target_day))
        return 0

    if now_local.hour < DAILY_SUMMARY_HOUR:
        if not quiet:
            print(f"Daily summary is not due yet: local time {now_local.strftime('%H:%M')}.")
        return 0

    if state.get("lastSentDate") == target_key:
        if not quiet:
            print(f"Daily summary already sent for {target_key}.")
        return 0

    message = build_daily_summary_message(target_day)
    try:
        send_message(db, message)
    except Exception as exc:
        if not quiet:
            print(f"ERROR: daily summary notification failed: {exc}", file=sys.stderr)
        return 1

    state["lastSentDate"] = target_key
    state["lastSentAt"] = utc_stamp()
    save_db_sections(db, ("dailySummaryState",))
    if not quiet:
        print(f"Daily summary sent for {target_key}.")
    return 0


def client_access_summary(entry):
    status = "отключён" if entry.get("enabled") is False else "включён"
    reason = entry.get("disabledReason", "")
    if reason == "expired":
        status = "отключён: срок истёк"
    elif reason == "traffic-limit":
        status = "отключён: лимит трафика"
    return "\n".join(
        [
            f"Статус: {status}",
            f"Доступ до: {format_access_until(entry.get('expiresAt', ''))}",
        ]
    )


def client_menu_keyboard():
    return telegram_keyboards.client_menu_keyboard()


def is_owner_chat(db, chat_id):
    return telegram_keyboards.is_owner_chat(db, chat_id)


def client_keyboard_for_chat(db, chat_id):
    return telegram_keyboards.client_keyboard_for_chat(db, chat_id)


def admin_menu_keyboard():
    return telegram_keyboards.admin_menu_keyboard()


def admin_notices_keyboard():
    return telegram_keyboards.admin_notices_keyboard()


def admin_notice_confirm_keyboard(kind):
    return telegram_keyboards.admin_notice_confirm_keyboard(kind)


def subscription_intro_text():
    db = load_db()
    return telegram_messages.subscription_intro_text(db, bot_name)


def subscribe_prompt_text():
    return telegram_messages.subscribe_prompt_text()


def admin_intro_text():
    return telegram_messages.admin_intro_text()


def truncate_telegram_text(text):
    return telegram_messages.truncate_telegram_text(text, TELEGRAM_MESSAGE_LIMIT)


def send_client_menu(db, chat_id, text=None, parse_mode=None):
    send_chat_message(
        db,
        chat_id,
        text or subscription_intro_text(),
        reply_markup=client_keyboard_for_chat(db, chat_id),
        parse_mode=parse_mode,
    )


def send_admin_menu(db, chat_id, text=None):
    send_chat_message(db, chat_id, text or admin_intro_text(), reply_markup=admin_menu_keyboard())


def send_admin_notices_menu(db, chat_id, text=None):
    send_chat_message(
        db,
        chat_id,
        text or telegram_messages.admin_notices_intro_text(),
        reply_markup=admin_notices_keyboard(),
    )


def subscription_status_for_chat(db, chat_id):
    subscription = db.get("clientSubscriptions", {}).get(str(chat_id))
    if not subscription:
        return "Ты пока не подписан на напоминания. Отправь свою VLESS-ссылку, чтобы подключить уведомления."
    client_db = load_client_db()
    name = subscription.get("client", "")
    entry = client_db_clients(client_db).get(name)
    if not entry:
        return "Подписка найдена, но клиента уже нет в базе. Отправь актуальную VLESS-ссылку или обратись к администратору."
    if subscription.get("clientId") and normalize_value(subscription.get("clientId")) != client_entry_id(entry):
        return "Подписка устарела: клиент был пересоздан. Отправь актуальную VLESS-ссылку заново."
    return "Текущая подписка:\n" + client_access_summary(entry)


def subscription_entry_for_chat(db, chat_id):
    subscription = db.get("clientSubscriptions", {}).get(str(chat_id))
    if not subscription:
        return None, None, "Ты пока не подписан на напоминания. Сначала отправь свою VLESS-ссылку."
    client_db = load_client_db()
    name = subscription.get("client", "")
    entry = client_db_clients(client_db).get(name)
    if not entry:
        return None, None, "Подписка найдена, но клиента уже нет в базе. Отправь актуальную VLESS-ссылку или обратись к администратору."
    if subscription.get("clientId") and normalize_value(subscription.get("clientId")) != client_entry_id(entry):
        return None, None, "Подписка устарела: клиент был пересоздан. Отправь актуальную VLESS-ссылку заново."
    return name, entry, ""


def neutral_vless_fragment(link):
    raw = str(link or "").strip()
    if not raw:
        return raw
    base = raw.split("#", 1)[0]
    return base + "#" + server_name_fragment()


def telegram_html_escape(value):
    return html.escape(str(value or ""), quote=False)


def current_vless_link_value_for_chat(db, chat_id):
    name, _entry, error = subscription_entry_for_chat(db, chat_id)
    if error:
        return "", error
    if not XRAY_CLIENT.exists():
        return "", "xray-client не найден на сервере. Обратись к администратору."
    result = run_capture([str(XRAY_CLIENT), "link", name], timeout=20)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        return "", "Не удалось получить актуальную VLESS-ссылку: " + detail
    link = ""
    for line in result.stdout.splitlines():
        if line.strip().startswith("vless://"):
            link = line.strip()
            break
    if not link:
        return "", "Не удалось получить актуальную VLESS-ссылку: xray-client не вернул ссылку."
    return neutral_vless_fragment(link), ""


def current_vless_link_for_chat(db, chat_id):
    link, error = current_vless_link_value_for_chat(db, chat_id)
    if error:
        return error
    return "\n".join(
        [
            "Актуальная VLESS Reality-ссылка:",
            "",
            link,
            "",
            "Если настройки подключения менялись, импортируй эту ссылку заново.",
        ]
    )


def current_vless_link_code_for_chat(db, chat_id):
    link, error = current_vless_link_value_for_chat(db, chat_id)
    if error:
        return error, None
    return (
        "\n".join(
            [
                "Актуальная VLESS Reality-ссылка:",
                "",
                f"<pre><code>{telegram_html_escape(link)}</code></pre>",
                "",
                "Если настройки подключения менялись, импортируй эту ссылку заново.",
            ]
        ),
        "HTML",
    )


def unsubscribe_chat(db, chat_id):
    removed = db.setdefault("clientSubscriptions", {}).pop(str(chat_id), None)
    if removed:
        return "Подписка на напоминания отключена."
    return "Активной подписки нет."


def admin_status_text(db):
    subscriptions = db.get("clientSubscriptions", {})
    subscription_state = db.get("clientSubscriptionState", {})
    daily_summary_state = db.get("dailySummaryState", {})
    return "\n".join(
        [
            "Xray VPS Manager: статус бота",
            "",
            f"Уведомления: {'включены' if db.get('enabled') else 'отключены'}",
            f"Маршрут Telegram: {db.get('routeMode', 'direct')}",
            f"Оплата: {payment_amount_label(db)}",
            f"Округление: {payment_rounding_label(db)}",
            f"Подписки клиентов: {len(subscriptions)}",
            f"Последний GeoIP: {db.get('geoipState', {}).get('lastGeoipNotification') or db.get('lastGeoipNotification') or 'never'}",
            f"Последний poll: {subscription_state.get('lastUserPoll') or 'never'}",
            f"Последнее напоминание: {subscription_state.get('lastExpiryReminder') or 'never'}",
            f"Последняя сводка: {daily_summary_state.get('lastSentDate') or 'never'}",
        ]
    )


def admin_subscribers_text(db):
    subscriptions = db.get("clientSubscriptions", {})
    if not subscriptions:
        return "Подписок клиентов пока нет."
    client_db = load_client_db()
    clients = client_db_clients(client_db)
    lines = ["Xray VPS Manager: подписки клиентов", ""]
    for chat_id, subscription in sorted(subscriptions.items(), key=lambda item: item[1].get("client", ""))[:25]:
        name = subscription.get("client", "-")
        entry = clients.get(name, {})
        access_until = format_access_until(entry.get("expiresAt", "") if isinstance(entry, dict) else "")
        valid = "актуальна" if entry and normalize_value(subscription.get("clientId")) == client_entry_id(entry) else "требует проверки"
        lines.append(f"- {name}: {valid}, до {access_until}, чат {subscription.get('chatLabel', chat_id)}")
    if len(subscriptions) > 25:
        lines.append(f"...и ещё подписок: {len(subscriptions) - 25}")
    return "\n".join(lines)


def admin_run_server_test_text():
    test_script = Path("/usr/local/sbin/xray-test")
    if not test_script.exists():
        return "xray-test не найден на сервере."
    result = run_capture([str(test_script)], timeout=90)
    output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    header = "Xray VPS Manager: проверка сервера"
    if result.returncode == 0:
        header += "\nСтатус: OK"
    else:
        header += f"\nСтатус: ошибка, exit {result.returncode}"
    return truncate_telegram_text(header + "\n\n" + output.strip())


def admin_create_backup_text():
    backup_script = Path("/usr/local/sbin/xray-backup")
    if not backup_script.exists():
        return "xray-backup не найден на сервере."
    result = run_capture([str(backup_script), "create", "--path-only"], timeout=120)
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return truncate_telegram_text(f"Не удалось создать backup, exit {result.returncode}.\n\n{output}")
    return "Backup создан на сервере:\n" + output


def subscribe_chat_to_client(db, chat, text):
    parsed_link = parse_vless_link(text)
    match, reason = match_vless_to_client(parsed_link)
    if not match:
        raise ValueError(reason)
    name, entry = match
    chat_id = str(chat["id"])
    db.setdefault("clientSubscriptions", {})[chat_id] = {
        "client": name,
        "clientId": client_entry_id(entry),
        "connection": entry.get("connection", ""),
        "chatLabel": telegram_chat_label(chat),
        "linkHash": parsed_link["hash"],
        "subscribedAt": utc_stamp(),
        "enabled": True,
    }
    return name, entry


def handle_user_message(db, update):
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or "id" not in chat:
        return False
    chat_id = str(chat["id"])
    text = str(message.get("text") or "").strip()
    if not text:
        send_client_menu(db, chat_id, "Отправь текстовую VLESS-ссылку или нажми кнопку ниже.")
        return True
    pending = db.get("adminState", {}).get(chat_id, {})
    if is_owner_chat(db, chat_id) and pending.get("action") == "custom-notice-text":
        if text.lower() in ("/cancel", "cancel", "отмена"):
            clear_admin_state(db, chat_id)
            send_admin_notices_menu(db, chat_id, "Создание своего сообщения отменено.")
            return True
        db.setdefault("adminState", {})["customNoticeText"] = text
        db["adminState"].pop(chat_id, None)
        save_db_sections(db, ("adminState",))
        preview_admin_notice(db, chat_id, "custom")
        return True
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command == "/admin":
        if is_owner_chat(db, chat_id):
            send_admin_menu(db, chat_id)
        else:
            send_client_menu(db, chat_id)
        return True
    if command in ("/start", "/help"):
        send_client_menu(db, chat_id)
        return True
    if command == "/status":
        send_client_menu(db, chat_id, subscription_status_for_chat(db, chat_id))
        return True
    if command == "/link":
        text, parse_mode = current_vless_link_code_for_chat(db, chat_id)
        send_client_menu(db, chat_id, text, parse_mode=parse_mode)
        return True
    if command in ("/unsubscribe", "/stop"):
        send_client_menu(db, chat_id, unsubscribe_chat(db, chat_id))
        return True
    if find_vless_link(text):
        name, entry = subscribe_chat_to_client(db, chat, text)
        send_client_menu(
            db,
            chat_id,
            "Подписка подключена.\n\n"
            + client_access_summary(entry)
            + "\n\nНапоминания придут в 08:00 за 5 дней и за 1 день до окончания доступа.",
        )
        return True
    send_client_menu(db, chat_id, "Я не нашёл VLESS-ссылку. Отправь свою ссылку целиком или нажми кнопку ниже.")
    return True


def handle_callback_query(db, update):
    callback = update.get("callback_query") or {}
    callback_id = callback.get("id", "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or "id" not in chat:
        answer_callback_query(db, callback_id)
        return False
    chat_id = str(chat["id"])
    data = str(callback.get("data") or "")

    if data.startswith("admin:"):
        if not is_owner_chat(db, chat_id):
            answer_callback_query(db, callback_id, "Админ-панель доступна только владельцу.", show_alert=True)
            send_client_menu(db, chat_id)
            return True
        answer_callback_query(db, callback_id)
        if data == "admin:menu":
            send_admin_menu(db, chat_id)
            return True
        if data == "admin:status":
            send_admin_menu(db, chat_id, admin_status_text(db))
            return True
        if data == "admin:subscribers":
            send_admin_menu(db, chat_id, admin_subscribers_text(db))
            return True
        if data == "admin:daily-summary":
            send_admin_menu(db, chat_id, build_daily_summary_message())
            return True
        if data == "admin:geoip":
            rc = notify_geoip(quiet=True)
            text = "GeoIP-проверка выполнена. Если были новые события, бот отправил отдельное уведомление."
            if rc != 0:
                text = f"GeoIP-проверка завершилась с ошибкой, exit {rc}."
            send_admin_menu(db, chat_id, text)
            return True
        if data == "admin:expiry":
            rc = notify_expiry(quiet=True)
            text = "Проверка напоминаний выполнена."
            if rc != 0:
                text = f"Проверка напоминаний завершилась с ошибкой, exit {rc}."
            send_admin_menu(db, chat_id, text)
            return True
        if data == "admin:test":
            send_admin_menu(db, chat_id, admin_run_server_test_text())
            return True
        if data == "admin:backup":
            send_admin_menu(db, chat_id, admin_create_backup_text())
            return True
        if data == "admin:notices":
            send_admin_notices_menu(db, chat_id)
            return True
        if data in ("admin:notice:start", "admin:notice:done"):
            preview_admin_notice(db, chat_id, data.rsplit(":", 1)[1])
            return True
        if data == "admin:notice:custom":
            set_custom_notice_waiting(db, chat_id)
            send_chat_message(
                db,
                chat_id,
                "Отправь следующим сообщением текст, который нужно разослать подписанным клиентам.\n\n"
                "Если передумаешь, отправь /cancel.",
                reply_markup={"inline_keyboard": [[{"text": "Отмена", "callback_data": "admin:notice-cancel"}]]},
            )
            return True
        if data.startswith("admin:notice-send:"):
            kind = data.rsplit(":", 1)[1]
            if kind not in ("start", "done", "custom"):
                send_admin_notices_menu(db, chat_id, "Неизвестный тип уведомления.")
                return True
            send_admin_notice(db, chat_id, kind)
            return True
        if data == "admin:notice-cancel":
            clear_admin_state(db, chat_id)
            send_admin_notices_menu(db, chat_id, "Рассылка отменена.")
            return True
        send_admin_menu(db, chat_id, "Неизвестная админская кнопка.")
        return True

    if data == "client:subscribe":
        answer_callback_query(db, callback_id)
        send_client_menu(db, chat_id, subscribe_prompt_text())
        return True
    if data == "client:status":
        answer_callback_query(db, callback_id)
        send_client_menu(db, chat_id, subscription_status_for_chat(db, chat_id))
        return True
    if data == "client:link":
        answer_callback_query(db, callback_id)
        text, parse_mode = current_vless_link_code_for_chat(db, chat_id)
        send_client_menu(db, chat_id, text, parse_mode=parse_mode)
        return True
    if data == "client:unsubscribe":
        answer_callback_query(db, callback_id, "Готово")
        send_client_menu(db, chat_id, unsubscribe_chat(db, chat_id))
        return True
    if data == "client:help":
        answer_callback_query(db, callback_id)
        send_client_menu(db, chat_id)
        return True

    answer_callback_query(db, callback_id, "Неизвестная кнопка")
    send_client_menu(db, chat_id)
    return True


def handle_telegram_update(db, update):
    if "callback_query" in update:
        return handle_callback_query(db, update)
    return handle_user_message(db, update)


def update_private_chat_id(update):
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") == "private" and chat.get("id"):
        return str(chat["id"])
    callback = update.get("callback_query") or {}
    callback_message = callback.get("message") or {}
    callback_chat = callback_message.get("chat") or {}
    if callback_chat.get("type") == "private" and callback_chat.get("id"):
        return str(callback_chat["id"])
    from_user = callback.get("from") or {}
    if from_user.get("id"):
        return str(from_user["id"])
    return ""


def user_poll_configured(db):
    return bool(db.get("enabled") and db.get("token") and db.get("chatId"))


def poll_user_subscriptions(quiet=False, telegram_timeout=USER_POLL_SHORT_TIMEOUT):
    db = load_db()
    if not db.get("enabled") or not db.get("token"):
        if not quiet:
            print("Telegram bot notifications are not configured or disabled.")
        return 0
    if not db.get("chatId"):
        if not quiet:
            print("Owner chat is not configured; user subscription polling skipped.")
        return 0
    state = db.setdefault("clientSubscriptionState", {})
    offset = int(state.get("userUpdateOffset", 0) or 0)
    payload = {"allowed_updates": ["message", "callback_query"], "timeout": max(0, int(telegram_timeout))}
    if offset:
        payload["offset"] = offset
    try:
        data = curl_json(db, "getUpdates", payload, timeout=max(15, int(telegram_timeout) + 10))
    except Exception as exc:
        if not quiet:
            print(f"ERROR: Telegram user polling failed: {exc}", file=sys.stderr)
            return 1
        return 0

    updates = data.get("result", [])
    processed = 0
    for update in updates:
        try:
            update_id = int(update.get("update_id", 0))
            state["userUpdateOffset"] = max(int(state.get("userUpdateOffset", 0) or 0), update_id + 1)
        except (TypeError, ValueError):
            pass
        try:
            if handle_telegram_update(db, update):
                processed += 1
        except Exception as exc:
            chat_id = update_private_chat_id(update)
            if chat_id:
                try:
                    send_client_menu(db, chat_id, "Не удалось обработать действие: " + str(exc))
                except Exception:
                    pass
            if not quiet:
                print(f"ERROR: failed to process Telegram user update: {exc}", file=sys.stderr)
    state["lastUserPoll"] = utc_stamp()
    save_db_sections(db, ("clientSubscriptions", "clientSubscriptionState"))
    if not quiet:
        print(f"Processed Telegram user messages: {processed}")
    return 0


def run_user_poller():
    print("Telegram user poller started.", flush=True)
    while True:
        try:
            db = load_db()
            if not user_poll_configured(db):
                print("Telegram user poller waits for enabled bot, token, and owner chat.", flush=True)
                time.sleep(USER_POLLER_SLEEP_UNCONFIGURED)
                continue
            rc = poll_user_subscriptions(quiet=True, telegram_timeout=USER_POLL_LONG_TIMEOUT)
            if rc != 0:
                time.sleep(USER_POLLER_SLEEP_ERROR)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"ERROR: Telegram user poller failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(USER_POLLER_SLEEP_ERROR)


def expiry_reminder_schedule(entry, days_before):
    expires_at = parse_time(entry.get("expiresAt", ""))
    if not expires_at:
        return None
    tzinfo, label = display_timezone()
    expiry_local = expires_at.astimezone(tzinfo)
    reminder_date = expiry_local.date() - timedelta(days=days_before)
    reminder_local = datetime(
        reminder_date.year,
        reminder_date.month,
        reminder_date.day,
        EXPIRY_REMINDER_HOUR,
        0,
        0,
        tzinfo=tzinfo,
    )
    return reminder_local, expiry_local, label


def expiry_reminder_due(entry, days_before, now=None):
    schedule = expiry_reminder_schedule(entry, days_before)
    if not schedule:
        return None
    reminder_local, expiry_local, label = schedule
    now_local = (now or utc_now()).astimezone(reminder_local.tzinfo)
    if now_local >= reminder_local and now_local < expiry_local:
        return reminder_local, expiry_local, label
    return None


def expiry_reminder_key(chat_id, name, entry, days_before):
    return "|".join(
        [
            str(chat_id),
            str(name),
            client_entry_id(entry),
            str(entry.get("expiresAt", "")),
            f"{days_before}d",
        ]
    )


def build_expiry_reminder_message(db, entry, days_before, expiry_local, timezone_label):
    return telegram_messages.build_expiry_reminder_message(
        db,
        entry,
        days_before,
        expiry_local,
        timezone_label,
        bot_name,
        payment_amount_label,
    )


def notify_expiry(quiet=False):
    db = load_db()
    if not db.get("enabled") or not db.get("token"):
        if not quiet:
            print("Telegram bot notifications are not configured or disabled.")
        return 0
    client_db = load_client_db()
    clients = client_db_clients(client_db)
    subscriptions = db.setdefault("clientSubscriptions", {})
    state = db.setdefault("clientSubscriptionState", {})
    sent = state.setdefault("expiryReminders", {})
    now = utc_now()
    sent_count = 0

    for chat_id, subscription in list(subscriptions.items()):
        if not isinstance(subscription, dict) or subscription.get("enabled") is False:
            continue
        name = subscription.get("client", "")
        entry = clients.get(name)
        if not isinstance(entry, dict):
            continue
        if entry.get("paymentType") != "paid":
            continue
        if subscription.get("clientId") and normalize_value(subscription.get("clientId")) != client_entry_id(entry):
            continue
        for days_before in EXPIRY_REMINDER_DAYS:
            due = expiry_reminder_due(entry, days_before, now)
            if not due:
                continue
            key = expiry_reminder_key(chat_id, name, entry, days_before)
            if key in sent:
                continue
            _reminder_local, expiry_local, timezone_label = due
            message = build_expiry_reminder_message(db, entry, days_before, expiry_local, timezone_label)
            try:
                send_chat_message(db, chat_id, message)
            except Exception as exc:
                if not quiet:
                    print(f"ERROR: expiry reminder failed for {name}/{chat_id}: {exc}", file=sys.stderr)
                continue
            sent[key] = utc_stamp()
            sent_count += 1

    if len(sent) > 2000:
        sent_items = sorted(sent.items(), key=lambda item: item[1])[-2000:]
        state["expiryReminders"] = dict(sent_items)
    state["lastExpiryReminderCheck"] = utc_stamp()
    if sent_count:
        state["lastExpiryReminder"] = utc_stamp()
    save_db_sections(db, ("clientSubscriptionState",))
    if not quiet:
        print(f"Expiry reminders sent: {sent_count}")
    return 0


def build_access_updated_message(db, entry):
    return telegram_messages.build_access_updated_message(db, entry, bot_name, format_access_until)


def notify_access_updated(name, quiet=False):
    db = load_db()
    if not db.get("enabled") or not db.get("token"):
        if not quiet:
            print("Telegram bot notifications are not configured or disabled.")
        return 0
    client_db = load_client_db()
    entry = client_db_clients(client_db).get(name)
    if not isinstance(entry, dict):
        if not quiet:
            print(f"Client not found: {name}")
        return 1
    if entry.get("paymentType") != "paid":
        if not quiet:
            print(f"Client is free, access payment notification skipped: {name}")
        return 0
    message = build_access_updated_message(db, entry)
    sent_count = 0
    for chat_id, subscription in db.get("clientSubscriptions", {}).items():
        if not isinstance(subscription, dict) or subscription.get("enabled") is False:
            continue
        if subscription.get("client") != name:
            continue
        if subscription.get("clientId") and normalize_value(subscription.get("clientId")) != client_entry_id(entry):
            continue
        try:
            send_chat_message(db, chat_id, message)
            sent_count += 1
        except Exception as exc:
            if not quiet:
                print(f"ERROR: access update notification failed for {name}/{chat_id}: {exc}", file=sys.stderr)
    if not quiet:
        print(f"Access update notifications sent: {sent_count}")
    return 0


def maintenance_notice_message(db, template_id):
    return telegram_messages.maintenance_notice_message(db, template_id, bot_name)


def normalize_maintenance_template_id(value):
    return telegram_messages.normalize_maintenance_template_id(value)


def maintenance_notice_recipients(db):
    recipients = []
    seen = set()
    for chat_id, subscription in db.get("clientSubscriptions", {}).items():
        if not isinstance(subscription, dict) or subscription.get("enabled") is False:
            continue
        chat_id = str(chat_id or "").strip()
        if not chat_id or chat_id in seen:
            continue
        recipients.append((chat_id, subscription))
        seen.add(chat_id)
    return recipients


def print_maintenance_notice_templates(db):
    print("Доступные уведомления о технических работах:")
    print()
    for index, (key, template) in enumerate(MAINTENANCE_NOTICE_TEMPLATES.items(), start=1):
        print(f"{index}) {template['title']} ({key})")
        print(maintenance_notice_message(db, key))
        print()


def send_notice_message(db, message, dry_run=False, yes=False, label="message"):
    recipients = maintenance_notice_recipients(db)
    if dry_run or not yes:
        print(f"Notice: {label}")
        print(f"Recipients: {len(recipients)}")
        print()
        print(message)
        if not dry_run:
            print()
            print("To send, repeat command with --yes.")
        return 0
    sent_count = 0
    failed_count = 0
    for chat_id, _subscription in recipients:
        try:
            send_chat_message(db, chat_id, message)
            sent_count += 1
        except Exception as exc:
            failed_count += 1
            print(f"ERROR: maintenance notice failed for {chat_id}: {exc}", file=sys.stderr)
    print(f"Maintenance notice sent: {sent_count}")
    if failed_count:
        print(f"Maintenance notice failed: {failed_count}")
        return 1
    return 0


def send_maintenance_notice(template_id="start", dry_run=False, yes=False):
    db = load_db()
    if not db.get("enabled") or not db.get("token"):
        print("Telegram bot notifications are not configured or disabled.")
        return 0
    template_id = normalize_maintenance_template_id(template_id)
    message = maintenance_notice_message(db, template_id)
    template = MAINTENANCE_NOTICE_TEMPLATES[str(template_id)]
    return send_notice_message(db, message, dry_run=dry_run, yes=yes, label=template["title"])


def preview_admin_notice(db, chat_id, kind):
    if kind == "custom":
        message = str(db.get("adminState", {}).get("customNoticeText") or "").strip()
        if not message:
            send_admin_notices_menu(db, chat_id, "Черновик своего сообщения пуст. Нажми «Своё сообщение» и отправь текст заново.")
            return
        title = "своё сообщение"
    else:
        message = maintenance_notice_message(db, kind)
        title = MAINTENANCE_NOTICE_TEMPLATES[kind]["title"]
    recipients = len(maintenance_notice_recipients(db))
    text = "\n".join(
        [
            f"Предпросмотр: {title}",
            f"Получателей: {recipients}",
            "",
            message,
        ]
    )
    send_chat_message(db, chat_id, text, reply_markup=admin_notice_confirm_keyboard(kind))


def set_custom_notice_waiting(db, chat_id):
    db.setdefault("adminState", {})[str(chat_id)] = {"action": "custom-notice-text", "startedAt": utc_stamp()}
    save_db_sections(db, ("adminState",))


def clear_admin_state(db, chat_id):
    state = db.setdefault("adminState", {})
    state.pop(str(chat_id), None)
    state.pop("customNoticeText", None)
    save_db_sections(db, ("adminState",))


def send_admin_notice(db, chat_id, kind):
    if kind == "custom":
        message = str(db.get("adminState", {}).get("customNoticeText") or "").strip()
        if not message:
            send_admin_notices_menu(db, chat_id, "Черновик своего сообщения пуст. Отправка отменена.")
            return
        label = "своё сообщение"
    else:
        message = maintenance_notice_message(db, kind)
        label = MAINTENANCE_NOTICE_TEMPLATES[kind]["title"]
    rc = send_notice_message(db, message, yes=True, label=label)
    clear_admin_state(db, chat_id)
    if rc == 0:
        send_admin_notices_menu(db, chat_id, "Уведомление отправлено подписанным клиентам.")
    else:
        send_admin_notices_menu(db, chat_id, "Уведомление отправлено не всем. Проверь логи сервера.")


def list_client_subscribers():
    db = load_db()
    client_db = load_client_db()
    clients = client_db_clients(client_db)
    subscriptions = db.get("clientSubscriptions", {})
    if not subscriptions:
        print("No client Telegram subscriptions.")
        return
    rows = []
    for chat_id, subscription in sorted(subscriptions.items(), key=lambda item: item[1].get("client", "")):
        name = subscription.get("client", "-")
        entry = clients.get(name, {})
        valid = "yes" if entry and normalize_value(subscription.get("clientId")) == client_entry_id(entry) else "no"
        rows.append(
            [
                name,
                subscription.get("chatLabel", "-"),
                chat_id,
                format_access_until(entry.get("expiresAt", "") if isinstance(entry, dict) else ""),
                valid,
                subscription.get("subscribedAt", "-"),
            ]
        )
    headers = ["CLIENT", "CHAT", "CHAT_ID", "ACCESS_UNTIL", "VALID", "SUBSCRIBED_AT"]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    print(border)
    print("| " + " | ".join(headers[index].ljust(widths[index]) for index in range(len(headers))) + " |")
    print(border)
    for row in rows:
        print("| " + " | ".join(str(row[index]).ljust(widths[index]) for index in range(len(row))) + " |")
    print(border)


def load_exceptions():
    data = load_json(ACTIVITY_EXCEPTIONS_PATH, {"items": []})
    items = data.get("items", [])
    return items if isinstance(items, list) else []


def normalize_host(host):
    value = str(host or "").strip().strip("[]").lower()
    if not value:
        return ""
    if "/" not in value and value.count(":") == 1:
        candidate, port = value.rsplit(":", 1)
        if port.isdigit():
            value = candidate.strip("[]")
    return value


def exception_matches(item, host):
    value = normalize_host(item.get("value", ""))
    kind = item.get("kind", "")
    host_value = normalize_host(host)
    if not value or not host_value:
        return False
    if kind == "cidr":
        try:
            return ipaddress.ip_address(host_value) in ipaddress.ip_network(value, strict=False)
        except ValueError:
            return False
    if kind == "ip":
        return host_value == value
    if kind == "mask":
        return fnmatch.fnmatchcase(host_value, value)
    return host_value == value


def is_exception(event, exceptions):
    return any(exception_matches(item, event.get("host", "")) for item in exceptions)


def geoip_regions(event):
    risks = set(event.get("risks") or [])
    outbound = str(event.get("outbound") or "").lower()
    if outbound.startswith(XRAY_GEOIP_OUTBOUND_PREFIX):
        code = outbound[len(XRAY_GEOIP_OUTBOUND_PREFIX):].upper()
        if code:
            risks.add(f"xray-geoip:{code}")
    regions = []
    for risk in sorted(risks):
        if str(risk).startswith("xray-geoip:"):
            regions.append(str(risk).split(":", 1)[1].upper())
    return regions


def event_id(event):
    payload = "|".join(
        str(event.get(key, ""))
        for key in ("time", "client", "host", "port", "outbound", "source", "target")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def iter_new_events(db):
    state = db.setdefault("geoipState", {})
    files = state.setdefault("files", {})
    if not CLIENT_LOG_DIR.exists():
        return []
    events = []
    for path in sorted(CLIENT_LOG_DIR.glob("*.jsonl")):
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            continue
        item = files.get(key, {})
        offset = int(item.get("offset", 0) or 0)
        if item.get("inode") != stat.st_ino or stat.st_size < offset:
            offset = 0
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
                files[key] = {"inode": stat.st_ino, "offset": handle.tell()}
        except OSError:
            continue
        for line in data.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(event)
    state["updated"] = utc_stamp()
    return events


def build_geoip_message(events):
    grouped = {}
    for event in events:
        regions = ",".join(geoip_regions(event)) or "-"
        host = event.get("host") or "-"
        port = str(event.get("port") or "-")
        key = (event.get("client") or "-", regions, host, port)
        row = grouped.setdefault(
            key,
            {
                "client": event.get("client") or "-",
                "regions": regions,
                "host": host,
                "port": port,
                "count": 0,
                "last": event.get("time") or "",
                "outbound": event.get("outbound") or "-",
            },
        )
        row["count"] += 1
        if event.get("time", "") >= row["last"]:
            row["last"] = event.get("time") or row["last"]
            row["outbound"] = event.get("outbound") or row["outbound"]

    rows = sorted(grouped.values(), key=lambda item: (item["count"], item["last"]), reverse=True)
    lines = [
        "Xray VPS Manager: обнаружено подключение по GeoIP",
        f"Новых событий: {len(events)}",
        "",
    ]
    for row in rows[:10]:
        lines.extend(
            [
                f"Клиент: {row['client']}",
                f"Регион: {row['regions']}",
                f"Цель: {row['host']}:{row['port']}",
                f"Событий: {row['count']}",
                f"Последнее: {format_event_time(row['last'])}",
                f"Outbound: {row['outbound']}",
                "",
            ]
        )
    if len(rows) > 10:
        lines.append(f"И ещё целей: {len(rows) - 10}")
    return "\n".join(lines).strip()


def notify_geoip(quiet=False):
    db = load_db()
    if not db.get("enabled") or not db.get("token") or not db.get("chatId"):
        if not quiet:
            print("Telegram bot notifications are not configured or disabled.")
        return 0

    state = db.setdefault("geoipState", {})
    sent_ids = list(state.get("sentIds", []))[-500:]
    sent_set = set(sent_ids)
    exceptions = load_exceptions()
    candidates = []
    for event in iter_new_events(db):
        if not geoip_regions(event):
            continue
        if is_exception(event, exceptions):
            continue
        item_id = event_id(event)
        if item_id in sent_set:
            continue
        candidates.append(event)
        sent_ids.append(item_id)
        sent_set.add(item_id)

    if not candidates:
        state["sentIds"] = sent_ids[-500:]
        save_db_sections(db, ("geoipState",))
        if not quiet:
            print("No new GeoIP events for Telegram notification.")
        return 0

    message = build_geoip_message(candidates)
    try:
        send_message(db, message)
    except Exception as exc:
        if not quiet:
            print(f"ERROR: Telegram notification failed: {exc}", file=sys.stderr)
            return 1
        return 0
    state["sentIds"] = sent_ids[-500:]
    state["lastGeoipNotification"] = utc_stamp()
    save_db_sections(db, ("geoipState",))
    if not quiet:
        print(f"Telegram GeoIP notification sent: {len(candidates)} events.")
    return 0


def status():
    db = load_db()
    subscriptions = db.get("clientSubscriptions", {})
    subscription_state = db.get("clientSubscriptionState", {})
    rows = [
        ("Enabled", "yes" if db.get("enabled") else "no"),
        ("Token", mask_token(db.get("token", ""))),
        ("Bot name", bot_name(db)),
        ("Chat", f"{db.get('chatLabel') or '-'} ({db.get('chatId') or '-'})"),
        ("Route mode", db.get("routeMode", "direct")),
        ("Payment amount", payment_amount_label(db)),
        ("Payment rounding", payment_rounding_label(db)),
        ("Client subscriptions", str(len(subscriptions))),
        ("Config", str(TELEGRAM_DB_PATH)),
        ("Last GeoIP notification", db.get("geoipState", {}).get("lastGeoipNotification") or db.get("lastGeoipNotification") or "never"),
        ("Last user poll", subscription_state.get("lastUserPoll") or "never"),
        ("Last expiry reminder", subscription_state.get("lastExpiryReminder") or "never"),
    ]
    width = max(len(key) for key, _value in rows)
    for key, value in rows:
        print(f"{key.ljust(width)} : {value}")


def usage():
    print(
        """Usage:
  xray-telegram status
  xray-telegram setup
  xray-telegram owner
  xray-telegram enable
  xray-telegram disable
  xray-telegram mode direct|cascade
  xray-telegram bot-name [NAME]
  xray-telegram test
  xray-telegram commands
  xray-telegram notify-geoip [--quiet]
  xray-telegram poll-users [--quiet]
  xray-telegram run-poller
  xray-telegram daily-summary [--dry-run]
  xray-telegram notify-daily-summary [--quiet|--dry-run]
  xray-telegram notify-expiry [--quiet]
  xray-telegram notify-access NAME [--quiet]
  xray-telegram maintenance-notice [start|done] [--dry-run|--yes]
  xray-telegram subscribers
  xray-telegram payment-amount [VALUE]
  xray-telegram payment-rounding [none|step VALUE]
"""
    )


def main():
    require_root()
    args = sys.argv[1:]
    command = args[0] if args else "status"
    try:
        if command == "status" and len(args) in (0, 1):
            status()
        elif command == "setup" and len(args) == 1:
            setup()
        elif command in ("owner", "chat", "finish-setup") and len(args) == 1:
            configure_owner(send_test=True)
        elif command == "enable" and len(args) == 1:
            set_enabled(True)
        elif command == "disable" and len(args) == 1:
            set_enabled(False)
        elif command == "mode" and len(args) == 2:
            set_route_mode(args[1])
        elif command == "bot-name" and len(args) in (1, 2):
            if len(args) == 1:
                print(f"Bot name: {bot_name(load_db())}")
            else:
                set_bot_name(args[1])
        elif command == "test" and len(args) == 1:
            test_message()
        elif command in ("commands", "set-commands") and len(args) == 1:
            configure_bot_commands()
        elif command == "notify-geoip" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--quiet":
                usage()
                sys.exit(1)
            sys.exit(notify_geoip(quiet=len(args) == 2))
        elif command == "poll-users" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--quiet":
                usage()
                sys.exit(1)
            sys.exit(poll_user_subscriptions(quiet=len(args) == 2))
        elif command in ("run-poller", "poll-daemon") and len(args) == 1:
            run_user_poller()
        elif command == "daily-summary" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--dry-run":
                usage()
                sys.exit(1)
            print(build_daily_summary_message())
        elif command == "notify-daily-summary" and len(args) in (1, 2):
            if len(args) == 2 and args[1] not in ("--quiet", "--dry-run"):
                usage()
                sys.exit(1)
            sys.exit(notify_daily_summary(quiet=len(args) == 2 and args[1] == "--quiet", dry_run=len(args) == 2 and args[1] == "--dry-run"))
        elif command == "notify-expiry" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--quiet":
                usage()
                sys.exit(1)
            sys.exit(notify_expiry(quiet=len(args) == 2))
        elif command == "notify-access" and len(args) in (2, 3):
            if len(args) == 3 and args[2] != "--quiet":
                usage()
                sys.exit(1)
            sys.exit(notify_access_updated(args[1], quiet=len(args) == 3))
        elif command == "maintenance-notice" and len(args) in (1, 2, 3):
            if len(args) == 1 or (len(args) == 2 and args[1] in ("list", "templates")):
                print_maintenance_notice_templates(load_db())
            else:
                template_id = args[1]
                flag = args[2] if len(args) == 3 else ""
                if flag and flag not in ("--dry-run", "--yes"):
                    usage()
                    sys.exit(1)
                sys.exit(send_maintenance_notice(template_id, dry_run=flag == "--dry-run", yes=flag == "--yes"))
        elif command in ("subscribers", "subscriptions") and len(args) == 1:
            list_client_subscribers()
        elif command == "payment-amount" and len(args) in (1, 2):
            if len(args) == 1:
                show_payment_amount()
            else:
                set_payment_amount(args[1])
        elif command == "payment-rounding" and len(args) in (1, 2, 3):
            if len(args) == 1:
                show_payment_amount()
            elif args[1] == "step":
                if len(args) != 3:
                    raise ValueError("Для режима step нужно указать шаг округления.")
                set_payment_rounding(args[1], args[2])
            elif len(args) == 2:
                set_payment_rounding(args[1])
            else:
                usage()
                sys.exit(1)
        else:
            usage()
            sys.exit(1)
    except Exception as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
