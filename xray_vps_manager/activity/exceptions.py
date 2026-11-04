"""Suspicious activity exception helpers."""

from __future__ import annotations

import fnmatch
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlparse

from xray_vps_manager.activity.constants import EXCEPTION_VALUE_RE
from xray_vps_manager.activity.parser import parse_target
from xray_vps_manager.activity.time import utc_stamp
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.storage import SQLiteReadUnavailable


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


def load_activity_exceptions(*, db_path: str | Path | None = None) -> dict:
    return load_activity_exceptions_for_read(db_path=db_path)


def save_activity_exceptions(
    db: dict,
    *,
    db_path: str | Path | None = None,
) -> None:
    write_activity_exceptions_to_sqlite_for_write(db, db_path=db_path, strict=True)


def _upsert_exception(connection, item: dict) -> None:
    with database.unique_conflict(f"Activity exception conflicts with an existing entry: {item['value']}"):
        sqlite_activity.upsert_exception(connection, item)


def add_exception(value: str, source: str = "manual") -> dict:
    normalized, kind = classify_exception_value(value)
    with database.write_session(database.database_path()) as connection:
        added = not sqlite_activity.exception_exists(connection, normalized)
        if added:
            _upsert_exception(connection, {
                "value": normalized, "kind": kind, "source": normalize_source(source), "createdAt": utc_stamp(),
            })
    return {"added": added, "value": normalized, "kind": kind}


def delete_exception(value: str) -> str:
    normalized, _kind = classify_exception_value(value)
    with database.write_session(database.database_path()) as connection:
        if not sqlite_activity.delete_exception(connection, normalized):
            raise KeyError(normalized)
    return normalized


def delete_all_exceptions() -> int:
    with database.write_session(database.database_path()) as connection:
        return sqlite_activity.clear_exceptions(connection)


def exception_items(*, db_path: str | Path | None = None) -> list[dict]:
    return load_activity_exceptions(db_path=db_path).get("items", [])


def load_activity_exceptions_for_read(
    *,
    db_path: str | Path | None = None,
) -> dict:
    try:
        with database.session(database.database_path(db_path), read_snapshot=True) as connection:
            return {"version": 1, "items": sqlite_activity.list_exceptions(connection)}
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite activity exceptions cannot be read: {exc}") from exc


def exception_items_for_read(
    *,
    db_path: str | Path | None = None,
) -> list[dict]:
    return load_activity_exceptions_for_read(db_path=db_path).get("items", [])


def write_activity_exceptions_to_sqlite_for_write(
    db: dict,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> bool:
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            incoming = {item["value"]: item for item in load_activity_exceptions_from_dict(db)["items"]}
            current = {item["value"]: item for item in sqlite_activity.list_exceptions(connection)}
            for value in current.keys() - incoming.keys():
                sqlite_activity.delete_exception(connection, value)
            for value, item in incoming.items():
                if item != current.get(value):
                    _upsert_exception(connection, item)
        return True
    except Exception:
        if strict:
            raise
        return False


def load_activity_exceptions_from_dict(db: dict) -> dict:
    if not isinstance(db, dict):
        db = {}
    items = []
    seen = set()
    for item in db.get("items", []):
        if isinstance(item, str):
            item = {"value": item, "source": "imported"}
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
