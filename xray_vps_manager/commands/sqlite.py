"""Команды администрирования SQLite."""

import sys

from xray_vps_manager.core.errors import StateError
from xray_vps_manager.core.paths import paths
from xray_vps_manager.db.database import DATABASE_RECOVERY_HINT
from xray_vps_manager.runner import command_entry
from xray_vps_manager.system import sqlite_admin


def _single_line(text: str) -> str:
    """Экранируем управляющие символы и обратный слеш в данных, выводимых в терминал."""
    return repr(text)[1:-1]


def status() -> int:
    print(f"Database: {_single_line(str(paths().manager_db))}")
    try:
        report = sqlite_admin.status()
    except StateError as exc:
        # Имена из SQLite могут попасть как в основную ошибку, так и в результат quick_check.
        raise StateError(
            _single_line(exc.message), hint=_single_line(exc.hint), detail=_single_line(exc.detail),
        ) from None
    if report is None:
        print("Status: missing")
        print(DATABASE_RECOVERY_HINT)
        return 1

    print(f"Schema: {report.version}")
    print(f"Quick check: {_single_line(report.quick_check)}")
    for name, size in report.file_sizes.items():
        print(f"Size {_single_line(name)}: {size} bytes")
    for table, count in report.counts.items():
        print(f"{_single_line(table)}: {count if count is not None else 'unavailable'}")
    return 0 if report.quick_check == "ok" else 1


def usage() -> None:
    print(
        """Usage:
  xray-vps-manager sqlite status
  xray-vps-manager sqlite init [--from-install]
  xray-vps-manager sqlite migrate
"""
    )


@command_entry("sqlite")
def main() -> None:
    command = sys.argv[1]
    if command == "status":
        sys.exit(status())
    if command == "init":
        sqlite_admin.initialize(from_install="--from-install" in sys.argv[2:])
    elif command == "migrate":
        sqlite_admin.migrate()


if __name__ == "__main__":
    main()
