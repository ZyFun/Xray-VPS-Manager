"""Runtime activity synchronization from Xray access.log."""

from __future__ import annotations

from typing import Callable

from xray_vps_manager.activity import parser
from xray_vps_manager.activity import repository
from xray_vps_manager.activity import settings
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.activity.constants import ACCESS_LOG_PATH, CLIENT_DB_PATH, CONFIG_PATH
from xray_vps_manager.clients import repository as client_repository

LogFunc = Callable[[str], None]


def known_clients() -> dict:
    config = repository.load_json(CONFIG_PATH, {})
    db = client_repository.load_db_sql(CLIENT_DB_PATH)
    return parser.config_clients(config, db)


def initialize_access_offset(db: dict) -> None:
    state = db.setdefault("accessLog", {})
    if ACCESS_LOG_PATH.exists():
        stat = ACCESS_LOG_PATH.stat()
        state.update({
            "path": str(ACCESS_LOG_PATH),
            "inode": stat.st_ino,
            "offset": stat.st_size,
            "updated": activity_time.utc_stamp(),
        })
    else:
        state.update({
            "path": str(ACCESS_LOG_PATH),
            "inode": None,
            "offset": 0,
            "updated": activity_time.utc_stamp(),
        })


def sync_activity(log: LogFunc) -> int:
    if not settings.activity_enabled():
        log("Activity logging disabled.")
        return 0
    repository.ensure_dirs()
    clients = known_clients()
    if not clients:
        log("No clients found.")
        return 0
    if not ACCESS_LOG_PATH.exists():
        log(f"Access log not found: {ACCESS_LOG_PATH}")
        return 0

    try:
        stat = ACCESS_LOG_PATH.stat()
    except OSError as exc:
        log(f"Cannot stat access log: {exc}")
        return 1

    db = repository.load_activity_db(settings.retention_days(), settings.activity_enabled())
    state = db.setdefault("accessLog", {})
    previous_inode = state.get("inode")
    previous_offset = int(state.get("offset", 0) or 0)
    offset = previous_offset if previous_inode == stat.st_ino and stat.st_size >= previous_offset else 0
    processed = 0
    skipped = 0

    try:
        with ACCESS_LOG_PATH.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
            new_offset = handle.tell()
    except OSError as exc:
        log(f"Cannot read access log: {exc}")
        return 1

    for raw_line in data.decode("utf-8", errors="replace").splitlines():
        event = parser.parse_access_line(raw_line, clients)
        if not event:
            skipped += 1
            continue
        repository.append_event(event)
        repository.update_summary(db, event)
        processed += 1

    removed = repository.prune_activity(
        db,
        settings.retention_days(),
        activity_time.today_utc_date(),
        activity_time.utc_now(),
    )
    stamp = activity_time.utc_stamp()
    state.update({
        "path": str(ACCESS_LOG_PATH),
        "inode": stat.st_ino,
        "offset": new_offset,
        "updated": stamp,
    })
    db["enabled"] = True
    db["retentionDays"] = settings.retention_days()
    db["lastSync"] = stamp
    repository.save_activity_db(db)
    log(f"Activity sync saved: {processed} events, {skipped} skipped, {removed} pruned.")
    return 0
