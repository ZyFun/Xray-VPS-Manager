"""Shared local transfer prompts used by interactive menu actions."""

from __future__ import annotations

from xray_vps_manager.core.server_env import read_server_env


def default_ssh_target() -> str:
    server_addr = (read_server_env().get("SERVER_ADDR") or "").strip().strip('"').strip("'")
    if server_addr:
        return server_addr if "@" in server_addr else f"root@{server_addr}"
    return "root@SERVER_HOST"


def choose_local_download_dir() -> str:
    print("Выбери систему компьютера, где будет выполняться команда scp.")
    print("1) macOS: ~/Downloads")
    print("2) Linux: ~/Downloads")
    print("3) Windows: %USERPROFILE%/Downloads")
    print("4) Свой путь")
    while True:
        choice = input("Система [1]: ").strip() or "1"
        if choice == "1":
            return "~/Downloads"
        if choice == "2":
            return "~/Downloads"
        if choice == "3":
            return "%USERPROFILE%/Downloads"
        if choice == "4":
            value = input("Путь к папке загрузок: ").strip()
            if value:
                return value
            print("Путь не может быть пустым.")
            continue
        print("Выбери 1, 2, 3 или 4.")


def backup_file_from_dir(directory: str) -> str:
    return directory.rstrip("/\\") + "/xray-backup.tar.gz"
