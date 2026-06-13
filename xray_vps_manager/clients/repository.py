"""Repository facade for client and Reality connection state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xray_vps_manager.clients.payments import normalize_payment_type
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.storage import (
    SQLiteReadUnavailable,
    sqlite_read_ready,
)


@dataclass(frozen=True)
class ClientDbReadResult:
    db: dict[str, Any]
    source: str


def db_clients(db: dict[str, Any]) -> dict[str, Any]:
    return db.setdefault("clients", {})


def db_connections(db: dict[str, Any]) -> dict[str, Any]:
    return db.setdefault("connections", {})


def normalize_client_defaults(db: dict[str, Any]) -> dict[str, Any]:
    for entry in db_clients(db).values():
        if isinstance(entry, dict):
            entry["paymentType"] = normalize_payment_type(entry.get("paymentType", "free"))
    return db


def load_db(path: Path | None = None, *, db_path: str | Path | None = None) -> dict[str, Any]:
    return load_db_sql(path, db_path=db_path)


def save_db(
    db: dict[str, Any],
    path: Path | None = None,
    *,
    db_path: str | Path | None = None,
) -> None:
    db = normalize_client_defaults(db)
    write_db_to_sqlite_for_write(db, db_path=db_path, strict=True)


def load_db_sql(
    path: Path | None = None,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    return load_db_sql_result(path, db_path=db_path).db


def load_db_sql_result(
    path: Path | None = None,
    *,
    db_path: str | Path | None = None,
) -> ClientDbReadResult:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        return ClientDbReadResult(load_db_from_sqlite(connection), "sqlite")
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite clients cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def load_db_from_sqlite(connection) -> dict[str, Any]:
    return normalize_client_defaults(
        {
            "connections": sqlite_connections.list_connections(connection),
            "clients": sqlite_clients.list_clients(connection),
        }
    )


def write_db_to_sqlite_for_write(
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

        connections = db_connections(db)
        clients = db_clients(db)
        with database.transaction(connection):
            for tag, record in connections.items():
                if isinstance(record, dict):
                    sqlite_connections.upsert_connection(connection, str(tag), record)
            for name, entry in clients.items():
                if isinstance(entry, dict):
                    sqlite_clients.upsert_client(connection, str(name), entry)

            desired_clients = {str(name) for name, entry in clients.items() if isinstance(entry, dict)}
            for name in set(sqlite_clients.list_clients(connection)) - desired_clients:
                sqlite_clients.delete_client(connection, name)

            desired_connections = {str(tag) for tag, record in connections.items() if isinstance(record, dict)}
            for tag in set(sqlite_connections.list_connections(connection)) - desired_connections:
                sqlite_connections.delete_connection(connection, tag)
        return True
    except Exception:
        if strict:
            raise
        return False
    finally:
        if connection is not None:
            connection.close()
