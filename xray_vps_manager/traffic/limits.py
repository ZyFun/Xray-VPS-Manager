"""Расчёт использования и вывод лимитов трафика."""

from datetime import datetime
from typing import Any

from xray_vps_manager.clients.access import local_now
from xray_vps_manager.clients.limits import (
    traffic_limit, traffic_limit_period_key, traffic_limit_period_label, traffic_limit_reset_time,
)
from xray_vps_manager.traffic.formatting import format_traffic
from xray_vps_manager.traffic.history import day_total, month_total
from xray_vps_manager.traffic.repository import traffic_entry


def format_traffic_limit(entry: dict[str, Any]) -> str:
    limit = traffic_limit(entry)
    if limit is None:
        return "без лимита"
    return f"{format_traffic(limit['bytes'])}/{traffic_limit_period_label(limit['period'])}"


def traffic_limit_usage(entry: dict[str, Any], period: str, now: datetime | None = None) -> int:
    now = now or local_now()
    if period == "daily":
        incoming, outgoing = day_total(entry, now.date())
    elif period == "monthly":
        incoming, outgoing = month_total(entry, now.strftime("%Y-%m"), today=local_now().date())
    else:
        raise ValueError("Traffic limit period must be daily or monthly.")
    return incoming + outgoing


def traffic_limit_status(
    db_entry: dict[str, Any],
    traffic_db_entry: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any] | None:
    limit = traffic_limit(db_entry)
    if limit is None:
        return None
    now = now or local_now()
    used = traffic_limit_usage(traffic_db_entry, limit["period"], now)
    remaining = max(0, limit["bytes"] - used)
    return {
        "period": limit["period"],
        "periodKey": traffic_limit_period_key(limit["period"], now),
        "limitBytes": limit["bytes"],
        "usedBytes": used,
        "remainingBytes": remaining,
        "resetAt": traffic_limit_reset_time(limit["period"], now),
        "exceeded": used >= limit["bytes"],
    }


def traffic_limit_row(
    row: dict[str, Any],
    client_entries: dict[str, Any],
    traffic_db: dict[str, Any],
    connection_label: str,
) -> list[str]:
    db_entry = client_entries.get(row["name"], {})
    traffic_db_entry = traffic_entry(traffic_db, row["name"])
    status = traffic_limit_status(db_entry, traffic_db_entry)
    if status is None:
        return [
            row["name"],
            row["status"],
            connection_label,
            "без лимита",
            "-",
            "-",
            "-",
        ]
    return [
        row["name"],
        row["status"],
        connection_label,
        format_traffic_limit(db_entry),
        format_traffic(status["usedBytes"]),
        format_traffic(status["remainingBytes"]),
        status["resetAt"],
    ]
