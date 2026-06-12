#!/usr/bin/env python3
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.core.server_env import read_server_env
from xray_vps_manager.traffic import repository as traffic_repository
from xray_vps_manager.telegram import admin as telegram_admin
from xray_vps_manager.telegram import api as telegram_api
from xray_vps_manager.telegram import notifications as telegram_notifications
from xray_vps_manager.telegram import payments as telegram_payments
from xray_vps_manager.telegram import poller as telegram_poller
from xray_vps_manager.telegram import settings as telegram_settings
from xray_vps_manager.telegram import subscriptions as telegram_subscriptions

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
CLIENT_DB_PATH = Path("/usr/local/etc/xray/clients.json")
SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
TRAFFIC_PATH = Path("/usr/local/etc/xray/traffic.json")
TELEGRAM_DB_PATH = Path("/usr/local/etc/xray/telegram-bot.json")
ACTIVITY_DIR = Path("/usr/local/etc/xray/activity")
CLIENT_LOG_DIR = ACTIVITY_DIR / "clients"
XRAY_BIN = Path("/usr/local/bin/xray")
XRAY_CLIENT = Path("/usr/local/sbin/xray-client")
CASCADE_UPSTREAM_TAG = "cascade-upstream"
TELEGRAM_SOCKS_TAG = "telegram-bot-socks"
TELEGRAM_SOCKS_HOST = "127.0.0.1"
TELEGRAM_SOCKS_PORT = 10810
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
DEFAULT_SERVER_NAME = "Xray"
DEFAULT_BOT_NAME = "Vireika"
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
    return read_server_env(SERVER_ENV_PATH)


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
    return telegram_poller.chat_label(chat)


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


def notification_context():
    return telegram_notifications.NotificationContext(
        load_db=load_db,
        save_db_sections=save_db_sections,
        load_client_db=load_client_db,
        load_traffic_db=load_traffic_db,
        display_timezone=display_timezone,
        format_event_time=format_event_time,
        format_access_until=format_access_until,
        parse_time=parse_time,
        utc_now=utc_now,
        utc_stamp=utc_stamp,
        run_capture=run_capture,
        send_chat_message=send_chat_message,
        send_message=send_message,
        bot_name=bot_name,
        client_log_dir=CLIENT_LOG_DIR,
    )


def admin_context():
    return telegram_admin.AdminContext(
        load_client_db=load_client_db,
        save_db_sections=save_db_sections,
        format_access_until=format_access_until,
        run_capture=run_capture,
        send_chat_message=send_chat_message,
        bot_name=bot_name,
        notification_context=notification_context(),
    )


def poller_context():
    return telegram_poller.PollerContext(
        load_db=load_db,
        save_db_sections=save_db_sections,
        load_client_db=load_client_db,
        format_access_until=format_access_until,
        run_capture=run_capture,
        send_chat_message=send_chat_message,
        answer_callback_query=answer_callback_query,
        curl_json=curl_json,
        bot_name=bot_name,
        server_name_fragment=server_name_fragment,
        utc_stamp=utc_stamp,
        admin_context=admin_context(),
        xray_client=XRAY_CLIENT,
    )


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


def format_access_until(value):
    if not value:
        return "бессрочно"
    moment = parse_time(value)
    if not moment:
        return value
    tzinfo, label = display_timezone()
    return f"{moment.astimezone(tzinfo).strftime('%Y-%m-%d %H:%M')} {label}"


def payment_amount_label(db, client_db=None):
    return telegram_payments.payment_amount_label(db, client_db or load_client_db())


def print_payment_summary(db, client_db=None):
    summary = telegram_payments.payment_summary(db, client_db or load_client_db())
    print(f"Total rent amount: {summary['total']}")
    print(f"Paid clients: {summary['paidCount']}")
    print(f"Rounding: {summary['rounding']}")
    print(f"Amount per paid client: {summary['share']}")
    if summary.get("warning"):
        print(f"WARN: {summary['warning']}")


def set_payment_amount(value):
    db = load_db()
    amount, _currency = telegram_payments.apply_payment_amount(db, value)
    save_db(db)
    if amount:
        print_payment_summary(db)
    else:
        print("Payment amount cleared.")


def set_payment_rounding(mode_value, step_value=None):
    db = load_db()
    telegram_payments.apply_payment_rounding(db, mode_value, step_value)
    save_db(db)
    print_payment_summary(db)


def show_payment_amount():
    db = load_db()
    print_payment_summary(db)


def build_daily_summary_message(target_day=None):
    return telegram_notifications.build_daily_summary_message(notification_context(), target_day)


def notify_daily_summary(quiet=False, dry_run=False):
    return telegram_notifications.notify_daily_summary(notification_context(), quiet=quiet, dry_run=dry_run)


def poll_user_subscriptions(quiet=False, telegram_timeout=telegram_poller.USER_POLL_SHORT_TIMEOUT):
    return telegram_poller.poll_user_subscriptions(poller_context(), quiet=quiet, telegram_timeout=telegram_timeout)


def run_user_poller():
    telegram_poller.run_user_poller(poller_context())


def notify_expiry(quiet=False):
    return telegram_notifications.notify_expiry(notification_context(), quiet=quiet)


def notify_access_updated(name, quiet=False):
    return telegram_notifications.notify_access_updated(notification_context(), name, quiet=quiet)


def print_maintenance_notice_templates(db):
    telegram_notifications.print_maintenance_notice_templates(notification_context(), db)


def send_maintenance_notice(template_id="start", dry_run=False, yes=False):
    return telegram_notifications.send_maintenance_notice(
        notification_context(),
        template_id,
        dry_run=dry_run,
        yes=yes,
    )


def list_client_subscribers():
    db = load_db()
    client_db = load_client_db()
    clients = telegram_subscriptions.client_db_clients(client_db)
    subscriptions = db.get("clientSubscriptions", {})
    if not subscriptions:
        print("No client Telegram subscriptions.")
        return
    rows = []
    for chat_id, subscription in sorted(subscriptions.items(), key=lambda item: item[1].get("client", "")):
        name = subscription.get("client", "-")
        entry = clients.get(name, {})
        valid = "yes" if telegram_subscriptions.subscription_is_current(subscription, entry) else "no"
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


def notify_geoip(quiet=False):
    return telegram_notifications.notify_geoip(notification_context(), quiet=quiet)


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
        ("Payment rounding", telegram_payments.payment_rounding_label(db)),
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
