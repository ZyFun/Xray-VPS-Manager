"""Client activity reports built from activity JSONL events."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import reports as activity_reports
from xray_vps_manager.activity import settings as activity_settings
from xray_vps_manager.activity import sync as activity_sync
from xray_vps_manager.activity import time as activity_time


def iter_events(name, start, end):
    yield from activity_repository.iter_events(name, start, end, activity_time.parse_time)


def client_report(name: str, days_value: str = "7") -> dict:
    days = int(days_value or "7", 10)
    start, end = activity_time.date_range_from_days(days)
    exceptions = activity_exceptions.exception_items()
    rows = []
    total_events = 0
    for day in activity_time.iter_dates(start, end):
        aggregate = activity_reports.aggregate_events(iter_events(name, day, day), exceptions=exceptions)
        total_events += aggregate["events"]
        rows.append(
            [
                day.isoformat(),
                aggregate["events"],
                len(aggregate["hosts"]),
                activity_reports.top_items(aggregate["ports"]),
                activity_reports.top_items(aggregate["outbounds"]),
                activity_reports.top_items(aggregate["risks"]),
                activity_reports.top_items(aggregate["exceptions"]),
                activity_reports.top_items(aggregate["hosts"]),
            ]
        )
    return {
        "name": name,
        "start": start,
        "end": end,
        "rows": rows,
        "totalEvents": total_events,
    }


def suspicious_report(days_value: str = "7") -> dict:
    days = int(days_value or "7", 10)
    start, end = activity_time.date_range_from_days(days)
    clients = activity_sync.known_clients()
    exceptions = activity_exceptions.exception_items()
    rows = []
    for name in sorted(clients):
        aggregate = activity_reports.aggregate_events(
            iter_events(name, start, end),
            skip_exceptions=True,
            exceptions=exceptions,
        )
        findings = activity_reports.risk_findings(aggregate, activity_settings.risk_limits())
        if not findings:
            continue
        risk_names = ", ".join(item[0] for item in findings)
        details = "; ".join(item[1] for item in findings[:3])
        recommendation = findings[0][2]
        rows.append(
            [
                name,
                risk_names,
                aggregate["events"],
                len(aggregate["hosts"]),
                activity_reports.top_items(aggregate["ports"]),
                details,
                recommendation,
            ]
        )
    return {
        "start": start,
        "end": end,
        "rows": rows,
    }


def activity_display_timezone():
    configured = (activity_settings.server_env_values().get("MANAGER_TIMEZONE") or "").strip()
    if configured:
        try:
            return ZoneInfo(configured), configured
        except ZoneInfoNotFoundError:
            return timezone.utc, f"UTC (invalid MANAGER_TIMEZONE: {configured})"
    local = datetime.now().astimezone().tzinfo or timezone.utc
    local_name = datetime.now(local).tzname()
    label = "server local time"
    if local_name:
        label += f" ({local_name})"
    return local, label


def format_event_time(value, tzinfo):
    moment = activity_time.parse_time(value)
    if not moment:
        return value or "-"
    return moment.astimezone(tzinfo).strftime("%Y-%m-%d %H:%M:%S")


def geoip_risk_details(days_value: str = "7") -> dict:
    days = int(days_value or "7", 10)
    start, end = activity_time.date_range_from_days(days)
    clients = activity_sync.known_clients()
    display_tz, display_tz_label = activity_display_timezone()
    exceptions = activity_exceptions.exception_items()
    client_rows = []
    for name in sorted(clients):
        rows = []
        for event in iter_events(name, start, end):
            if activity_exceptions.event_exception(event, exceptions):
                continue
            risks = activity_reports.geoip_risks_for_event(event)
            if not risks:
                continue
            ip_value, domain_value = activity_reports.split_ip_or_domain(event.get("host", ""))
            rows.append(
                [
                    format_event_time(event.get("time"), display_tz),
                    ip_value,
                    domain_value,
                    event.get("port") or "-",
                    ", ".join(risk.split(":", 1)[1] for risk in risks),
                    event.get("outbound") or "-",
                ]
            )
        if rows:
            client_rows.append({"name": name, "rows": rows})
    return {
        "start": start,
        "end": end,
        "timezoneLabel": display_tz_label,
        "clients": client_rows,
    }
