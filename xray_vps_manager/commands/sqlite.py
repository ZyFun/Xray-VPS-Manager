#!/usr/bin/env python3
"""SQLite migration and cutover helper commands."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from xray_vps_manager.core.paths import MANAGER_DB_PATH, SERVER_ENV_PATH
from xray_vps_manager.core.server_env import read_server_env, write_server_env
from xray_vps_manager.db import database, json_import, schema
from xray_vps_manager.db.storage import (
    SQLITE_READS_SERVER_ENV,
    SQLITE_WRITES_SERVER_ENV,
    sqlite_read_ready,
    sqlite_reads_enabled,
    sqlite_writes_enabled,
)


COUNT_TABLES = {
    "connections": "reality_connections",
    "clients": "clients",
    "traffic": "traffic_totals",
    "activity_events": "activity_events",
    "activity_exceptions": "activity_exceptions",
    "telegram_subscriptions": "telegram_subscriptions",
}


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def require_root() -> None:
    if os.geteuid() != 0:
        die("Run this command as root.")


def table_count(connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def database_counts(connection) -> dict[str, int]:
    return {label: table_count(connection, table) for label, table in COUNT_TABLES.items()}


def print_counts(counts: dict[str, int]) -> None:
    for key in sorted(counts):
        print(f"{key}: {counts[key]}")


def status() -> int:
    print(f"Database: {MANAGER_DB_PATH}")
    print(f"Reads flag: {'enabled' if sqlite_reads_enabled() else 'disabled'}")
    print(f"Writes flag: {'enabled' if sqlite_writes_enabled() else 'disabled'}")
    if not MANAGER_DB_PATH.exists():
        print("Status: missing")
        print("Run: xray-vps-manager sqlite import-json")
        return 1

    connection = database.open_database(MANAGER_DB_PATH, initialize=False)
    try:
        print(f"Schema: {schema.schema_version(connection)}")
        print(f"Quick check: {database.quick_check(connection)}")
        print(f"Import ready: {'yes' if sqlite_read_ready(connection) else 'no'}")
        print_counts(database_counts(connection))
    finally:
        connection.close()
    return 0


def import_json(replace: bool = True) -> int:
    require_root()
    if MANAGER_DB_PATH.exists():
        backup = database.backup_database(MANAGER_DB_PATH, label="pre-json-import")
        if backup:
            print(f"Pre-import SQLite backup: {backup}")

    summary = json_import.import_json_files(db_path=MANAGER_DB_PATH, replace=replace)
    print("JSON-to-SQLite import complete.")
    print_counts(summary.counts)
    if summary.warnings:
        print()
        print("Warnings:")
        for warning in summary.warnings:
            print(f" - {warning}")

    connection = database.open_database(MANAGER_DB_PATH)
    try:
        print()
        print(f"Schema: {schema.schema_version(connection)}")
        print(f"Quick check: {database.quick_check(connection)}")
        print(f"Import ready: {'yes' if sqlite_read_ready(connection) else 'no'}")
    finally:
        connection.close()
    return 0


def set_server_env_flag(key: str, enabled: bool) -> int:
    require_root()
    values = read_server_env(SERVER_ENV_PATH)
    values[key] = "true" if enabled else "false"
    write_server_env(values, SERVER_ENV_PATH)
    print(f"{key}={'true' if enabled else 'false'}")
    return 0


def usage() -> None:
    print(
        """Usage:
  xray-vps-manager sqlite status
  xray-vps-manager sqlite import-json [--no-replace]
  xray-vps-manager sqlite enable-reads
  xray-vps-manager sqlite disable-reads
  xray-vps-manager sqlite enable-writes
  xray-vps-manager sqlite disable-writes
  xray-vps-manager sqlite enable
  xray-vps-manager sqlite disable
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
    if command == "import-json":
        replace = "--no-replace" not in args
        sys.exit(import_json(replace=replace))
    if command == "enable-reads" and not args:
        sys.exit(set_server_env_flag(SQLITE_READS_SERVER_ENV, True))
    if command == "disable-reads" and not args:
        sys.exit(set_server_env_flag(SQLITE_READS_SERVER_ENV, False))
    if command == "enable-writes" and not args:
        sys.exit(set_server_env_flag(SQLITE_WRITES_SERVER_ENV, True))
    if command == "disable-writes" and not args:
        sys.exit(set_server_env_flag(SQLITE_WRITES_SERVER_ENV, False))
    if command == "enable" and not args:
        set_server_env_flag(SQLITE_READS_SERVER_ENV, True)
        sys.exit(set_server_env_flag(SQLITE_WRITES_SERVER_ENV, True))
    if command == "disable" and not args:
        set_server_env_flag(SQLITE_WRITES_SERVER_ENV, False)
        sys.exit(set_server_env_flag(SQLITE_READS_SERVER_ENV, False))

    usage()
    sys.exit(1)


if __name__ == "__main__":
    main()
