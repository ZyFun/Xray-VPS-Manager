"""SQLite database opening, transactions, and safety helpers."""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from itertools import count
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Iterator

from xray_vps_manager.core.errors import StateError
from xray_vps_manager.core.json_store import chown_xray
from xray_vps_manager.core.paths import MANAGER_DB_PATH
from xray_vps_manager.db import schema

DEFAULT_BUSY_TIMEOUT_MS = 30000
# После пика записи (VACUUM, большой прогон) `-wal` усекается до этого размера при checkpoint.
JOURNAL_SIZE_LIMIT_BYTES = 128 * 1024 * 1024
WAL_FILE_SUFFIXES = ("-wal", "-shm")
JOURNAL_FILE_SUFFIXES = (*WAL_FILE_SUFFIXES, "-journal")
_SAVEPOINT_IDS = count(1)


def database_path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else MANAGER_DB_PATH


def is_memory_database(path: str | Path) -> bool:
    return str(path) == ":memory:"


def ensure_database_parent(path: str | Path) -> None:
    if not is_memory_database(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)


def sidecar_paths(path: str | Path, suffixes: tuple[str, ...] = WAL_FILE_SUFFIXES) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in suffixes)


def ensure_database_permissions(path: str | Path, mode: int = 0o640) -> None:
    if is_memory_database(path):
        return
    db_path = Path(path)
    if not db_path.exists():
        return
    for item in (db_path, *sidecar_paths(db_path)):
        try:
            os.chmod(item, mode)
        except FileNotFoundError:
            # `-wal`/`-shm` удаляет последнее закрытое соединение.
            continue
        chown_xray(item)


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    schema.configure_connection(connection)
    connection.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
    # В WAL при NORMAL fsync идёт на checkpoint, а не на каждую транзакцию (VIREI-245).
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT_BYTES}")


def journal_mode(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA journal_mode").fetchone()
    return str(row[0] if row else "").lower()


def enable_wal(connection: sqlite3.Connection) -> str:
    """Переводит базу в WAL, если она ещё не в нём; режим хранится в файле базы."""
    mode = journal_mode(connection)
    if mode in ("wal", "memory"):
        return mode
    try:
        row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.OperationalError:
        # Занятая база не мешает работе: режим переключит следующее открытие с инициализацией.
        return mode
    return str(row[0] if row else mode).lower()


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
    # Только здесь, не в configure_connection: read-пути (xray-test, sqlite status) базу не пишут.
    enable_wal(connection)
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

    with closing(sqlite3.connect(str(db_path))) as source, closing(sqlite3.connect(str(backup_path))) as target:
        source.backup(target)
        # Backup API забирает кадры `-wal`, но копирует и флаг WAL; копия остаётся одним файлом, как до WAL.
        target.execute("PRAGMA journal_mode = DELETE")

    os.chmod(backup_path, 0o600)
    return backup_path


def copy_database_files(source: str | Path, destination: str | Path, mode: int = 0o600) -> Path:
    """Копирует файл базы вместе с `-wal`: без него теряются транзакции, ещё не перенесённые checkpoint."""
    destination_path = Path(destination)
    shutil.copy2(source, destination_path)
    os.chmod(destination_path, mode)
    source_wal = Path(f"{source}-wal")
    if source_wal.exists():
        destination_wal = Path(f"{destination_path}-wal")
        shutil.copy2(source_wal, destination_wal)
        os.chmod(destination_wal, mode)
    return destination_path


def remove_journal_files(path: str | Path) -> tuple[Path, ...]:
    """Удаляет `-wal`, `-shm` и `-journal` перед заменой файла базы.

    Вызывать только когда базу никто не держит открытой (restore с остановленными писателями):
    кадры прежнего `-wal` или откатный журнал при следующем открытии легли бы на новый файл.
    """
    removed = []
    for item in sidecar_paths(path, JOURNAL_FILE_SUFFIXES):
        try:
            item.unlink()
        except FileNotFoundError:
            continue
        removed.append(item)
    return tuple(removed)


def database_disk_bytes(path: str | Path | None = None) -> int:
    """Размер базы на диске вместе с `-wal`."""
    db_path = database_path(path)
    return sum(item.stat().st_size for item in (db_path, Path(f"{db_path}-wal")) if item.exists())


def ensure_free_space(path: str | Path | None, required_bytes: int, *, purpose: str) -> None:
    db_path = database_path(path)
    free_bytes = shutil.disk_usage(db_path.parent).free
    if free_bytes < required_bytes:
        mib = 1024 * 1024
        raise StateError(
            f"Not enough free disk space for {purpose}: need {required_bytes / mib:.1f} MiB, "
            f"free {free_bytes / mib:.1f} MiB on {db_path.parent}.",
            hint=f"Free disk space and retry; {db_path.name} was not changed.",
        )


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
