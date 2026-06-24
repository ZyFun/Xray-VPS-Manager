"""Traffic storage helpers backed by SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import traffic as sqlite_traffic
from xray_vps_manager.db.storage import (
    SQLiteReadUnavailable,
    sqlite_read_ready,
)


@dataclass(frozen=True)
class TrafficDbReadResult:
    db: dict[str, Any]
    source: str


def default_db() -> dict[str, Any]:
    return {"clients": {}, "credentials": {}}


def load_traffic_db(path: Path | None = None) -> dict[str, Any]:
    return load_traffic_db_for_read(path)


def load_traffic_db_for_read(
    path: Path | None = None,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    return load_traffic_db_for_read_result(path, db_path=db_path).db


def load_traffic_db_for_read_result(
    path: Path | None = None,
    *,
    db_path: str | Path | None = None,
) -> TrafficDbReadResult:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        return TrafficDbReadResult(load_traffic_db_from_sqlite(connection), "sqlite")
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite traffic cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def load_traffic_db_from_sqlite(connection) -> dict[str, Any]:
    db = {
        "clients": sqlite_traffic.list_traffic_entries(connection),
        "credentials": sqlite_traffic.list_credential_traffic_entries(connection),
    }
    access_log_state = sqlite_traffic.get_access_log_state(connection)
    if access_log_state:
        db["accessLog"] = access_log_state
    return db


def save_traffic_db(
    db: dict[str, Any],
    path: Path | None = None,
    *,
    db_path: str | Path | None = None,
) -> None:
    write_traffic_db_to_sqlite_for_write(db, db_path=db_path, strict=True)


def traffic_clients(db: dict | None) -> dict:
    if not isinstance(db, dict):
        return {}
    clients = db.get("clients", {})
    return clients if isinstance(clients, dict) else {}


def traffic_entry(db: dict | None, name: str) -> dict:
    entry = traffic_clients(db).get(name, {})
    return entry if isinstance(entry, dict) else {}


def traffic_credentials(db: dict | None) -> dict:
    if not isinstance(db, dict):
        return {}
    credentials = db.get("credentials", {})
    return credentials if isinstance(credentials, dict) else {}


def credential_traffic_entries(db: dict | None, name: str) -> dict:
    entries = traffic_credentials(db).get(name, {})
    return entries if isinstance(entries, dict) else {}


def credential_traffic_entry(db: dict | None, name: str, connection_tag: str) -> dict:
    entry = credential_traffic_entries(db, name).get(connection_tag, {})
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


def ensure_credential_entry(entries: dict, name: str, connection_tag: str, email: str) -> dict:
    client_credentials = entries.setdefault(name, {})
    if not isinstance(client_credentials, dict):
        client_credentials = {}
        entries[name] = client_credentials
    entry = client_credentials.setdefault(
        connection_tag,
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
    path: Path | None = None,
    *,
    db_path: str | Path | None = None,
) -> bool:
    return remove_traffic_clients_from_sqlite_for_write(names, db_path=db_path, strict=True)


def write_traffic_db_to_sqlite_for_write(
    db: dict[str, Any],
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> bool:
    if not database.database_file_exists(db_path):
        if strict:
            raise RuntimeError("SQLite manager database is missing")
        return False

    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            if strict:
                raise RuntimeError("SQLite database is not marked ready")
            return False

        entries = traffic_clients(db)
        credential_entries = traffic_credentials(db)
        known_clients = set(sqlite_clients.list_clients(connection))
        desired_clients = {str(name) for name, entry in entries.items() if isinstance(entry, dict)}
        mirrorable_clients = desired_clients & known_clients

        with database.transaction(connection):
            for name in sorted(mirrorable_clients):
                sqlite_traffic.upsert_traffic_entry(connection, name, entries[name])
            if "credentials" in db:
                known_credentials = {
                    client_name: set(sqlite_clients.list_client_credentials(connection, client_name))
                    for client_name in known_clients
                }
                for name, credentials in credential_entries.items():
                    if name not in known_credentials or not isinstance(credentials, dict):
                        continue
                    for connection_tag, entry in credentials.items():
                        if isinstance(entry, dict) and str(connection_tag) in known_credentials[name]:
                            sqlite_traffic.upsert_credential_traffic_entry(
                                connection,
                                str(name),
                                str(connection_tag),
                                entry,
                            )
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
    if not database.database_file_exists(db_path):
        if strict:
            raise RuntimeError("SQLite manager database is missing")
        return False

    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            if strict:
                raise RuntimeError("SQLite database is not marked ready")
            return False
        return sqlite_traffic.remove_traffic_clients(connection, names)
    except Exception:
        if strict:
            raise
        return False
    finally:
        if connection is not None:
            connection.close()
