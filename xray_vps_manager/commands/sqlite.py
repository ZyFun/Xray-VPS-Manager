#!/usr/bin/env python3
"""SQLite migration and cutover helper commands."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from xray_vps_manager.commands import backup as backup_command
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.core.paths import MANAGER_DB_PATH, SERVER_ENV_PATH
from xray_vps_manager.core.server_env import read_server_env, write_server_env
from xray_vps_manager.db import database, json_import, schema
from xray_vps_manager.telegram import settings as telegram_settings
from xray_vps_manager.traffic import repository as traffic_repository
from xray_vps_manager.db.storage import (
    SQLITE_READS_ENV,
    SQLITE_READS_SERVER_ENV,
    SQLITE_WRITES_ENV,
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

WRITER_STOP_UNITS = (
    "xray-traffic-sync.timer",
    "xray-client-expire.timer",
    "xray-traffic-sync.service",
    "xray-client-expire.service",
    "xray-telegram-poller.service",
)
WRITER_START_UNITS = (
    "xray-traffic-sync.timer",
    "xray-client-expire.timer",
    "xray-telegram-poller.service",
)
XRAY_TEST = Path("/usr/local/sbin/xray-test")


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
            raise RuntimeError("jsonImport.completed is not true")
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

    client_result = client_repository.load_db_for_read_result(db_path=MANAGER_DB_PATH)
    sources["clients"] = client_result.source
    if client_result.source != "sqlite":
        issues.append("clients read layer is not using SQLite")

    traffic_result = traffic_repository.load_traffic_db_for_read_result(db_path=MANAGER_DB_PATH)
    sources["traffic"] = traffic_result.source
    if traffic_result.source != "sqlite":
        issues.append("traffic read layer is not using SQLite")

    telegram_result = telegram_settings.load_db_for_read_result(db_path=MANAGER_DB_PATH)
    sources["telegram"] = telegram_result.source
    if telegram_result.source != "sqlite":
        issues.append("Telegram read layer is not using SQLite")

    activity_clients = activity_repository.event_client_names_for_read(db_path=MANAGER_DB_PATH)
    sources["activity"] = "sqlite" if activity_clients is not None else "json"
    if activity_clients is None:
        issues.append("activity read layer is not using SQLite")

    return issues, sources


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

    connection = database.open_database(MANAGER_DB_PATH)
    try:
        issues.extend(relationship_issues(connection))
    finally:
        connection.close()

    print("SQLite cutover validation")
    print_counts(counts)
    for name in sorted(sources):
        print(f"{name}_source: {sources[name]}")

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


def print_import_warnings(summary: json_import.ImportSummary) -> None:
    if not summary.warnings:
        return
    print("Warnings:")
    for warning in summary.warnings:
        print(f" - {warning}")


def preflight() -> int:
    require_root()
    issues = []
    with tempfile.TemporaryDirectory(prefix="xray-sqlite-preflight-") as tmp_dir:
        temp_db_path = Path(tmp_dir) / "manager-preflight.db"
        print("Running JSON-to-SQLite preflight import...")
        try:
            summary = json_import.import_json_files(db_path=temp_db_path, replace=True)
            print_counts(summary.counts)
            print_import_warnings(summary)
            print("Validating temporary SQLite database...")
            counts = validate_database_file_ready(temp_db_path)
            print_counts(counts)
            connection = database.open_database(temp_db_path)
            try:
                issues.extend(relationship_issues(connection))
            finally:
                connection.close()
        except Exception as exc:
            print(f"ERROR preflight import/validation failed: {exc}")
            return 1

    if not XRAY_TEST.exists():
        issues.append(f"xray-test not found: {XRAY_TEST}")

    if issues:
        print()
        for issue in issues:
            print(f"ERROR {issue}")
        return 1

    print()
    print("OK SQLite preflight passed. Real manager.db was not changed.")
    return 0


def run_systemctl(args: list[str], *, timeout: int = 30) -> None:
    result = subprocess.run(
        ["systemctl", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        raise RuntimeError(f"systemctl {' '.join(args)} failed: {detail}")


def stop_writers() -> None:
    run_systemctl(["stop", *WRITER_STOP_UNITS])


def start_writers() -> None:
    run_systemctl(["enable", "--now", *WRITER_START_UNITS])


def write_sqlite_flags(reads: bool, writes: bool) -> None:
    values = read_server_env(SERVER_ENV_PATH)
    values[SQLITE_READS_SERVER_ENV] = "true" if reads else "false"
    values[SQLITE_WRITES_SERVER_ENV] = "true" if writes else "false"
    write_server_env(values, SERVER_ENV_PATH)
    os.environ[SQLITE_READS_ENV] = "1" if reads else "0"
    os.environ[SQLITE_WRITES_ENV] = "1" if writes else "0"


def run_xray_test() -> str:
    if not XRAY_TEST.exists():
        raise RuntimeError(f"xray-test not found: {XRAY_TEST}")
    result = subprocess.run(
        [str(XRAY_TEST)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        raise RuntimeError("xray-test failed: " + "\n".join(detail.splitlines()[:12]))
    return "xray-test passed"


def confirm_cutover(yes: bool) -> None:
    if yes:
        return
    print("This will stop manager writer services, back up current state, import JSON into SQLite, and enable SQLite reads/writes.")
    print("Do not use xray-menu or other mutating commands until cutover finishes.")
    if not sys.stdin.isatty():
        die("Refusing SQLite cutover without --yes in non-interactive mode.")
    answer = input("Continue with SQLite cutover? [y/N]: ").strip().lower()
    if answer not in ("y", "yes", "д", "да"):
        die("SQLite cutover cancelled.")


def cutover(*, yes: bool = False, run_test: bool = True) -> int:
    require_root()
    confirm_cutover(yes)
    writers_stopped = False
    flags_enabled = False
    try:
        print("Stopping manager writer services...")
        stop_writers()
        writers_stopped = True

        print("Creating pre-cutover backup...")
        backup_path = backup_command.create_backup(path_only=False, quiet=True, sync=False)
        print(f"Pre-cutover backup: {backup_path}")

        if MANAGER_DB_PATH.exists():
            sqlite_backup = database.backup_database(MANAGER_DB_PATH, label="pre-cutover")
            if sqlite_backup:
                print(f"Pre-cutover SQLite backup: {sqlite_backup}")

        print("Importing JSON state into SQLite...")
        summary = json_import.import_json_files(db_path=MANAGER_DB_PATH, replace=True)
        print_counts(summary.counts)
        print_import_warnings(summary)

        print("Validating SQLite database...")
        counts = validate_database_ready()
        print_counts(counts)

        print("Enabling SQLite reads and writes...")
        write_sqlite_flags(True, True)
        flags_enabled = True

        print("Validating SQLite cutover...")
        run_cutover_validation()

        print("Starting manager writer services...")
        start_writers()
        writers_stopped = False

        if run_test:
            print("Running xray-test...")
            print(run_xray_test())

        print("SQLite cutover complete.")
        return 0
    except Exception as exc:
        if flags_enabled:
            try:
                write_sqlite_flags(False, False)
                print("SQLite flags were disabled after cutover failure.", file=sys.stderr)
            except Exception as flag_exc:
                print(f"Failed to disable SQLite flags after cutover failure: {flag_exc}", file=sys.stderr)
        if writers_stopped:
            try:
                start_writers()
                print("Manager writer services were started after cutover failure.", file=sys.stderr)
            except Exception as start_exc:
                print(f"Failed to start manager writer services after cutover failure: {start_exc}", file=sys.stderr)
        die(f"SQLite cutover failed: {exc}")


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
  xray-vps-manager sqlite preflight
  xray-vps-manager sqlite validate-cutover
  xray-vps-manager sqlite cutover [--yes] [--skip-test]
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
    if command == "preflight" and not args:
        sys.exit(preflight())
    if command == "validate-cutover" and not args:
        sys.exit(validate_cutover())
    if command == "cutover":
        allowed = {"--yes", "--skip-test"}
        unknown = [arg for arg in args if arg not in allowed]
        if unknown:
            usage()
            sys.exit(1)
        sys.exit(cutover(yes="--yes" in args, run_test="--skip-test" not in args))
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
