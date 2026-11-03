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
from xray_vps_manager.db.storage import SQLiteReadUnavailable


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


def load_activity_db(
    retention_days: int,
    enabled: bool,
    *,
    db_path: str | Path | None = None,
    with_alert_windows: bool = False,
) -> dict:
    """Снимок состояния активности; ``with_alert_windows`` добавляет ключ ``alertWindows``
    (состояние окон подозрительной активности), который ``save_activity_db`` записывает
    вместе с offset. Без ключа строка ``activity.alertWindows`` не трогается."""
    try:
        with database.session(database.database_path(db_path)) as connection:
            return load_activity_db_from_sqlite(
                connection, retention_days, enabled, with_alert_windows=with_alert_windows
            )
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite activity state cannot be read: {exc}") from exc


def load_activity_db_from_sqlite(
    connection,
    retention_days: int,
    enabled: bool,
    *,
    with_alert_windows: bool = False,
) -> dict:
    metadata = sqlite_activity.get_source_metadata(connection)
    db = {
        "version": int(metadata.get("version") or 1),
        # Сводка по клиентам больше не ведётся (VIREI-248); ключ оставлен пустым для вызывающих.
        "clients": {},
        "accessLog": sqlite_activity.get_access_log_state(connection),
        "retentionDays": retention_days,
        "enabled": enabled,
    }
    for key in ("lastSync", "lastPrune"):
        if metadata.get(key):
            db[key] = metadata[key]
    if with_alert_windows:
        db["alertWindows"] = sqlite_activity.get_metadata_json(
            connection, sqlite_activity.ACTIVITY_ALERT_WINDOW_STATE_KEY
        )
    return db


def append_event(event: dict, *, db_path: str | Path | None = None) -> None:
    write_event_to_sqlite_for_write(event, db_path=db_path, strict=True)


def detail_capture_status_for_read(
    *,
    legacy_enabled: bool | None = None,
    db_path: str | Path | None = None,
) -> dict:
    try:
        with database.session(database.database_path(db_path)) as connection:
            return sqlite_activity.detail_capture_status(connection, legacy_enabled=legacy_enabled)
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite activity capture status cannot be read: {exc}") from exc


def set_detail_mode_for_write(
    mode: str,
    *,
    db_path: str | Path | None = None,
    strict: bool = True,
) -> str:
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            return sqlite_activity.set_detail_mode(connection, mode)
    except Exception:
        if strict:
            raise
        return DETAIL_MODE_OFF


def set_detail_clients_for_write(
    names: Iterable[str],
    *,
    db_path: str | Path | None = None,
    strict: bool = True,
) -> None:
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            sqlite_activity.set_detail_clients(connection, names)
    except Exception:
        if strict:
            raise


def should_store_detail_event(event: dict, mode: str, selected_clients: Iterable[str]) -> bool:
    normalized = sqlite_activity.normalized_detail_mode(mode)
    if normalized == DETAIL_MODE_ALL:
        return True
    if normalized == DETAIL_MODE_SELECTED:
        client_name = str(event.get("client") or event.get("client_name") or "").strip()
        return client_name in {str(name or "").strip() for name in selected_clients}
    return False


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
    try:
        with database.session(database.database_path(db_path)) as connection:
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


def event_client_names_for_read(
    start: date | None = None,
    end: date | None = None,
    *,
    db_path: str | Path | None = None,
) -> list[str]:
    try:
        with database.session(database.database_path(db_path)) as connection:
            start_key = None
            end_key = None
            if start is not None and end is not None:
                start_key, end_key = sqlite_date_bounds(start, end)
            return sqlite_activity.list_event_clients(connection, start=start_key, end=end_key)
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite activity clients cannot be read: {exc}") from exc


def first_event_time_for_read(*, db_path: str | Path | None = None) -> str | None:
    try:
        with database.session(database.database_path(db_path)) as connection:
            return sqlite_activity.first_event_time(connection)
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite first activity event cannot be read: {exc}") from exc


def geoip_events_after_for_read(
    *,
    after_id: int = 0,
    after_time: str | None = None,
    limit: int = 1000,
    db_path: str | Path | None = None,
) -> tuple[list[dict], int]:
    try:
        with database.session(database.database_path(db_path)) as connection:
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


def geoip_alerts_after_for_read(
    *,
    after_id: int = 0,
    after_time: str | None = None,
    limit: int = 1000,
    db_path: str | Path | None = None,
) -> tuple[list[dict], int]:
    try:
        with database.session(database.database_path(db_path)) as connection:
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


def alert_events_for_read(
    *,
    risk_prefix: str | None = None,
    client_name: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[dict]:
    try:
        with database.session(database.database_path(db_path)) as connection:
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


def mark_alerts_admin_notified_for_write(
    alert_ids: Iterable[int],
    stamp: str,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> int:
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            return sqlite_activity.mark_alerts_admin_notified(connection, alert_ids, stamp)
    except Exception:
        if strict:
            raise
        return 0


def client_counters_for_read(
    *,
    bucket_type: str,
    start: str | None = None,
    end: str | None = None,
    client_name: str | None = None,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[dict]:
    try:
        with database.session(database.database_path(db_path)) as connection:
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


def xray_error_events_for_read(
    *,
    level: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[dict]:
    try:
        with database.session(database.database_path(db_path)) as connection:
            return sqlite_activity.list_xray_error_events(connection, level=level, start=start, end=end, limit=limit)
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite Xray error events cannot be read: {exc}") from exc


def xray_error_event_for_read(
    event_id: int,
    *,
    db_path: str | Path | None = None,
) -> dict | None:
    try:
        with database.session(database.database_path(db_path)) as connection:
            return sqlite_activity.get_xray_error_event(connection, event_id)
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite Xray error event cannot be read: {exc}") from exc


def record_xray_error_for_write(
    item: dict,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> int | None:
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            return sqlite_activity.upsert_xray_error_event(connection, item)
    except Exception:
        if strict:
            raise
        return None


def record_pipeline_event_for_write(
    event: dict,
    *,
    detail_mode: str,
    selected_clients: Iterable[str],
    alerts_enabled: bool,
    db_path: str | Path | None = None,
    strict: bool = False,
    window_state: dict | None = None,
    risk_limits: dict | None = None,
    display_tz=None,
) -> dict:
    """Записывает одно событие конвейера: счётчики, detailed-событие, alert-строки, blocklist hit.

    ``window_state``, ``risk_limits`` и ``display_tz`` передаёт прогон sync, прочитав их один раз
    (VIREI-246): состояние окон тогда меняется в памяти и не пишется на каждое событие.
    Без них — прежнее поведение: часовой пояс и лимиты читаются здесь, окна — в ``manager_metadata``.
    """
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            client_name = str(event.get("client") or event.get("client_name") or "").strip()
            if not sqlite_clients.client_exists(connection, client_name):
                return {"storedDetail": False, "storedAlerts": 0, "storedCounters": False}
            if display_tz is None:
                display_tz, _label = manager_timezone()
            detail_event_id = None
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
                if risk_limits is None:
                    from xray_vps_manager.activity import settings as activity_settings

                    risk_limits = activity_settings.risk_limits()
                alert_ids.extend(
                    sqlite_activity.add_window_alerts_for_event(
                        connection,
                        event,
                        risk_limits,
                        display_tz,
                        state=window_state,
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


def write_event_to_sqlite_for_write(
    event: dict,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> bool:
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            client_name = str(event.get("client") or event.get("client_name") or "").strip()
            if not sqlite_clients.client_exists(connection, client_name):
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


def write_activity_db_to_sqlite_for_write(
    db: dict,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> bool:
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            metadata = {
                "version": db.get("version", 1),
                "enabled": db.get("enabled"),
                "retentionDays": db.get("retentionDays"),
                "lastSync": db.get("lastSync"),
                "lastPrune": db.get("lastPrune"),
            }
            alert_windows = db.get("alertWindows")
            # Накопленную сводку удаляем здесь же: после первого сохранения это пустая операция.
            sqlite_activity.delete_summary(connection)
            sqlite_activity.set_source_metadata(connection, metadata)
            sqlite_activity.upsert_access_log_state(connection, db.get("accessLog"))
            if isinstance(alert_windows, dict):
                # В одной транзакции с offset: окна сохранены ровно тогда, когда offset продвинут.
                sqlite_activity.set_metadata_json(
                    connection, sqlite_activity.ACTIVITY_ALERT_WINDOW_STATE_KEY, alert_windows
                )
            return True
    except Exception:
        if strict:
            raise
        return False


def prune_sqlite_activity_for_write(
    cutoff_dt: datetime,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> int:
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            cutoff = cutoff_dt.isoformat().replace("+00:00", "Z")
            return sqlite_activity.delete_events_before(connection, cutoff)
    except Exception:
        if strict:
            raise
        return 0


def prune_alerts_for_write(
    cutoff_dt: datetime,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> int:
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            return sqlite_activity.delete_alert_events_before(connection, cutoff_dt.isoformat().replace("+00:00", "Z"))
    except Exception:
        if strict:
            raise
        return 0


def prune_xray_errors_for_write(
    cutoff_dt: datetime,
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> int:
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            return sqlite_activity.delete_xray_error_events_before(connection, cutoff_dt.isoformat().replace("+00:00", "Z"))
    except Exception:
        if strict:
            raise
        return 0
