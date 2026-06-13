#!/usr/bin/env python3
"""SQLite status, validation, and cleanup helper commands."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from xray_vps_manager.commands import backup as backup_command
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity.time import parse_time
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.core.paths import (
    ACTIVITY_EXCEPTIONS_PATH,
    ACTIVITY_PATH,
    CLIENT_DB_PATH,
    CLIENT_LOG_DIR,
    MANAGER_DB_PATH,
    SERVER_ENV_PATH,
    TELEGRAM_DB_PATH,
    TRAFFIC_PATH,
)
from xray_vps_manager.core.server_env import read_server_env, write_server_env
from xray_vps_manager.db import database, schema
from xray_vps_manager.telegram import payments as telegram_payments
from xray_vps_manager.telegram import settings as telegram_settings
from xray_vps_manager.traffic import history as traffic_history
from xray_vps_manager.traffic import repository as traffic_repository
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


def validate_database_file_ready(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        raise RuntimeError(f"SQLite database was not created: {db_path}")
    connection = database.open_database(db_path)
    try:
        quick_check = database.quick_check(connection)
        if quick_check != "ok":
            raise RuntimeError(f"PRAGMA quick_check returned: {quick_check}")
        version = schema.schema_version(connection)
        if version != schema.CURRENT_SCHEMA_VERSION:
            raise RuntimeError(f"schema version {version}, expected {schema.CURRENT_SCHEMA_VERSION}")
        if not sqlite_read_ready(connection):
            raise RuntimeError("SQLite read-ready metadata is not true")
        return database_counts(connection)
    finally:
        connection.close()


def validate_database_ready() -> dict[str, int]:
    return validate_database_file_ready(MANAGER_DB_PATH)


def relationship_issues(connection) -> list[str]:
    checks = (
        (
            "clients with missing Reality connection",
            """
            SELECT COUNT(*)
            FROM clients c
            WHERE c.connection_tag IS NOT NULL
              AND c.connection_tag != ''
              AND NOT EXISTS (
                  SELECT 1 FROM reality_connections r WHERE r.tag = c.connection_tag
              )
            """,
        ),
        (
            "traffic rows with missing client",
            """
            SELECT COUNT(*)
            FROM traffic_totals t
            WHERE NOT EXISTS (
                SELECT 1 FROM clients c WHERE c.name = t.client_name
            )
            """,
        ),
        (
            "activity rows with missing client",
            """
            SELECT COUNT(*)
            FROM activity_events a
            WHERE NOT EXISTS (
                SELECT 1 FROM clients c WHERE c.name = a.client_name
            )
            """,
        ),
        (
            "Telegram subscriptions with missing client",
            """
            SELECT COUNT(*)
            FROM telegram_subscriptions t
            WHERE t.client_name IS NOT NULL
              AND t.client_name != ''
              AND NOT EXISTS (
                  SELECT 1 FROM clients c WHERE c.name = t.client_name
              )
            """,
        ),
    )
    issues = []
    for label, query in checks:
        count = int(connection.execute(query).fetchone()[0])
        if count:
            issues.append(f"{label}: {count}")
    return issues


def validate_read_layers() -> tuple[list[str], dict[str, str]]:
    issues = []
    sources: dict[str, str] = {}

    client_result = client_repository.load_db_sql_result(db_path=MANAGER_DB_PATH)
    sources["clients"] = client_result.source
    if client_result.source != "sqlite":
        issues.append("clients read layer is not using SQLite")

    traffic_result = traffic_repository.load_traffic_db_for_read_result(db_path=MANAGER_DB_PATH)
    sources["traffic"] = traffic_result.source
    if traffic_result.source != "sqlite":
        issues.append("traffic read layer is not using SQLite")

    telegram_result = telegram_settings.load_db_sql_result(db_path=MANAGER_DB_PATH)
    sources["telegram"] = telegram_result.source
    if telegram_result.source != "sqlite":
        issues.append("Telegram read layer is not using SQLite")

    activity_clients = activity_repository.event_client_names_for_read(db_path=MANAGER_DB_PATH)
    sources["activity"] = "sqlite" if activity_clients is not None else "json"
    if activity_clients is None:
        issues.append("activity read layer is not using SQLite")

    return issues, sources


def runtime_scenario_issues() -> list[str]:
    issues = []
    client_db = {"connections": {}, "clients": {}}
    clients = {}

    try:
        client_result = client_repository.load_db_sql_result(db_path=MANAGER_DB_PATH)
        client_db = client_result.db
        clients = client_repository.db_clients(client_db)
        connections = client_repository.db_connections(client_db)
        if not isinstance(clients, dict):
            issues.append("clients runtime read returned invalid clients section")
            clients = {}
        if not isinstance(connections, dict):
            issues.append("clients runtime read returned invalid connections section")
            connections = {}
        for name, entry in clients.items():
            if not isinstance(entry, dict):
                issues.append(f"client runtime record is invalid: {name}")
                continue
            connection_tag = str(entry.get("connection") or "").strip()
            if connection_tag and connection_tag not in connections:
                issues.append(f"client runtime connection is missing: {name} -> {connection_tag}")
    except Exception as exc:
        issues.append(f"clients runtime scenario failed: {exc}")
        clients = {}

    try:
        traffic_result = traffic_repository.load_traffic_db_for_read_result(db_path=MANAGER_DB_PATH)
        traffic_entries = traffic_repository.traffic_clients(traffic_result.db)
        for name, entry in traffic_entries.items():
            if not isinstance(entry, dict):
                issues.append(f"traffic runtime record is invalid: {name}")
                continue
            if name not in clients:
                issues.append(f"traffic runtime client is missing from clients: {name}")
            traffic_history.all_time_total(entry)
            traffic_history.period_day_rows(entry, date.today(), date.today(), str)
    except Exception as exc:
        issues.append(f"traffic runtime scenario failed: {exc}")

    try:
        activity_clients = activity_repository.event_client_names_for_read(db_path=MANAGER_DB_PATH)
        if activity_clients is None:
            issues.append("activity runtime clients are not readable from SQLite")
        elif activity_clients:
            list(
                activity_repository.iter_events_for_read(
                    activity_clients[0],
                    date(1970, 1, 1),
                    date(2100, 12, 31),
                    parse_time,
                    db_path=MANAGER_DB_PATH,
                )
            )
    except Exception as exc:
        issues.append(f"activity runtime scenario failed: {exc}")

    try:
        telegram_result = telegram_settings.load_db_sql_result(db_path=MANAGER_DB_PATH)
        telegram_db = telegram_result.db
        subscriptions = telegram_db.get("clientSubscriptions", {})
        if not isinstance(subscriptions, dict):
            issues.append("Telegram runtime subscriptions section is invalid")
        else:
            for chat_id, subscription in subscriptions.items():
                if not isinstance(subscription, dict):
                    issues.append(f"Telegram runtime subscription is invalid: {chat_id}")
                    continue
                client_name = str(subscription.get("client") or "").strip()
                if client_name and client_name not in clients:
                    issues.append(f"Telegram runtime subscription client is missing: {chat_id} -> {client_name}")
        telegram_payments.payment_amount_label(telegram_db, client_db if isinstance(client_db, dict) else {"clients": {}})
    except Exception as exc:
        issues.append(f"Telegram runtime scenario failed: {exc}")

    return issues


def validate_cutover() -> int:
    issues = []
    try:
        counts = validate_database_ready()
    except Exception as exc:
        print(f"ERROR database readiness: {exc}")
        return 1

    if not sqlite_reads_enabled():
        issues.append("SQLite reads flag is disabled")
    if not sqlite_writes_enabled():
        issues.append("SQLite writes flag is disabled")

    read_issues, sources = validate_read_layers()
    issues.extend(read_issues)
    runtime_issues = runtime_scenario_issues()
    issues.extend(runtime_issues)

    connection = database.open_database(MANAGER_DB_PATH)
    try:
        issues.extend(relationship_issues(connection))
    finally:
        connection.close()

    print("SQLite cutover validation")
    print_counts(counts)
    for name in sorted(sources):
        print(f"{name}_source: {sources[name]}")
    print("runtime_scenarios: ok" if not runtime_issues else "runtime_scenarios: failed")

    if issues:
        print()
        for issue in issues:
            print(f"ERROR {issue}")
        return 1

    print()
    print("OK SQLite cutover validation passed.")
    return 0


def run_cutover_validation() -> None:
    if validate_cutover() != 0:
        raise RuntimeError("SQLite cutover validation failed")


def verify_backup_file(path: Path | str | None, label: str) -> Path:
    if not path:
        raise RuntimeError(f"{label} backup path is empty")
    backup_path = Path(path)
    if not backup_path.exists():
        raise RuntimeError(f"{label} backup was not created: {backup_path}")
    if not backup_path.is_file():
        raise RuntimeError(f"{label} backup is not a file: {backup_path}")
    if backup_path.stat().st_size <= 0:
        raise RuntimeError(f"{label} backup is empty: {backup_path}")
    return backup_path


def set_server_env_flag(key: str, enabled: bool) -> int:
    require_root()
    values = read_server_env(SERVER_ENV_PATH)
    values[key] = "true" if enabled else "false"
    write_server_env(values, SERVER_ENV_PATH)
    print(f"{key}={'true' if enabled else 'false'}")
    return 0


def legacy_state_file_paths() -> tuple[Path, ...]:
    return (
        CLIENT_DB_PATH,
        TRAFFIC_PATH,
        ACTIVITY_PATH,
        ACTIVITY_EXCEPTIONS_PATH,
        TELEGRAM_DB_PATH,
    )


def legacy_activity_log_paths() -> list[Path]:
    if not CLIENT_LOG_DIR.exists():
        return []
    return sorted(path for path in CLIENT_LOG_DIR.glob("*.jsonl") if path.is_file() or path.is_symlink())


def existing_legacy_state_paths() -> list[Path]:
    paths = [path for path in legacy_state_file_paths() if path.exists()]
    paths.extend(legacy_activity_log_paths())
    return sorted(paths, key=lambda item: str(item))


def verify_legacy_paths_are_files(paths: list[Path]) -> None:
    invalid = [str(path) for path in paths if not path.is_file() and not path.is_symlink()]
    if invalid:
        raise RuntimeError("refusing to delete non-file legacy paths: " + ", ".join(invalid))


def cleanup_legacy(*, yes: bool = False) -> int:
    require_root()
    paths = existing_legacy_state_paths()
    if not paths:
        print("No legacy JSON/JSONL state files found.")
        return 0

    print("Legacy JSON/JSONL state files:")
    for path in paths:
        print(f" - {path}")

    if not yes:
        print()
        print("Dry run only. Re-run with --yes to validate SQLite, create a backup, and delete these files.")
        return 0

    try:
        verify_legacy_paths_are_files(paths)
        print()
        print("Validating SQLite cutover before cleanup...")
        run_cutover_validation()

        print("Creating backup before deleting legacy state...")
        backup_path = backup_command.create_backup(path_only=False, quiet=True, sync=True)
        verify_backup_file(backup_path, "Pre-cleanup")
        print(f"Pre-cleanup backup: {backup_path}")

        deleted = []
        for path in paths:
            if path.exists() or path.is_symlink():
                path.unlink()
                deleted.append(path)
    except Exception as exc:
        die(f"SQLite legacy cleanup failed: {exc}")

    print()
    print(f"Deleted legacy JSON/JSONL state files: {len(deleted)}")
    for path in deleted:
        print(f" - {path}")
    return 0


def usage() -> None:
    print(
        """Usage:
  xray-vps-manager sqlite status
  xray-vps-manager sqlite validate-cutover
  xray-vps-manager sqlite cleanup-legacy [--yes]
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
    if command == "validate-cutover" and not args:
        sys.exit(validate_cutover())
    if command == "cleanup-legacy":
        allowed = {"--yes"}
        unknown = [arg for arg in args if arg not in allowed]
        if unknown:
            usage()
            sys.exit(1)
        sys.exit(cleanup_legacy(yes="--yes" in args))
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
