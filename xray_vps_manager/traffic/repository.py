"""Traffic JSON storage helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xray_vps_manager.core.json_store import load_json, save_json
from xray_vps_manager.core.paths import TRAFFIC_PATH


def default_db() -> dict[str, Any]:
    return {"clients": {}}


def load_traffic_db(path: Path = TRAFFIC_PATH) -> dict[str, Any]:
    db = load_json(path, default_db())
    return db if isinstance(db, dict) else default_db()


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
