"""Traffic total/history consistency helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any


@dataclass(frozen=True)
class RetainedHistoryGap:
    name: str
    missing_incoming: int
    missing_outgoing: int
    total_incoming: int
    total_outgoing: int
    history_incoming: int
    history_outgoing: int

    @property
    def missing_total(self) -> int:
        return self.missing_incoming + self.missing_outgoing


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def client_created_date(entry: dict[str, Any]) -> date | None:
    parsed = parse_timestamp(entry.get("created") or entry.get("created_at"))
    return parsed.date() if parsed else None


def traffic_bucket_totals(bucket: dict[str, Any] | None) -> tuple[int, int]:
    if not isinstance(bucket, dict):
        return 0, 0
    return int(bucket.get("incoming") or 0), int(bucket.get("outgoing") or 0)


def history_totals(entry: dict[str, Any], cutoff_date: date | None = None) -> tuple[int, int]:
    history = entry.get("history", {})
    if not isinstance(history, dict):
        return 0, 0
    incoming = 0
    outgoing = 0
    for day_key, hours in history.items():
        if cutoff_date is not None:
            try:
                if date.fromisoformat(str(day_key)) < cutoff_date:
                    continue
            except ValueError:
                continue
        if not isinstance(hours, dict):
            continue
        for bucket in hours.values():
            bucket_in, bucket_out = traffic_bucket_totals(bucket)
            incoming += bucket_in
            outgoing += bucket_out
    return incoming, outgoing


def retained_history_gap(
    name: str,
    traffic_entry: dict[str, Any],
    client_entry: dict[str, Any],
    cutoff_date: date,
) -> RetainedHistoryGap | None:
    created = client_created_date(client_entry)
    if created is None or created < cutoff_date:
        return None

    total_in = int(traffic_entry.get("incoming") or 0)
    total_out = int(traffic_entry.get("outgoing") or 0)
    history_in, history_out = history_totals(traffic_entry, cutoff_date=cutoff_date)
    missing_in = max(0, total_in - history_in)
    missing_out = max(0, total_out - history_out)
    if missing_in <= 0 and missing_out <= 0:
        return None
    return RetainedHistoryGap(
        name=name,
        missing_incoming=missing_in,
        missing_outgoing=missing_out,
        total_incoming=total_in,
        total_outgoing=total_out,
        history_incoming=history_in,
        history_outgoing=history_out,
    )


def retained_history_gaps(
    traffic_db: dict[str, Any],
    client_entries: dict[str, Any],
    cutoff_date: date,
) -> list[RetainedHistoryGap]:
    traffic_clients = traffic_db.get("clients", {})
    if not isinstance(traffic_clients, dict):
        return []
    gaps = []
    for name, entry in traffic_clients.items():
        if not isinstance(entry, dict):
            continue
        client_entry = client_entries.get(name, {})
        if not isinstance(client_entry, dict):
            continue
        gap = retained_history_gap(str(name), entry, client_entry, cutoff_date)
        if gap:
            gaps.append(gap)
    return gaps


def repair_bucket_time(entry: dict[str, Any], bucket_time: datetime) -> datetime:
    parsed = parse_timestamp(entry.get("updated") or entry.get("lastOnline") or entry.get("lastAccepted"))
    if parsed is None:
        return bucket_time
    return parsed.astimezone(bucket_time.tzinfo)


def add_history_delta(entry: dict[str, Any], bucket_time: datetime, incoming: int, outgoing: int) -> None:
    history = entry.setdefault("history", {})
    if not isinstance(history, dict):
        history = {}
        entry["history"] = history
    day_key = bucket_time.strftime("%Y-%m-%d")
    hour_key = bucket_time.strftime("%H")
    bucket = history.setdefault(day_key, {}).setdefault(hour_key, {"incoming": 0, "outgoing": 0})
    bucket["incoming"] = int(bucket.get("incoming") or 0) + int(incoming or 0)
    bucket["outgoing"] = int(bucket.get("outgoing") or 0) + int(outgoing or 0)


def repair_retained_history_gaps(
    traffic_db: dict[str, Any],
    client_entries: dict[str, Any],
    cutoff_date: date,
    bucket_time: datetime,
) -> list[RetainedHistoryGap]:
    repaired = retained_history_gaps(traffic_db, client_entries, cutoff_date)
    traffic_clients = traffic_db.get("clients", {})
    if not isinstance(traffic_clients, dict):
        return []
    for gap in repaired:
        entry = traffic_clients.get(gap.name, {})
        if not isinstance(entry, dict):
            continue
        add_history_delta(
            entry,
            repair_bucket_time(entry, bucket_time),
            gap.missing_incoming,
            gap.missing_outgoing,
        )
    return repaired
