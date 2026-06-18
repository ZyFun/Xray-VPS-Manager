"""SQLite repository for global activity blocklist entries and hit counters."""

from __future__ import annotations

import sqlite3
from typing import Any

from xray_vps_manager.db import database


def _item_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "value": row["value"] or "",
        "kind": row["kind"] or "",
        "sourceClient": row["source_client_name"] or "",
        "sourceEventId": int(row["source_event_id"]) if row["source_event_id"] is not None else None,
        "source": row["source"] or "",
        "comment": row["comment"] or "",
        "createdAt": row["created_at"] or "",
        "expiresAt": row["expires_at"] or "",
        "enabled": bool(row["enabled"]),
    }


def upsert_block(connection: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO activity_blocklist(
                value, kind, source_client_name, source_event_id, source,
                comment, created_at, expires_at, enabled
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(value) DO UPDATE SET
                kind = excluded.kind,
                source_client_name = excluded.source_client_name,
                source_event_id = excluded.source_event_id,
                source = excluded.source,
                comment = excluded.comment,
                expires_at = excluded.expires_at,
                enabled = excluded.enabled
            """,
            (
                item.get("value") or "",
                item.get("kind") or "domain",
                item.get("sourceClient") or None,
                item.get("sourceEventId"),
                item.get("source") or "manual",
                item.get("comment") or "",
                item.get("createdAt") or "",
                item.get("expiresAt") or None,
                1 if item.get("enabled", True) else 0,
            ),
        )
    stored = get_block_by_value(connection, str(item.get("value") or ""))
    if stored is None:
        raise RuntimeError("Failed to store activity blocklist item.")
    return stored


def get_block_by_value(connection: sqlite3.Connection, value: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM activity_blocklist WHERE value = ?",
        (value,),
    ).fetchone()
    return _item_from_row(row) if row else None


def get_block_by_id(connection: sqlite3.Connection, block_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM activity_blocklist WHERE id = ?",
        (int(block_id),),
    ).fetchone()
    return _item_from_row(row) if row else None


def get_block(connection: sqlite3.Connection, value_or_id: str) -> dict[str, Any] | None:
    value = str(value_or_id or "").strip()
    if value.isdigit():
        item = get_block_by_id(connection, int(value))
        if item is not None:
            return item
    return get_block_by_value(connection, value)


def list_blocks(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM activity_blocklist
        ORDER BY COALESCE(source_client_name, ''), value
        """
    ).fetchall()
    return [_item_from_row(row) for row in rows]


def active_blocks(connection: sqlite3.Connection, now_iso: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM activity_blocklist
        WHERE enabled = 1
          AND (expires_at IS NULL OR expires_at = '' OR expires_at > ?)
        ORDER BY COALESCE(source_client_name, ''), value
        """,
        (now_iso,),
    ).fetchall()
    return [_item_from_row(row) for row in rows]


def delete_block(connection: sqlite3.Connection, value_or_id: str) -> dict[str, Any] | None:
    item = get_block(connection, value_or_id)
    if item is None:
        return None
    with database.transaction(connection):
        connection.execute("DELETE FROM activity_blocklist WHERE id = ?", (item["id"],))
    return item


def record_hit(connection: sqlite3.Connection, blocklist_id: int, client_name: str, event_time: str) -> None:
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO activity_blocklist_hits(blocklist_id, client_name, hits, first_seen_at, last_seen_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(blocklist_id, client_name) DO UPDATE SET
                hits = activity_blocklist_hits.hits + 1,
                first_seen_at = COALESCE(activity_blocklist_hits.first_seen_at, excluded.first_seen_at),
                last_seen_at = excluded.last_seen_at
            """,
            (int(blocklist_id), client_name, event_time, event_time),
        )


def list_hit_stats(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    block_rows = connection.execute(
        """
        SELECT
            b.id,
            b.value,
            b.kind,
            b.source_client_name,
            b.comment,
            b.expires_at,
            b.enabled,
            COALESCE(SUM(h.hits), 0) AS total_hits,
            MIN(h.first_seen_at) AS first_seen_at,
            MAX(h.last_seen_at) AS last_seen_at
        FROM activity_blocklist b
        LEFT JOIN activity_blocklist_hits h ON h.blocklist_id = b.id
        GROUP BY b.id
        ORDER BY b.value
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in block_rows:
        hit_rows = connection.execute(
            """
            SELECT client_name, hits, first_seen_at, last_seen_at
            FROM activity_blocklist_hits
            WHERE blocklist_id = ?
            ORDER BY hits DESC, client_name
            """,
            (int(row["id"]),),
        ).fetchall()
        result.append(
            {
                "id": int(row["id"]),
                "value": row["value"] or "",
                "kind": row["kind"] or "",
                "sourceClient": row["source_client_name"] or "",
                "comment": row["comment"] or "",
                "expiresAt": row["expires_at"] or "",
                "enabled": bool(row["enabled"]),
                "totalHits": int(row["total_hits"] or 0),
                "firstSeen": row["first_seen_at"] or "",
                "lastSeen": row["last_seen_at"] or "",
                "clients": {
                    hit["client_name"]: int(hit["hits"] or 0)
                    for hit in hit_rows
                },
            }
        )
    return result
