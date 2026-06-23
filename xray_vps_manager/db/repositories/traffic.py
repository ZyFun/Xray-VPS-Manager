"""SQLite repository for traffic totals and hourly history."""

from __future__ import annotations

import sqlite3
from typing import Any

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories.base import decode_json, encode_json

TRAFFIC_ACCESS_LOG_OFFSET = "traffic-access-log"


def upsert_traffic_entry(connection: sqlite3.Connection, name: str, entry: dict[str, Any]) -> None:
    last = entry.get("last") if isinstance(entry.get("last"), dict) else {}
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO traffic_totals(
                client_name, email, incoming_bytes, outgoing_bytes, last_runtime_uplink,
                last_runtime_downlink, last_online_at, last_online_source, last_accepted_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_name) DO UPDATE SET
                email = excluded.email,
                incoming_bytes = excluded.incoming_bytes,
                outgoing_bytes = excluded.outgoing_bytes,
                last_runtime_uplink = excluded.last_runtime_uplink,
                last_runtime_downlink = excluded.last_runtime_downlink,
                last_online_at = excluded.last_online_at,
                last_online_source = excluded.last_online_source,
                last_accepted_at = excluded.last_accepted_at,
                updated_at = excluded.updated_at
            """,
            (
                name,
                entry.get("email") or name,
                int(entry.get("incoming") or 0),
                int(entry.get("outgoing") or 0),
                last.get("uplink"),
                last.get("downlink"),
                entry.get("lastOnline") or None,
                entry.get("lastOnlineSource") or None,
                entry.get("lastAccepted") or None,
                entry.get("updated") or None,
            ),
        )
        sync_history(connection, name, entry)


def upsert_credential_traffic_entry(
    connection: sqlite3.Connection,
    name: str,
    connection_tag: str,
    entry: dict[str, Any],
) -> None:
    last = entry.get("last") if isinstance(entry.get("last"), dict) else {}
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO credential_traffic_totals(
                client_name, connection_tag, email, incoming_bytes, outgoing_bytes, last_runtime_uplink,
                last_runtime_downlink, last_online_at, last_online_source, last_accepted_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_name, connection_tag) DO UPDATE SET
                email = excluded.email,
                incoming_bytes = excluded.incoming_bytes,
                outgoing_bytes = excluded.outgoing_bytes,
                last_runtime_uplink = excluded.last_runtime_uplink,
                last_runtime_downlink = excluded.last_runtime_downlink,
                last_online_at = excluded.last_online_at,
                last_online_source = excluded.last_online_source,
                last_accepted_at = excluded.last_accepted_at,
                updated_at = excluded.updated_at
            """,
            (
                name,
                connection_tag,
                entry.get("email") or name,
                int(entry.get("incoming") or 0),
                int(entry.get("outgoing") or 0),
                last.get("uplink"),
                last.get("downlink"),
                entry.get("lastOnline") or None,
                entry.get("lastOnlineSource") or None,
                entry.get("lastAccepted") or None,
                entry.get("updated") or None,
            ),
        )
        sync_credential_history(connection, name, connection_tag, entry)


def get_traffic_entry(connection: sqlite3.Connection, name: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM traffic_totals WHERE client_name = ?", (name,)).fetchone()
    if row is None:
        return {}
    entry = {
        "email": row["email"] or name,
        "incoming": int(row["incoming_bytes"]),
        "outgoing": int(row["outgoing_bytes"]),
        "last": {
            "uplink": int(row["last_runtime_uplink"] or 0),
            "downlink": int(row["last_runtime_downlink"] or 0),
        },
        "history": history_for_client(connection, name),
    }
    for db_key, json_key in (
        ("last_online_at", "lastOnline"),
        ("last_online_source", "lastOnlineSource"),
        ("last_accepted_at", "lastAccepted"),
        ("updated_at", "updated"),
    ):
        if row[db_key]:
            entry[json_key] = row[db_key]
    return entry


def _traffic_entry_from_row(connection: sqlite3.Connection, row, *, credential: bool = False) -> dict[str, Any]:
    name = row["client_name"]
    connection_tag = row["connection_tag"] if credential else ""
    entry = {
        "email": row["email"] or name,
        "incoming": int(row["incoming_bytes"]),
        "outgoing": int(row["outgoing_bytes"]),
        "last": {
            "uplink": int(row["last_runtime_uplink"] or 0),
            "downlink": int(row["last_runtime_downlink"] or 0),
        },
        "history": (
            credential_history_for_client(connection, name, connection_tag)
            if credential
            else history_for_client(connection, name)
        ),
    }
    for db_key, json_key in (
        ("last_online_at", "lastOnline"),
        ("last_online_source", "lastOnlineSource"),
        ("last_accepted_at", "lastAccepted"),
        ("updated_at", "updated"),
    ):
        if row[db_key]:
            entry[json_key] = row[db_key]
    return entry


def list_traffic_entries(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute("SELECT client_name FROM traffic_totals ORDER BY client_name").fetchall()
    return {row["client_name"]: get_traffic_entry(connection, row["client_name"]) for row in rows}


def get_credential_traffic_entry(connection: sqlite3.Connection, name: str, connection_tag: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT *
        FROM credential_traffic_totals
        WHERE client_name = ? AND connection_tag = ?
        """,
        (name, connection_tag),
    ).fetchone()
    if row is None:
        return {}
    return _traffic_entry_from_row(connection, row, credential=True)


def list_credential_traffic_entries(connection: sqlite3.Connection) -> dict[str, dict[str, dict[str, Any]]]:
    rows = connection.execute(
        "SELECT client_name, connection_tag FROM credential_traffic_totals ORDER BY client_name, connection_tag"
    ).fetchall()
    entries: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        entries.setdefault(row["client_name"], {})[row["connection_tag"]] = get_credential_traffic_entry(
            connection,
            row["client_name"],
            row["connection_tag"],
        )
    return entries


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
                TRAFFIC_ACCESS_LOG_OFFSET,
                state.get("path") or "",
                state.get("inode"),
                int(state.get("offset") or 0),
                state.get("updated") or state.get("updated_at") or "",
            ),
        )


def get_access_log_state(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        "SELECT path, inode, offset, updated_at FROM file_offsets WHERE name = ?",
        (TRAFFIC_ACCESS_LOG_OFFSET,),
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


def replace_history(connection: sqlite3.Connection, name: str, history: dict[str, Any]) -> None:
    with database.transaction(connection):
        connection.execute("DELETE FROM traffic_history WHERE client_name = ?", (name,))
        for day_key, hours in history.items():
            if not isinstance(hours, dict):
                continue
            for hour_key, bucket in hours.items():
                if not isinstance(bucket, dict):
                    continue
                connection.execute(
                    """
                    INSERT INTO traffic_history(
                        client_name, bucket_date, bucket_hour, incoming_bytes, outgoing_bytes
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        str(day_key),
                        int(hour_key),
                        int(bucket.get("incoming") or 0),
                        int(bucket.get("outgoing") or 0),
                    ),
                )


def replace_credential_history(
    connection: sqlite3.Connection,
    name: str,
    connection_tag: str,
    history: dict[str, Any],
) -> None:
    with database.transaction(connection):
        connection.execute(
            "DELETE FROM credential_traffic_history WHERE client_name = ? AND connection_tag = ?",
            (name, connection_tag),
        )
        for day_key, hours in history.items():
            if not isinstance(hours, dict):
                continue
            for hour_key, bucket in hours.items():
                if not isinstance(bucket, dict):
                    continue
                connection.execute(
                    """
                    INSERT INTO credential_traffic_history(
                        client_name, connection_tag, bucket_date, bucket_hour, incoming_bytes, outgoing_bytes
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        connection_tag,
                        str(day_key),
                        int(hour_key),
                        int(bucket.get("incoming") or 0),
                        int(bucket.get("outgoing") or 0),
                    ),
                )


def history_bucket_count(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS count FROM traffic_history WHERE client_name = ?",
        (name,),
    ).fetchone()
    return int(row["count"] if row else 0)


def credential_history_bucket_count(connection: sqlite3.Connection, name: str, connection_tag: str) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM credential_traffic_history
        WHERE client_name = ? AND connection_tag = ?
        """,
        (name, connection_tag),
    ).fetchone()
    return int(row["count"] if row else 0)


def sync_history(connection: sqlite3.Connection, name: str, entry: dict[str, Any]) -> None:
    history = entry.get("history") if isinstance(entry.get("history"), dict) else {}
    has_total = int(entry.get("incoming") or 0) > 0 or int(entry.get("outgoing") or 0) > 0
    if not history and has_total and history_bucket_count(connection, name) > 0:
        return
    replace_history(connection, name, history)


def sync_credential_history(
    connection: sqlite3.Connection,
    name: str,
    connection_tag: str,
    entry: dict[str, Any],
) -> None:
    history = entry.get("history") if isinstance(entry.get("history"), dict) else {}
    has_total = int(entry.get("incoming") or 0) > 0 or int(entry.get("outgoing") or 0) > 0
    if not history and has_total and credential_history_bucket_count(connection, name, connection_tag) > 0:
        return
    replace_credential_history(connection, name, connection_tag, history)


def add_history_delta(
    connection: sqlite3.Connection,
    name: str,
    day_key: str,
    hour: int,
    incoming: int,
    outgoing: int,
) -> None:
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO traffic_history(client_name, bucket_date, bucket_hour, incoming_bytes, outgoing_bytes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(client_name, bucket_date, bucket_hour) DO UPDATE SET
                incoming_bytes = incoming_bytes + excluded.incoming_bytes,
                outgoing_bytes = outgoing_bytes + excluded.outgoing_bytes
            """,
            (name, day_key, int(hour), int(incoming), int(outgoing)),
        )


def add_credential_history_delta(
    connection: sqlite3.Connection,
    name: str,
    connection_tag: str,
    day_key: str,
    hour: int,
    incoming: int,
    outgoing: int,
) -> None:
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO credential_traffic_history(
                client_name, connection_tag, bucket_date, bucket_hour, incoming_bytes, outgoing_bytes
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_name, connection_tag, bucket_date, bucket_hour) DO UPDATE SET
                incoming_bytes = incoming_bytes + excluded.incoming_bytes,
                outgoing_bytes = outgoing_bytes + excluded.outgoing_bytes
            """,
            (name, connection_tag, day_key, int(hour), int(incoming), int(outgoing)),
        )


def history_for_client(connection: sqlite3.Connection, name: str) -> dict[str, dict[str, dict[str, int]]]:
    rows = connection.execute(
        """
        SELECT bucket_date, bucket_hour, incoming_bytes, outgoing_bytes
        FROM traffic_history
        WHERE client_name = ?
        ORDER BY bucket_date, bucket_hour
        """,
        (name,),
    ).fetchall()
    history: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        day = history.setdefault(row["bucket_date"], {})
        day[f"{int(row['bucket_hour']):02d}"] = {
            "incoming": int(row["incoming_bytes"]),
            "outgoing": int(row["outgoing_bytes"]),
        }
    return history


def credential_history_for_client(
    connection: sqlite3.Connection,
    name: str,
    connection_tag: str,
) -> dict[str, dict[str, dict[str, int]]]:
    rows = connection.execute(
        """
        SELECT bucket_date, bucket_hour, incoming_bytes, outgoing_bytes
        FROM credential_traffic_history
        WHERE client_name = ? AND connection_tag = ?
        ORDER BY bucket_date, bucket_hour
        """,
        (name, connection_tag),
    ).fetchall()
    history: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        day = history.setdefault(row["bucket_date"], {})
        day[f"{int(row['bucket_hour']):02d}"] = {
            "incoming": int(row["incoming_bytes"]),
            "outgoing": int(row["outgoing_bytes"]),
        }
    return history


def remove_traffic_clients(connection: sqlite3.Connection, names: list[str] | tuple[str, ...] | set[str]) -> bool:
    changed = False
    with database.transaction(connection):
        for name in names:
            result = connection.execute("DELETE FROM traffic_totals WHERE client_name = ?", (name,))
            connection.execute("DELETE FROM traffic_history WHERE client_name = ?", (name,))
            connection.execute("DELETE FROM credential_traffic_totals WHERE client_name = ?", (name,))
            connection.execute("DELETE FROM credential_traffic_history WHERE client_name = ?", (name,))
            changed = changed or result.rowcount > 0
    return changed


def encode_access_state(state: dict[str, Any]) -> str:
    return encode_json(state)


def decode_access_state(value: str | None) -> dict[str, Any]:
    decoded = decode_json(value)
    return decoded if isinstance(decoded, dict) else {}
