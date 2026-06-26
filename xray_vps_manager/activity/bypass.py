"""Activity helpers for GeoIP bypass reporting."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.core.paths import CONFIG_PATH
from xray_vps_manager.db import database as sqlite_database
from xray_vps_manager.db.repositories import bypass as sqlite_bypass
from xray_vps_manager.xray import bypass as bypass_config
from xray_vps_manager.xray import config as xray_config


def load_config() -> dict[str, Any]:
    try:
        return xray_config.load_config(CONFIG_PATH)
    except Exception:
        return {}


def append_bypass_risk(event: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    outbound = str(event.get("outbound") or "")
    if not outbound:
        return event
    current_config = config if config is not None else load_config()
    risk = bypass_config.bypass_event_risk(current_config, outbound)
    if not risk:
        return event
    risks = [str(item) for item in event.get("risks", []) if str(item or "").strip()]
    if risk not in risks:
        risks.append(risk)
    event["risks"] = risks
    return event


def bypass_risks_for_event(event: dict[str, Any], config: dict[str, Any] | None = None) -> list[str]:
    risks = [str(risk) for risk in event.get("risks") or [] if str(risk).startswith("xray-bypass:")]
    if risks:
        return sorted(set(risks))
    annotated = append_bypass_risk(dict(event), config=config)
    return sorted(str(risk) for risk in annotated.get("risks", []) if str(risk).startswith("xray-bypass:"))


def bypass_tag_for_event(event: dict[str, Any], config: dict[str, Any] | None = None) -> str:
    outbound = str(event.get("outbound") or "")
    if not bypass_config.is_geoip_warning_tag(outbound):
        return ""
    current_config = config if config is not None else load_config()
    try:
        region = bypass_config.region_from_geoip_warning_tag(outbound)
    except ValueError:
        return ""
    return bypass_config.configured_bypass_for_warning(current_config, region)


def status_rows(config: dict[str, Any] | None = None) -> list[list[Any]]:
    current_config = config if config is not None else load_config()
    try:
        connection = sqlite_database.open_database()
        try:
            routes = sqlite_bypass.list_routes(connection)
        finally:
            connection.close()
    except Exception:
        routes = {}

    rows = []
    for tag in sorted(set(routes) | {str(outbound.get("tag") or "") for outbound in bypass_config.bypass_outbounds(current_config)}):
        if not tag:
            continue
        record = routes.get(tag, {})
        region = str(record.get("regionCode") or "-")
        configured_tag = bypass_config.configured_bypass_for_warning(current_config, region) if region != "-" else ""
        status = "enabled" if record.get("enabled") and configured_tag == tag else "disabled"
        if record.get("enabled") and configured_tag != tag:
            status = "drift"
        rows.append(
            [
                tag,
                region,
                record.get("regionLabel") or "-",
                status,
                configured_tag or "-",
            ]
        )
    return rows


def event_rows(days_value: str = "7") -> dict[str, Any]:
    try:
        days = max(1, int(days_value or "7"))
    except ValueError:
        days = 7
    start, end = activity_time.date_range_from_days(days)
    start_iso = f"{start.isoformat()}T00:00:00Z"
    end_iso = f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z"
    config = load_config()
    events = activity_repository.alert_events_for_read(
        risk_prefix="xray-geoip:",
        start=start_iso,
        end=end_iso,
        limit=10000,
    )
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    def add_event(event: dict[str, Any]) -> None:
        bypass_tag = bypass_tag_for_event(event, config=config)
        if not bypass_tag:
            return
        risks = bypass_risks_for_event(event, config=config)
        region = ",".join(risk.split(":", 1)[1] for risk in risks) or "-"
        key = (
            str(event.get("client") or "-"),
            str(event.get("host") or "-"),
            str(event.get("port") or "-"),
            region,
            bypass_tag,
        )
        row = grouped.setdefault(
            key,
            {
                "client": key[0],
                "host": key[1],
                "port": key[2],
                "region": key[3],
                "bypassTag": key[4],
                "events": 0,
                "last": str(event.get("time") or ""),
            },
        )
        row["events"] += int(event.get("event_count") or 1)
        row["last"] = max(str(row.get("last") or ""), str(event.get("last_seen_at") or event.get("time") or ""))

    for event in events:
        add_event(event)

    if not grouped:
        for client_name in activity_repository.event_client_names_for_read(start, end):
            for event in activity_repository.iter_events_for_read(client_name, start, end, activity_time.parse_time):
                add_event(event)

    rows = sorted(grouped.values(), key=lambda row: (row["client"], row["last"], row["events"]), reverse=True)
    return {
        "start": start,
        "end": end,
        "rows": rows,
    }
