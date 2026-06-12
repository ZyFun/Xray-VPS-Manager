"""Repository for clients.json."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from xray_vps_manager.clients.payments import normalize_payment_type
from xray_vps_manager.core.paths import CLIENT_DB_PATH


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
