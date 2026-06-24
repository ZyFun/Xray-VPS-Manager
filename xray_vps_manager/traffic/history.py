"""Traffic history aggregation helpers."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Callable, Iterable


TrafficFormatter = Callable[[int], str]


def iter_dates(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def month_bounds(month_key: str, today: date | None = None) -> tuple[date, date]:
    year, month = (int(part, 10) for part in month_key.split("-", 1))
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    if today and start <= today <= end:
        end = today
    return start, end


def traffic_bucket_totals(bucket: dict | None) -> tuple[int, int]:
    if not isinstance(bucket, dict):
        return 0, 0
    return int(bucket.get("incoming", 0) or 0), int(bucket.get("outgoing", 0) or 0)


def history_for_entry(entry: dict | None) -> dict:
    if not isinstance(entry, dict):
        return {}
    history = entry.get("history", {})
    return history if isinstance(history, dict) else {}


def day_total(entry: dict | None, day: date) -> tuple[int, int]:
    return traffic_day_total(entry, day.isoformat())


def traffic_day_total(entry: dict | None, day_key: str) -> tuple[int, int]:
    hours = history_for_entry(entry).get(day_key, {})
    if not isinstance(hours, dict):
        return 0, 0
    incoming = 0
    outgoing = 0
    for bucket in hours.values():
        bucket_in, bucket_out = traffic_bucket_totals(bucket)
        incoming += bucket_in
        outgoing += bucket_out
    return incoming, outgoing


def day_hour_totals(entry: dict | None, day: date, formatter: TrafficFormatter) -> list[list[str]]:
    hours = history_for_entry(entry).get(day.isoformat(), {})
    if not isinstance(hours, dict):
        hours = {}
    rows = []
    total_in = 0
    total_out = 0
    for hour in range(24):
        incoming, outgoing = traffic_bucket_totals(hours.get(f"{hour:02d}", {}))
        total_in += incoming
        total_out += outgoing
        rows.append([f"{hour:02d}:00", formatter(incoming), formatter(outgoing), formatter(incoming + outgoing)])
    rows.append(["TOTAL", formatter(total_in), formatter(total_out), formatter(total_in + total_out)])
    return rows


def period_day_rows(entry: dict | None, start: date, end: date, formatter: TrafficFormatter) -> list[list[str]]:
    rows = []
    total_in = 0
    total_out = 0
    for day in iter_dates(start, end):
        incoming, outgoing = day_total(entry, day)
        total_in += incoming
        total_out += outgoing
        rows.append([day.isoformat(), formatter(incoming), formatter(outgoing), formatter(incoming + outgoing)])
    rows.append(["TOTAL", formatter(total_in), formatter(total_out), formatter(total_in + total_out)])
    return rows


def period_total(entry: dict | None, start: date, end: date) -> tuple[int, int]:
    incoming = 0
    outgoing = 0
    for day in iter_dates(start, end):
        day_in, day_out = day_total(entry, day)
        incoming += day_in
        outgoing += day_out
    return incoming, outgoing


def month_total(entry: dict | None, month_key: str, today: date | None = None) -> tuple[int, int]:
    start, end = month_bounds(month_key, today=today)
    return period_total(entry, start, end)


def all_time_total(entry: dict | None) -> tuple[int, int]:
    if not isinstance(entry, dict):
        return 0, 0
    return int(entry.get("incoming", 0) or 0), int(entry.get("outgoing", 0) or 0)
