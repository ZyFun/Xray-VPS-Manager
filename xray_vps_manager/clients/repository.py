"""Repository for clients.json."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
from pathlib import Path
from typing import Any

from xray_vps_manager.clients.payments import normalize_payment_type
from xray_vps_manager.core.paths import CLIENT_DB_PATH
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.storage import sqlite_read_ready, sqlite_reads_enabled


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


def load_db(path: Path = CLIENT_DB_PATH) -> dict[str, Any]:
    if path.exists():
        db = json.loads(path.read_text())
    else:
        db = {"clients": {}}
    return normalize_client_defaults(db)


def save_db(db: dict[str, Any], path: Path = CLIENT_DB_PATH) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(path)


def load_db_for_read(
    path: Path = CLIENT_DB_PATH,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    return load_db_for_read_result(path, db_path=db_path).db


def load_db_for_read_result(
    path: Path = CLIENT_DB_PATH,
    *,
    db_path: str | Path | None = None,
) -> ClientDbReadResult:
    if sqlite_reads_enabled() and database.database_file_exists(db_path):
        try:
            connection = database.open_database(db_path)
            try:
                if not sqlite_read_ready(connection):
                    return ClientDbReadResult(load_db(path), "json")
                return ClientDbReadResult(load_db_from_sqlite(connection), "sqlite")
            finally:
                connection.close()
        except Exception:
            pass
    return ClientDbReadResult(load_db(path), "json")


def load_db_from_sqlite(connection) -> dict[str, Any]:
    return normalize_client_defaults(
        {
            "connections": sqlite_connections.list_connections(connection),
            "clients": sqlite_clients.list_clients(connection),
        }
    )
