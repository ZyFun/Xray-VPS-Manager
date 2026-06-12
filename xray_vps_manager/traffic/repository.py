"""Traffic JSON storage helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xray_vps_manager.core.json_store import load_json, save_json
from xray_vps_manager.core.paths import TRAFFIC_PATH
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import traffic as sqlite_traffic
from xray_vps_manager.db.storage import sqlite_read_ready, sqlite_reads_enabled


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
    if sqlite_reads_enabled() and database.database_file_exists(db_path):
        try:
            connection = database.open_database(db_path)
            try:
                if not sqlite_read_ready(connection):
                    return TrafficDbReadResult(load_traffic_db(path), "json")
                return TrafficDbReadResult(load_traffic_db_from_sqlite(connection), "sqlite")
            finally:
                connection.close()
        except Exception:
            pass
    return TrafficDbReadResult(load_traffic_db(path), "json")


def load_traffic_db_from_sqlite(connection) -> dict[str, Any]:
    return {"clients": sqlite_traffic.list_traffic_entries(connection)}


def save_traffic_db(db: dict[str, Any], path: Path = TRAFFIC_PATH) -> None:
    save_json(path, db, mode=0o640, group_xray=True)


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


def remove_traffic_clients(names: list[str] | tuple[str, ...] | set[str], path: Path = TRAFFIC_PATH) -> bool:
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
