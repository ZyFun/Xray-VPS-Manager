"""Совместимые контракты времени; канон находится в core.clock."""
from __future__ import annotations

from datetime import datetime, tzinfo
from pathlib import Path
from xray_vps_manager.core import clock
from xray_vps_manager.core.paths import SERVER_ENV_PATH, Paths, paths
from xray_vps_manager.core.settings import read_env as read_server_env
from xray_vps_manager.core.clock import parse_time, server_local_timezone, utc_stamp


def utc_now() -> datetime:
    return clock.now()


def manager_timezone(path: Path = SERVER_ENV_PATH):
    path = paths().server_env if path == Paths.default().server_env else path
    result = clock.manager_timezone(read_server_env(path))
    return result.tz, result.label


def xray_access_time_to_iso(value: str, source_timezone: tzinfo | None = None) -> str:
    return clock.xray_access_time_to_iso(value, source_timezone or server_local_timezone())
