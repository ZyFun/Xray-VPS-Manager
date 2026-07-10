"""Activity data cleanup and SQLite compaction helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Callable, Iterable

from xray_vps_manager.activity import controls
from xray_vps_manager.activity import repository
from xray_vps_manager.activity import settings
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.core.paths import MANAGER_DB_PATH
from xray_vps_manager.db import database


WRITER_STOP_UNITS = (
    "xray-traffic-sync.timer",
    "xray-raw-log-rotate.timer",
    "xray-client-expire.timer",
    "xray-traffic-sync.service",
    "xray-raw-log-rotate.service",
    "xray-client-expire.service",
    "xray-telegram-poller.service",
)
WRITER_RESTART_UNITS = (
    "xray-traffic-sync.timer",
    "xray-raw-log-rotate.timer",
    "xray-client-expire.timer",
    "xray-telegram-poller.service",
)


@dataclass(frozen=True)
class CleanupStats:
    file_bytes: int
    page_size: int
    page_count: int
    freelist_count: int
    activity_events: int
    old_activity_events: int

    @property
    def freelist_bytes(self) -> int:
        return self.page_size * self.freelist_count


@dataclass(frozen=True)
class CleanupResult:
    retention_days: int
    removed_events: int
    backup_path: Path | None
    quick_check: str
    vacuum_seconds: float
    before: CleanupStats
    after: CleanupStats
    stopped_units: tuple[str, ...]
    restarted_units: tuple[str, ...]


Runner = Callable[..., subprocess.CompletedProcess]
Logger = Callable[[str], None]


def _cutoff_for_retention(days: int) -> str:
    cutoff_date = activity_time.today_utc_date() - timedelta(days=days - 1)
    cutoff_dt = datetime.combine(cutoff_date, datetime.min.time(), tzinfo=timezone.utc)
    return cutoff_dt.isoformat().replace("+00:00", "Z")


def database_stats(db_path: str | Path = MANAGER_DB_PATH, *, retention_days: int | None = None) -> CleanupStats:
    path = Path(db_path)
    if not path.exists():
        raise RuntimeError(f"SQLite manager database is missing: {path}")
    days = retention_days or settings.retention_days()
    cutoff = _cutoff_for_retention(days)
    connection = sqlite3.connect(str(path))
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        activity_events = int(connection.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0])
        old_activity_events = int(
            connection.execute("SELECT COUNT(*) FROM activity_events WHERE event_time < ?", (cutoff,)).fetchone()[0]
        )
    finally:
        connection.close()
    return CleanupStats(
        file_bytes=path.stat().st_size,
        page_size=page_size,
        page_count=page_count,
        freelist_count=freelist_count,
        activity_events=activity_events,
        old_activity_events=old_activity_events,
    )


def quick_check(db_path: str | Path = MANAGER_DB_PATH) -> str:
    connection = sqlite3.connect(str(db_path))
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
        return str(row[0] if row else "")
    finally:
        connection.close()


def vacuum_database(db_path: str | Path = MANAGER_DB_PATH) -> float:
    start = time.monotonic()
    connection = sqlite3.connect(str(db_path), timeout=300)
    try:
        connection.execute("VACUUM")
    finally:
        connection.close()
    return time.monotonic() - start


def _run_systemctl(args: list[str], *, runner: Runner = subprocess.run, check: bool = True) -> subprocess.CompletedProcess:
    result = runner(
        ["systemctl", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and int(result.returncode or 0) != 0:
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        raise RuntimeError(output or f"systemctl {' '.join(args)} failed")
    return result


def active_units(units: Iterable[str], *, runner: Runner = subprocess.run) -> tuple[str, ...]:
    active = []
    for unit in units:
        result = _run_systemctl(["is-active", "--quiet", unit], runner=runner, check=False)
        if int(result.returncode or 0) == 0:
            active.append(unit)
    return tuple(active)


def stop_writer_units(*, runner: Runner = subprocess.run) -> tuple[str, ...]:
    _run_systemctl(["stop", *WRITER_STOP_UNITS], runner=runner)
    return WRITER_STOP_UNITS


def restart_units(units: Iterable[str], *, runner: Runner = subprocess.run) -> tuple[str, ...]:
    selected = tuple(units)
    if selected:
        _run_systemctl(["start", *selected], runner=runner)
    return selected


def _apply_retention(days: int, *, db_path: str | Path = MANAGER_DB_PATH) -> tuple[int, int]:
    path = Path(db_path)
    if path == MANAGER_DB_PATH:
        return controls.set_retention_days(str(days))
    db = repository.load_activity_db(days, settings.activity_enabled(), db_path=path)
    db["retentionDays"] = days
    removed = repository.prune_activity(
        db,
        days,
        activity_time.today_utc_date(),
        activity_time.utc_now(),
        force=True,
        db_path=path,
    )
    repository.save_activity_db(db, db_path=path)
    return days, removed


def cleanup_activity_data(
    *,
    retention_days: int | None = None,
    db_path: str | Path = MANAGER_DB_PATH,
    backup_dir: str | Path | None = None,
    run_systemctl: bool = True,
    runner: Runner = subprocess.run,
    log: Logger | None = None,
) -> CleanupResult:
    days = retention_days or settings.retention_days()
    before = database_stats(db_path, retention_days=days)
    log = log or (lambda _message: None)
    units_to_restart: tuple[str, ...] = ()
    stopped_units: tuple[str, ...] = ()
    backup_path: Path | None = None
    try:
        if run_systemctl:
            units_to_restart = active_units(WRITER_RESTART_UNITS, runner=runner)
            log("Stopping manager writer services and timers...")
            stopped_units = stop_writer_units(runner=runner)

        log("Creating SQLite backup before cleanup...")
        backup_path = database.backup_database(db_path, backup_dir=backup_dir, label="pre-cleanup")

        log(f"Applying detailed activity retention: {days} days...")
        actual_days, removed = _apply_retention(days, db_path=db_path)

        log("Running SQLite quick_check before VACUUM...")
        check = quick_check(db_path)
        if check != "ok":
            raise RuntimeError(f"PRAGMA quick_check returned: {check}")

        log("Compacting manager.db with VACUUM...")
        vacuum_seconds = vacuum_database(db_path)
        after = database_stats(db_path, retention_days=actual_days)
        return CleanupResult(
            retention_days=actual_days,
            removed_events=removed,
            backup_path=backup_path,
            quick_check=check,
            vacuum_seconds=vacuum_seconds,
            before=before,
            after=after,
            stopped_units=stopped_units,
            restarted_units=units_to_restart,
        )
    finally:
        if run_systemctl:
            log("Starting manager writer services and timers...")
            restart_units(units_to_restart, runner=runner)
