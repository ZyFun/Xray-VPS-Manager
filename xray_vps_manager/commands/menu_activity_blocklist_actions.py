"""Global activity blocklist actions used by the interactive menu."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

from xray_vps_manager.core.terminal import table_border, table_row

CommandRunner = Callable[[list[str]], None]
ClientChooser = Callable[[str, str], str]
DaysPrompt = Callable[[int], str]


def show_activity_blocklist(call: CommandRunner) -> None:
    call(["xray-activity", "blocklist"])


def show_activity_block_stats(call: CommandRunner) -> None:
    call(["xray-activity", "block-stats"])


def sync_activity_blocklist(call: CommandRunner) -> None:
    call(["xray-activity", "block-sync"])


def blocklist_rows() -> list[dict[str, str]]:
    result = subprocess.run(
        ["xray-activity", "blocklist", "--plain"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip() or "Не удалось получить список блокировок.")
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        block_id, value, kind, client, created, expires, status, last_hit, comment = parts[:9]
        rows.append(
            {
                "id": block_id,
                "value": value,
                "kind": kind,
                "client": client,
                "created": created,
                "expires": expires,
                "status": status,
                "lastHit": last_hit,
                "comment": comment,
            }
        )
    return rows


def print_blocklist_table(rows: list[dict[str, str]]) -> None:
    headers = ("№", "CLIENT", "VALUE", "KIND", "STATUS", "EXPIRES", "LAST HIT", "COMMENT")
    values = [
        (
            str(index),
            row["client"] or "-",
            row["value"],
            row["kind"],
            row["status"],
            row["expires"],
            row["lastHit"] or "-",
            row["comment"],
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
    for row in values:
        print(table_row(row, widths))
    print(border)


def choose_block(action: str) -> dict[str, str] | None:
    rows = blocklist_rows()
    if not rows:
        print("Глобальные блокировки не настроены.")
        return None
    print(f"Выбери блокировку для действия: {action}.")
    print_blocklist_table(rows)
    while True:
        choice = input("Блокировка: ").strip()
        if choice == "0":
            return None
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1]
        print("Неизвестная блокировка. Выбери номер из списка или 0 для возврата.")


def candidate_rows(client_name: str, days: str, region: str = "RU") -> list[dict[str, str]]:
    result = subprocess.run(
        ["xray-activity", "block-candidates", client_name, days, region, "--plain"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip() or "Не удалось получить кандидатов для блокировки.")
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        value, kind, events, ports, last_seen, sample, event_id = parts[:7]
        rows.append(
            {
                "value": value,
                "kind": kind,
                "events": events,
                "ports": ports,
                "lastSeen": last_seen,
                "sample": sample,
                "eventId": event_id,
            }
        )
    return rows


def print_candidate_table(rows: list[dict[str, str]]) -> None:
    headers = ("№", "VALUE", "KIND", "EVENTS", "PORTS", "LAST SEEN")
    values = [
        (
            str(index),
            row["value"],
            row["kind"],
            row["events"],
            row["ports"],
            row["lastSeen"],
        )
        for index, row in enumerate(rows, start=1)
    ]
    values.append(("0", "Назад", "", "", "", ""))
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


def choose_candidate_from_geoip_ru(
    choose_client: ClientChooser,
    ask_activity_days: DaysPrompt,
) -> tuple[str, dict[str, str] | None]:
    client_name = choose_client("добавления блокировки из GeoIP RU", "all")
    if not client_name:
        return "", None
    days = ask_activity_days(7)
    rows = candidate_rows(client_name, days, "RU")
    if not rows:
        print("Кандидаты GeoIP RU для этого клиента не найдены.")
        return client_name, None
    print("Выбери IP или домен из GeoIP RU-событий клиента.")
    print("Блокировка будет глобальной: она сработает для всех клиентов.")
    displayed = rows[:50]
    print_candidate_table(displayed)
    while True:
        choice = input("Кандидат: ").strip()
        if choice == "0":
            return client_name, None
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(displayed):
                return client_name, displayed[index - 1]
        print("Неизвестный кандидат. Выбери номер из списка или 0 для возврата.")


def ask_block_duration() -> str:
    print("Срок блокировки:")
    print("1 - бессрочно")
    print("2 - количество дней")
    while True:
        choice = input("Срок: ").strip()
        if choice in ("", "1"):
            return "forever"
        if choice == "2":
            days = input("Количество дней: ").strip()
            if re.fullmatch(r"[0-9]+", days) and int(days, 10) >= 1:
                return days
            print("Количество дней должно быть положительным числом.")
            continue
        print("Выбери 1 для бессрочной блокировки или 2 для срока в днях.")


def ask_block_comment() -> str:
    return input("Комментарий (можно оставить пустым): ").strip()


def add_block_from_geoip_ru(
    choose_client: ClientChooser,
    call: CommandRunner,
    ask_activity_days: DaysPrompt,
) -> None:
    client_name, row = choose_candidate_from_geoip_ru(choose_client, ask_activity_days)
    if not client_name or not row:
        print("Действие отменено.")
        return
    comment = ask_block_comment()
    duration = ask_block_duration()
    call(["xray-activity", "block-add", row["value"], client_name, duration, comment, row["eventId"]])


def add_block_manual(call: CommandRunner) -> None:
    print("Введи домен, IP или CIDR для глобальной блокировки.")
    print("Примеры: example.com, 203.0.113.10, 203.0.113.0/24")
    value = input("Адрес или домен: ").strip()
    if not value:
        print("Действие отменено.")
        return
    comment = ask_block_comment()
    duration = ask_block_duration()
    call(["xray-activity", "block-add", value, "", duration, comment])


def delete_block_from_menu(call: CommandRunner) -> None:
    row = choose_block("удаления")
    if not row:
        print("Действие отменено.")
        return
    call(["xray-activity", "block-delete", row["id"]])
