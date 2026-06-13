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
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.repositories import telegram as sqlite_telegram
from xray_vps_manager.db.storage import (
    SQLiteReadUnavailable,
    sqlite_read_ready,
    sqlite_reads_enabled,
    sqlite_writes_enabled,
    truthy,
)
from xray_vps_manager.telegram.payments import (
    PAYMENT_SETTING_KEYS,
    normalize_payment_transfer_method,
    parse_payment_rounding_step,
    parse_payment_value,
)

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
    "paymentTransferMethod": "none",
    "paymentPhone": "",
    "paymentBank": "",
    "paymentCard": "",
    "paymentBankAccount": "",
    "geoipState": {"files": {}, "sentIds": []},
    "clientSubscriptions": {},
    "clientSubscriptionState": {"userUpdateOffset": 0, "expiryReminders": {}},
    "dailySummaryState": {},
    "adminState": {},
}

TELEGRAM_SETTING_KEYS = ("version", "enabled", "token", "botName", "chatId", "chatLabel", "routeMode")
STATE_KEYS = ("geoipState", "clientSubscriptionState", "dailySummaryState", "adminState")


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
    try:
        merged["paymentTransferMethod"] = normalize_payment_transfer_method(merged.get("paymentTransferMethod", "none"))
    except ValueError:
        merged["paymentTransferMethod"] = "none"
    for key in ("paymentPhone", "paymentBank", "paymentCard", "paymentBankAccount"):
        if not isinstance(merged.get(key, ""), str):
            merged[key] = ""
    if merged.get("routeMode") not in ("direct", "cascade"):
        merged["routeMode"] = "direct"
    return merged


def load_db(path=TELEGRAM_DB_PATH):
    return normalize_db(load_json(path, DEFAULT_DB))


def load_db_sql(path=TELEGRAM_DB_PATH, *, db_path: str | Path | None = None):
    return load_db_sql_result(path, db_path=db_path).db


def load_db_sql_result(path=TELEGRAM_DB_PATH, *, db_path: str | Path | None = None) -> TelegramDbReadResult:
    if sqlite_reads_enabled():
        if not database.database_file_exists(db_path):
            raise SQLiteReadUnavailable("SQLite reads are enabled but manager database is missing.")
        connection = None
        try:
            connection = database.open_database(db_path)
            if not sqlite_read_ready(connection):
                raise SQLiteReadUnavailable("SQLite reads are enabled but JSON import is not marked ready.")
            return TelegramDbReadResult(load_db_from_sqlite(connection), "sqlite")
        except SQLiteReadUnavailable:
            raise
        except Exception as exc:
            raise SQLiteReadUnavailable(f"SQLite reads are enabled but Telegram settings cannot be read: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()
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


def save_db(db, path=TELEGRAM_DB_PATH, *, db_path: str | Path | None = None):
    db = normalize_db(db)
    if sqlite_writes_enabled() and sqlite_reads_enabled():
        write_db_to_sqlite_for_write(db, db_path=db_path, strict=True)
        return
    write_json_db(db, path)
    mirror_db_to_sqlite_for_write(db, db_path=db_path)


def write_json_db(db, path=TELEGRAM_DB_PATH):
    ensure_config_dir(path)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n")
    chown_xray(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(path)


def save_db_sections(db, sections, path=TELEGRAM_DB_PATH, *, db_path: str | Path | None = None):
    if sqlite_writes_enabled() and sqlite_reads_enabled():
        write_db_sections_to_sqlite_for_write(db, sections, db_path=db_path, strict=True)
        return
    current = load_db(path)
    for section in sections:
        if section in db:
            current[section] = db[section]
    save_db(current, path, db_path=db_path)


def mirror_db_to_sqlite_for_write(db, *, db_path: str | Path | None = None) -> bool:
    return write_db_to_sqlite_for_write(db, db_path=db_path, strict=False)


def write_db_to_sqlite_for_write(db, *, db_path: str | Path | None = None, strict: bool = False) -> bool:
    if not sqlite_writes_enabled() or not database.database_file_exists(db_path):
        if strict:
            raise RuntimeError("SQLite writes are enabled but manager database is missing")
        return False

    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            if strict:
                raise RuntimeError("SQLite writes are enabled but JSON import is not marked ready")
            return False

        normalized = normalize_db(db)
        with database.transaction(connection):
            for key in TELEGRAM_SETTING_KEYS:
                sqlite_telegram.set_setting(connection, key, _sqlite_scalar(normalized.get(key, "")))
            for key in PAYMENT_SETTING_KEYS:
                sqlite_settings.set_payment_setting(connection, key, str(normalized.get(key, "")))
            for key in STATE_KEYS:
                state = normalized.get(key)
                sqlite_telegram.set_state(connection, key, state if isinstance(state, dict) else {})
            replace_subscriptions_in_sqlite(connection, normalized)
        return True
    except Exception:
        if strict:
            raise
        return False
    finally:
        if connection is not None:
            connection.close()


def write_db_sections_to_sqlite_for_write(
    db,
    sections,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> bool:
    if not sqlite_writes_enabled() or not database.database_file_exists(db_path):
        if strict:
            raise RuntimeError("SQLite writes are enabled but manager database is missing")
        return False

    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            if strict:
                raise RuntimeError("SQLite writes are enabled but JSON import is not marked ready")
            return False

        normalized = normalize_db(db)
        with database.transaction(connection):
            for section in sections:
                if section in TELEGRAM_SETTING_KEYS:
                    sqlite_telegram.set_setting(connection, section, _sqlite_scalar(normalized.get(section, "")))
                elif section in PAYMENT_SETTING_KEYS:
                    sqlite_settings.set_payment_setting(connection, section, str(normalized.get(section, "")))
                elif section in STATE_KEYS:
                    state = normalized.get(section)
                    if not isinstance(state, dict):
                        state = {}
                    if section == "clientSubscriptionState":
                        current = sqlite_telegram.get_state(connection, section, {})
                        state = merge_client_subscription_state(current, state)
                    sqlite_telegram.set_state(connection, section, state)
                elif section == "clientSubscriptions":
                    replace_subscriptions_in_sqlite(connection, normalized)
        return True
    except Exception:
        if strict:
            raise
        return False
    finally:
        if connection is not None:
            connection.close()


def replace_subscriptions_in_sqlite(connection, normalized: dict) -> None:
    known_clients = set(sqlite_clients.list_clients(connection))
    sqlite_telegram.delete_all_subscriptions(connection)
    subscriptions = normalized.get("clientSubscriptions", {})
    if not isinstance(subscriptions, dict):
        return
    for chat_id, subscription in subscriptions.items():
        if not isinstance(subscription, dict):
            continue
        client_uuid = str(subscription.get("clientId") or subscription.get("clientUuid") or "").strip()
        if not client_uuid:
            continue
        client_name = str(subscription.get("client") or subscription.get("clientName") or "").strip()
        sqlite_telegram.upsert_subscription(
            connection,
            {
                "chatId": str(chat_id),
                "chatLabel": subscription.get("chatLabel") or "",
                "clientName": client_name if client_name in known_clients else "",
                "clientUuid": client_uuid,
                "connection": subscription.get("connection") or "",
                "linkSignature": {"linkHash": subscription.get("linkHash") or ""},
                "enabled": subscription.get("enabled") is not False,
                "createdAt": subscription.get("subscribedAt") or subscription.get("createdAt") or "",
                "updatedAt": subscription.get("updatedAt") or subscription.get("subscribedAt") or "",
            },
        )


def merge_client_subscription_state(current: dict, incoming: dict) -> dict:
    merged = dict(current if isinstance(current, dict) else {})
    incoming = incoming if isinstance(incoming, dict) else {}
    merged.update(incoming)

    try:
        merged["userUpdateOffset"] = max(
            int((current or {}).get("userUpdateOffset", 0) or 0),
            int(incoming.get("userUpdateOffset", 0) or 0),
        )
    except (TypeError, ValueError):
        merged["userUpdateOffset"] = int((current or {}).get("userUpdateOffset", 0) or 0)

    current_reminders = (current or {}).get("expiryReminders", {})
    incoming_reminders = incoming.get("expiryReminders", {})
    reminders = {}
    if isinstance(current_reminders, dict):
        reminders.update(current_reminders)
    if isinstance(incoming_reminders, dict):
        reminders.update(incoming_reminders)
    if len(reminders) > 2000:
        reminders = dict(sorted(reminders.items(), key=lambda item: item[1])[-2000:])
    merged["expiryReminders"] = reminders

    for key in ("lastUserPoll", "lastExpiryReminderCheck", "lastExpiryReminder"):
        current_value = str((current or {}).get(key) or "")
        incoming_value = str(incoming.get(key) or "")
        if current_value or incoming_value:
            merged[key] = max(current_value, incoming_value)
    return merged


def _sqlite_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


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
