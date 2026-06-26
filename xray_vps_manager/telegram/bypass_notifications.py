"""Owner Telegram notifications for GeoIP bypass configuration changes."""

from __future__ import annotations

from xray_vps_manager.telegram import api, settings


EVENT_TITLES = {
    "enabled": "GeoIP bypass включён",
    "disabled": "GeoIP bypass отключён",
    "region-changed": "GeoIP bypass изменён",
    "removed": "GeoIP bypass удалён",
}


def build_config_event_message(event: str, record: dict) -> str:
    title = EVENT_TITLES.get(event, "GeoIP bypass изменён")
    region = str(record.get("regionCode") or "-")
    label = str(record.get("regionLabel") or "-")
    tag = str(record.get("tag") or "-")
    lines = [
        f"Xray VPS Manager: {title}",
        f"Region: {region} / {label}",
        f"Outbound: {tag}",
    ]
    if event != "removed":
        lines.append(f"Route: geoip:{region.lower()} -> geoip-warning-{region} -> {tag}")
    return "\n".join(lines)


def notify_config_event(event: str, record: dict) -> int:
    db = settings.load_db_sql()
    if not db.get("enabled") or not db.get("token") or not db.get("chatId"):
        return 0
    api.send_message(db, build_config_event_message(event, record))
    return 1
