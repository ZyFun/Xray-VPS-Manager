"""Activity status report helpers."""

from __future__ import annotations

from xray_vps_manager.activity import blocklist as activity_blocklist
from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import parser as activity_parser
from xray_vps_manager.activity import repository
from xray_vps_manager.activity import settings
from xray_vps_manager.activity.constants import (
    CONFIG_PATH,
)
from xray_vps_manager.activity.reports import format_size
from xray_vps_manager.core.paths import MANAGER_DB_PATH


def manager_db_status() -> str:
    if not MANAGER_DB_PATH.exists():
        return f"{MANAGER_DB_PATH}, missing"
    return f"{MANAGER_DB_PATH}, {format_size(MANAGER_DB_PATH.stat().st_size)}"


def status_rows() -> tuple[list[list[object]], list[str]]:
    db = repository.load_activity_db(settings.retention_days(), settings.activity_enabled())
    config = repository.load_json(CONFIG_PATH, {})
    access = config.get("log", {}).get("access", "")
    exceptions = activity_exceptions.exception_items_for_read()
    block_items = activity_blocklist.block_items()
    active_block_items = activity_blocklist.active_block_items()
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
        ["Global blocklist", f"{len(active_block_items)} active / {len(block_items)} total"],
        ["Xray route GeoIP warnings", geoip_code or "disabled"],
        ["Access log", access or "not configured"],
        ["GeoIP data", str(geoip_path) if geoip_path else "geoip.dat not available"],
        ["Manager DB", manager_db_status()],
        ["Last sync", db.get("lastSync", "never")],
    ]
    warnings = []
    if not settings.activity_enabled():
        warnings.append("Activity log parser is disabled. Enable it from menu or run: xray-activity enable")
    return rows, warnings
