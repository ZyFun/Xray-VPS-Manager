"""Client-facing Telegram traffic reports."""

from __future__ import annotations

from datetime import datetime, timedelta
import html
from typing import Callable

from xray_vps_manager.core.terminal import table_lines
from xray_vps_manager.telegram import subscriptions
from xray_vps_manager.traffic import history
from xray_vps_manager.traffic.formatting import format_traffic
from xray_vps_manager.traffic.repository import traffic_entry


TRAFFIC_REPORT_KINDS = {"day", "day-hours", "week-days"}


def traffic_menu_text() -> str:
    return "Выбери, какую статистику трафика показать."


def local_today(display_timezone: Callable[[], tuple[object, str]], now: datetime | None = None):
    tzinfo, label = display_timezone()
    current = now or datetime.now(tzinfo)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tzinfo)
    local = current.astimezone(tzinfo)
    return local.date(), label


def html_pre(lines: list[str]) -> str:
    return "<pre>" + html.escape("\n".join(lines), quote=False) + "</pre>"


def day_summary_lines(entry: dict, day) -> list[str]:
    incoming, outgoing = history.day_total(entry, day)
    return table_lines(
        ["PERIOD", "IN", "OUT", "TOTAL"],
        [[day.isoformat(), format_traffic(incoming), format_traffic(outgoing), format_traffic(incoming + outgoing)]],
        enable_ansi=False,
    )


def day_hour_lines(entry: dict, day) -> list[str]:
    return table_lines(
        ["HOUR", "IN", "OUT", "TOTAL"],
        history.day_hour_totals(entry, day, format_traffic),
        enable_ansi=False,
    )


def week_day_lines(entry: dict, today) -> list[str]:
    start = today - timedelta(days=6)
    return table_lines(
        ["DATE", "IN", "OUT", "TOTAL"],
        history.period_day_rows(entry, start, today, format_traffic),
        enable_ansi=False,
    )


def report_title(kind: str, today, timezone_label: str) -> str:
    if kind == "day":
        return f"Статистика трафика за сутки\nДата: {today.isoformat()} {timezone_label}"
    if kind == "day-hours":
        return f"Статистика трафика за сутки по часам\nДата: {today.isoformat()} {timezone_label}"
    if kind == "week-days":
        start = today - timedelta(days=6)
        return f"Статистика трафика за неделю по дням\nПериод: {start.isoformat()} - {today.isoformat()} {timezone_label}"
    raise ValueError("Неизвестный отчёт по трафику.")


def report_lines(kind: str, entry: dict, today) -> list[str]:
    if kind == "day":
        return day_summary_lines(entry, today)
    if kind == "day-hours":
        return day_hour_lines(entry, today)
    if kind == "week-days":
        return week_day_lines(entry, today)
    raise ValueError("Неизвестный отчёт по трафику.")


def traffic_report_for_chat(
    db: dict,
    chat_id: str,
    client_db: dict,
    traffic_db: dict,
    display_timezone: Callable[[], tuple[object, str]],
    kind: str,
    now: datetime | None = None,
) -> tuple[str, str | None]:
    if kind not in TRAFFIC_REPORT_KINDS:
        return "Неизвестный отчёт по трафику.", None
    name, _entry, error = subscriptions.subscription_entry_for_chat(db, chat_id, client_db)
    if error:
        return error, None
    today, timezone_label = local_today(display_timezone, now=now)
    traffic = traffic_entry(traffic_db, name)
    text = "\n\n".join(
        [
            report_title(kind, today, timezone_label),
            html_pre(report_lines(kind, traffic, today)),
        ]
    )
    return text, "HTML"
