"""Activity JSON and JSONL storage helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from xray_vps_manager.activity.constants import (
    ACTIVITY_DB_PATH,
    ACTIVITY_DIR,
    CLIENT_LOG_DIR,
    EXPORT_DIR,
)
from xray_vps_manager.activity.parser import parse_json_line
from xray_vps_manager.activity.time import parse_time, utc_stamp
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.storage import sqlite_read_ready, sqlite_reads_enabled


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def chown_xray(path: Path) -> None:
    try:
        shutil.chown(path, user="root", group="xray")
    except LookupError:
        try:
            shutil.chown(path, user="root")
        except PermissionError:
            return
    except PermissionError:
        return


def ensure_dirs() -> None:
    ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    chown_xray(ACTIVITY_DIR)
    chown_xray(CLIENT_LOG_DIR)
    try:
        shutil.chown(EXPORT_DIR, user="root")
    except PermissionError:
        pass
    os.chmod(ACTIVITY_DIR, 0o750)
    os.chmod(CLIENT_LOG_DIR, 0o750)
    os.chmod(EXPORT_DIR, 0o700)


def save_activity_db(db: dict) -> None:
    ensure_dirs()
    tmp = ACTIVITY_DB_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n")
    chown_xray(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(ACTIVITY_DB_PATH)


def load_activity_db(retention_days: int, enabled: bool) -> dict:
    db = load_json(ACTIVITY_DB_PATH, {})
    if not isinstance(db, dict):
        db = {}
    db.setdefault("version", 1)
    db.setdefault("clients", {})
    db.setdefault("accessLog", {})
    db["retentionDays"] = retention_days
    db["enabled"] = enabled
    return db


def safe_client_file(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.@-]+", "_", name).strip("._")
    return CLIENT_LOG_DIR / f"{safe or 'client'}.jsonl"


def append_event(event: dict) -> None:
    ensure_dirs()
    path = safe_client_file(event["client"])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    chown_xray(path)
    os.chmod(path, 0o640)


def update_summary(db: dict, event: dict) -> None:
    clients = db.setdefault("clients", {})
    entry = clients.setdefault(event["client"], {"days": {}, "totalEvents": 0})
    entry["email"] = event.get("email", "")
    entry["connection"] = event.get("connection", "")
    entry["totalEvents"] = int(entry.get("totalEvents", 0)) + 1
    entry.setdefault("firstSeen", event["time"])
    entry["lastSeen"] = event["time"]

    day_key = event["time"][:10]
    day = entry.setdefault("days", {}).setdefault(
        day_key,
        {
            "events": 0,
            "hosts": {},
            "ports": {},
            "outbounds": {},
            "risks": {},
        },
    )
    day["events"] = int(day.get("events", 0)) + 1
    if event.get("host"):
        day.setdefault("hosts", {})[event["host"]] = int(day.setdefault("hosts", {}).get(event["host"], 0)) + 1
    if event.get("port"):
        day.setdefault("ports", {})[str(event["port"])] = int(day.setdefault("ports", {}).get(str(event["port"]), 0)) + 1
    if event.get("outbound"):
        day.setdefault("outbounds", {})[event["outbound"]] = int(day.setdefault("outbounds", {}).get(event["outbound"], 0)) + 1
    for risk in event.get("risks", []):
        day.setdefault("risks", {})[risk] = int(day.setdefault("risks", {}).get(risk, 0)) + 1


def prune_db_summary(db: dict, cutoff: date) -> None:
    for entry in db.setdefault("clients", {}).values():
        days = entry.get("days", {})
        if not isinstance(days, dict):
            entry["days"] = {}
            continue
        for key in list(days):
            try:
                if date.fromisoformat(key) < cutoff:
                    del days[key]
            except ValueError:
                del days[key]


def prune_client_log(path: Path, cutoff_dt: datetime) -> int:
    if not path.exists():
        return 0
    kept = []
    removed = 0
    for line in path.read_text(errors="replace").splitlines():
        event = parse_json_line(line)
        if event is None:
            removed += 1
            continue
        event_time = parse_time(event.get("time"))
        if event_time and event_time >= cutoff_dt:
            kept.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        else:
            removed += 1
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(kept) + ("\n" if kept else ""))
    chown_xray(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(path)
    return removed


def prune_activity(
    db: dict,
    retention_days: int,
    today: date,
    now,
    force: bool = False,
) -> int:
    last_prune = parse_time(db.get("lastPrune", ""))
    if not force and last_prune and now - last_prune < timedelta(hours=20):
        return 0
    cutoff_date = today - timedelta(days=retention_days - 1)
    cutoff_dt = datetime.combine(cutoff_date, datetime.min.time(), tzinfo=timezone.utc)
    prune_db_summary(db, cutoff_date)
    removed = 0
    if CLIENT_LOG_DIR.exists():
        for path in CLIENT_LOG_DIR.glob("*.jsonl"):
            removed += prune_client_log(path, cutoff_dt)
    db["lastPrune"] = utc_stamp()
    return removed


def iter_events(name: str, start: date, end: date, time_parser: Callable[[str | None], datetime | None]) -> Iterable[dict]:
    path = safe_client_file(name)
    if not path.exists():
        return
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    for line in path.read_text(errors="replace").splitlines():
        event = parse_json_line(line)
        if event is None:
            continue
        event_time = time_parser(event.get("time"))
        if event_time and start_dt <= event_time < end_dt:
            yield event


def sqlite_date_bounds(start: date, end: date) -> tuple[str, str]:
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return start_dt.isoformat().replace("+00:00", "Z"), end_dt.isoformat().replace("+00:00", "Z")


def iter_events_for_read(
    name: str,
    start: date,
    end: date,
    time_parser: Callable[[str | None], datetime | None],
    *,
    db_path: str | Path | None = None,
) -> Iterable[dict]:
    if sqlite_reads_enabled() and database.database_file_exists(db_path):
        connection = None
        try:
            connection = database.open_database(db_path)
            if sqlite_read_ready(connection):
                start_key, end_key = sqlite_date_bounds(start, end)
                yield from sqlite_activity.iter_events(
                    connection,
                    client_name=name,
                    start=start_key,
                    end=end_key,
                )
                return
        except Exception:
            pass
        finally:
            if connection is not None:
                connection.close()
    yield from iter_events(name, start, end, time_parser)


def event_client_names_for_read(
    start: date | None = None,
    end: date | None = None,
    *,
    db_path: str | Path | None = None,
) -> list[str] | None:
    if not sqlite_reads_enabled() or not database.database_file_exists(db_path):
        return None
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            return None
        start_key = None
        end_key = None
        if start is not None and end is not None:
            start_key, end_key = sqlite_date_bounds(start, end)
        return sqlite_activity.list_event_clients(connection, start=start_key, end=end_key)
    except Exception:
        return None
    finally:
        if connection is not None:
            connection.close()
