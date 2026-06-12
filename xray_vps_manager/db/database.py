"""SQLite database opening, transactions, and safety helpers."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import count
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterator

from xray_vps_manager.core.json_store import chown_xray
from xray_vps_manager.core.paths import MANAGER_DB_PATH
from xray_vps_manager.db import schema

DEFAULT_BUSY_TIMEOUT_MS = 5000
_SAVEPOINT_IDS = count(1)


def database_path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else MANAGER_DB_PATH


def is_memory_database(path: str | Path) -> bool:
    return str(path) == ":memory:"


def ensure_database_parent(path: str | Path) -> None:
    if not is_memory_database(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)


def ensure_database_permissions(path: str | Path, mode: int = 0o640) -> None:
    if is_memory_database(path):
        return
    db_path = Path(path)
    if not db_path.exists():
        return
    os.chmod(db_path, mode)
    chown_xray(db_path)


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    schema.configure_connection(connection)
    connection.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")


def open_database(
    path: str | Path | None = None,
    *,
    initialize: bool = True,
    timeout: float = 30.0,
    backup_before_destructive_migrations: bool = True,
    backup_dir: str | Path | None = None,
) -> sqlite3.Connection:
    if initialize:
        return initialize_database(
            path,
            backup_before_destructive_migrations=backup_before_destructive_migrations,
            backup_dir=backup_dir,
            timeout=timeout,
        )

    db_path = database_path(path)
    ensure_database_parent(db_path)
    connection = sqlite3.connect(str(db_path), timeout=timeout)
    configure_connection(connection)
    return connection


def initialize_database(
    path: str | Path | None = None,
    *,
    backup_before_destructive_migrations: bool = True,
    backup_dir: str | Path | None = None,
    timeout: float = 30.0,
) -> sqlite3.Connection:
    db_path = database_path(path)
    existed_before_open = database_file_exists(db_path)
    connection = open_database(db_path, initialize=False, timeout=timeout)
    pending = schema.pending_migrations(connection)
    if (
        backup_before_destructive_migrations
        and schema.pending_migrations_require_backup(pending)
        and existed_before_open
    ):
        backup_database(db_path, backup_dir=backup_dir, label="pre-migration")
    schema.ensure_schema(connection)
    ensure_database_permissions(db_path)
    return connection


def database_file_exists(path: str | Path | None = None) -> bool:
    db_path = database_path(path)
    return not is_memory_database(db_path) and db_path.exists()


def backup_database(
    path: str | Path | None = None,
    *,
    backup_dir: str | Path | None = None,
    label: str = "manual",
) -> Path | None:
    db_path = database_path(path)
    if not database_file_exists(db_path):
        return None

    destination_dir = Path(backup_dir) if backup_dir is not None else db_path.parent / "manager-db-backups"
    destination_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(destination_dir, 0o700)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "backup"
    backup_path = destination_dir / f"{db_path.stem}-{safe_label}-{stamp}.db"
    counter_value = 1
    while backup_path.exists():
        backup_path = destination_dir / f"{db_path.stem}-{safe_label}-{stamp}-{counter_value}.db"
        counter_value += 1

    with sqlite3.connect(str(db_path)) as source, sqlite3.connect(str(backup_path)) as target:
        source.backup(target)

    os.chmod(backup_path, 0o600)
    return backup_path


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if connection.in_transaction:
        savepoint = f"xvm_sp_{next(_SAVEPOINT_IDS)}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield connection
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return

    connection.execute("BEGIN")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def quick_check(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA quick_check").fetchone()
    return str(row[0] if row else "")
