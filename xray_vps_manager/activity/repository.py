"""Activity storage helpers backed by SQLite."""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from xray_vps_manager.activity.constants import EXPORT_DIR
from xray_vps_manager.activity.time import parse_time, utc_stamp
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.storage import (
    SQLiteReadUnavailable,
    sqlite_read_ready,
)


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
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        shutil.chown(EXPORT_DIR, user="root")
    except PermissionError:
        pass
    os.chmod(EXPORT_DIR, 0o700)


def save_activity_db(db: dict, *, db_path: str | Path | None = None) -> None:
    write_activity_db_to_sqlite_for_write(db, db_path=db_path, strict=True)


def load_activity_db(retention_days: int, enabled: bool, *, db_path: str | Path | None = None) -> dict:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if sqlite_read_ready(connection):
            return load_activity_db_from_sqlite(connection, retention_days, enabled)
        raise SQLiteReadUnavailable("SQLite database is not marked ready.")
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite activity state cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def load_activity_db_from_sqlite(connection, retention_days: int, enabled: bool) -> dict:
    metadata = sqlite_activity.get_source_metadata(connection)
    summary = sqlite_activity.get_summary(connection)
    db = {
        "version": int(metadata.get("version") or 1),
        "clients": summary if isinstance(summary, dict) else {},
        "accessLog": sqlite_activity.get_access_log_state(connection),
        "retentionDays": retention_days,
        "enabled": enabled,
    }
    for key in ("lastSync", "lastPrune"):
        if metadata.get(key):
            db[key] = metadata[key]
    return db


def append_event(event: dict, *, db_path: str | Path | None = None) -> None:
    write_event_to_sqlite_for_write(event, db_path=db_path, strict=True)


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


def prune_activity(
    db: dict,
    retention_days: int,
    today: date,
    now,
    force: bool = False,
    *,
    db_path: str | Path | None = None,
) -> int:
    last_prune = parse_time(db.get("lastPrune", ""))
    if not force and last_prune and now - last_prune < timedelta(hours=20):
        return 0
    cutoff_date = today - timedelta(days=retention_days - 1)
    cutoff_dt = datetime.combine(cutoff_date, datetime.min.time(), tzinfo=timezone.utc)
    prune_db_summary(db, cutoff_date)
    removed = prune_sqlite_activity_for_write(cutoff_dt, db_path=db_path, strict=True)
    db["lastPrune"] = utc_stamp()
    return removed


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
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        start_key, end_key = sqlite_date_bounds(start, end)
        yield from sqlite_activity.iter_events(
            connection,
            client_name=name,
            start=start_key,
            end=end_key,
        )
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite activity events cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def event_client_names_for_read(
    start: date | None = None,
    end: date | None = None,
    *,
    db_path: str | Path | None = None,
) -> list[str]:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        start_key = None
        end_key = None
        if start is not None and end is not None:
            start_key, end_key = sqlite_date_bounds(start, end)
        return sqlite_activity.list_event_clients(connection, start=start_key, end=end_key)
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite activity clients cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def geoip_events_after_for_read(
    *,
    after_id: int = 0,
    after_time: str | None = None,
    limit: int = 1000,
    db_path: str | Path | None = None,
) -> tuple[list[dict], int]:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        if after_id <= 0 and not after_time:
            return [], sqlite_activity.max_event_id(connection)
        events = list(
            sqlite_activity.iter_geoip_events_after(
                connection,
                after_id=after_id,
                after_time=after_time,
                limit=limit,
            )
        )
        if events:
            return events, max(int(event.get("id") or 0) for event in events)
        return [], max(after_id, sqlite_activity.max_event_id(connection))
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite GeoIP activity events cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def write_event_to_sqlite_for_write(
    event: dict,
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
        client_name = str(event.get("client") or event.get("client_name") or "").strip()
        if client_name not in sqlite_clients.list_clients(connection):
            if strict:
                raise RuntimeError(f"Activity event client is missing from SQLite clients: {client_name}")
            return False
        sqlite_activity.add_event(connection, event)
        return True
    except Exception:
        if strict:
            raise
        return False
    finally:
        if connection is not None:
            connection.close()


def write_activity_db_to_sqlite_for_write(
    db: dict,
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
        clients = db.get("clients") if isinstance(db.get("clients"), dict) else {}
        metadata = {
            "version": db.get("version", 1),
            "enabled": db.get("enabled"),
            "retentionDays": db.get("retentionDays"),
            "lastSync": db.get("lastSync"),
            "lastPrune": db.get("lastPrune"),
        }
        with database.transaction(connection):
            sqlite_activity.set_summary(connection, clients)
            sqlite_activity.set_source_metadata(connection, metadata)
            sqlite_activity.upsert_access_log_state(connection, db.get("accessLog"))
        return True
    except Exception:
        if strict:
            raise
        return False
    finally:
        if connection is not None:
            connection.close()


def prune_sqlite_activity_for_write(
    cutoff_dt: datetime,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> int:
    if not database.database_file_exists(db_path):
        if strict:
            raise RuntimeError("SQLite manager database is missing")
        return 0

    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            if strict:
                raise RuntimeError("SQLite database is not marked ready")
            return 0
        cutoff = cutoff_dt.isoformat().replace("+00:00", "Z")
        return sqlite_activity.delete_events_before(connection, cutoff)
    except Exception:
        if strict:
            raise
        return 0
    finally:
        if connection is not None:
            connection.close()
