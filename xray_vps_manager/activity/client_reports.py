"""Client activity reports built from recorded activity events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import reports as activity_reports
from xray_vps_manager.activity import settings as activity_settings
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients import repository as client_repository


UNKNOWN_CONNECTION = "-"
COUNTER_GROWTH_FIELDS = ("totalEvents", "uniqueHosts", "uniquePorts")


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
                activity_reports.top_items(aggregate["bypass"]),
                activity_reports.top_items(aggregate["exceptions"]),
                activity_reports.top_items(aggregate["hosts"]),
            ]
        )
    if rows:
        rows.append(["TOTAL", total_events, "-", "-", "-", "-", "-", "-", "-"])
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
                activity_reports.top_items(aggregate["bypass"]),
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
    start_iso = f"{start.isoformat()}T00:00:00Z"
    end_iso = f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z"
    exceptions = activity_exceptions.exception_items_for_read()
    events = activity_repository.alert_events_for_read(start=start_iso, end=end_iso, limit=10000)
    grouped: dict[str, dict] = {}
    for event in events:
        if activity_exceptions.event_exception(event, exceptions):
            continue
        name = str(event.get("client") or "-")
        item = grouped.setdefault(name, {"events": 0, "hosts": set(), "ports": {}, "risks": {}})
        count = int(event.get("event_count") or 1)
        item["events"] += count
        if event.get("host"):
            item["hosts"].add(event["host"])
        if event.get("port"):
            port = str(event["port"])
            item["ports"][port] = item["ports"].get(port, 0) + count
        for risk in event.get("risks") or [event.get("risk")]:
            if risk:
                item["risks"][risk] = item["risks"].get(risk, 0) + count

    rows = []
    for name, aggregate in sorted(grouped.items()):
        risks = aggregate["risks"]
        if not risks:
            continue
        ordered_risks = sorted(risks.items(), key=lambda item: int(item[1]), reverse=True)
        primary_risk = str(ordered_risks[0][0])
        details = activity_reports.top_items(risks, limit=5)
        recommendation = suspicious_recommendation(primary_risk)
        rows.append(
            [
                name,
                activity_reports.top_items(risks, limit=5),
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


def suspicious_recommendation(risk: str) -> str:
    if risk == "smtp":
        return "Уточнить назначение SMTP-like трафика; при необходимости временно отключить клиента."
    if risk == "admin-port":
        return "Проверить, не используется ли сканирование или доступ к служебным портам."
    if risk in {"blocked", "torrent"}:
        return "Проверить blocklist/torrent события и оставить запрет включённым."
    if risk == "burst":
        return "Похоже на всплеск автоматизированного трафика; включить detailed activity для клиента."
    if risk == "unique-hosts":
        return "Много уникальных host; включить detailed activity и проверить назначение трафика."
    if risk == "unique-ports":
        return "Много уникальных port; проверить клиента на сканирование."
    if risk.startswith("xray-geoip:"):
        return "Проверить split tunneling клиента и при необходимости добавить исключение или блокировку."
    return "Проверить события alert-log и уточнить назначение трафика."


def _counter_int(row: dict, field: str) -> int:
    try:
        return max(0, int(row.get(field) or 0))
    except (TypeError, ValueError):
        return 0


def _counter_bucket_metrics(row: dict) -> dict[str, int]:
    return {field: _counter_int(row, field) for field in COUNTER_GROWTH_FIELDS}


def _merge_counter_metrics(target: dict[str, int], source: dict[str, int]) -> None:
    for field in COUNTER_GROWTH_FIELDS:
        target[field] = int(target.get(field, 0)) + int(source.get(field, 0))


def _avg_metric(rows: list[dict[str, int]], field: str) -> float:
    if not rows:
        return 0.0
    return sum(int(row.get(field, 0)) for row in rows) / len(rows)


def counter_growth_rows(counter_rows: list[dict], limit: int = 50) -> list[dict]:
    buckets_by_client: dict[str, dict[str, dict[str, int]]] = {}
    for row in counter_rows:
        client = str(row.get("client") or "").strip()
        bucket = str(row.get("bucketStart") or "").strip()
        if not client or not bucket:
            continue
        buckets = buckets_by_client.setdefault(client, {})
        metrics = buckets.setdefault(bucket, {field: 0 for field in COUNTER_GROWTH_FIELDS})
        _merge_counter_metrics(metrics, _counter_bucket_metrics(row))

    growth_rows = []
    for client, buckets in buckets_by_client.items():
        ordered = sorted(buckets.items())
        if not ordered:
            continue
        latest_bucket, latest = ordered[-1]
        previous = [metrics for _bucket, metrics in ordered[:-1]]
        averages = {field: _avg_metric(previous, field) for field in COUNTER_GROWTH_FIELDS}
        deltas = {field: float(latest.get(field, 0)) - averages[field] for field in COUNTER_GROWTH_FIELDS}
        if not any(value > 0 for value in deltas.values()):
            continue
        ratios = {
            field: (float(latest.get(field, 0)) / averages[field]) if averages[field] > 0 else float(latest.get(field, 0))
            for field in COUNTER_GROWTH_FIELDS
        }
        growth_rows.append(
            {
                "client": client,
                "bucketStart": latest_bucket,
                "baselineBuckets": len(previous),
                "totalEvents": int(latest.get("totalEvents", 0)),
                "avgTotalEvents": averages["totalEvents"],
                "totalEventsDelta": deltas["totalEvents"],
                "uniqueHosts": int(latest.get("uniqueHosts", 0)),
                "avgUniqueHosts": averages["uniqueHosts"],
                "uniqueHostsDelta": deltas["uniqueHosts"],
                "uniquePorts": int(latest.get("uniquePorts", 0)),
                "avgUniquePorts": averages["uniquePorts"],
                "uniquePortsDelta": deltas["uniquePorts"],
                "growthScore": max(ratios.values()),
            }
        )

    growth_rows.sort(
        key=lambda row: (
            float(row.get("growthScore") or 0),
            float(row.get("totalEventsDelta") or 0),
            float(row.get("uniqueHostsDelta") or 0),
            float(row.get("uniquePortsDelta") or 0),
        ),
        reverse=True,
    )
    return growth_rows[: max(1, int(limit or 50))]


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
    display_tz, display_tz_label = activity_display_timezone()
    exceptions = activity_exceptions.exception_items_for_read()

    start_iso = f"{start.isoformat()}T00:00:00Z"
    end_iso = f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z"
    alert_events = activity_repository.alert_events_for_read(
        risk_prefix="xray-geoip:",
        start=start_iso,
        end=end_iso,
        limit=10000,
    )
    alert_rows: dict[str, list[list]] = {}
    for event in alert_events:
        if activity_exceptions.event_exception(event, exceptions):
            continue
        risks = activity_reports.geoip_risks_for_event(event)
        if not risks:
            continue
        ip_value, domain_value = activity_reports.split_ip_or_domain(event.get("host", ""))
        name = str(event.get("client") or "-")
        alert_rows.setdefault(name, []).append(
            [
                format_event_time(event.get("last_seen_at") or event.get("time"), display_tz),
                ip_value,
                domain_value,
                event.get("port") or "-",
                ", ".join(risk.split(":", 1)[1] for risk in risks),
                event.get("outbound") or "-",
            ]
        )
    if alert_rows:
        return {
            "start": start,
            "end": end,
            "timezoneLabel": display_tz_label,
            "clients": [
                {"name": name, "rows": sorted(rows)}
                for name, rows in sorted(alert_rows.items())
                if rows
            ],
        }

    clients = known_clients_for_reports(start, end)
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
