"""Activity status report helpers."""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from pathlib import Path

from xray_vps_manager.activity import blocklist as activity_blocklist
from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import parser as activity_parser
from xray_vps_manager.activity import repository
from xray_vps_manager.activity import settings
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.activity.constants import (
    CONFIG_PATH,
)
from xray_vps_manager.activity.reports import format_size
from xray_vps_manager.core.paths import MANAGER_DB_PATH
from xray_vps_manager.core.time import manager_timezone
from xray_vps_manager.db.storage import SQLiteReadUnavailable


def manager_db_status() -> str:
    if not MANAGER_DB_PATH.exists():
        return f"{MANAGER_DB_PATH}, missing"
    return f"{MANAGER_DB_PATH}, {format_size(MANAGER_DB_PATH.stat().st_size)}"


def first_event_age(
    first_event_time: str | None,
    *,
    now: datetime | None = None,
    display_tz: tzinfo | None = None,
) -> tuple[str | None, int | None]:
    parsed = activity_time.parse_time(first_event_time)
    if not parsed:
        return None, None
    current = now or activity_time.utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    tz = display_tz or manager_timezone()[0] or timezone.utc
    first_local = parsed.astimezone(tz)
    current_local = current.astimezone(tz)
    return first_local.date().isoformat(), max(0, (current_local.date() - first_local.date()).days)


def format_first_event_status(
    first_event_time: str | None,
    *,
    now: datetime | None = None,
    display_tz: tzinfo | None = None,
    language: str = "en",
) -> str:
    first_date, days_ago = first_event_age(first_event_time, now=now, display_tz=display_tz)
    if first_date is None or days_ago is None:
        return "нет событий" if language == "ru" else "no events"
    if language == "ru":
        return f"{first_date} ({days_ago} дн. назад)"
    return f"{first_date} ({days_ago} days ago)"


def first_event_status(
    *,
    db_path: str | Path | None = None,
    now: datetime | None = None,
    display_tz: tzinfo | None = None,
    language: str = "en",
) -> str:
    try:
        first_event_time = repository.first_event_time_for_read(db_path=db_path)
    except SQLiteReadUnavailable as exc:
        if language == "ru":
            return f"недоступен: {exc}"
        return f"unavailable: {exc}"
    return format_first_event_status(first_event_time, now=now, display_tz=display_tz, language=language)


def status_rows() -> tuple[list[list[object]], list[str]]:
    legacy_enabled = settings.activity_enabled()
    db = repository.load_activity_db(settings.retention_days(), legacy_enabled)
    try:
        capture_status = repository.detail_capture_status_for_read(legacy_enabled=legacy_enabled)
    except SQLiteReadUnavailable:
        capture_status = {"mode": "all" if legacy_enabled else "off", "selectedClients": []}
    config = repository.load_json(CONFIG_PATH, {})
    access = config.get("log", {}).get("access", "")
    exceptions = activity_exceptions.exception_items_for_read()
    block_items = activity_blocklist.block_items()
    active_block_items = activity_blocklist.active_block_items()
    limits = settings.risk_limits()
    geoip_code = settings.xray_geoip_warning_code()
    geoip_path = activity_parser.geoip_path()
    rows = [
        ["Detailed mode", capture_status.get("mode") or "off"],
        ["Selected detailed clients", len(capture_status.get("selectedClients") or [])],
        ["Detailed retention", f"{settings.retention_days()} days"],
        ["Alert-log", "enabled" if settings.alerts_enabled() else "disabled"],
        ["Alert retention", f"{settings.alert_retention_days()} days"],
        ["Lightweight counters", "enabled"],
        ["Xray error event retention", f"{settings.xray_error_event_retention_days()} days"],
        ["Raw access.log retention", f"{settings.xray_access_log_retention_days()} days"],
        ["Raw error.log retention", f"{settings.xray_error_log_retention_days()} days"],
        ["Raw log rotate time", settings.raw_log_rotate_time()],
        ["Suspicious burst", f"{limits['burstEvents']} events / {limits['burstWindowMinutes']} min"],
        ["Suspicious hosts", limits["uniqueHosts"]],
        ["Suspicious ports", limits["uniquePorts"]],
        ["Suspicious exceptions", len(exceptions)],
        ["Global blocklist", f"{len(active_block_items)} active / {len(block_items)} total"],
        ["Xray route GeoIP warnings", geoip_code or "disabled"],
        ["Access log", access or "not configured"],
        ["GeoIP data", str(geoip_path) if geoip_path else "geoip.dat not available"],
        ["Manager DB", manager_db_status()],
        ["First event", first_event_status()],
        ["Last sync", db.get("lastSync", "never")],
    ]
    warnings = []
    if capture_status.get("mode") == "off":
        warnings.append("Detailed activity logging is disabled. Alert-log and lightweight counters continue to work.")
    if capture_status.get("mode") == "selected" and not capture_status.get("selectedClients"):
        warnings.append("Detailed activity mode is selected, but no clients are selected.")
    return rows, warnings
