"""Activity export archive actions used by the interactive menu."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xray_vps_manager.commands import menu_local_transfer
from xray_vps_manager.core.terminal import table_border, table_row

CommandRunner = Callable[[list[str]], None]
ConfirmCallback = Callable[[str], bool]
ClientChooser = Callable[[str, str], str]


def list_activity_exports(call: CommandRunner) -> None:
    call(["xray-activity", "export-list"])


def activity_export_rows_for_selection() -> list[dict[str, str]]:
    result = subprocess.run(
        ["xray-activity", "export-list", "--plain"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip() or "Не удалось получить список экспортов активности.")
        return []

    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        path, created, size, client, period, events = parts[:6]
        rows.append({
            "path": path,
            "file": Path(path).name,
            "created": created,
            "size": size,
            "client": client,
            "period": period,
            "events": events,
        })
    return rows


def print_activity_export_selection_table(rows: list[dict[str, str]]) -> None:
    headers = ("№", "FILE", "CREATED", "SIZE", "CLIENT", "PERIOD", "EVENTS")
    values = [
        (
            str(index),
            row["file"],
            row["created"],
            row["size"],
            row["client"],
            row["period"],
            row["events"],
        )
        for index, row in enumerate(rows, start=1)
    ]
    values.append(("0", "Назад", "", "", "", "", ""))
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


def choose_activity_export(action: str) -> str:
    rows = activity_export_rows_for_selection()
    if not rows:
        print("Архивы экспорта активности на сервере не найдены.")
        return ""

    print(f"Выбери архив экспорта активности для действия: {action}.")
    print_activity_export_selection_table(rows)
    while True:
        choice = input("Архив: ").strip()
        if choice == "0":
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1]["path"]
        print("Неизвестный архив. Выбери номер из списка или 0 для возврата.")


def activity_export_report(choose_client: ClientChooser, call: CommandRunner) -> None:
    name = choose_client("экспорта журнала активности", "all")
    if not name:
        print("Действие отменено.")
        return
    today = datetime.now(timezone.utc).date()
    default_start = today - timedelta(days=6)
    start = input(f"START_DATE [{default_start.isoformat()}]: ").strip() or default_start.isoformat()
    end = input(f"END_DATE [{today.isoformat()}]: ").strip() or today.isoformat()
    result = subprocess.run(
        ["xray-activity", "export", name, start, end, "--path-only"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip() or "Не удалось создать экспорт журнала активности.")
        return

    archive = result.stdout.strip().splitlines()[-1]
    print(f"Экспорт создан на сервере: {archive}")
    print()
    print("Ниже будет команда для локального терминала на компьютере, с которого нужно скачать архив.")
    default_target = menu_local_transfer.default_ssh_target()
    ssh_target = input(f"SSH target/user@host [{default_target}]: ").strip() or default_target
    default_dir = menu_local_transfer.choose_local_download_dir()
    local_dir = input(f"Куда сохранить на локальной машине [{default_dir}]: ").strip() or default_dir
    print()
    call(["xray-activity", "download-command", archive, ssh_target, local_dir])


def delete_activity_export_from_menu(call: CommandRunner, confirm: ConfirmCallback) -> None:
    archive = choose_activity_export("удаления")
    if not archive:
        print("Действие отменено.")
        return
    print()
    print(f"Будет удалён архив экспорта активности: {archive}")
    print("Это действие удалит только архив на сервере. Журнал активности и данные Xray не изменятся.")
    if not confirm("Удалить выбранный архив экспорта"):
        print("Удаление отменено.")
        return
    call(["xray-activity", "export-delete", archive])


def delete_all_activity_exports_from_menu(call: CommandRunner, confirm: ConfirmCallback) -> None:
    rows = activity_export_rows_for_selection()
    if not rows:
        print("Архивы экспорта активности не найдены.")
        return
    print()
    print(f"Будут удалены все архивы экспорта активности: {len(rows)}")
    print("Это действие удалит только архивы в /root/xray_activity_exports.")
    print("Журнал активности, traffic.json, clients.json и конфигурация Xray не изменятся.")
    if not confirm("Удалить все архивы экспорта активности"):
        print("Удаление отменено.")
        return
    call(["xray-activity", "export-delete-all", "--yes"])
