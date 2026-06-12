"""Feature flags for gradual SQLite cutover."""

from __future__ import annotations

import os

from xray_vps_manager.core.server_env import read_server_env

SQLITE_READS_ENV = "XRAY_MANAGER_SQLITE_READS"
SQLITE_READS_SERVER_ENV = "MANAGER_SQLITE_READS_ENABLED"
SQLITE_READ_READY_KEY = "jsonImport.completed"
TRUE_VALUES = {"1", "true", "yes", "y", "on", "enable", "enabled"}


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def sqlite_reads_enabled() -> bool:
    if SQLITE_READS_ENV in os.environ:
        return truthy(os.environ.get(SQLITE_READS_ENV))
    return truthy(read_server_env().get(SQLITE_READS_SERVER_ENV))


def sqlite_read_ready(connection) -> bool:
    row = connection.execute(
        "SELECT value FROM manager_metadata WHERE key = ?",
        (SQLITE_READ_READY_KEY,),
    ).fetchone()
    return truthy(row["value"] if row else "")
