"""Backup actions used by the interactive menu."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from xray_vps_manager.commands import menu_local_transfer
from xray_vps_manager.core.terminal import table_border, table_row

CommandRunner = Callable[[list[str]], None]
ConfirmCallback = Callable[[str], bool]


def list_backups(call: CommandRunner) -> None:
    call(["xray-backup", "list"])


def backup_rows_for_selection() -> list[dict[str, str]]:
    result = subprocess.run(
        ["xray-backup", "list", "--plain"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip() or "Не удалось получить список бэкапов.")
        return []

    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        path, created, size = parts[:3]
        rows.append({
            "path": path,
            "file": Path(path).name,
            "created": created,
            "size": size,
        })
    return rows


def print_backup_selection_table(rows: list[dict[str, str]]) -> None:
    headers = ("№", "FILE", "CREATED", "SIZE")
    values = [
        (
            str(index),
            row["file"],
            row["created"],
            row["size"],
        )
        for index, row in enumerate(rows, start=1)
    ]
    values.append(("0", "Назад", "", ""))
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


def choose_backup(action: str) -> str:
    rows = backup_rows_for_selection()
    if not rows:
        print("Бэкапы на сервере не найдены.")
        return ""

    print(f"Выбери бэкап для действия: {action}.")
    print_backup_selection_table(rows)
    while True:
        choice = input("Бэкап: ").strip()
        if choice == "0":
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1]["path"]
        print("Неизвестный бэкап. Выбери номер из списка или 0 для возврата.")


def create_backup_server(call: CommandRunner) -> None:
    call(["xray-backup", "create"])


def create_backup_download_command(call: CommandRunner) -> None:
    result = subprocess.run(
        ["xray-backup", "create", "--path-only"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip() or "Не удалось создать бэкап.")
        return

    archive = result.stdout.strip().splitlines()[-1]
    print(f"Бэкап создан на сервере: {archive}")
    print()
    print("Ниже будет команда для локального терминала на компьютере, с которого нужно скачать архив.")
    default_target = menu_local_transfer.default_ssh_target()
    ssh_target = input(f"SSH target/user@host [{default_target}]: ").strip() or default_target
    default_dir = menu_local_transfer.choose_local_download_dir()
    local_dir = input(f"Куда сохранить на локальной машине [{default_dir}]: ").strip() or default_dir
    print()
    call(["xray-backup", "download-command", archive, ssh_target, local_dir])


def show_backup_upload_command(call: CommandRunner) -> None:
    print("Эта команда нужна, если архив лежит на локальной машине и его надо загрузить на сервер перед восстановлением.")
    default_target = menu_local_transfer.default_ssh_target()
    ssh_target = input(f"SSH target/user@host [{default_target}]: ").strip() or default_target
    default_file = menu_local_transfer.backup_file_from_dir(menu_local_transfer.choose_local_download_dir())
    local_file = input(f"Путь к архиву на локальной машине [{default_file}]: ").strip() or default_file
    print()
    call(["xray-backup", "upload-command", ssh_target, local_file])


def restore_backup_from_menu(call: CommandRunner, confirm: ConfirmCallback) -> None:
    archive = choose_backup("восстановления")
    if not archive:
        print("Действие отменено.")
        return
    print()
    print(f"Будет восстановлен бэкап: {archive}")
    print("Текущие config.json, clients.json, server.env и traffic.json будут заменены данными из архива.")
    print("Перед восстановлением будет автоматически создан pre-restore бэкап текущего состояния.")
    if not confirm("Продолжить восстановление"):
        print("Восстановление отменено.")
        return
    call(["xray-backup", "restore", archive])


def delete_backup_from_menu(call: CommandRunner, confirm: ConfirmCallback) -> None:
    archive = choose_backup("удаления")
    if not archive:
        print("Действие отменено.")
        return
    print()
    print(f"Будет удалён бэкап: {archive}")
    print("Это действие удалит только архив на сервере. Данные Xray не изменятся.")
    if not confirm("Удалить выбранный бэкап"):
        print("Удаление отменено.")
        return
    call(["xray-backup", "delete", archive])
