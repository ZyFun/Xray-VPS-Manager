"""Команды администрирования SQLite."""

import sys

from xray_vps_manager.core.paths import paths
from xray_vps_manager.db.database import DATABASE_RECOVERY_HINT
from xray_vps_manager.runner import command_entry
from xray_vps_manager.system import sqlite_admin


def status() -> int:
    print(f"Database: {paths().manager_db}")
    report = sqlite_admin.status()
    if report is None:
        print("Status: missing")
        print(DATABASE_RECOVERY_HINT)
        return 1

    print(f"Schema: {report.version}")
    print(f"Quick check: {report.quick_check}")
    for key in sorted(report.counts):
        print(f"{key}: {report.counts[key]}")
    return 0


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
