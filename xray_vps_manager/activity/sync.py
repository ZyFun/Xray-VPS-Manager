"""Runtime activity synchronization from Xray access.log."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from xray_vps_manager.activity import blocklist as activity_blocklist
from xray_vps_manager.activity import bypass as activity_bypass
from xray_vps_manager.activity import parser
from xray_vps_manager.activity import raw_logs
from xray_vps_manager.activity import repository
from xray_vps_manager.activity import settings
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.activity.constants import DETAIL_MODE_OFF
from xray_vps_manager.activity.constants import ACCESS_LOG_PATH, CONFIG_PATH
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.db.storage import SQLiteReadUnavailable

LogFunc = Callable[[str], None]


def known_clients() -> dict:
    config = repository.load_json(CONFIG_PATH, {})
    db = client_repository.load_db_sql()
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


def retention_cutoff(days: int):
    cutoff_date = activity_time.today_utc_date() - timedelta(days=max(1, int(days)) - 1)
    return datetime.combine(cutoff_date, datetime.min.time(), tzinfo=timezone.utc)


def run_independent_maintenance(log: LogFunc) -> dict[str, int]:
    removed_alerts = repository.prune_alerts_for_write(retention_cutoff(settings.alert_retention_days()))
    error_sync_result = raw_logs.sync_error_log(log)
    removed_errors = repository.prune_xray_errors_for_write(retention_cutoff(settings.xray_error_event_retention_days()))
    blocklist_result = 0
    try:
        backup = activity_blocklist.reconcile_xray_config()
        if backup:
            log(f"Activity blocklist routing synced. Backup: {backup}")
    except Exception as exc:
        log(f"Activity blocklist routing sync failed: {exc}")
        blocklist_result = 1
    return {
        "removed_alerts": removed_alerts,
        "error_sync_result": error_sync_result,
        "removed_errors": removed_errors,
        "blocklist_result": blocklist_result,
    }


def maintenance_exit_code(result: dict[str, int]) -> int:
    return 1 if result.get("error_sync_result") or result.get("blocklist_result") else 0


def log_maintenance_result(log: LogFunc, result: dict[str, int]) -> None:
    log(
        "Activity maintenance saved: "
        f"{result.get('removed_alerts', 0)} alerts pruned, "
        f"{result.get('removed_errors', 0)} errors pruned."
    )


def sync_activity(log: LogFunc) -> int:
    repository.ensure_dirs()
    clients = known_clients()
    config = repository.load_json(CONFIG_PATH, {})
    if not clients:
        log("No clients found; skipping access log parsing.")
        maintenance = run_independent_maintenance(log)
        log_maintenance_result(log, maintenance)
        return maintenance_exit_code(maintenance)
    if not ACCESS_LOG_PATH.exists():
        log(f"Access log not found: {ACCESS_LOG_PATH}; skipping access log parsing.")
        maintenance = run_independent_maintenance(log)
        log_maintenance_result(log, maintenance)
        return maintenance_exit_code(maintenance)

    try:
        stat = ACCESS_LOG_PATH.stat()
    except OSError as exc:
        log(f"Cannot stat access log: {exc}")
        maintenance = run_independent_maintenance(log)
        log_maintenance_result(log, maintenance)
        return 1

    legacy_enabled = settings.activity_enabled()
    try:
        capture_status = repository.detail_capture_status_for_read(legacy_enabled=legacy_enabled)
    except SQLiteReadUnavailable:
        capture_status = {
            "mode": "all" if legacy_enabled else DETAIL_MODE_OFF,
            "selectedClients": [],
        }
    detail_mode = str(capture_status.get("mode") or DETAIL_MODE_OFF)
    selected_clients = capture_status.get("selectedClients") or []
    alert_log_enabled = settings.alerts_enabled()

    db = repository.load_activity_db(settings.retention_days(), detail_mode != DETAIL_MODE_OFF)
    state = db.setdefault("accessLog", {})
    previous_inode = state.get("inode")
    previous_offset = int(state.get("offset", 0) or 0)
    offset = previous_offset if previous_inode == stat.st_ino and stat.st_size >= previous_offset else 0
    processed = 0
    detailed = 0
    counters = 0
    alerts = 0
    skipped = 0

    try:
        with ACCESS_LOG_PATH.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
            new_offset = handle.tell()
    except OSError as exc:
        log(f"Cannot read access log: {exc}")
        maintenance = run_independent_maintenance(log)
        log_maintenance_result(log, maintenance)
        return 1

    for raw_line in data.decode("utf-8", errors="replace").splitlines():
        event = parser.parse_access_line(raw_line, clients)
        if not event:
            skipped += 1
            continue
        activity_bypass.append_bypass_risk(event, config=config)
        try:
            result = repository.record_pipeline_event_for_write(
                event,
                detail_mode=detail_mode,
                selected_clients=selected_clients,
                alerts_enabled=alert_log_enabled,
                strict=True,
            )
        except Exception as exc:
            log(f"Activity pipeline write failed; access log offset was not advanced: {exc}")
            return 1
        if result.get("storedDetail"):
            repository.update_summary(db, event)
            detailed += 1
        if result.get("storedCounters"):
            counters += 1
        alerts += int(result.get("storedAlerts") or 0)
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
    db["enabled"] = detail_mode != DETAIL_MODE_OFF
    db["detailMode"] = detail_mode
    db["alertLogEnabled"] = alert_log_enabled
    db["retentionDays"] = settings.retention_days()
    db["lastSync"] = stamp
    repository.save_activity_db(db)
    maintenance = run_independent_maintenance(log)
    log(
        "Activity sync saved: "
        f"{processed} parsed, {detailed} detailed, {counters} counter updates, "
        f"{alerts} alerts, {skipped} skipped, {removed} detailed pruned, "
        f"{maintenance.get('removed_alerts', 0)} alerts pruned, "
        f"{maintenance.get('removed_errors', 0)} errors pruned."
    )
    return maintenance_exit_code(maintenance)
