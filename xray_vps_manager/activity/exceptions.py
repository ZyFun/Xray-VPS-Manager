"""Suspicious activity exception helpers."""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from xray_vps_manager.activity.constants import ACTIVITY_EXCEPTIONS_PATH, EXCEPTION_VALUE_RE
from xray_vps_manager.activity.parser import parse_target
from xray_vps_manager.activity.repository import chown_xray, ensure_dirs, load_json
from xray_vps_manager.activity.time import utc_stamp
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.storage import sqlite_read_ready, sqlite_reads_enabled, sqlite_writes_enabled


def normalize_exception_value(value: str, fatal: bool = True) -> str:
    def fail(message):
        raise ValueError(message)

    raw = str(value or "").strip()
    if not raw:
        fail("Exception value must not be empty.")

    if raw.startswith(("tcp:", "udp:")):
        _network, host, _port = parse_target(raw)
        raw = host or raw

    if "://" in raw:
        parsed = urlparse(raw)
        raw = parsed.hostname or raw

    raw = raw.strip().strip("[]").strip().lower()
    if not raw:
        fail("Exception value must contain a domain, IP, CIDR, or wildcard mask.")

    if "/" not in raw and raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if port.isdigit():
            raw = host.strip("[]")

    if not EXCEPTION_VALUE_RE.fullmatch(raw):
        fail("Exception may contain only letters, digits, dot, dash, underscore, *, ?, /, and :.")
    return raw


def classify_exception_value(value: str, fatal: bool = True) -> tuple[str, str]:
    normalized = normalize_exception_value(value, fatal=fatal)
    if "/" in normalized:
        try:
            ipaddress.ip_network(normalized, strict=False)
            return normalized, "cidr"
        except ValueError:
            if fatal:
                raise ValueError("CIDR exception must be a valid IP network, for example 203.0.113.0/24.")
            raise
    try:
        ipaddress.ip_address(normalized)
        return normalized, "ip"
    except ValueError:
        pass
    if "*" in normalized or "?" in normalized:
        return normalized, "mask"
    return normalized, "domain"


def load_activity_exceptions(path=ACTIVITY_EXCEPTIONS_PATH) -> dict:
    if sqlite_writes_enabled() and sqlite_reads_enabled():
        return load_activity_exceptions_for_read(path)
    return load_activity_exceptions_json(path)


def load_activity_exceptions_json(path=ACTIVITY_EXCEPTIONS_PATH) -> dict:
    db = load_json(path, {})
    return load_activity_exceptions_from_dict(db)


def save_activity_exceptions(
    db: dict,
    path=ACTIVITY_EXCEPTIONS_PATH,
    *,
    db_path: str | Path | None = None,
) -> None:
    if sqlite_writes_enabled() and sqlite_reads_enabled():
        write_activity_exceptions_to_sqlite_for_write(db, db_path=db_path, strict=True)
        return
    write_activity_exceptions_json(db, path)
    mirror_activity_exceptions_to_sqlite_for_write(db, db_path=db_path)


def write_activity_exceptions_json(db: dict, path=ACTIVITY_EXCEPTIONS_PATH) -> None:
    ensure_dirs()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n")
    chown_xray(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(path)


def exception_items() -> list[dict]:
    return load_activity_exceptions().get("items", [])


def load_activity_exceptions_for_read(
    path=ACTIVITY_EXCEPTIONS_PATH,
    *,
    db_path: str | Path | None = None,
) -> dict:
    if sqlite_reads_enabled() and database.database_file_exists(db_path):
        connection = None
        try:
            connection = database.open_database(db_path)
            if sqlite_read_ready(connection):
                return {"version": 1, "items": sqlite_activity.list_exceptions(connection)}
        except Exception:
            pass
        finally:
            if connection is not None:
                connection.close()
    return load_activity_exceptions_json(path)


def exception_items_for_read(
    path=ACTIVITY_EXCEPTIONS_PATH,
    *,
    db_path: str | Path | None = None,
) -> list[dict]:
    return load_activity_exceptions_for_read(path, db_path=db_path).get("items", [])


def mirror_activity_exceptions_to_sqlite_for_write(
    db: dict,
    *,
    db_path: str | Path | None = None,
) -> bool:
    return write_activity_exceptions_to_sqlite_for_write(db, db_path=db_path, strict=False)


def write_activity_exceptions_to_sqlite_for_write(
    db: dict,
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
        normalized = load_activity_exceptions_from_dict(db)
        with database.transaction(connection):
            sqlite_activity.clear_exceptions(connection)
            for item in normalized.get("items", []):
                sqlite_activity.upsert_exception(connection, item)
        return True
    except Exception:
        if strict:
            raise
        return False
    finally:
        if connection is not None:
            connection.close()


def load_activity_exceptions_from_dict(db: dict) -> dict:
    if not isinstance(db, dict):
        db = {}
    items = []
    seen = set()
    for item in db.get("items", []):
        if isinstance(item, str):
            item = {"value": item, "source": "legacy"}
        if not isinstance(item, dict):
            continue
        try:
            value, kind = classify_exception_value(item.get("value", ""), fatal=False)
        except ValueError:
            continue
        if value in seen:
            continue
        seen.add(value)
        items.append({
            "value": value,
            "kind": kind,
            "createdAt": item.get("createdAt") or utc_stamp(),
            "source": item.get("source") or "manual",
        })
    return {"version": 1, "items": items}


def host_for_exception_match(host: str) -> str:
    value = str(host or "").strip().strip("[]").lower()
    if not value:
        return ""
    if "/" not in value and value.count(":") == 1:
        candidate, port = value.rsplit(":", 1)
        if port.isdigit():
            value = candidate.strip("[]")
    return value


def exception_matches_host(item: dict, host: str) -> bool:
    value = item.get("value", "")
    kind = item.get("kind", "")
    host_value = host_for_exception_match(host)
    if not value or not host_value:
        return False
    if kind == "cidr":
        try:
            return ipaddress.ip_address(host_value) in ipaddress.ip_network(value, strict=False)
        except ValueError:
            return False
    if kind == "ip":
        return host_value == value
    if kind == "mask":
        return fnmatch.fnmatchcase(host_value, value)
    return host_value == value


def event_exception(event: dict, exceptions: list[dict] | None = None) -> dict | None:
    exceptions = exceptions if exceptions is not None else exception_items()
    host = event.get("host") or ""
    for item in exceptions:
        if exception_matches_host(item, host):
            return item
    return None


def normalize_source(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@:-]+", "_", str(value or "manual")).strip("_") or "manual"
