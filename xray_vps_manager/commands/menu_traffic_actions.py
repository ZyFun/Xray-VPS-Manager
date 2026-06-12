"""Traffic report actions used by the interactive menu."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from xray_vps_manager.commands import menu_client_actions
from xray_vps_manager.core.paths import XRAY_TRAFFIC_SYNC
from xray_vps_manager.core.terminal import table_border, table_row, visible_len
from xray_vps_manager.core.time import manager_timezone
from xray_vps_manager.traffic.formatting import format_traffic
from xray_vps_manager.traffic.history import all_time_total, month_total
from xray_vps_manager.traffic.repository import load_traffic_db, traffic_entry

CommandRunner = Callable[[list[str]], None]


def die(message: str) -> None:
    raise SystemExit(message)


def local_today() -> date:
    timezone, _ = manager_timezone()
    return datetime.now(timezone).date()


def parse_date_value(value: str, label: str = "DATE") -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        die(f"{label} must be in YYYY-MM-DD format.")


def parse_month_value(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value or ""):
        die("MONTH must be in YYYY-MM format.")
    year, month = (int(part, 10) for part in value.split("-", 1))
    if month < 1 or month > 12:
        die("MONTH must be in YYYY-MM format.")
    return f"{year:04d}-{month:02d}"


def current_month_key() -> str:
    today = local_today()
    return f"{today.year:04d}-{today.month:02d}"


def sync_traffic_quiet(sync_path: Path = XRAY_TRAFFIC_SYNC) -> None:
    if sync_path.exists():
        try:
            subprocess.run([str(sync_path), "--quiet"], check=False, timeout=10)
        except subprocess.TimeoutExpired:
            pass


def traffic_rows_for_selection(month_key: str) -> list[dict]:
    sync_traffic_quiet()
    traffic_db = load_traffic_db()
    rows = []
    today = local_today()
    for row in menu_client_actions.client_rows_for_selection("all"):
        entry = traffic_entry(traffic_db, row["name"])
        month_in, month_out = month_total(entry, month_key, today=today)
        all_in, all_out = all_time_total(entry)
        rows.append({
            "name": row["name"],
            "status": row["status"],
            "connectionName": row["connectionName"],
            "monthIn": month_in,
            "monthOut": month_out,
            "monthTotal": month_in + month_out,
            "allTimeTotal": all_in + all_out,
        })
    return rows


def print_traffic_selection_table(rows: list[dict]) -> None:
    headers = ("№", "CONNECTION", "NAME", "STATUS", "MONTH IN", "MONTH OUT", "MONTH TOTAL", "ALL TIME")
    values = [
        [
            str(index),
            row["connectionName"],
            row["name"],
            row["status"],
            format_traffic(row["monthIn"]),
            format_traffic(row["monthOut"]),
            format_traffic(row["monthTotal"]),
            format_traffic(row["allTimeTotal"]),
        ]
        for index, row in enumerate(rows, start=1)
    ]
    values.append(["0", "Назад", "", "", "", "", "", ""])
    widths = [
        max(visible_len(headers[column]), *(visible_len(row[column]) for row in values))
        for column in range(len(headers))
    ]
    border = table_border(widths)
    print(border)
    print(table_row(headers, widths))
    print(border)
    for row in values:
        print(table_row(row, widths))
    print(border)


def choose_traffic_client() -> str:
    month_key = current_month_key()
    rows = traffic_rows_for_selection(month_key)
    if not rows:
        print("Нет клиентов для просмотра трафика.")
        return ""

    print(f"Трафик за текущий месяц: {month_key}")
    print_traffic_selection_table(rows)
    while True:
        choice = input("Клиент: ").strip()
        if choice == "0":
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1]["name"]
        print("Неизвестный клиент. Выбери номер из списка или 0 для возврата.")


def prompt_date(default: str, label: str, description: str) -> str:
    print(description)
    value = input(f"{label} [{default}]: ").strip() or default
    return parse_date_value(value, label).isoformat()


def prompt_month(default: str, description: str) -> str:
    print(description)
    value = input(f"MONTH [{default}]: ").strip() or default
    return parse_month_value(value)


def show_traffic_day(call: CommandRunner, name: str) -> None:
    today = local_today().isoformat()
    day = prompt_date(today, "DATE", "DATE: день отчёта в формате YYYY-MM-DD. Вывод будет по часам.")
    call(["xray-client", "traffic-day", name, day])


def show_traffic_week(call: CommandRunner, name: str) -> None:
    default = (local_today() - timedelta(days=6)).isoformat()
    start = prompt_date(default, "START_DATE", "START_DATE: первый день 7-дневного периода в формате YYYY-MM-DD.")
    call(["xray-client", "traffic-week", name, start])


def show_traffic_month(call: CommandRunner, name: str) -> None:
    month = prompt_month(current_month_key(), "MONTH: месяц отчёта в формате YYYY-MM. Вывод будет по дням.")
    call(["xray-client", "traffic-month", name, month])


def show_traffic_period(call: CommandRunner, name: str) -> None:
    today = local_today().isoformat()
    start = prompt_date(today, "START_DATE", "START_DATE: первый день периода в формате YYYY-MM-DD.")
    end = prompt_date(today, "END_DATE", "END_DATE: последний день периода в формате YYYY-MM-DD.")
    call(["xray-client", "traffic-period", name, start, end])
