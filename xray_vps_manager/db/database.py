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
from xray_vps_manager.core.paths import MANAGER_DB_PATH, paths
from xray_vps_manager.db import schema

DEFAULT_BUSY_TIMEOUT_MS = 30000
# После пика записи (VACUUM, большой прогон) `-wal` усекается до этого размера при checkpoint.
JOURNAL_SIZE_LIMIT_BYTES = 128 * 1024 * 1024
WAL_FILE_SUFFIXES = ("-wal", "-shm")
JOURNAL_FILE_SUFFIXES = (*WAL_FILE_SUFFIXES, "-journal")
_SAVEPOINT_IDS = count(1)
_initialized_files: dict[Path, tuple[int, int]] = {}


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


def configure_connection(connection: sqlite3.Connection, *, busy_timeout_ms: int | None = None) -> None:
    connection.row_factory = sqlite3.Row
    schema.configure_connection(connection)
    timeout = DEFAULT_BUSY_TIMEOUT_MS if busy_timeout_ms is None else busy_timeout_ms
    connection.execute(f"PRAGMA busy_timeout = {timeout}")
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


@contextmanager
def report_errors() -> Iterator[None]:
    """Перевести ошибки SQLite и файлов в ошибку сценария на границе администрирования."""
    try:
        yield
    except (sqlite3.Error, OSError) as exc:
        raise StateError(f"SQLite manager database: {exc}") from exc


def _open(
    db_path: Path, *, create: bool, timeout: float, autocommit: bool | int
) -> sqlite3.Connection:
    if create:
        ensure_database_parent(db_path)
        target = str(db_path)
    else:
        # mode=rw исключает создание файла даже при его удалении прямо перед открытием.
        target = db_path.absolute().as_uri() + "?mode=rw"
    connection = sqlite3.connect(target, uri=not create, timeout=timeout, autocommit=autocommit)
    try:
        busy_ms = None if autocommit is not True or timeout == 30.0 else int(timeout * 1000)
        configure_connection(connection, busy_timeout_ms=busy_ms)
    except BaseException:
        connection.close()
        raise
    return connection


def connect(path: str | Path | None = None, *, timeout: float = 30.0) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else paths().manager_db
    try:
        connection = _open(db_path, create=False, timeout=timeout, autocommit=True)
    except sqlite3.Error as exc:
        if not db_path.exists():
            raise StateError(
                f"SQLite manager database is missing: {db_path}",
                hint="On a configured server, restore a backup containing manager.db with xray-backup restore. "
                     "To create an empty database, run xray-vps-manager sqlite init.",
            ) from exc
        raise StateError(f"Cannot open SQLite manager database: {exc}") from exc
    return connection


def _migrate(
    connection: sqlite3.Connection, db_path: Path, *, backup: bool, backup_dir: str | Path | None = None,
) -> None:
    if not schema.pending_migrations(connection):
        return
    enable_wal(connection)
    with transaction(connection, immediate=True):
        pending = schema.pending_migrations(connection)
        if backup and schema.pending_migrations_require_backup(pending):
            backup_database(db_path, backup_dir=backup_dir, label="pre-migration")
        for migration in pending:
            schema.apply_migration(connection, migration)


def _initialize(
    db_path: Path, *, backup: bool, backup_dir: str | Path | None, timeout: float, autocommit: bool | int,
) -> sqlite3.Connection:
    existed = database_file_exists(db_path)
    connection = _open(db_path, create=True, timeout=timeout, autocommit=autocommit)
    try:
        enable_wal(connection)
        _migrate(connection, db_path, backup=backup and existed, backup_dir=backup_dir)
        ensure_database_permissions(db_path)
    except BaseException:
        connection.close()
        raise
    return connection


def initialize(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else paths().manager_db
    try:
        return _initialize(db_path, backup=True, backup_dir=None, timeout=30.0, autocommit=True)
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        raise StateError(f"Cannot initialize SQLite manager database: {exc}") from exc


def _ensure_initialized(connection, db_path, *, force=False):
    stat = db_path.stat()
    identity = (stat.st_dev, stat.st_ino)
    if not force and _initialized_files.get(db_path) == identity:
        return
    _initialized_files.pop(db_path, None)
    try:
        _migrate(connection, db_path, backup=True)
    except (RuntimeError, sqlite3.Error, OSError) as exc:
        raise StateError(f"Cannot migrate SQLite manager database: {exc}") from exc
    _initialized_files[db_path] = identity


def ensure_initialized(path: str | Path | None = None, *, force: bool = False) -> None:
    db_path = Path(path).absolute() if path is not None else paths().manager_db
    with closing(connect(db_path)) as connection:
        _ensure_initialized(connection, db_path, force=force)


@contextmanager
def session(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    db_path = Path(path).absolute() if path is not None else paths().manager_db
    with closing(connect(db_path)) as connection:
        _ensure_initialized(connection, db_path)
        yield connection


@contextmanager
def write_session(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    try:
        with session(path) as connection:
            enable_wal(connection)
            with transaction(connection, immediate=True):
                yield connection
    except sqlite3.OperationalError as exc:
        primary_code = getattr(exc, "sqlite_errorcode", 0) & 0xff
        if primary_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise StateError(f"SQLite manager database is locked: {exc}") from exc
        raise


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

    return _open(database_path(path), create=True, timeout=timeout,
                 autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL)


def initialize_database(
    path: str | Path | None = None,
    *,
    backup_before_destructive_migrations: bool = True,
    backup_dir: str | Path | None = None,
    timeout: float = 30.0,
) -> sqlite3.Connection:
    return _initialize(database_path(path), backup=backup_before_destructive_migrations,
                       backup_dir=backup_dir, timeout=timeout,
                       autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL)


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
    total = 0
    for item in (db_path, Path(f"{db_path}-wal")):
        try:
            total += item.stat().st_size
        except FileNotFoundError:
            # `-wal` удаляет последнее закрытое соединение другого процесса, в том числе между проверками.
            continue
    return total


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
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
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

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def quick_check(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA quick_check").fetchone()
    return str(row[0] if row else "")
