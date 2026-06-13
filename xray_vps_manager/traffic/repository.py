"""Traffic JSON storage helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xray_vps_manager.core.json_store import load_json, save_json
from xray_vps_manager.core.paths import TRAFFIC_PATH
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import traffic as sqlite_traffic
from xray_vps_manager.db.storage import (
    SQLiteReadUnavailable,
    sqlite_read_ready,
    sqlite_reads_enabled,
    sqlite_writes_enabled,
)


@dataclass(frozen=True)
class TrafficDbReadResult:
    db: dict[str, Any]
    source: str


def default_db() -> dict[str, Any]:
    return {"clients": {}}


def load_traffic_db(path: Path = TRAFFIC_PATH) -> dict[str, Any]:
    db = load_json(path, default_db())
    return db if isinstance(db, dict) else default_db()


def load_traffic_db_for_read(
    path: Path = TRAFFIC_PATH,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    return load_traffic_db_for_read_result(path, db_path=db_path).db


def load_traffic_db_for_read_result(
    path: Path = TRAFFIC_PATH,
    *,
    db_path: str | Path | None = None,
) -> TrafficDbReadResult:
    if sqlite_reads_enabled():
        if not database.database_file_exists(db_path):
            raise SQLiteReadUnavailable("SQLite reads are enabled but manager database is missing.")
        connection = None
        try:
            connection = database.open_database(db_path)
            if not sqlite_read_ready(connection):
                raise SQLiteReadUnavailable("SQLite reads are enabled but JSON import is not marked ready.")
            return TrafficDbReadResult(load_traffic_db_from_sqlite(connection), "sqlite")
        except SQLiteReadUnavailable:
            raise
        except Exception as exc:
            raise SQLiteReadUnavailable(f"SQLite reads are enabled but traffic cannot be read: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()
    return TrafficDbReadResult(load_traffic_db(path), "json")


def load_traffic_db_from_sqlite(connection) -> dict[str, Any]:
    db = {"clients": sqlite_traffic.list_traffic_entries(connection)}
    access_log_state = sqlite_traffic.get_access_log_state(connection)
    if access_log_state:
        db["accessLog"] = access_log_state
    return db


def save_traffic_db(
    db: dict[str, Any],
    path: Path = TRAFFIC_PATH,
    *,
    db_path: str | Path | None = None,
) -> None:
    if sqlite_writes_enabled() and sqlite_reads_enabled():
        write_traffic_db_to_sqlite_for_write(db, db_path=db_path, strict=True)
        return
    save_json(path, db, mode=0o640, group_xray=True)
    mirror_traffic_db_to_sqlite_for_write(db, db_path=db_path)


def traffic_clients(db: dict | None) -> dict:
    if not isinstance(db, dict):
        return {}
    clients = db.get("clients", {})
    return clients if isinstance(clients, dict) else {}


def traffic_entry(db: dict | None, name: str) -> dict:
    entry = traffic_clients(db).get(name, {})
    return entry if isinstance(entry, dict) else {}


def ensure_entry(entries: dict, name: str, email: str) -> dict:
    entry = entries.setdefault(
        name,
        {
            "email": email,
            "incoming": 0,
            "outgoing": 0,
            "last": {},
            "history": {},
        },
    )
    entry["email"] = email
    entry.setdefault("history", {})
    return entry


def remove_traffic_clients(
    names: list[str] | tuple[str, ...] | set[str],
    path: Path = TRAFFIC_PATH,
    *,
    db_path: str | Path | None = None,
) -> bool:
    if sqlite_writes_enabled() and sqlite_reads_enabled():
        return remove_traffic_clients_from_sqlite_for_write(names, db_path=db_path, strict=True)

    db = load_traffic_db(path)
    clients = db.setdefault("clients", {})
    changed = False
    for name in names:
        if name in clients:
            clients.pop(name, None)
            changed = True
    if changed:
        save_traffic_db(db, path)
    return changed


def mirror_traffic_db_to_sqlite_for_write(
    db: dict[str, Any],
    *,
    db_path: str | Path | None = None,
) -> bool:
    return write_traffic_db_to_sqlite_for_write(db, db_path=db_path, strict=False)


def write_traffic_db_to_sqlite_for_write(
    db: dict[str, Any],
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> bool:
    if not sqlite_writes_enabled() or not database.database_file_exists(db_path):
        if strict:
            raise RuntimeError("SQLite writes are enabled but manager database is missing")
        return False

    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            if strict:
                raise RuntimeError("SQLite writes are enabled but JSON import is not marked ready")
            return False

        entries = traffic_clients(db)
        known_clients = set(sqlite_clients.list_clients(connection))
        desired_clients = {str(name) for name, entry in entries.items() if isinstance(entry, dict)}
        mirrorable_clients = desired_clients & known_clients

        with database.transaction(connection):
            for name in sorted(mirrorable_clients):
                sqlite_traffic.upsert_traffic_entry(connection, name, entries[name])
            sqlite_traffic.upsert_access_log_state(connection, db.get("accessLog"))

            current_clients = set(sqlite_traffic.list_traffic_entries(connection))
            stale_clients = current_clients - mirrorable_clients
            if stale_clients:
                sqlite_traffic.remove_traffic_clients(connection, stale_clients)
        return True
    except Exception:
        if strict:
            raise
        return False
    finally:
        if connection is not None:
            connection.close()


def remove_traffic_clients_from_sqlite_for_write(
    names: list[str] | tuple[str, ...] | set[str],
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> bool:
    if not sqlite_writes_enabled() or not database.database_file_exists(db_path):
        if strict:
            raise RuntimeError("SQLite writes are enabled but manager database is missing")
        return False

    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            if strict:
                raise RuntimeError("SQLite writes are enabled but JSON import is not marked ready")
            return False
        return sqlite_traffic.remove_traffic_clients(connection, names)
    except Exception:
        if strict:
            raise
        return False
    finally:
        if connection is not None:
            connection.close()
