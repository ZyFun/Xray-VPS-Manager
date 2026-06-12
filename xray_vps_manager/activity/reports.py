"""Activity reporting and risk aggregation helpers."""

from __future__ import annotations

import ipaddress
from datetime import timedelta

from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import parser as activity_parser
from xray_vps_manager.activity.time import parse_time


def top_items(counter: dict, limit: int = 3) -> str:
    if not isinstance(counter, dict) or not counter:
        return "-"
    items = sorted(counter.items(), key=lambda item: int(item[1]), reverse=True)[:limit]
    return ", ".join(f"{key}({value})" for key, value in items)


def format_size(value: int) -> str:
    value = int(value or 0)
    if value < 1024:
        return f"{value}B"
    for suffix, size in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        next_size = size * 1024
        if value < next_size or suffix == "GB":
            return f"{value / size:.2f}{suffix}"
    return f"{value}B"


def aggregate_events(events, skip_exceptions: bool = False, exceptions: list[dict] | None = None) -> dict:
    exceptions = exceptions if exceptions is not None else activity_exceptions.exception_items_for_read()
    result = {
        "events": 0,
        "hosts": {},
        "ports": {},
        "outbounds": {},
        "risks": {},
        "exceptions": {},
        "hours": {},
        "times": [],
        "sources": {},
    }
    for event in events:
        matched_exception = activity_exceptions.event_exception(event, exceptions)
        if matched_exception:
            value = matched_exception.get("value", "")
            if value:
                result["exceptions"][value] = result["exceptions"].get(value, 0) + 1
            if skip_exceptions:
                continue
        result["events"] += 1
        if event.get("host"):
            result["hosts"][event["host"]] = result["hosts"].get(event["host"], 0) + 1
        if event.get("port"):
            port = str(event["port"])
            result["ports"][port] = result["ports"].get(port, 0) + 1
        if event.get("outbound"):
            result["outbounds"][event["outbound"]] = result["outbounds"].get(event["outbound"], 0) + 1
        if event.get("source"):
            result["sources"][event["source"]] = result["sources"].get(event["source"], 0) + 1
        if not matched_exception:
            for risk in event.get("risks", []):
                result["risks"][risk] = result["risks"].get(risk, 0) + 1
        event_time = parse_time(event.get("time"))
        if event_time:
            result["times"].append(event_time)
            hour_key = event_time.strftime("%Y-%m-%d %H:00")
            result["hours"][hour_key] = result["hours"].get(hour_key, 0) + 1
    return result


def rolling_burst(times, window_minutes: int) -> tuple[int, object | None]:
    ordered = sorted(time for time in times if time)
    if not ordered:
        return 0, None
    best_count = 0
    best_start = None
    end_index = 0
    window = timedelta(minutes=window_minutes)
    for start_index, start_time in enumerate(ordered):
        while end_index < len(ordered) and ordered[end_index] < start_time + window:
            end_index += 1
        count = end_index - start_index
        if count > best_count:
            best_count = count
            best_start = start_time
    return best_count, best_start


def risk_findings(aggregate: dict, limits: dict) -> list[tuple[str, str, str]]:
    findings = []
    risks = aggregate["risks"]
    if risks.get("smtp", 0) > 0:
        findings.append(("smtp", f"SMTP-like ports used: {risks['smtp']}", "Уточнить назначение трафика; при необходимости временно отключить клиента."))
    if risks.get("blocked", 0) > 0 or risks.get("torrent", 0) > 0:
        count = risks.get("blocked", 0) + risks.get("torrent", 0)
        findings.append(("blocked", f"Blocked/torrent events: {count}", "Проверить отчёт клиента и оставить запрет торрентов включённым."))
    for risk, count in sorted(risks.items()):
        if risk.startswith("xray-geoip:"):
            code = risk.split(":", 1)[1]
            findings.append((risk, f"Xray routed destination events in geoip:{code}: {count}", f"Xray routing зафиксировал трафик в регион {code}; проверить раздельное туннелирование клиента."))
    burst_count, burst_start = rolling_burst(aggregate.get("times", []), limits["burstWindowMinutes"])
    if burst_count >= limits["burstEvents"]:
        start = burst_start.strftime("%Y-%m-%d %H:%M UTC") if burst_start else "unknown time"
        findings.append(("burst", f"{burst_count} events during {limits['burstWindowMinutes']} min from {start}", "Похоже на автоматизацию/парсинг; проверить клиента и лимиты."))
    if len(aggregate["hosts"]) >= limits["uniqueHosts"]:
        findings.append(("many-hosts", f"Unique hosts: {len(aggregate['hosts'])}", "Похоже на парсинг или сканирование; запросить объяснение у клиента."))
    if len(aggregate["ports"]) >= limits["uniquePorts"]:
        findings.append(("many-ports", f"Unique ports: {len(aggregate['ports'])}", "Похоже на сканирование; временно отключить клиента при повторении."))
    return findings


def risk_names_for_event(event: dict) -> list[str]:
    risks = set(event.get("risks") or [])
    risks.update(activity_parser.event_risks(event))
    return sorted(str(risk) for risk in risks if risk)


def geoip_risks_for_event(event: dict) -> list[str]:
    return sorted(risk for risk in risk_names_for_event(event) if str(risk).startswith("xray-geoip:"))


def split_ip_or_domain(host: str) -> tuple[str, str]:
    value = (host or "").strip().strip("[]")
    if not value:
        return "-", "-"
    try:
        ipaddress.ip_address(value)
        return value, "-"
    except ValueError:
        return "-", value
