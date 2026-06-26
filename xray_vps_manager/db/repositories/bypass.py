"""SQLite repository for GeoIP bypass route metadata."""

from __future__ import annotations

import sqlite3
from typing import Any

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories.base import decode_json, encode_json, without_keys

_KNOWN_KEYS = {
    "tag",
    "name",
    "regionCode",
    "region_code",
    "regionLabel",
    "region_label",
    "label",
    "enabled",
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
            "name": row["name"] or "",
            "regionCode": row["region_code"] or "",
            "regionLabel": row["region_label"] or "",
            "label": row["label"] or "",
            "enabled": bool(row["enabled"]),
            "created": row["created_at"] or "",
            "updated": row["updated_at"] or "",
        }
    )
    return record


def upsert_route(connection: sqlite3.Connection, tag: str, record: dict[str, Any]) -> None:
    extra = record.get("extra")
    if not isinstance(extra, dict):
        extra = without_keys(record, _KNOWN_KEYS)
    region_code = str(record.get("regionCode") or record.get("region_code") or "").upper()
    region_label = str(record.get("regionLabel") or record.get("region_label") or "")
    created = record.get("created") or record.get("createdAt") or record.get("created_at") or ""
    updated = record.get("updated") or record.get("updatedAt") or record.get("updated_at") or ""
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO bypass_routes(
                tag, name, region_code, region_label, label, enabled, created_at, updated_at, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tag) DO UPDATE SET
                name = excluded.name,
                region_code = excluded.region_code,
                region_label = excluded.region_label,
                label = excluded.label,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at,
                extra_json = excluded.extra_json
            """,
            (
                tag,
                str(record.get("name") or ""),
                region_code,
                region_label,
                str(record.get("label") or ""),
                1 if record.get("enabled") is True else 0,
                created,
                updated,
                encode_json(extra),
            ),
        )


def get_route(connection: sqlite3.Connection, tag: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM bypass_routes WHERE tag = ?", (tag,)).fetchone()
    return _record_from_row(row)


def list_routes(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute("SELECT * FROM bypass_routes ORDER BY tag").fetchall()
    return {row["tag"]: _record_from_row(row) for row in rows}


def list_enabled_routes(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute("SELECT * FROM bypass_routes WHERE enabled = 1 ORDER BY tag").fetchall()
    return {row["tag"]: _record_from_row(row) for row in rows}


def active_route_for_region(connection: sqlite3.Connection, region_code: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM bypass_routes WHERE region_code = ? AND enabled = 1",
        (str(region_code or "").upper(),),
    ).fetchone()
    return _record_from_row(row)


def set_enabled(connection: sqlite3.Connection, tag: str, enabled: bool, updated_at: str = "") -> bool:
    with database.transaction(connection):
        result = connection.execute(
            """
            UPDATE bypass_routes
            SET enabled = ?, updated_at = ?
            WHERE tag = ?
            """,
            (1 if enabled else 0, updated_at, tag),
        )
    return result.rowcount > 0


def delete_route(connection: sqlite3.Connection, tag: str) -> bool:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM bypass_routes WHERE tag = ?", (tag,))
    return result.rowcount > 0
