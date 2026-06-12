"""Time helpers for activity events."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_stamp() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def access_time_to_iso(value: str) -> str:
    parsed = datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def today_utc_date() -> date:
    return utc_now().date()


def date_range_from_days(days: int | str) -> tuple[date, date]:
    end = today_utc_date()
    start = end - timedelta(days=max(1, int(days)) - 1)
    return start, end


def iter_dates(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)

