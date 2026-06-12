"""SQLite repository for manager metadata and shared payment settings."""

from __future__ import annotations

import sqlite3

from xray_vps_manager.db import database


def set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    _set_key_value(connection, "manager_metadata", key, value)


def get_metadata(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    return _get_key_value(connection, "manager_metadata", key, default)


def list_metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return _list_key_values(connection, "manager_metadata")


def set_payment_setting(connection: sqlite3.Connection, key: str, value: str) -> None:
    _set_key_value(connection, "payment_settings", key, value)


def get_payment_setting(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    return _get_key_value(connection, "payment_settings", key, default)


def list_payment_settings(connection: sqlite3.Connection) -> dict[str, str]:
    return _list_key_values(connection, "payment_settings")


def _set_key_value(connection: sqlite3.Connection, table: str, key: str, value: str) -> None:
    with database.transaction(connection):
        connection.execute(
            f"""
            INSERT INTO {table}(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (key, value),
        )


def _get_key_value(connection: sqlite3.Connection, table: str, key: str, default: str = "") -> str:
    row = connection.execute(f"SELECT value FROM {table} WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _list_key_values(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    rows = connection.execute(f"SELECT key, value FROM {table} ORDER BY key").fetchall()
    return {row["key"]: row["value"] for row in rows}
