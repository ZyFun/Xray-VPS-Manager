"""Traffic report row builders."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from xray_vps_manager.traffic import history
from xray_vps_manager.traffic import settings as traffic_settings
from xray_vps_manager.traffic.formatting import format_traffic
from xray_vps_manager.traffic.repository import credential_traffic_entry, traffic_entry


def month_bounds(month_key: str, today: date | None = None) -> tuple[date, date]:
    return history.month_bounds(month_key, today=today)


def day_hour_rows(entry: dict[str, Any] | None, day: date) -> list[list[str]]:
    return history.day_hour_totals(entry, day, format_traffic)


def period_day_rows(entry: dict[str, Any] | None, start: date, end: date) -> list[list[str]]:
    return history.period_day_rows(entry, start, end, format_traffic)


def credential_period_rows(
    credential_rows: list[dict[str, Any]],
    traffic_db: dict[str, Any],
    start: date,
    end: date,
) -> list[list[str]]:
    table_rows: list[list[str]] = []
    total_in = 0
    total_out = 0
    for row in credential_rows:
        entry = credential_traffic_entry(traffic_db, row["name"], row["connection"])
        incoming, outgoing = history.period_total(entry, start, end)
        total_in += incoming
        total_out += outgoing
        table_rows.append(
            [
                row["protocol"],
                row.get("security") or "-",
                row.get("transport") or "-",
                row["connection"],
                row["status"],
                format_traffic(incoming),
                format_traffic(outgoing),
                format_traffic(incoming + outgoing),
            ]
        )
    if table_rows:
        table_rows.append(
            [
                "TOTAL",
                "-",
                "-",
                "-",
                "-",
                format_traffic(total_in),
                format_traffic(total_out),
                format_traffic(total_in + total_out),
            ]
        )
    return table_rows


def month_summary_rows(
    rows: list[dict[str, Any]],
    traffic_db: dict[str, Any],
    client_entries: dict[str, Any],
    month_key: str,
    *,
    connection_label: Callable[[dict[str, Any]], str],
    limit_label: Callable[[dict[str, Any]], str],
    today: date | None = None,
) -> list[list[str]]:
    table_rows: list[list[str]] = []
    for row in rows:
        entry = traffic_entry(traffic_db, row["name"])
        db_entry = client_entries.get(row["name"], {})
        incoming, outgoing = history.month_total(entry, month_key, today=today)
        all_in, all_out = history.all_time_total(entry)
        table_rows.append(
            [
                row["name"],
                row["status"],
                connection_label(row),
                format_traffic(incoming),
                format_traffic(outgoing),
                format_traffic(incoming + outgoing),
                limit_label(db_entry),
                format_traffic(all_in + all_out),
            ]
        )
    return table_rows


def total_summary_rows(
    total_bytes: int,
    *,
    multiplier_enabled: bool,
    multiplier: Decimal,
) -> list[list[str]]:
    label = traffic_settings.total_multiplier_label(multiplier)
    rows = [["TOTAL", format_traffic(total_bytes)]]
    if multiplier_enabled:
        rows.append(
            [
                f"TOTAL {label}",
                format_traffic(traffic_settings.multiplied_total_bytes(total_bytes, multiplier)),
            ]
        )
    rows.append([f"Множитель {label}", "Вкл" if multiplier_enabled else "Выкл"])
    return rows
