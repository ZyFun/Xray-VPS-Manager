"""SQLite repository for managed cascade outbound metadata."""

from __future__ import annotations

import sqlite3
from typing import Any

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories.base import decode_json, encode_json, without_keys

_KNOWN_KEYS = {
    "tag",
    "country",
    "label",
    "created",
    "createdAt",
    "created_at",
    "updated",
    "updatedAt",
    "updated_at",
    "extra",
    "extra_json",
}


def _record_from_row(row) -> dict[str, Any] | None:
    if row is None:
        return None
    extra = decode_json(row["extra_json"])
    record = dict(extra) if isinstance(extra, dict) else {}
    record.update(
        {
            "tag": row["tag"],
            "country": row["country"] or "",
            "label": row["label"] or "",
            "created": row["created_at"] or "",
            "updated": row["updated_at"] or "",
        }
    )
    return record


def upsert_route(connection: sqlite3.Connection, tag: str, record: dict[str, Any]) -> None:
    extra = record.get("extra")
    if not isinstance(extra, dict):
        extra = without_keys(record, _KNOWN_KEYS)
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO cascade_routes(tag, country, label, created_at, updated_at, extra_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tag) DO UPDATE SET
                country = excluded.country,
                label = excluded.label,
                updated_at = excluded.updated_at,
                extra_json = excluded.extra_json
            """,
            (
                tag,
                str(record.get("country") or ""),
                str(record.get("label") or ""),
                record.get("created") or record.get("createdAt") or record.get("created_at") or "",
                record.get("updated") or record.get("updatedAt") or record.get("updated_at") or "",
                encode_json(extra),
            ),
        )


def get_route(connection: sqlite3.Connection, tag: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM cascade_routes WHERE tag = ?", (tag,)).fetchone()
    return _record_from_row(row)


def list_routes(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute("SELECT * FROM cascade_routes ORDER BY tag").fetchall()
    return {row["tag"]: _record_from_row(row) for row in rows}


def delete_route(connection: sqlite3.Connection, tag: str) -> bool:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM cascade_routes WHERE tag = ?", (tag,))
        connection.execute(
            "UPDATE clients SET selected_cascade_tag = NULL WHERE selected_cascade_tag = ?",
            (tag,),
        )
    return result.rowcount > 0
