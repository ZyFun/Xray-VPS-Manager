"""Suspicious activity exception actions used by the interactive menu."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

from xray_vps_manager.core.terminal import table_border, table_row

CommandRunner = Callable[[list[str]], None]
ConfirmCallback = Callable[[str], bool]
DaysPrompt = Callable[[int], str]


def show_activity_exceptions(call: CommandRunner) -> None:
    call(["xray-activity", "exceptions"])


def activity_exception_rows() -> list[dict[str, str]]:
    result = subprocess.run(
        ["xray-activity", "exceptions", "--plain"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip() or "Не удалось получить список исключений активности.")
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        value, kind, created, source = parts[:4]
        rows.append({
            "value": value,
            "kind": kind,
            "created": created,
            "source": source,
        })
    return rows


def print_activity_exception_table(rows: list[dict[str, str]]) -> None:
    headers = ("№", "VALUE", "KIND", "CREATED", "SOURCE")
    values = [
        (str(index), row["value"], row["kind"], row["created"], row["source"])
        for index, row in enumerate(rows, start=1)
    ]
    values.append(("0", "Назад", "", "", ""))
    widths = [
        max(len(headers[column]), *(len(str(row[column])) for row in values))
        for column in range(len(headers))
    ]
    border = table_border(widths)
    print(border)
    print(table_row(headers, widths))
    print(border)
    for index, row in enumerate(values):
        print(table_row(row, widths, row_index=index))
    print(border)


def choose_activity_exception(action: str) -> str:
    rows = activity_exception_rows()
    if not rows:
        print("Исключения suspicious не настроены.")
        return ""
    print(f"Выбери исключение для действия: {action}.")
    print_activity_exception_table(rows)
    while True:
        choice = input("Исключение: ").strip()
        if choice == "0":
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1]["value"]
        print("Неизвестное исключение. Выбери номер из списка или 0 для возврата.")


def activity_exception_candidate_rows(days: str) -> list[dict[str, str]]:
    result = subprocess.run(
        ["xray-activity", "exception-candidates", days, "--plain"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip() or "Не удалось получить кандидатов для исключений.")
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        value, kind, events, clients, risks, ports, last_seen, sample = parts[:8]
        rows.append({
            "value": value,
            "kind": kind,
            "events": events,
            "clients": clients,
            "risks": risks,
            "ports": ports,
            "lastSeen": last_seen,
            "sample": sample,
        })
    return rows


def print_activity_exception_candidate_table(rows: list[dict[str, str]]) -> None:
    headers = ("№", "VALUE", "KIND", "EVENTS", "CLIENTS", "RISKS", "PORTS", "LAST SEEN")
    values = [
        (
            str(index),
            row["value"],
            row["kind"],
            row["events"],
            row["clients"],
            row["risks"],
            row["ports"],
            row["lastSeen"],
        )
        for index, row in enumerate(rows, start=1)
    ]
    values.append(("0", "Назад", "", "", "", "", "", ""))
    widths = [
        max(len(headers[column]), *(len(str(row[column])) for row in values))
        for column in range(len(headers))
    ]
    border = table_border(widths)
    print(border)
    print(table_row(headers, widths))
    print(border)
    for index, row in enumerate(values):
        print(table_row(row, widths, row_index=index))
    print(border)


def choose_activity_exception_candidate(ask_activity_days: DaysPrompt) -> str:
    days = ask_activity_days(7)
    rows = activity_exception_candidate_rows(days)
    if not rows:
        print("Кандидаты для исключений не найдены.")
        return ""
    print("Выбери адрес или IP из подозрительной активности, который нужно добавить в исключения.")
    print("Уже добавленные исключения в этот список не попадают.")
    print_activity_exception_candidate_table(rows[:50])
    while True:
        choice = input("Кандидат: ").strip()
        if choice == "0":
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= min(len(rows), 50):
                return rows[index - 1]["value"]
        print("Неизвестный кандидат. Выбери номер из списка или 0 для возврата.")


def activity_exception_add_from_suspicious(call: CommandRunner, ask_activity_days: DaysPrompt) -> None:
    value = choose_activity_exception_candidate(ask_activity_days)
    if not value:
        print("Действие отменено.")
        return
    call(["xray-activity", "exception-add", value, "suspicious-menu"])


def activity_exception_add_manual(call: CommandRunner) -> None:
    print("Введи домен, IP, CIDR или маску для исключения из suspicious.")
    print("Примеры: mask.icloud.com, *.apple.com, 203.0.113.10, 203.0.113.0/24")
    print("Исключение не удаляет события из журнала, а скрывает совпавшие цели из suspicious/GeoIP-рисков.")
    value = input("Исключение: ").strip()
    if not value:
        print("Действие отменено.")
        return
    call(["xray-activity", "exception-add", value, "manual-menu"])


def activity_exception_delete_from_menu(call: CommandRunner) -> None:
    value = choose_activity_exception("удаления")
    if not value:
        print("Действие отменено.")
        return
    call(["xray-activity", "exception-delete", value])


def activity_exception_delete_all_from_menu(call: CommandRunner, confirm: ConfirmCallback) -> None:
    rows = activity_exception_rows()
    if not rows:
        print("Исключения suspicious не настроены.")
        return
    print()
    print(f"Будут удалены все исключения suspicious: {len(rows)}")
    print("Журнал активности, клиенты и конфигурация Xray не изменятся.")
    if not confirm("Удалить все исключения suspicious"):
        print("Удаление отменено.")
        return
    call(["xray-activity", "exception-delete-all", "--yes"])
