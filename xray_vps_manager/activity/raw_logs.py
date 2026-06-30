"""Raw Xray log rotation managed by Xray VPS Manager."""

from __future__ import annotations

import gzip
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from xray_vps_manager.activity import repository
from xray_vps_manager.activity import settings
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.activity.constants import ACCESS_LOG_PATH, ERROR_LOG_PATH
from xray_vps_manager.core.time import manager_timezone
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.storage import sqlite_read_ready

RAW_LOG_ROTATION_STATE_KEY = "xrayRawLogRotation.state"
ERROR_LOG_OFFSET_NAME = "xray-error-log"
RAW_LOG_ROTATE_SERVICE_NAME = "xray-raw-log-rotate.service"
RAW_LOG_ROTATE_TIMER_NAME = "xray-raw-log-rotate.timer"
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
RAW_LOG_ROTATE_SERVICE_PATH = SYSTEMD_UNIT_DIR / RAW_LOG_ROTATE_SERVICE_NAME
RAW_LOG_ROTATE_TIMER_PATH = SYSTEMD_UNIT_DIR / RAW_LOG_ROTATE_TIMER_NAME
XRAY_ERROR_RE = re.compile(
    r"^(?P<time>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?(?:\s+\[(?P<level>[^\]]+)\])?\s*(?P<body>.*)$"
)
RAW_LOG_TIME_RE = re.compile(r"^(?P<time>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})")


def _rotate_time_parts(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def raw_log_on_calendar(env: dict[str, str] | None = None) -> str:
    env = settings.with_activity_defaults(dict(env or settings.server_env_values()))
    rotate_time = settings.raw_log_rotate_time(env)
    timezone_name = (env.get("MANAGER_TIMEZONE") or "").strip()
    suffix = f" {timezone_name}" if timezone_name else ""
    return f"*-*-* {rotate_time}:00{suffix}"


def raw_log_timer_unit(env: dict[str, str] | None = None) -> str:
    on_calendar = raw_log_on_calendar(env)
    return f"""[Unit]
Description=Rotate raw Xray access/error logs

[Timer]
OnCalendar={on_calendar}
Persistent=true
AccuracySec=1min
Unit={RAW_LOG_ROTATE_SERVICE_NAME}

[Install]
WantedBy=timers.target
"""


def raw_log_service_unit() -> str:
    return """[Unit]
Description=Rotate raw Xray access/error logs
After=xray.service
ConditionPathExists=/usr/local/etc/xray/config.json

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/xray-activity rotate-raw-logs --due
"""


def next_rotation_label(now: datetime | None = None) -> str:
    tzinfo, timezone_label = manager_timezone()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(tzinfo)
    rotate_hour, rotate_minute = _rotate_time_parts(settings.raw_log_rotate_time())
    next_run = local.replace(hour=rotate_hour, minute=rotate_minute, second=0, microsecond=0)
    if local >= next_run:
        next_run = next_run + timedelta(days=1)
    return f"{next_run.strftime('%Y-%m-%d %H:%M:%S')} {timezone_label}"


def _run_systemctl(args: list[str]) -> None:
    result = subprocess.run(
        ["systemctl", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        raise RuntimeError(output.strip() or f"systemctl {' '.join(args)} failed")


def sync_raw_log_timer(
    *,
    run_systemctl: bool = True,
    service_path: Path = RAW_LOG_ROTATE_SERVICE_PATH,
    timer_path: Path = RAW_LOG_ROTATE_TIMER_PATH,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    env_values = settings.with_activity_defaults(dict(env or settings.server_env_values()))
    on_calendar = raw_log_on_calendar(env_values)
    service = raw_log_service_unit()
    unit = raw_log_timer_unit(env_values)
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(service)
    service_path.chmod(0o644)
    timer_path.parent.mkdir(parents=True, exist_ok=True)
    timer_path.write_text(unit)
    timer_path.chmod(0o644)
    if run_systemctl:
        _run_systemctl(["daemon-reload"])
        _run_systemctl(["enable", "--now", RAW_LOG_ROTATE_TIMER_NAME])
        _run_systemctl(["restart", RAW_LOG_ROTATE_TIMER_NAME])
    return {
        "path": str(timer_path),
        "servicePath": str(service_path),
        "timerPath": str(timer_path),
        "onCalendar": on_calendar,
        "systemctl": "yes" if run_systemctl else "no",
    }


def _load_state() -> dict:
    if not database.database_file_exists():
        return {}
    connection = None
    try:
        connection = database.open_database()
        if not sqlite_read_ready(connection):
            return {}
        return sqlite_activity.get_metadata_json(connection, RAW_LOG_ROTATION_STATE_KEY)
    except Exception:
        return {}
    finally:
        if connection is not None:
            connection.close()


def _save_state(state: dict) -> None:
    if not database.database_file_exists():
        return
    connection = None
    try:
        connection = database.open_database()
        if not sqlite_read_ready(connection):
            return
        sqlite_activity.set_metadata_json(connection, RAW_LOG_ROTATION_STATE_KEY, state)
    finally:
        if connection is not None:
            connection.close()


def _load_error_offset(connection) -> dict:
    row = connection.execute(
        "SELECT path, inode, offset, updated_at FROM file_offsets WHERE name = ?",
        (ERROR_LOG_OFFSET_NAME,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "path": row["path"] or "",
        "inode": int(row["inode"]) if row["inode"] is not None else None,
        "offset": int(row["offset"] or 0),
        "updated": row["updated_at"] or "",
    }


def _save_error_offset(connection, state: dict) -> None:
    connection.execute(
        """
        INSERT INTO file_offsets(name, path, inode, offset, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            path = excluded.path,
            inode = excluded.inode,
            offset = excluded.offset,
            updated_at = excluded.updated_at
        """,
        (
            ERROR_LOG_OFFSET_NAME,
            state.get("path") or "",
            state.get("inode"),
            int(state.get("offset") or 0),
            state.get("updated") or "",
        ),
    )


def parse_xray_error_line(line: str) -> dict | None:
    match = XRAY_ERROR_RE.match(line.strip())
    if not match:
        return None
    body = (match.group("body") or "").strip()
    if not body:
        return None
    raw_level = (match.group("level") or "").strip().lower()
    level = raw_level or ("error" if "error" in body.lower() else "info")
    component = ""
    message = body
    if ":" in body:
        possible_component, rest = body.split(":", 1)
        if possible_component and len(possible_component) <= 120:
            component = possible_component.strip()
            message = rest.strip() or body
    return {
        "event_time": activity_time.access_time_to_iso(match.group("time")),
        "level": level,
        "source": "xray-error-log",
        "component": component,
        "message": message,
        "raw_line": line,
    }


def sync_error_log(log: Callable[[str], None] | None = None) -> int:
    emit = log or (lambda _message: None)
    if not ERROR_LOG_PATH.exists():
        emit(f"Xray error log not found: {ERROR_LOG_PATH}")
        return 0
    if not database.database_file_exists():
        emit("SQLite manager database is missing; Xray error log sync skipped.")
        return 0

    try:
        stat = ERROR_LOG_PATH.stat()
    except OSError as exc:
        emit(f"Cannot stat Xray error log: {exc}")
        return 1

    connection = None
    try:
        connection = database.open_database()
        if not sqlite_read_ready(connection):
            emit("SQLite database is not marked ready; Xray error log sync skipped.")
            return 0
        state = _load_error_offset(connection)
        previous_inode = state.get("inode")
        previous_offset = int(state.get("offset") or 0)
        offset = previous_offset if previous_inode == stat.st_ino and stat.st_size >= previous_offset else 0
        with ERROR_LOG_PATH.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
            new_offset = handle.tell()
        parsed = 0
        skipped = 0
        with database.transaction(connection):
            for raw_line in data.decode("utf-8", errors="replace").splitlines():
                item = parse_xray_error_line(raw_line)
                if not item:
                    skipped += 1
                    continue
                sqlite_activity.upsert_xray_error_event(connection, item)
                parsed += 1
            _save_error_offset(
                connection,
                {
                    "path": str(ERROR_LOG_PATH),
                    "inode": stat.st_ino,
                    "offset": new_offset,
                    "updated": activity_time.utc_stamp(),
                },
            )
        emit(f"Xray error log sync saved: {parsed} events, {skipped} skipped.")
        return 0
    except OSError as exc:
        emit(f"Cannot read Xray error log: {exc}")
        return 1
    finally:
        if connection is not None:
            connection.close()


def rotation_due(now: datetime | None = None, state: dict | None = None) -> tuple[bool, str, str]:
    tzinfo, timezone_label = manager_timezone()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(tzinfo)
    rotate_hour, rotate_minute = _rotate_time_parts(settings.raw_log_rotate_time())
    rotate_at = local.replace(hour=rotate_hour, minute=rotate_minute, second=0, microsecond=0)
    local_date = local.date().isoformat()
    state = state if state is not None else _load_state()
    if state.get("lastRunDate") == local_date:
        return False, local_date, timezone_label
    return local >= rotate_at, local_date, timezone_label


def _ensure_log_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    try:
        shutil.chown(path, user="xray", group="xray")
    except (LookupError, PermissionError):
        pass
    path.chmod(0o644)


def _compress(path: Path) -> Path:
    gz_path = path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as source, gzip.open(gz_path, "wb") as target:
        shutil.copyfileobj(source, target)
    path.unlink()
    return gz_path


def rotate_file(path: Path, stamp: str) -> Path | None:
    _ensure_log_file(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    rotated = path.with_name(f"{path.name}.{stamp}")
    path.rename(rotated)
    _ensure_log_file(path)
    return _compress(rotated)


def _rotated_files(path: Path) -> list[Path]:
    if not path.parent.exists():
        return []
    return sorted(path.parent.glob(f"{path.name}.*"))


def raw_log_archive_rows() -> list[dict[str, str]]:
    tzinfo, _timezone_label = manager_timezone()
    rows = []
    archives = [(ACCESS_LOG_PATH, "access"), (ERROR_LOG_PATH, "error")]
    for log_path, kind in archives:
        for item in _rotated_files(log_path):
            try:
                stat = item.stat()
            except OSError:
                continue
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).astimezone(tzinfo)
            rows.append(
                {
                    "type": kind,
                    "file": item.name,
                    "path": str(item),
                    "modified": modified.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "size": _format_size(stat.st_size),
                }
            )
    rows.sort(key=lambda row: row["modified"], reverse=True)
    return rows


def _open_raw_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def raw_log_timestamp_range(path: Path | None = None) -> tuple[str, str] | None:
    path = path or ACCESS_LOG_PATH
    earliest = ""
    latest = ""
    for item in [path, *_rotated_files(path)]:
        if not item.exists() or not item.is_file():
            continue
        try:
            with _open_raw_text(item) as handle:
                for line in handle:
                    match = RAW_LOG_TIME_RE.match(line)
                    if not match:
                        continue
                    stamp = activity_time.access_time_to_iso(match.group("time"))
                    if not earliest or stamp < earliest:
                        earliest = stamp
                    if not latest or stamp > latest:
                        latest = stamp
        except OSError:
            continue
    if not earliest or not latest:
        return None
    return earliest, latest


def raw_log_timestamp_range_label(path: Path | None = None) -> str:
    value = raw_log_timestamp_range(path)
    if value is None:
        return "no timestamps found"
    return f"{value[0]} - {value[1]}"


def prune_rotated_logs(path: Path, retention_days: int, now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    cutoff = current.timestamp() - max(1, int(retention_days)) * 86400
    removed = 0
    for item in _rotated_files(path):
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def restart_xray() -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "try-restart", "xray.service"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def record_rotation_error(message: str, *, raw_line: str = "") -> None:
    repository.record_xray_error_for_write(
        {
            "event_time": activity_time.utc_stamp(),
            "level": "error",
            "source": "manager",
            "component": "xray-logrotate",
            "message": message,
            "raw_line": raw_line or message,
        }
    )


def drain_logs_before_rotation(log: Callable[[str], None] | None = None) -> int:
    emit = log or (lambda _message: None)
    try:
        from xray_vps_manager.activity import sync as activity_sync

        activity_result = activity_sync.sync_activity(emit)
        error_result = sync_error_log(emit)
    except Exception as exc:
        record_rotation_error("raw log rotation pre-sync failed", raw_line=str(exc))
        emit(f"ERROR: raw log rotation pre-sync failed: {exc}")
        return 1
    if activity_result or error_result:
        detail = f"activity sync={activity_result}, error sync={error_result}"
        record_rotation_error("raw log rotation pre-sync failed", raw_line=detail)
        emit(f"ERROR: raw log rotation pre-sync failed: {detail}")
        return 1
    return 0


def rotate_raw_logs(
    *,
    only_if_due: bool = False,
    log: Callable[[str], None] | None = None,
) -> int:
    emit = log or (lambda _message: None)
    state = _load_state()
    due, local_date, timezone_label = rotation_due(state=state)
    if only_if_due and not due:
        emit(f"Raw log rotation is not due yet for {local_date} ({timezone_label}).")
        return 0

    if drain_logs_before_rotation(emit):
        emit("ERROR: raw log rotation aborted before renaming log files.")
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    rotated = []
    try:
        for path in (ACCESS_LOG_PATH, ERROR_LOG_PATH):
            result = rotate_file(path, stamp)
            if result:
                rotated.append(result)
        removed_access = prune_rotated_logs(ACCESS_LOG_PATH, settings.xray_access_log_retention_days())
        removed_error = prune_rotated_logs(ERROR_LOG_PATH, settings.xray_error_log_retention_days())
    except OSError as exc:
        record_rotation_error(f"raw log rotation failed: {exc}", raw_line=str(exc))
        emit(f"ERROR: raw log rotation failed: {exc}")
        return 1

    restart = restart_xray()
    restart_ok = restart.returncode == 0
    state.update(
        {
            "lastRunDate": local_date,
            "lastRunAt": activity_time.utc_stamp(),
            "lastTimezone": timezone_label,
            "lastRotatedFiles": [str(path) for path in rotated],
            "lastRemovedAccessArchives": removed_access,
            "lastRemovedErrorArchives": removed_error,
            "lastRestartOk": restart_ok,
        }
    )
    if not restart_ok:
        raw = (restart.stdout or "") + (restart.stderr or "")
        state["lastError"] = raw.strip() or "systemctl try-restart xray.service failed"
        record_rotation_error("systemctl try-restart xray.service failed", raw_line=state["lastError"])
    else:
        state.pop("lastError", None)
    _save_state(state)

    emit(f"Rotated raw logs: {len(rotated)} files.")
    emit(f"Pruned access archives: {removed_access}")
    emit(f"Pruned error archives: {removed_error}")
    if restart_ok:
        emit("Xray try-restart completed.")
        return 0
    emit("ERROR: Xray try-restart failed. Details were written to xray_error_events.")
    return 1


def raw_log_rows() -> list[list[object]]:
    state = _load_state()
    return [
        ["access.log", f"{ACCESS_LOG_PATH} ({_size_label(ACCESS_LOG_PATH)})"],
        ["error.log", f"{ERROR_LOG_PATH} ({_size_label(ERROR_LOG_PATH)})"],
        ["Access retention", f"{settings.xray_access_log_retention_days()} days"],
        ["Error retention", f"{settings.xray_error_log_retention_days()} days"],
        ["Rotate time", settings.raw_log_rotate_time()],
        ["Timezone", settings.server_env_values().get("MANAGER_TIMEZONE") or "server local time"],
        ["Timer OnCalendar", raw_log_on_calendar()],
        ["Next rotation", next_rotation_label()],
        ["Last rotation", state.get("lastRunAt") or "never"],
        ["Last restart", "ok" if state.get("lastRestartOk") else state.get("lastError", "unknown")],
        ["Backfill access range", raw_log_timestamp_range_label()],
    ]


def _size_label(path: Path) -> str:
    if not path.exists():
        return "missing"
    return _format_size(path.stat().st_size)


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    for suffix, unit in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if size < unit * 1024 or suffix == "GB":
            return f"{size / unit:.2f}{suffix}"
    return f"{size}B"
