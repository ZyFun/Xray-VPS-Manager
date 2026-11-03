"""Telegram bot settings stored in SQLite."""

from __future__ import annotations

import copy
from itertools import chain
from dataclasses import dataclass
import re
import shutil
from pathlib import Path

from xray_vps_manager.core.errors import ConflictError
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import settings as sqlite_settings
from xray_vps_manager.db.repositories import telegram as sqlite_telegram
from xray_vps_manager.db.storage import (
    SQLiteReadUnavailable,
    ready_write_session,
    sqlite_read_ready,
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
    "botUsername": "",
    "chatId": "",
    "chatLabel": "",
    "routeMode": "direct",
    "paymentAmount": "",
    "paymentTotalAmount": "",
    "paymentDomainAnnualAmount": "",
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

TELEGRAM_SETTING_KEYS = ("version", "enabled", "token", "botName", "botUsername", "chatId", "chatLabel", "routeMode")
STATE_KEYS = ("geoipState", "clientSubscriptionState", "dailySummaryState", "adminState")


@dataclass(frozen=True)
class TelegramDbReadResult:
    db: dict
    source: str


def chown_xray(path):
    try:
        shutil.chown(path, user="root", group="xray")
    except LookupError:
        shutil.chown(path, user="root")


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
    if merged.get("paymentCurrency") not in ("₽", "$", "€"):
        merged["paymentCurrency"] = "₽"
    try:
        domain_amount, _currency = parse_payment_value(
            str(merged.get("paymentDomainAnnualAmount", "")),
            merged.get("paymentCurrency", "₽"),
        )
        merged["paymentDomainAnnualAmount"] = domain_amount
    except ValueError:
        merged["paymentDomainAnnualAmount"] = ""
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
    merged["botUsername"] = normalize_bot_username(merged.get("botUsername", ""))
    return merged


def load_db(path=None, *, db_path: str | Path | None = None):
    return load_db_sql(path, db_path=db_path)


def load_db_sql(path=None, *, db_path: str | Path | None = None):
    return load_db_sql_result(path, db_path=db_path).db


def load_db_sql_result(path=None, *, db_path: str | Path | None = None) -> TelegramDbReadResult:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        return TelegramDbReadResult(load_db_from_sqlite(connection), "sqlite")
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite Telegram settings cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


class _TelegramSnapshot(dict):
    """Подписки на момент чтения нужны poller, чтобы сохранить только свои изменения."""

    def __init__(self, values, *, original=None, stored=None):
        super().__init__(values)
        self.original_subscriptions = copy.deepcopy(values["clientSubscriptions"]) if original is None else original
        # После записи в словаре poller могут оставаться поля, которых в SQLite нет.
        self.stored_subscriptions = self.original_subscriptions if stored is None else stored

    def copy(self):
        return _TelegramSnapshot(self, original=self.original_subscriptions, stored=self.stored_subscriptions)


def load_db_from_sqlite(connection) -> dict:
    values = load_settings(connection)
    values["clientSubscriptions"] = list_subscriptions(connection)
    return _TelegramSnapshot(values)


def load_settings(connection) -> dict:
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

    for key in STATE_KEYS:
        db[key] = sqlite_telegram.get_state(connection, key, db.get(key, {}))

    settings = normalize_db(db)
    del settings["clientSubscriptions"]
    return settings


def _subscription_from_row(item):
    link_signature = item.get("linkSignature")
    record = {
        "client": item["clientName"],
        "clientId": item["clientUuid"],
        "connection": item["connection"],
        "chatLabel": item["chatLabel"],
        "linkHash": link_signature.get("linkHash", "") if isinstance(link_signature, dict) else "",
        "subscribedAt": item["createdAt"],
        "enabled": item["enabled"],
        "activityNotificationsEnabled": item["activityNotificationsEnabled"],
    }
    if item["updatedAt"]:
        record["updatedAt"] = item["updatedAt"]
    return record


def list_subscriptions(connection) -> dict:
    return {
        row["chatId"].strip(): _subscription_from_row(row)
        for row in sqlite_telegram.list_subscriptions(connection) if row["chatId"].strip()
    }


def get_subscription(connection, chat_id):
    rows = sqlite_telegram.subscriptions_for_chat(connection, chat_id)
    return _subscription_from_row(rows[-1]) if rows else None


def delete_subscription(connection, chat_id):
    for row in sqlite_telegram.subscriptions_for_chat(connection, chat_id):
        sqlite_telegram.delete_subscription(connection, row["chatId"], row["clientUuid"])


def save_subscription(connection, chat_id, subscription):
    """Одна подписка на чат в фасаде, составной ключ (chat_id, client_uuid) в SQLite."""
    key = str(chat_id).strip()
    rows = sqlite_telegram.subscriptions_for_chat(connection, key)
    record = _subscription_to_row(connection, key, subscription)
    for row in rows:
        if (not record or row["chatId"] != key or row["clientUuid"] != record["clientUuid"]
                or row["createdAt"] != record["createdAt"]):
            sqlite_telegram.delete_subscription(connection, row["chatId"], row["clientUuid"])
    if record:
        sqlite_telegram.upsert_subscription(connection, record)
        return _subscription_from_row(record)
    return None


def _subscription_to_row(connection, chat_id, subscription):
    if not isinstance(subscription, dict):
        return None
    uuid = str(subscription.get("clientId") or subscription.get("clientUuid") or "").strip()
    if not uuid:
        return None
    name = str(subscription.get("client") or subscription.get("clientName") or "").strip()
    return {
        "chatId": str(chat_id),
        "chatLabel": subscription.get("chatLabel") or "",
        "clientName": name if name and sqlite_clients.client_exists(connection, name) else "",
        "clientUuid": uuid,
        "connection": subscription.get("connection") or "",
        "linkSignature": {"linkHash": subscription.get("linkHash") or ""},
        "enabled": subscription.get("enabled") is not False,
        "activityNotificationsEnabled": subscription.get("activityNotificationsEnabled") is True,
        "createdAt": subscription.get("subscribedAt") or subscription.get("createdAt") or "",
        "updatedAt": subscription.get("updatedAt") or subscription.get("subscribedAt") or "",
    }


def update_state(connection, key, mutate):
    """Чтение и запись выполняются в write_session вызывающего сценария."""
    state = mutate(sqlite_telegram.get_state(connection, key, {}))
    sqlite_telegram.set_state(connection, key, state)
    return state


def save_settings(connection, changes, *, sections=None):
    normalized = normalize_db(changes)
    keys = changes if sections is None else sections
    for key in keys:
        value = normalized.get(key)
        if key in TELEGRAM_SETTING_KEYS:
            sqlite_telegram.set_setting(connection, key, _sqlite_scalar(value))
        elif key in PAYMENT_SETTING_KEYS:
            sqlite_settings.set_payment_setting(connection, key, str(value))
        elif key == "clientSubscriptionState":
            update_state(connection, key, lambda current: merge_client_subscription_state(current, value))
        elif key in STATE_KEYS:
            sqlite_telegram.set_state(connection, key, value)


def save_db(db, path=None, *, db_path: str | Path | None = None):
    write_db_to_sqlite_for_write(db, db_path=db_path, strict=True)


def save_db_sections(db, sections, path=None, *, db_path: str | Path | None = None):
    write_db_sections_to_sqlite_for_write(db, sections, db_path=db_path, strict=True)


def write_db_to_sqlite_for_write(db, *, db_path: str | Path | None = None, strict: bool = False) -> bool:
    """Полная замена для совместимости с прежней инициализацией."""
    try:
        with ready_write_session(db_path) as connection:
            normalized = normalize_db(db)
            save_settings(connection, {key: normalized[key] for key in (*TELEGRAM_SETTING_KEYS, *PAYMENT_SETTING_KEYS)})
            for key in STATE_KEYS:
                sqlite_telegram.set_state(connection, key, normalized[key])
            sqlite_telegram.delete_all_subscriptions(connection)
            for chat_id, item in normalized["clientSubscriptions"].items():
                record = _subscription_to_row(connection, chat_id, item)
                if record:
                    sqlite_telegram.upsert_subscription(connection, record)
        return True
    except Exception:
        if strict:
            raise
        return False


def _save_changed_subscriptions(connection, snapshot, incoming):
    tracked = isinstance(snapshot, _TelegramSnapshot)
    original = snapshot.original_subscriptions if tracked else {}
    stored = snapshot.stored_subscriptions.copy() if tracked else {}
    removed = (chat_id for chat_id in original if chat_id not in incoming)
    for chat_id in chain(removed, incoming):
        if chat_id in original and chat_id in incoming and original[chat_id] == incoming[chat_id]:
            continue
        if tracked and get_subscription(connection, chat_id) != stored.get(chat_id):
            raise ConflictError("Подписка чата изменилась. Откройте меню заново и повторите действие.")
        if chat_id in incoming:
            stored[chat_id] = save_subscription(connection, chat_id, incoming[chat_id])
        else:
            delete_subscription(connection, chat_id)
            stored.pop(chat_id, None)
    return stored


def write_db_sections_to_sqlite_for_write(
    db,
    sections,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> bool:
    try:
        with ready_write_session(db_path) as connection:
            sections = tuple(sections)
            save_settings(connection, db, sections=sections)
            if "clientSubscriptions" in sections:
                incoming = db.get("clientSubscriptions")
                if not isinstance(incoming, dict):
                    incoming = {}
                stored = _save_changed_subscriptions(connection, db, incoming)
        if "clientSubscriptions" in sections and isinstance(db, _TelegramSnapshot):
            db.original_subscriptions = copy.deepcopy(incoming)
            db.stored_subscriptions = stored
        return True
    except Exception:
        if strict:
            raise
        return False


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


def normalize_bot_username(value):
    raw = str(value or "").strip().lstrip("@")
    if not raw:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", raw):
        return ""
    return raw


def bot_name(db=None, loader=load_db):
    source = db if isinstance(db, dict) else loader()
    try:
        return normalize_display_name(source.get("botName", DEFAULT_BOT_NAME), DEFAULT_BOT_NAME, "BOT_NAME")
    except ValueError:
        return DEFAULT_BOT_NAME
