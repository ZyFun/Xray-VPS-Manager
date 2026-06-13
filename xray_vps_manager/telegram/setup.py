"""Telegram bot setup and route configuration."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone

from xray_vps_manager.core.paths import CLIENT_LOG_DIR, CONFIG_PATH, XRAY_BIN
from xray_vps_manager.core.process import run_capture
from xray_vps_manager.telegram import api, poller, settings

CASCADE_UPSTREAM_TAG = "cascade-upstream"
TELEGRAM_SOCKS_TAG = "telegram-bot-socks"
TELEGRAM_SOCKS_HOST = api.TELEGRAM_SOCKS_HOST
TELEGRAM_SOCKS_PORT = api.TELEGRAM_SOCKS_PORT


def utc_stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text())


def save_config(config):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    backup = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.bak.telegram.{timestamp}")
    shutil.copy2(CONFIG_PATH, backup)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    settings.chown_xray(tmp)
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
        subprocess.run([str(XRAY_BIN), "run", "-test", "-config", str(CONFIG_PATH)], check=True, text=True)
        restart_xray()
    except subprocess.CalledProcessError as exc:
        shutil.copy2(backup, CONFIG_PATH)
        settings.chown_xray(CONFIG_PATH)
        os.chmod(CONFIG_PATH, 0o640)
        restart_xray()
        raise RuntimeError(f"New config failed. Restored backup: {backup}") from exc
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
        raise RuntimeError("Cascade outbound is not configured. Add cascade first or use direct Telegram route mode.")

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
        raise ValueError("Route mode must be direct or cascade.")
    db = settings.load_db_sql()
    previous_mode = db.get("routeMode", "direct")
    config = load_config()
    if mode == "cascade":
        db["routeMode"] = mode
        settings.save_db(db)
        ensure_telegram_proxy_config(config)
        try:
            backup = apply_config(config)
        except Exception:
            db["routeMode"] = previous_mode
            settings.save_db(db)
            raise
        if not wait_for_tcp(TELEGRAM_SOCKS_HOST, TELEGRAM_SOCKS_PORT):
            raise RuntimeError(f"Telegram SOCKS inbound did not open: {TELEGRAM_SOCKS_HOST}:{TELEGRAM_SOCKS_PORT}")
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
    settings.save_db(db)


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
        rows.append({"id": chat_id, "label": poller.chat_label(chat)})
    return rows


def choose_private_chat(db):
    print("Теперь отправь любое сообщение новому боту в Telegram, например /start.")
    print("После этого нажми Enter здесь, чтобы сервер увидел твой chat_id.")
    while True:
        input("Enter после отправки /start боту: ")
        data = api.curl_json(db, "getUpdates", {"allowed_updates": ["message"], "timeout": 2}, timeout=15)
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


def refresh_user_update_offset(db):
    try:
        data = api.curl_json(db, "getUpdates", {"allowed_updates": ["message", "callback_query"], "timeout": 1}, timeout=10)
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
        raise ValueError("BOT_TOKEN выглядит некорректно.")
    db["token"] = token
    settings.save_db(db)
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
        settings.save_db(db)
    return db


def configure_bot_commands():
    db = settings.load_db_sql()
    if not db.get("token"):
        raise ValueError("Telegram bot token is not configured.")
    commands = [
        {"command": "start", "description": "Открыть меню"},
        {"command": "status", "description": "Показать подписку"},
        {"command": "link", "description": "Получить VLESS-ссылку"},
        {"command": "traffic", "description": "Показать статистику трафика"},
        {"command": "unsubscribe", "description": "Отключить напоминания"},
        {"command": "help", "description": "Помощь"},
    ]
    api.curl_json(db, "setMyCommands", {"commands": commands}, timeout=30)
    print("Telegram command menu updated.")


def configure_owner(send_test=True):
    db = ask_token_if_missing(settings.load_db_sql())
    db = maybe_adopt_existing_cascade_route(db)
    me = api.curl_json(db, "getMe")
    bot = me.get("result", {})
    print(f"Бот найден: @{bot.get('username', 'unknown')}")
    chat = choose_private_chat(db)
    db["chatId"] = chat["id"]
    db["chatLabel"] = chat["label"]
    db["enabled"] = True
    initialize_geoip_offsets(db)
    refresh_user_update_offset(db)
    settings.save_db(db)
    configure_bot_commands()
    if send_test:
        api.send_message(db, "Xray VPS Manager: Telegram-уведомления подключены. GeoIP-уведомления будут отправляться только в этот чат.")
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
        raise ValueError("BOT_TOKEN выглядит некорректно.")
    db = settings.load_db_sql()
    db["token"] = token

    print()
    print("BOT_NAME: имя бота в сообщениях пользователям.")
    print("Например: Vireika. Если нажать Enter, останется текущее значение.")
    current_bot_name = settings.bot_name(db)
    value = input(f"BOT_NAME [{current_bot_name}]: ").strip()
    db["botName"] = settings.normalize_display_name(value or current_bot_name, settings.DEFAULT_BOT_NAME, "BOT_NAME")

    print()
    print("Как боту выходить в интернет?")
    print("1. direct: напрямую с этого сервера")
    print("2. cascade: через исходящий сервер, настроенный как cascade-upstream")
    mode_choice = input("Route mode [1-direct]: ").strip() or "1"
    mode = "cascade" if mode_choice == "2" else "direct"
    settings.save_db(db)
    if mode == "cascade":
        print()
        print("Сейчас будет применён cascade-маршрут для Telegram Bot API.")
        print("Если SSH-сессия оборвётся на перезапуске Xray, подключись заново и запусти:")
        print("  xray-telegram owner")
        print("или в меню: Telegram бот -> Донастроить владельца/чат")
    set_route_mode(mode)
    configure_owner(send_test=True)


def set_enabled(value):
    db = settings.load_db_sql()
    db["enabled"] = bool(value)
    if value:
        initialize_geoip_offsets(db)
    settings.save_db(db)
    print("Telegram notifications enabled." if value else "Telegram notifications disabled.")


def set_bot_name(value):
    db = settings.load_db_sql()
    db["botName"] = settings.normalize_display_name(value, settings.DEFAULT_BOT_NAME, "BOT_NAME")
    settings.save_db(db)
    print(f"Bot name: {db['botName']}")


def test_message():
    db = settings.load_db_sql()
    api.send_message(db, "Xray VPS Manager: тестовое сообщение Telegram-бота.")
    print("Test message sent.")
