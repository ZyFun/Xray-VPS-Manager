"""SQLite repository for activity events and suspicious exceptions."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories.base import decode_json, encode_json


def add_event(connection: sqlite3.Connection, event: dict[str, Any]) -> int:
    risks = event.get("risks") if isinstance(event.get("risks"), list) else []
    with database.transaction(connection):
        cursor = connection.execute(
            """
            INSERT INTO activity_events(
                event_time, client_name, email, connection_tag, source, status, network,
                target, host, port, inbound, outbound, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("time") or event.get("event_time") or "",
                event.get("client") or event.get("client_name") or "",
                event.get("email") or "",
                event.get("connection") or event.get("connection_tag") or "",
                event.get("source") or "",
                event.get("status") or "",
                event.get("network") or "",
                event.get("target") or "",
                event.get("host") or "",
                int(event.get("port") or 0) if str(event.get("port") or "").isdigit() else None,
                event.get("inbound") or "",
                event.get("outbound") or "",
                encode_json(event),
            ),
        )
        event_id = int(cursor.lastrowid)
        for risk in risks:
            connection.execute(
                "INSERT OR IGNORE INTO activity_event_risks(event_id, risk) VALUES (?, ?)",
                (event_id, str(risk)),
            )
    return event_id


def _event_from_row(connection: sqlite3.Connection, row) -> dict[str, Any]:
    raw = decode_json(row["raw_json"])
    event = dict(raw) if isinstance(raw, dict) else {}
    event.update(
        {
            "id": int(row["id"]),
            "time": row["event_time"],
            "client": row["client_name"],
            "email": row["email"] or "",
            "connection": row["connection_tag"] or "",
            "source": row["source"] or "",
            "status": row["status"] or "",
            "network": row["network"] or "",
            "target": row["target"] or "",
            "host": row["host"] or "",
            "port": str(row["port"] or ""),
            "inbound": row["inbound"] or "",
            "outbound": row["outbound"] or "",
            "risks": event_risks(connection, int(row["id"])),
        }
    )
    return event


def event_risks(connection: sqlite3.Connection, event_id: int) -> list[str]:
    rows = connection.execute(
        "SELECT risk FROM activity_event_risks WHERE event_id = ? ORDER BY risk",
        (event_id,),
    ).fetchall()
    return [row["risk"] for row in rows]


def iter_events(
    connection: sqlite3.Connection,
    *,
    client_name: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> Iterable[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if client_name:
        clauses.append("client_name = ?")
        params.append(client_name)
    if start:
        clauses.append("event_time >= ?")
        params.append(start)
    if end:
        clauses.append("event_time < ?")
        params.append(end)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM activity_events{where} ORDER BY event_time, id",
        params,
    ).fetchall()
    for row in rows:
        yield _event_from_row(connection, row)


def upsert_exception(connection: sqlite3.Connection, item: dict[str, Any]) -> None:
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO activity_exceptions(value, kind, source, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(value) DO UPDATE SET
                kind = excluded.kind,
                source = excluded.source,
                created_at = excluded.created_at
            """,
            (
                item.get("value") or "",
                item.get("kind") or "domain",
                item.get("source") or "manual",
                item.get("createdAt") or item.get("created_at") or "",
            ),
        )


def list_exceptions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT value, kind, source, created_at FROM activity_exceptions ORDER BY value"
    ).fetchall()
    return [
        {
            "value": row["value"],
            "kind": row["kind"],
            "source": row["source"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def delete_exception(connection: sqlite3.Connection, value: str) -> bool:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM activity_exceptions WHERE value = ?", (value,))
    return result.rowcount > 0


def clear_exceptions(connection: sqlite3.Connection) -> int:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM activity_exceptions")
    return int(result.rowcount or 0)
