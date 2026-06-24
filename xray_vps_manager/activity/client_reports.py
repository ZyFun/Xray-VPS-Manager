"""Client activity reports built from recorded activity events."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import reports as activity_reports
from xray_vps_manager.activity import settings as activity_settings
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients import repository as client_repository


UNKNOWN_CONNECTION = "-"


def iter_events(name, start, end):
    yield from activity_repository.iter_events_for_read(name, start, end, activity_time.parse_time)


def known_clients_for_reports(start=None, end=None):
    return {name: {} for name in activity_repository.event_client_names_for_read(start, end)}


def known_credential_connections(name: str) -> list[str]:
    try:
        entry = client_repository.db_clients(client_repository.load_db_sql()).get(name)
    except Exception:
        return []
    if not isinstance(entry, dict):
        return []
    return sorted(client_credentials.normalize_entry_credentials(entry))


def credential_rows(
    events: list[dict],
    exceptions: list[dict],
    known_connections: list[str] | None = None,
) -> list[list]:
    grouped: dict[str, list[dict]] = {connection: [] for connection in known_connections or []}
    for event in events:
        connection = str(event.get("connection") or UNKNOWN_CONNECTION)
        grouped.setdefault(connection, []).append(event)

    rows = []
    total_events = 0
    for connection in sorted(grouped):
        aggregate = activity_reports.aggregate_events(grouped[connection], exceptions=exceptions)
        total_events += aggregate["events"]
        rows.append(
            [
                connection,
                aggregate["events"],
                len(aggregate["hosts"]),
                activity_reports.top_items(aggregate["ports"]),
                activity_reports.top_items(aggregate["outbounds"]),
                activity_reports.top_items(aggregate["risks"]),
                activity_reports.top_items(aggregate["exceptions"]),
                activity_reports.top_items(aggregate["hosts"]),
            ]
        )
    if rows:
        rows.append(["TOTAL", total_events, "-", "-", "-", "-", "-", "-"])
    return rows


def client_report(name: str, days_value: str = "7") -> dict:
    days = int(days_value or "7", 10)
    start, end = activity_time.date_range_from_days(days)
    exceptions = activity_exceptions.exception_items_for_read()
    period_events = list(iter_events(name, start, end))
    known_connections = known_credential_connections(name)
    rows = []
    total_events = 0
    for day in activity_time.iter_dates(start, end):
        aggregate = activity_reports.aggregate_events(
            [event for event in period_events if str(event.get("time") or "").startswith(day.isoformat())],
            exceptions=exceptions,
        )
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
        "credentialRows": credential_rows(period_events, exceptions, known_connections),
        "totalEvents": total_events,
    }


def suspicious_report(days_value: str = "7") -> dict:
    days = int(days_value or "7", 10)
    start, end = activity_time.date_range_from_days(days)
    clients = known_clients_for_reports(start, end)
    exceptions = activity_exceptions.exception_items_for_read()
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
    clients = known_clients_for_reports(start, end)
    display_tz, display_tz_label = activity_display_timezone()
    exceptions = activity_exceptions.exception_items_for_read()
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
