"""SQLite runtime storage helpers."""

from __future__ import annotations

from contextlib import contextmanager

from xray_vps_manager.db import database

SQLITE_READ_READY_KEY = "jsonImport.completed"
TRUE_VALUES = {"1", "true", "yes", "y", "on", "enable", "enabled"}


class SQLiteReadUnavailable(RuntimeError):
    """Raised when SQLite reads are enabled but the SQLite source cannot be used."""


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def sqlite_read_ready(connection) -> bool:
    row = connection.execute(
        "SELECT value FROM manager_metadata WHERE key = ?",
        (SQLITE_READ_READY_KEY,),
    ).fetchone()
    return truthy(row["value"] if row else "")


@contextmanager
def ready_write_session(db_path=None, *, legacy_read_errors=False):
    """Переходный контракт фасадов до удаления ready-флага."""
    def unavailable(message):
        if legacy_read_errors:
            return SQLiteReadUnavailable(message + ".")
        return RuntimeError(message)

    target = database.database_path(db_path)
    if not database.database_file_exists(target):
        raise unavailable("SQLite manager database is missing")
    with database.write_session(target) as connection:
        if not sqlite_read_ready(connection):
            raise unavailable("SQLite database is not marked ready")
        yield connection
