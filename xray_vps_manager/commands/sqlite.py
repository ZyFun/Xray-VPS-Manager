#!/usr/bin/env python3
"""SQLite status helper commands."""

from __future__ import annotations

import sys

from xray_vps_manager.core.paths import MANAGER_DB_PATH
from xray_vps_manager.db import database, schema
from xray_vps_manager.db.storage import sqlite_read_ready


COUNT_TABLES = {
    "connections": "reality_connections",
    "clients": "clients",
    "traffic": "traffic_totals",
    "activity_events": "activity_events",
    "activity_alert_events": "activity_alert_events",
    "activity_client_counters": "activity_client_counters",
    "activity_exceptions": "activity_exceptions",
    "activity_blocklist": "activity_blocklist",
    "activity_blocklist_hits": "activity_blocklist_hits",
    "xray_error_events": "xray_error_events",
    "telegram_subscriptions": "telegram_subscriptions",
}


def table_count(connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def database_counts(connection) -> dict[str, int]:
    return {label: table_count(connection, table) for label, table in COUNT_TABLES.items()}


def print_counts(counts: dict[str, int]) -> None:
    for key in sorted(counts):
        print(f"{key}: {counts[key]}")


def status() -> int:
    print(f"Database: {MANAGER_DB_PATH}")
    if not MANAGER_DB_PATH.exists():
        print("Status: missing")
        print("Run install.sh or restore a backup that contains manager.db.")
        return 1

    connection = database.open_database(MANAGER_DB_PATH, initialize=False)
    try:
        print(f"Schema: {schema.schema_version(connection)}")
        print(f"Quick check: {database.quick_check(connection)}")
        print(f"SQLite ready: {'yes' if sqlite_read_ready(connection) else 'no'}")
        print_counts(database_counts(connection))
    finally:
        connection.close()
    return 0


def usage() -> None:
    print(
        """Usage:
  xray-vps-manager sqlite status
"""
    )


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        usage()
        return

    command = sys.argv[1]
    args = sys.argv[2:]
    if command == "status" and not args:
        sys.exit(status())

    usage()
    sys.exit(1)


if __name__ == "__main__":
    main()
