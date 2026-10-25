"""Time helpers for activity events."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from xray_vps_manager.core import clock
from xray_vps_manager.core.time import xray_access_time_to_iso


def utc_now():
    return clock.now().replace(microsecond=0)


def utc_stamp():
    return utc_now().isoformat().replace("+00:00", "Z")


parse_time = clock.parse_time

def access_time_to_iso(value: str) -> str:
    return xray_access_time_to_iso(value)


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
