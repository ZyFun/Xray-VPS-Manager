"""SQLite repository for Telegram bot settings, state, and subscriptions."""

from __future__ import annotations

import sqlite3
from typing import Any

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories.base import decode_json, encode_json


def set_setting(connection: sqlite3.Connection, key: str, value: str) -> None:
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO telegram_settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (key, value),
        )


def get_setting(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM telegram_settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def list_settings(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT key, value FROM telegram_settings ORDER BY key").fetchall()
    return {row["key"]: row["value"] for row in rows}


def set_state(connection: sqlite3.Connection, key: str, value: dict[str, Any]) -> None:
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO telegram_state(key, value_json)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (key, encode_json(value)),
        )


def get_state(connection: sqlite3.Connection, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    row = connection.execute("SELECT value_json FROM telegram_state WHERE key = ?", (key,)).fetchone()
    if not row:
        return {} if default is None else dict(default)
    decoded = decode_json(row["value_json"], default or {})
    return decoded if isinstance(decoded, dict) else ({} if default is None else dict(default))


def upsert_subscription(connection: sqlite3.Connection, subscription: dict[str, Any]) -> int:
    with database.transaction(connection):
        cursor = connection.execute(
            """
            INSERT INTO telegram_subscriptions(
                chat_id, chat_label, client_name, client_uuid, connection_tag,
                link_signature_json, enabled, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, client_uuid) DO UPDATE SET
                chat_label = excluded.chat_label,
                client_name = excluded.client_name,
                connection_tag = excluded.connection_tag,
                link_signature_json = excluded.link_signature_json,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                str(subscription.get("chatId") or subscription.get("chat_id") or ""),
                subscription.get("chatLabel") or subscription.get("chat_label") or "",
                subscription.get("clientName") or subscription.get("client_name") or None,
                str(subscription.get("clientUuid") or subscription.get("client_uuid") or ""),
                subscription.get("connection") or subscription.get("connection_tag") or None,
                encode_json(subscription.get("linkSignature") or subscription.get("link_signature") or {}),
                0 if subscription.get("enabled") is False else 1,
                subscription.get("createdAt") or subscription.get("created_at") or "",
                subscription.get("updatedAt") or subscription.get("updated_at") or "",
            ),
        )
    return int(cursor.lastrowid or 0)


def list_subscriptions(connection: sqlite3.Connection, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    where = " WHERE enabled = 1" if enabled_only else ""
    rows = connection.execute(
        f"SELECT * FROM telegram_subscriptions{where} ORDER BY chat_id, client_uuid"
    ).fetchall()
    return [_subscription_from_row(row) for row in rows]


def delete_subscription(connection: sqlite3.Connection, chat_id: str, client_uuid: str) -> bool:
    with database.transaction(connection):
        result = connection.execute(
            "DELETE FROM telegram_subscriptions WHERE chat_id = ? AND client_uuid = ?",
            (str(chat_id), str(client_uuid)),
        )
    return result.rowcount > 0


def delete_all_subscriptions(connection: sqlite3.Connection) -> int:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM telegram_subscriptions")
    return int(result.rowcount or 0)


def _subscription_from_row(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "chatId": row["chat_id"],
        "chatLabel": row["chat_label"] or "",
        "clientName": row["client_name"] or "",
        "clientUuid": row["client_uuid"],
        "connection": row["connection_tag"] or "",
        "linkSignature": decode_json(row["link_signature_json"]),
        "enabled": bool(row["enabled"]),
        "createdAt": row["created_at"] or "",
        "updatedAt": row["updated_at"] or "",
    }
