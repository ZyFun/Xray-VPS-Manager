"""Traffic limit helpers."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from xray_vps_manager.clients.access import local_now

BYTES_IN_GB = 1024 ** 3


@dataclass
class TrafficLimitUpdateResult:
    entry: dict[str, Any]
    period: str
    limit_bytes: int | None


@dataclass
class TrafficLimitClearResult:
    entry: dict[str, Any]
    was_disabled_by_limit: bool


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


def set_client_traffic_limit(
    entry: dict[str, Any],
    period: str,
    limit_bytes: int | None,
    now_iso: Callable[[], str],
) -> TrafficLimitUpdateResult:
    set_entry_traffic_limit(entry, period, limit_bytes, now_iso)
    return TrafficLimitUpdateResult(
        entry=entry,
        period=validate_limit_period(period),
        limit_bytes=limit_bytes,
    )


def clear_client_traffic_limit(entry: dict[str, Any], now_iso: Callable[[], str]) -> TrafficLimitClearResult:
    was_disabled_by_limit = entry.get("enabled") is False and entry.get("disabledReason") == "traffic-limit"
    set_entry_traffic_limit(entry, "daily", None, now_iso)
    return TrafficLimitClearResult(
        entry=entry,
        was_disabled_by_limit=was_disabled_by_limit,
    )


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
