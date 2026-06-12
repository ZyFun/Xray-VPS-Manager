"""Activity status report helpers."""

from __future__ import annotations

from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import parser as activity_parser
from xray_vps_manager.activity import repository
from xray_vps_manager.activity import settings
from xray_vps_manager.activity.constants import (
    ACTIVITY_DB_PATH,
    ACTIVITY_EXCEPTIONS_PATH,
    CLIENT_LOG_DIR,
    CONFIG_PATH,
)
from xray_vps_manager.activity.reports import format_size


def client_log_stats() -> tuple[int, int]:
    client_files = list(CLIENT_LOG_DIR.glob("*.jsonl")) if CLIENT_LOG_DIR.exists() else []
    size = sum(path.stat().st_size for path in client_files if path.exists())
    return len(client_files), size


def status_rows() -> tuple[list[list[object]], list[str]]:
    db = repository.load_activity_db(settings.retention_days(), settings.activity_enabled())
    config = repository.load_json(CONFIG_PATH, {})
    access = config.get("log", {}).get("access", "")
    client_file_count, client_log_size = client_log_stats()
    exceptions = activity_exceptions.exception_items_for_read()
    limits = settings.risk_limits()
    geoip_code = settings.xray_geoip_warning_code()
    geoip_path = activity_parser.geoip_path()
    rows = [
        ["Parser enabled", "yes" if settings.activity_enabled() else "no"],
        ["Retention", f"{settings.retention_days()} days"],
        ["Suspicious burst", f"{limits['burstEvents']} events / {limits['burstWindowMinutes']} min"],
        ["Suspicious hosts", limits["uniqueHosts"]],
        ["Suspicious ports", limits["uniquePorts"]],
        ["Suspicious exceptions", len(exceptions)],
        ["Xray route GeoIP warnings", geoip_code or "disabled"],
        ["Access log", access or "not configured"],
        ["GeoIP data", str(geoip_path) if geoip_path else "geoip.dat not available"],
        ["Activity DB", str(ACTIVITY_DB_PATH)],
        ["Exception DB", str(ACTIVITY_EXCEPTIONS_PATH)],
        ["Client logs", f"{client_file_count} files, {format_size(client_log_size)}"],
        ["Last sync", db.get("lastSync", "never")],
    ]
    warnings = []
    if not settings.activity_enabled():
        warnings.append("Activity log parser is disabled. Enable it from menu or run: xray-activity enable")
    if geoip_code and geoip_code not in activity_parser.available_geoip_codes():
        warnings.append(f"WARN: Xray route GeoIP warnings are set to {geoip_code}, but this region was not found in geoip.dat.")
    return rows, warnings
