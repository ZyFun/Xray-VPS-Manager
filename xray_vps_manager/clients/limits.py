"""Traffic limit helpers."""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from typing import Any

from xray_vps_manager.clients.access import local_now
from xray_vps_manager.traffic.formatting import format_traffic
from xray_vps_manager.traffic.repository import traffic_entry

BYTES_IN_GB = 1024 ** 3


def parse_limit_gb(value: str | None) -> int | None:
    raw = (value or "").strip().replace(",", ".").lower()
    if raw in ("", "0", "none", "no", "unlimited", "forever", "без лимита", "бессрочно"):
        return None
    if raw.endswith("gb"):
        raw = raw[:-2].strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
        raise ValueError("Traffic limit must be a number in GB. Empty or 0 means no limit.")
    gb = float(raw)
    if gb <= 0:
        return None
    if gb > 1048576:
        raise ValueError("Traffic limit is too large. Use a value up to 1048576 GB.")
    return int(gb * BYTES_IN_GB)


def validate_limit_period(value: str | None) -> str:
    value = (value or "").strip().lower()
    aliases = {
        "day": "daily",
        "daily": "daily",
        "d": "daily",
        "день": "daily",
        "сутки": "daily",
        "month": "monthly",
        "monthly": "monthly",
        "m": "monthly",
        "месяц": "monthly",
    }
    period = aliases.get(value)
    if period is None:
        raise ValueError("Traffic limit period must be daily or monthly.")
    return period


def set_entry_traffic_limit(entry: dict[str, Any], period: str, limit_bytes: int | None, now_iso) -> None:
    if limit_bytes is None:
        entry.pop("trafficLimit", None)
        entry.pop("trafficLimitExceededAt", None)
        entry.pop("trafficLimitExceededPeriod", None)
        entry.pop("trafficLimitExceededBytes", None)
        if entry.get("disabledReason") == "traffic-limit":
            entry.pop("disabledReason", None)
        return

    entry["trafficLimit"] = {
        "period": validate_limit_period(period),
        "bytes": int(limit_bytes),
        "setAt": now_iso(),
    }


def traffic_limit(entry: dict[str, Any]) -> dict[str, Any] | None:
    limit = entry.get("trafficLimit")
    if not isinstance(limit, dict):
        return None
    period = limit.get("period")
    try:
        limit_bytes = int(limit.get("bytes", 0) or 0)
    except (TypeError, ValueError):
        return None
    if period not in ("daily", "monthly") or limit_bytes <= 0:
        return None
    return {"period": period, "bytes": limit_bytes}


def traffic_limit_period_key(period: str, now: datetime | None = None) -> str:
    now = now or local_now()
    if period == "daily":
        return now.date().isoformat()
    if period == "monthly":
        return now.strftime("%Y-%m")
    raise ValueError("Traffic limit period must be daily or monthly.")


def traffic_limit_reset_time(period: str, now: datetime | None = None) -> str:
    now = now or local_now()
    if period == "daily":
        reset = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=now.tzinfo)
    elif period == "monthly":
        year = now.year + (1 if now.month == 12 else 0)
        month = 1 if now.month == 12 else now.month + 1
        reset = datetime(year, month, 1, tzinfo=now.tzinfo)
    else:
        raise ValueError("Traffic limit period must be daily or monthly.")
    return reset.isoformat(timespec="minutes")


def traffic_limit_period_label(period: str) -> str:
    return "day" if period == "daily" else "month"


def format_traffic_limit(entry: dict[str, Any]) -> str:
    limit = traffic_limit(entry)
    if limit is None:
        return "без лимита"
    return f"{format_traffic(limit['bytes'])}/{traffic_limit_period_label(limit['period'])}"


def traffic_bucket_totals(bucket: dict[str, Any]) -> tuple[int, int]:
    if not isinstance(bucket, dict):
        return 0, 0
    return int(bucket.get("incoming", 0) or 0), int(bucket.get("outgoing", 0) or 0)


def history_for_entry(entry: dict[str, Any]) -> dict[str, Any]:
    history = entry.get("history", {})
    return history if isinstance(history, dict) else {}


def day_total(entry: dict[str, Any], day: date) -> tuple[int, int]:
    hours = history_for_entry(entry).get(day.isoformat(), {})
    if not isinstance(hours, dict):
        return 0, 0
    incoming = 0
    outgoing = 0
    for bucket in hours.values():
        bucket_in, bucket_out = traffic_bucket_totals(bucket)
        incoming += bucket_in
        outgoing += bucket_out
    return incoming, outgoing


def month_bounds(month_key: str) -> tuple[date, date]:
    year, month = (int(part, 10) for part in month_key.split("-", 1))
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    today = local_now().date()
    if start <= today <= end:
        end = today
    return start, end


def iter_dates(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def month_total(entry: dict[str, Any], month_key: str) -> tuple[int, int]:
    start, end = month_bounds(month_key)
    incoming = 0
    outgoing = 0
    for day in iter_dates(start, end):
        day_in, day_out = day_total(entry, day)
        incoming += day_in
        outgoing += day_out
    return incoming, outgoing


def traffic_limit_usage(entry: dict[str, Any], period: str, now: datetime | None = None) -> int:
    now = now or local_now()
    if period == "daily":
        incoming, outgoing = day_total(entry, now.date())
    elif period == "monthly":
        incoming, outgoing = month_total(entry, now.strftime("%Y-%m"))
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
