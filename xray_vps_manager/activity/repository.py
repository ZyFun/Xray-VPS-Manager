"""Activity storage helpers backed by SQLite."""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from xray_vps_manager.activity.constants import DETAIL_MODE_ALL, DETAIL_MODE_OFF, DETAIL_MODE_SELECTED
from xray_vps_manager.activity.constants import EXPORT_DIR
from xray_vps_manager.activity.time import parse_time, utc_stamp
from xray_vps_manager.core.time import manager_timezone
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


def detail_capture_status_for_read(
    *,
    legacy_enabled: bool | None = None,
    db_path: str | Path | None = None,
) -> dict:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        return sqlite_activity.detail_capture_status(connection, legacy_enabled=legacy_enabled)
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite activity capture status cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def set_detail_mode_for_write(
    mode: str,
    *,
    db_path: str | Path | None = None,
    strict: bool = True,
) -> str:
    if not database.database_file_exists(db_path):
        if strict:
            raise RuntimeError("SQLite manager database is missing")
        return DETAIL_MODE_OFF
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            if strict:
                raise RuntimeError("SQLite database is not marked ready")
            return DETAIL_MODE_OFF
        return sqlite_activity.set_detail_mode(connection, mode)
    except Exception:
        if strict:
            raise
        return DETAIL_MODE_OFF
    finally:
        if connection is not None:
            connection.close()


def set_detail_clients_for_write(
    names: Iterable[str],
    *,
    db_path: str | Path | None = None,
    strict: bool = True,
) -> None:
    if not database.database_file_exists(db_path):
        if strict:
            raise RuntimeError("SQLite manager database is missing")
        return
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            if strict:
                raise RuntimeError("SQLite database is not marked ready")
            return
        sqlite_activity.set_detail_clients(connection, names)
    except Exception:
        if strict:
            raise
    finally:
        if connection is not None:
            connection.close()


def should_store_detail_event(event: dict, mode: str, selected_clients: Iterable[str]) -> bool:
    normalized = sqlite_activity.normalized_detail_mode(mode)
    if normalized == DETAIL_MODE_ALL:
        return True
    if normalized == DETAIL_MODE_SELECTED:
        client_name = str(event.get("client") or event.get("client_name") or "").strip()
        return client_name in {str(name or "").strip() for name in selected_clients}
    return False


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


def first_event_time_for_read(*, db_path: str | Path | None = None) -> str | None:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        return sqlite_activity.first_event_time(connection)
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite first activity event cannot be read: {exc}") from exc
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


def geoip_alerts_after_for_read(
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
            return [], sqlite_activity.max_alert_id(connection)
        events = list(
            sqlite_activity.iter_geoip_alerts_after(
                connection,
                after_id=after_id,
                after_time=after_time,
                limit=limit,
            )
        )
        if events:
            return events, max(
                after_id,
                *(int(event.get("alertId") or event.get("id") or 0) for event in events),
            )
        return [], max(after_id, sqlite_activity.max_alert_id(connection))
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite GeoIP alert events cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def alert_events_for_read(
    *,
    risk_prefix: str | None = None,
    client_name: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[dict]:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        return sqlite_activity.list_alert_events(
            connection,
            risk_prefix=risk_prefix,
            client_name=client_name,
            start=start,
            end=end,
            limit=limit,
        )
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite activity alerts cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def mark_alerts_admin_notified_for_write(
    alert_ids: Iterable[int],
    stamp: str,
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
        return sqlite_activity.mark_alerts_admin_notified(connection, alert_ids, stamp)
    except Exception:
        if strict:
            raise
        return 0
    finally:
        if connection is not None:
            connection.close()


def client_counters_for_read(
    *,
    bucket_type: str,
    start: str | None = None,
    end: str | None = None,
    client_name: str | None = None,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[dict]:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        return sqlite_activity.list_client_counters(
            connection,
            bucket_type=bucket_type,
            start=start,
            end=end,
            client_name=client_name,
            limit=limit,
        )
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite activity counters cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def xray_error_events_for_read(
    *,
    level: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[dict]:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        return sqlite_activity.list_xray_error_events(connection, level=level, start=start, end=end, limit=limit)
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite Xray error events cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def xray_error_event_for_read(
    event_id: int,
    *,
    db_path: str | Path | None = None,
) -> dict | None:
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            raise SQLiteReadUnavailable("SQLite database is not marked ready.")
        return sqlite_activity.get_xray_error_event(connection, event_id)
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite Xray error event cannot be read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def record_xray_error_for_write(
    item: dict,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> int | None:
    if not database.database_file_exists(db_path):
        if strict:
            raise RuntimeError("SQLite manager database is missing")
        return None
    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            if strict:
                raise RuntimeError("SQLite database is not marked ready")
            return None
        return sqlite_activity.upsert_xray_error_event(connection, item)
    except Exception:
        if strict:
            raise
        return None
    finally:
        if connection is not None:
            connection.close()


def record_pipeline_event_for_write(
    event: dict,
    *,
    detail_mode: str,
    selected_clients: Iterable[str],
    alerts_enabled: bool,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> dict:
    if not database.database_file_exists(db_path):
        if strict:
            raise RuntimeError("SQLite manager database is missing")
        return {"storedDetail": False, "storedAlerts": 0, "storedCounters": False}

    connection = None
    try:
        connection = database.open_database(db_path)
        if not sqlite_read_ready(connection):
            if strict:
                raise RuntimeError("SQLite database is not marked ready")
            return {"storedDetail": False, "storedAlerts": 0, "storedCounters": False}
        client_name = str(event.get("client") or event.get("client_name") or "").strip()
        if client_name not in sqlite_clients.list_clients(connection):
            if strict:
                raise RuntimeError(f"Activity event client is missing from SQLite clients: {client_name}")
            return {"storedDetail": False, "storedAlerts": 0, "storedCounters": False}
        display_tz, _label = manager_timezone()
        detail_event_id = None
        with database.transaction(connection):
            sqlite_activity.upsert_client_counters(connection, event, display_tz)
            if should_store_detail_event(event, detail_mode, selected_clients):
                detail_event_id = sqlite_activity.add_event(connection, event)
            alert_ids = []
            if alerts_enabled:
                alert_ids.extend(
                    sqlite_activity.add_alerts_for_event(
                        connection,
                        event,
                        raw_ref_event_id=detail_event_id,
                    )
                )
                from xray_vps_manager.activity import settings as activity_settings

                alert_ids.extend(
                    sqlite_activity.add_window_alerts_for_event(
                        connection,
                        event,
                        activity_settings.risk_limits(),
                        display_tz,
                    )
                )
            from xray_vps_manager.activity import blocklist as activity_blocklist

            activity_blocklist.record_blocked_event_hit(connection, event)
        return {
            "storedDetail": detail_event_id is not None,
            "storedAlerts": len(alert_ids),
            "storedCounters": True,
        }
    except Exception:
        if strict:
            raise
        return {"storedDetail": False, "storedAlerts": 0, "storedCounters": False}
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
        from xray_vps_manager.activity import blocklist as activity_blocklist

        activity_blocklist.record_blocked_event_hit(connection, event)
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


def prune_alerts_for_write(
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
        return sqlite_activity.delete_alert_events_before(connection, cutoff_dt.isoformat().replace("+00:00", "Z"))
    except Exception:
        if strict:
            raise
        return 0
    finally:
        if connection is not None:
            connection.close()


def prune_xray_errors_for_write(
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
        return sqlite_activity.delete_xray_error_events_before(connection, cutoff_dt.isoformat().replace("+00:00", "Z"))
    except Exception:
        if strict:
            raise
        return 0
    finally:
        if connection is not None:
            connection.close()
