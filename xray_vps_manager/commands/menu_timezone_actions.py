"""Timezone actions used by the interactive menu."""

from __future__ import annotations

import re
from collections.abc import Callable
from zoneinfo import available_timezones

from xray_vps_manager.commands import menu_status
from xray_vps_manager.core.terminal import table_border, table_row

TIMEZONE_PRESETS = [
    ("", "Системное время сервера"),
    ("Europe/Moscow", "Москва"),
    ("Europe/Kaliningrad", "Калининград"),
    ("Europe/Samara", "Самара"),
    ("Asia/Yekaterinburg", "Екатеринбург"),
    ("Asia/Omsk", "Омск"),
    ("Asia/Novosibirsk", "Новосибирск"),
    ("Asia/Krasnoyarsk", "Красноярск"),
    ("Asia/Irkutsk", "Иркутск"),
    ("Asia/Yakutsk", "Якутск"),
    ("Asia/Vladivostok", "Владивосток"),
    ("Asia/Magadan", "Магадан"),
    ("Asia/Sakhalin", "Сахалин"),
    ("Asia/Kamchatka", "Камчатка"),
    ("UTC", "UTC"),
]
TIMEZONE_SEARCH_LIMIT = 30

CommandRunner = Callable[[list[str]], None]


def timezone_value_label(value: str) -> str:
    return value or "server"


def print_timezone_selection_table(rows: list[tuple[str, str]], include_search: bool = False) -> None:
    headers = ("№", "TIMEZONE", "ОПИСАНИЕ")
    values = [
        (str(index), timezone_value_label(value), label)
        for index, (value, label) in enumerate(rows, start=1)
    ]
    if include_search:
        values.append(("S", "Поиск", "найти другой часовой пояс"))
    values.append(("0", "Назад", ""))
    widths = [
        max(len(headers[column]), *(len(str(row[column])) for row in values))
        for column in range(len(headers))
    ]
    border = table_border(widths)
    print(border)
    print(table_row(headers, widths))
    print(border)
    for row in values:
        print(table_row(row, widths))
    print(border)


def timezone_search_matches(query: str) -> list[tuple[str, str]]:
    needle = query.strip().lower()
    if not needle:
        return []
    try:
        zones = sorted(available_timezones())
    except Exception:
        zones = sorted(value for value, _ in TIMEZONE_PRESETS if value)
    return [(zone, "") for zone in zones if needle in zone.lower()][:TIMEZONE_SEARCH_LIMIT]


def choose_timezone_from_rows(rows: list[tuple[str, str]], prompt: str) -> str | None:
    while True:
        choice = input(prompt).strip()
        if choice in ("", "0"):
            return None
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1][0]
        print("Неизвестный часовой пояс. Выбери номер из списка или 0 для возврата.")


def search_timezone() -> str | None:
    while True:
        query = input("Фильтр timezone, например Moscow или Europe (Enter - назад): ").strip()
        if not query:
            return None
        matches = timezone_search_matches(query)
        if not matches:
            print("По этому фильтру ничего не найдено.")
            continue
        print_timezone_selection_table(matches)
        selected = choose_timezone_from_rows(matches, "Часовой пояс: ")
        if selected is not None:
            return selected


def choose_timezone() -> str | None:
    current = menu_status.configured_timezone_name() or "server"
    print(f"Текущее значение: {current}")
    while True:
        print_timezone_selection_table(TIMEZONE_PRESETS, include_search=True)
        choice = input("Часовой пояс: ").strip().lower()
        if choice in ("", "0"):
            return None
        if choice in ("s", "search", "поиск"):
            selected = search_timezone()
            if selected is not None:
                return selected
            continue
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(TIMEZONE_PRESETS):
                return TIMEZONE_PRESETS[index - 1][0]
        print("Неизвестный часовой пояс. Выбери номер из списка, S для поиска или 0 для возврата.")


def show_timezone(call: CommandRunner) -> None:
    call(["xray-client", "timezone"])


def update_timezone(call: CommandRunner) -> None:
    print("MANAGER_TIMEZONE: часовой пояс для сроков доступа, лимитов трафика, отчётов и отображения времени.")
    print("Выбери значение из списка, чтобы не ошибиться при ручном вводе.")
    value = choose_timezone()
    if value is None:
        print("Изменение отменено.")
        return
    call(["xray-client", "set-timezone", value])
    print("Новая настройка будет использоваться в следующих расчётах и выводе времени.")
