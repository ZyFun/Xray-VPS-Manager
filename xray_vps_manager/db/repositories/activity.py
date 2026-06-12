"""SQLite repository for activity events and suspicious exceptions."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories.base import decode_json, encode_json

ACTIVITY_ACCESS_LOG_OFFSET = "activity-access-log"
ACTIVITY_SOURCE_METADATA_KEY = "activity.sourceMetadata"
ACTIVITY_SUMMARY_KEY = "activity.summary"


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


def list_event_clients(
    connection: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[str]:
    clauses = ["client_name != ''"]
    params: list[Any] = []
    if start:
        clauses.append("event_time >= ?")
        params.append(start)
    if end:
        clauses.append("event_time < ?")
        params.append(end)
    rows = connection.execute(
        f"""
        SELECT DISTINCT client_name
        FROM activity_events
        WHERE {" AND ".join(clauses)}
        ORDER BY client_name
        """,
        params,
    ).fetchall()
    return [row["client_name"] for row in rows]


def delete_events_before(connection: sqlite3.Connection, cutoff: str) -> int:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM activity_events WHERE event_time < ?", (cutoff,))
    return int(result.rowcount or 0)


def upsert_access_log_state(connection: sqlite3.Connection, state: dict[str, Any] | None) -> None:
    if not isinstance(state, dict):
        return
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO file_offsets(name, path, inode, offset, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                path = excluded.path,
                inode = excluded.inode,
                offset = excluded.offset,
                updated_at = excluded.updated_at
            """,
            (
                ACTIVITY_ACCESS_LOG_OFFSET,
                state.get("path") or "",
                state.get("inode"),
                int(state.get("offset") or 0),
                state.get("updated") or state.get("updated_at") or "",
            ),
        )


def get_access_log_state(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        "SELECT path, inode, offset, updated_at FROM file_offsets WHERE name = ?",
        (ACTIVITY_ACCESS_LOG_OFFSET,),
    ).fetchone()
    if row is None:
        return {}
    state: dict[str, Any] = {
        "path": row["path"] or "",
        "offset": int(row["offset"] or 0),
    }
    if row["inode"] is not None:
        state["inode"] = int(row["inode"])
    if row["updated_at"]:
        state["updated"] = row["updated_at"]
    return state


def set_source_metadata(connection: sqlite3.Connection, value: dict[str, Any]) -> None:
    set_metadata_json(connection, ACTIVITY_SOURCE_METADATA_KEY, value)


def get_source_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    return get_metadata_json(connection, ACTIVITY_SOURCE_METADATA_KEY)


def set_summary(connection: sqlite3.Connection, value: dict[str, Any]) -> None:
    set_metadata_json(connection, ACTIVITY_SUMMARY_KEY, value)


def get_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    return get_metadata_json(connection, ACTIVITY_SUMMARY_KEY)


def set_metadata_json(connection: sqlite3.Connection, key: str, value: dict[str, Any]) -> None:
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO manager_metadata(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (key, encode_json(value)),
        )


def get_metadata_json(connection: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = connection.execute("SELECT value FROM manager_metadata WHERE key = ?", (key,)).fetchone()
    decoded = decode_json(row["value"] if row else "", {})
    return decoded if isinstance(decoded, dict) else {}


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
