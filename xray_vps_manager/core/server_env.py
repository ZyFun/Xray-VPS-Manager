"""Совместимые имена server.env; реализация находится в core.settings."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping
from xray_vps_manager.core.paths import SERVER_ENV_PATH, Paths, paths
from xray_vps_manager.core import settings
from xray_vps_manager.core.settings import ORDERED_ENV_KEYS, UPDATE_LOCK_TIMEOUT, chown_server_env


def _path(path: Path) -> Path:
    # Старый захваченный default уважает paths.use; явные временные пути сохраняются.
    return paths().server_env if path == Paths.default().server_env else path


def read_server_env(path: Path = SERVER_ENV_PATH, *, strict: bool = False, require_exists: bool = False) -> dict[str, str]:
    return settings.read_env(_path(path), strict=strict, require_exists=require_exists)


def server_env_values(path: Path = SERVER_ENV_PATH) -> dict[str, str]:
    return read_server_env(path)


def write_server_env(values: dict[str, str], path: Path = SERVER_ENV_PATH, ordered_keys: list[str] | None = None) -> None:
    # Полная запись нужна старым адаптерам и restore; не превращаем её в merge.
    settings.write_env(values, path=_path(path), ordered_keys=ordered_keys)


def update_server_env(changes: Mapping[str, str], path: Path = SERVER_ENV_PATH, *, timeout: float = UPDATE_LOCK_TIMEOUT) -> dict[str, str]:
    return settings.update_env(changes, path=_path(path), timeout=timeout)
