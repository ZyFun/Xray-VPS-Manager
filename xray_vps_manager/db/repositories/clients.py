"""SQLite repository for client records and access/limit metadata."""

from __future__ import annotations

import sqlite3
from typing import Any

from xray_vps_manager.clients.payments import normalize_payment_type
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories.base import decode_json, encode_json, without_keys

_CLIENT_KNOWN_KEYS = {
    "id",
    "created",
    "enabled",
    "connection",
    "client",
    "expiresAt",
    "accessDays",
    "expiredAt",
    "disabledReason",
    "disabledAt",
    "trafficLimit",
    "trafficLimitExceededAt",
    "trafficLimitExceededPeriod",
    "trafficLimitExceededBytes",
    "trafficLimitResetAt",
    "paymentType",
    "selectedCascadeTag",
    "extra",
}


def _traffic_limit(entry: dict[str, Any]) -> dict[str, Any] | None:
    limit = entry.get("trafficLimit")
    return limit if isinstance(limit, dict) and limit.get("bytes") is not None else None


def _client_json(entry: dict[str, Any], name: str) -> dict[str, Any]:
    client = dict(entry.get("client") or {})
    client.setdefault("id", entry.get("id", ""))
    if not client.get("email"):
        created = entry.get("created", "")
        client["email"] = f"{name}|created={created}" if created else name
    return client


def _entry_from_row(connection: sqlite3.Connection, row) -> dict[str, Any] | None:
    if row is None:
        return None
    extra = decode_json(row["extra_json"])
    entry = dict(extra) if isinstance(extra, dict) else {}
    entry.update(
        {
            "id": row["uuid"],
            "created": row["created_at"] or "",
            "enabled": bool(row["enabled"]),
            "connection": row["connection_tag"] or "",
            "client": decode_json(row["xray_client_json"]),
            "paymentType": row["payment_type"] or "free",
            "selectedCascadeTag": row["selected_cascade_tag"] or "",
        }
    )
    for db_key, json_key in (
        ("disabled_reason", "disabledReason"),
        ("disabled_at", "disabledAt"),
        ("expires_at", "expiresAt"),
        ("access_days", "accessDays"),
        ("expired_at", "expiredAt"),
    ):
        if row[db_key] not in (None, ""):
            entry[json_key] = row[db_key]

    limit = get_traffic_limit(connection, row["name"])
    if limit:
        entry["trafficLimit"] = limit
    state = get_traffic_limit_state(connection, row["name"])
    entry.update(state)
    return entry


def upsert_client(connection: sqlite3.Connection, name: str, entry: dict[str, Any]) -> None:
    client = _client_json(entry, name)
    uuid = str(entry.get("id") or client.get("id") or "")
    if not uuid:
        raise ValueError(f"Client has no UUID: {name}")
    extra = entry.get("extra")
    if not isinstance(extra, dict):
        extra = without_keys(entry, _CLIENT_KNOWN_KEYS)

    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO clients(
                name, uuid, email, connection_tag, created_at, enabled, disabled_reason, disabled_at,
                expires_at, access_days, expired_at, payment_type, selected_cascade_tag, xray_client_json, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                uuid = excluded.uuid,
                email = excluded.email,
                connection_tag = excluded.connection_tag,
                created_at = excluded.created_at,
                enabled = excluded.enabled,
                disabled_reason = excluded.disabled_reason,
                disabled_at = excluded.disabled_at,
                expires_at = excluded.expires_at,
                access_days = excluded.access_days,
                expired_at = excluded.expired_at,
                payment_type = excluded.payment_type,
                selected_cascade_tag = excluded.selected_cascade_tag,
                xray_client_json = excluded.xray_client_json,
                extra_json = excluded.extra_json
            """,
            (
                name,
                uuid,
                client.get("email") or name,
                entry.get("connection") or None,
                entry.get("created") or "",
                0 if entry.get("enabled") is False else 1,
                entry.get("disabledReason") or None,
                entry.get("disabledAt") or None,
                entry.get("expiresAt") or None,
                entry.get("accessDays"),
                entry.get("expiredAt") or None,
                normalize_payment_type(entry.get("paymentType", "free")),
                entry.get("selectedCascadeTag") or None,
                encode_json(client),
                encode_json(extra),
            ),
        )
        limit = _traffic_limit(entry)
        if limit:
            set_traffic_limit(connection, name, str(limit.get("period") or "daily"), int(limit.get("bytes") or 0), limit.get("setAt") or "")
        else:
            clear_traffic_limit(connection, name)
        set_traffic_limit_state(
            connection,
            name,
            exceeded_at=entry.get("trafficLimitExceededAt") or "",
            exceeded_period=entry.get("trafficLimitExceededPeriod") or "",
            exceeded_bytes=int(entry.get("trafficLimitExceededBytes") or 0),
            reset_at=entry.get("trafficLimitResetAt") or "",
        )


def get_client(connection: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM clients WHERE name = ?", (name,)).fetchone()
    return _entry_from_row(connection, row)


def list_clients(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute("SELECT * FROM clients ORDER BY name").fetchall()
    return {row["name"]: _entry_from_row(connection, row) for row in rows}


def delete_client(connection: sqlite3.Connection, name: str) -> bool:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM clients WHERE name = ?", (name,))
    return result.rowcount > 0


def set_traffic_limit(
    connection: sqlite3.Connection,
    name: str,
    period: str,
    limit_bytes: int,
    set_at: str = "",
) -> None:
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO client_traffic_limits(client_name, period, limit_bytes, set_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(client_name) DO UPDATE SET
                period = excluded.period,
                limit_bytes = excluded.limit_bytes,
                set_at = excluded.set_at
            """,
            (name, period, int(limit_bytes), set_at or None),
        )


def clear_traffic_limit(connection: sqlite3.Connection, name: str) -> None:
    with database.transaction(connection):
        connection.execute("DELETE FROM client_traffic_limits WHERE client_name = ?", (name,))


def get_traffic_limit(connection: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT period, limit_bytes, set_at FROM client_traffic_limits WHERE client_name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    limit = {"period": row["period"], "bytes": int(row["limit_bytes"])}
    if row["set_at"]:
        limit["setAt"] = row["set_at"]
    return limit


def set_traffic_limit_state(
    connection: sqlite3.Connection,
    name: str,
    *,
    exceeded_at: str = "",
    exceeded_period: str = "",
    exceeded_bytes: int = 0,
    reset_at: str = "",
) -> None:
    if not any((exceeded_at, exceeded_period, exceeded_bytes, reset_at)):
        with database.transaction(connection):
            connection.execute("DELETE FROM client_traffic_limit_state WHERE client_name = ?", (name,))
        return
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO client_traffic_limit_state(
                client_name, exceeded_at, exceeded_period, exceeded_bytes, reset_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(client_name) DO UPDATE SET
                exceeded_at = excluded.exceeded_at,
                exceeded_period = excluded.exceeded_period,
                exceeded_bytes = excluded.exceeded_bytes,
                reset_at = excluded.reset_at
            """,
            (name, exceeded_at or None, exceeded_period or None, int(exceeded_bytes), reset_at or None),
        )


def get_traffic_limit_state(connection: sqlite3.Connection, name: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT exceeded_at, exceeded_period, exceeded_bytes, reset_at
        FROM client_traffic_limit_state
        WHERE client_name = ?
        """,
        (name,),
    ).fetchone()
    if row is None:
        return {}
    state: dict[str, Any] = {}
    if row["exceeded_at"]:
        state["trafficLimitExceededAt"] = row["exceeded_at"]
    if row["exceeded_period"]:
        state["trafficLimitExceededPeriod"] = row["exceeded_period"]
    if row["exceeded_bytes"]:
        state["trafficLimitExceededBytes"] = int(row["exceeded_bytes"])
    if row["reset_at"]:
        state["trafficLimitResetAt"] = row["reset_at"]
    return state
