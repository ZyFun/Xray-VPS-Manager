"""SQLite repository for Reality connection records."""

from __future__ import annotations

import sqlite3
from typing import Any

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories.base import decode_json, encode_json, without_keys

_KNOWN_KEYS = {
    "tag",
    "name",
    "port",
    "sni",
    "dest",
    "fingerprint",
    "publicKey",
    "public_key",
    "shortId",
    "short_id",
    "created",
    "created_at",
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
            "name": row["name"],
            "created": row["created_at"] or "",
            "port": int(row["port"]),
            "sni": row["sni"],
            "dest": row["dest"],
            "fingerprint": row["fingerprint"],
            "publicKey": row["public_key"] or "",
            "shortId": row["short_id"] or "",
        }
    )
    return record


def upsert_connection(connection: sqlite3.Connection, tag: str, record: dict[str, Any]) -> None:
    extra = record.get("extra")
    if not isinstance(extra, dict):
        extra = without_keys(record, _KNOWN_KEYS)
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO reality_connections(
                tag, name, port, sni, dest, fingerprint, public_key, short_id, created_at, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tag) DO UPDATE SET
                name = excluded.name,
                port = excluded.port,
                sni = excluded.sni,
                dest = excluded.dest,
                fingerprint = excluded.fingerprint,
                public_key = excluded.public_key,
                short_id = excluded.short_id,
                created_at = excluded.created_at,
                extra_json = excluded.extra_json
            """,
            (
                tag,
                str(record.get("name") or tag),
                int(record.get("port") or 0),
                str(record.get("sni") or ""),
                str(record.get("dest") or ""),
                str(record.get("fingerprint") or ""),
                record.get("publicKey") or record.get("public_key") or "",
                record.get("shortId") or record.get("short_id") or "",
                record.get("created") or record.get("created_at") or "",
                encode_json(extra),
            ),
        )


def get_connection(connection: sqlite3.Connection, tag: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM reality_connections WHERE tag = ?", (tag,)).fetchone()
    return _record_from_row(row)


def list_connections(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute("SELECT * FROM reality_connections ORDER BY tag").fetchall()
    return {row["tag"]: _record_from_row(row) for row in rows}


def delete_connection(connection: sqlite3.Connection, tag: str) -> bool:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM reality_connections WHERE tag = ?", (tag,))
    return result.rowcount > 0
