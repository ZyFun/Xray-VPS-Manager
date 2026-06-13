"""SQLite runtime storage helpers."""

from __future__ import annotations

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
