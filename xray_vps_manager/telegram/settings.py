"""Telegram bot settings stored in telegram-bot.json."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import os
import shutil
from pathlib import Path

from xray_vps_manager.core.paths import TELEGRAM_DB_PATH
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.repositories import telegram as sqlite_telegram
from xray_vps_manager.db.storage import sqlite_read_ready, sqlite_reads_enabled, truthy
from xray_vps_manager.telegram.payments import parse_payment_rounding_step, parse_payment_value

DEFAULT_BOT_NAME = "Vireika"

DEFAULT_DB = {
    "version": 1,
    "enabled": False,
    "token": "",
    "botName": DEFAULT_BOT_NAME,
    "chatId": "",
    "chatLabel": "",
    "routeMode": "direct",
    "paymentAmount": "",
    "paymentTotalAmount": "",
    "paymentCurrency": "₽",
    "paymentRoundingMode": "none",
    "paymentRoundingStep": "10",
    "geoipState": {"files": {}, "sentIds": []},
    "clientSubscriptions": {},
    "clientSubscriptionState": {"userUpdateOffset": 0, "expiryReminders": {}},
    "dailySummaryState": {},
    "adminState": {},
}


@dataclass(frozen=True)
class TelegramDbReadResult:
    db: dict
    source: str


def load_json(path, default):
    if not path.exists():
        return copy.deepcopy(default)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return copy.deepcopy(default)
    return data if isinstance(data, dict) else copy.deepcopy(default)


def chown_xray(path):
    try:
        shutil.chown(path, user="root", group="xray")
    except LookupError:
        shutil.chown(path, user="root")


def ensure_config_dir(path=TELEGRAM_DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    chown_xray(path.parent)
    os.chmod(path.parent, 0o750)


def normalize_db(db):
    merged = copy.deepcopy(DEFAULT_DB)
    merged.update(db)

    geoip_state = merged.get("geoipState")
    if not isinstance(geoip_state, dict):
        geoip_state = {}
    geoip_state.setdefault("files", {})
    geoip_state.setdefault("sentIds", [])
    merged["geoipState"] = geoip_state

    subscriptions = merged.get("clientSubscriptions")
    if not isinstance(subscriptions, dict):
        subscriptions = {}
    merged["clientSubscriptions"] = subscriptions

    subscription_state = merged.get("clientSubscriptionState")
    if not isinstance(subscription_state, dict):
        subscription_state = {}
    try:
        subscription_state["userUpdateOffset"] = int(subscription_state.get("userUpdateOffset", 0) or 0)
    except (TypeError, ValueError):
        subscription_state["userUpdateOffset"] = 0
    reminders = subscription_state.get("expiryReminders")
    if not isinstance(reminders, dict):
        reminders = {}
    subscription_state["expiryReminders"] = reminders
    merged["clientSubscriptionState"] = subscription_state

    daily_summary_state = merged.get("dailySummaryState")
    if not isinstance(daily_summary_state, dict):
        daily_summary_state = {}
    merged["dailySummaryState"] = daily_summary_state

    admin_state = merged.get("adminState")
    if not isinstance(admin_state, dict):
        admin_state = {}
    merged["adminState"] = admin_state

    if not str(merged.get("paymentTotalAmount", "")).strip() and str(merged.get("paymentAmount", "")).strip():
        try:
            amount, currency = parse_payment_value(str(merged.get("paymentAmount", "")))
            merged["paymentTotalAmount"] = amount
            merged["paymentCurrency"] = currency
        except ValueError:
            pass
    if merged.get("paymentRoundingMode") not in ("none", "step"):
        merged["paymentRoundingMode"] = "none"
    try:
        merged["paymentRoundingStep"] = parse_payment_rounding_step(merged.get("paymentRoundingStep", "10"))
    except ValueError:
        merged["paymentRoundingStep"] = "10"
    if merged.get("routeMode") not in ("direct", "cascade"):
        merged["routeMode"] = "direct"
    return merged


def load_db(path=TELEGRAM_DB_PATH):
    return normalize_db(load_json(path, DEFAULT_DB))


def load_db_for_read(path=TELEGRAM_DB_PATH, *, db_path: str | Path | None = None):
    return load_db_for_read_result(path, db_path=db_path).db


def load_db_for_read_result(path=TELEGRAM_DB_PATH, *, db_path: str | Path | None = None) -> TelegramDbReadResult:
    if sqlite_reads_enabled() and database.database_file_exists(db_path):
        try:
            connection = database.open_database(db_path)
            try:
                if not sqlite_read_ready(connection):
                    return TelegramDbReadResult(load_db(path), "json")
                return TelegramDbReadResult(load_db_from_sqlite(connection), "sqlite")
            finally:
                connection.close()
        except Exception:
            pass
    return TelegramDbReadResult(load_db(path), "json")


def load_db_from_sqlite(connection) -> dict:
    db = copy.deepcopy(DEFAULT_DB)
    for key, value in sqlite_telegram.list_settings(connection).items():
        if key == "version":
            try:
                db[key] = int(value)
            except (TypeError, ValueError):
                db[key] = DEFAULT_DB["version"]
        elif key == "enabled":
            db[key] = truthy(value)
        else:
            db[key] = value

    for key, value in sqlite_settings.list_payment_settings(connection).items():
        db[key] = value

    for key in ("geoipState", "clientSubscriptionState", "dailySummaryState", "adminState"):
        db[key] = sqlite_telegram.get_state(connection, key, db.get(key, {}))

    subscriptions = {}
    for item in sqlite_telegram.list_subscriptions(connection):
        chat_id = str(item.get("chatId") or "").strip()
        if not chat_id:
            continue
        link_signature = item.get("linkSignature") if isinstance(item.get("linkSignature"), dict) else {}
        subscriptions[chat_id] = {
            "client": item.get("clientName") or "",
            "clientId": item.get("clientUuid") or "",
            "connection": item.get("connection") or "",
            "chatLabel": item.get("chatLabel") or "",
            "linkHash": link_signature.get("linkHash", ""),
            "subscribedAt": item.get("createdAt") or "",
            "enabled": item.get("enabled") is not False,
        }
        if item.get("updatedAt"):
            subscriptions[chat_id]["updatedAt"] = item["updatedAt"]
    db["clientSubscriptions"] = subscriptions
    return normalize_db(db)


def save_db(db, path=TELEGRAM_DB_PATH):
    ensure_config_dir(path)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n")
    chown_xray(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(path)


def save_db_sections(db, sections, path=TELEGRAM_DB_PATH):
    current = load_db(path)
    for section in sections:
        if section in db:
            current[section] = db[section]
    save_db(current, path)


def mask_token(token):
    if not token:
        return "not configured"
    if len(token) <= 12:
        return "***"
    return token[:6] + "..." + token[-6:]


def normalize_display_name(value, default, label):
    raw = str(value or "").strip()
    if not raw:
        return default
    if any(char in raw for char in "\r\n\t"):
        raise ValueError(f"{label} must not contain control characters.")
    if len(raw) > 64:
        raise ValueError(f"{label} must be 64 characters or shorter.")
    return raw


def bot_name(db=None, loader=load_db):
    source = db if isinstance(db, dict) else loader()
    try:
        return normalize_display_name(source.get("botName", DEFAULT_BOT_NAME), DEFAULT_BOT_NAME, "BOT_NAME")
    except ValueError:
        return DEFAULT_BOT_NAME
