"""Telegram Bot API command menu helpers."""

from __future__ import annotations

from xray_vps_manager.telegram import api

UNSUBSCRIBED_COMMANDS = [
    {"command": "start", "description": "Открыть меню"},
    {"command": "help", "description": "Помощь"},
]

SUBSCRIBED_COMMANDS = [
    {"command": "start", "description": "Открыть меню"},
    {"command": "status", "description": "Показать подписку"},
    {"command": "link", "description": "Получить VPN-ссылку"},
    {"command": "traffic", "description": "Показать статистику трафика"},
    {"command": "help", "description": "Помощь"},
    {"command": "unsubscribe", "description": "Отписаться от бота"},
]


def chat_scope(chat_id):
    return {"type": "chat", "chat_id": str(chat_id)}


def set_default_commands(db, curl_json=None):
    curl_json = curl_json or api.curl_json
    return curl_json(db, "setMyCommands", {"commands": UNSUBSCRIBED_COMMANDS}, timeout=30)


def set_subscribed_chat_commands(db, chat_id, curl_json=None):
    curl_json = curl_json or api.curl_json
    return curl_json(
        db,
        "setMyCommands",
        {"commands": SUBSCRIBED_COMMANDS, "scope": chat_scope(chat_id)},
        timeout=30,
    )


def delete_chat_commands(db, chat_id, curl_json=None):
    curl_json = curl_json or api.curl_json
    return curl_json(db, "deleteMyCommands", {"scope": chat_scope(chat_id)}, timeout=30)


def sync_all_command_menus(db, curl_json=None) -> tuple[int, list[str]]:
    curl_json = curl_json or api.curl_json
    set_default_commands(db, curl_json=curl_json)
    failures = []
    updated = 0
    subscriptions = db.get("clientSubscriptions", {})
    if not isinstance(subscriptions, dict):
        return updated, failures
    for chat_id, subscription in subscriptions.items():
        if not isinstance(subscription, dict) or subscription.get("enabled") is False:
            continue
        try:
            set_subscribed_chat_commands(db, chat_id, curl_json=curl_json)
            updated += 1
        except Exception as exc:
            failures.append(f"{chat_id}: {exc}")
    return updated, failures
