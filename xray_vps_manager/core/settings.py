"""Каноническое чтение и запись server.env. Парсинг R1.2 сохраняет контракт T1."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, Mapping

from xray_vps_manager.core.paths import paths

UPDATE_LOCK_TIMEOUT = 60.0

ORDERED_ENV_KEYS = [
    "SERVER_ADDR",
    "SERVER_NAME",
    "PORT",
    "REALITY_SNI",
    "REALITY_DEST",
    "FINGERPRINT",
    "REALITY_TRANSPORT",
    "GRPC_SERVICE_NAME",
    "XHTTP_PATH",
    "XHTTP_MODE",
    "MANAGER_TIMEZONE",
    "MANAGER_PYTHON",
    "TRAFFIC_TOTAL_MULTIPLIER_ENABLED",
    "TRAFFIC_TOTAL_MULTIPLIER",
    "SECURITY_AUDIT_LAST_RUN",
    "ACTIVITY_LOGGING_ENABLED",
    "ACTIVITY_RETENTION_DAYS",
    "ACTIVITY_RISK_BURST_EVENTS",
    "ACTIVITY_RISK_BURST_WINDOW_MINUTES",
    "ACTIVITY_RISK_UNIQUE_HOSTS",
    "ACTIVITY_RISK_UNIQUE_PORTS",
    "ACTIVITY_XRAY_GEOIP_WARNING_CODE",
    "ACTIVITY_ALERTS_ENABLED",
    "ACTIVITY_ALERT_RETENTION_DAYS",
    "XRAY_ERROR_EVENT_RETENTION_DAYS",
    "XRAY_ACCESS_LOG_RETENTION_DAYS",
    "XRAY_ERROR_LOG_RETENTION_DAYS",
    "XRAY_RAW_LOG_ROTATE_TIME",
    "ACTIVITY_XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY",
]


def read_env(
    path: Path | None = None,
    *,
    strict: bool = False,
    require_exists: bool = False,
) -> dict[str, str]:
    path = paths().server_env if path is None else path
    values: dict[str, str] = {}
    if not path.exists():
        if require_exists:
            raise RuntimeError(f"not found: {path}")
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            if strict:
                raise RuntimeError(f"invalid line without '=': {line}")
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def chown_server_env(path: Path) -> None:
    try:
        shutil.chown(path, user="root", group="xray")
    except LookupError:
        try:
            shutil.chown(path, user="root")
        except PermissionError:
            return
    except PermissionError:
        return


def write_env(
    values: Mapping[str, str],
    path: Path | None = None,
    ordered_keys: list[str] | None = None,
) -> None:
    path = paths().server_env if path is None else path
    values = dict(values)
    values.pop("ACTIVITY_GEOIP_WARNING_CODE", None)
    ordered = ordered_keys or ORDERED_ENV_KEYS
    lines = [f"{key}={values.get(key, '')}" for key in ordered if key in values]
    for key in sorted(values):
        if key not in ordered:
            lines.append(f"{key}={values[key]}")

    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    chown_server_env(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(path)


def env_value(key: str, default: str = "", path: Path | None = None) -> str:
    """Сохраняет клиентский fallback: пустое значение файла → окружение → default."""
    return read_env(path).get(key) or os.environ.get(key, default)


def update_env(
    changes: Mapping[str, str],
    *,
    defaults: Mapping[str, str] | None = None,
    remove: Iterable[str] = (),
    path: Path | None = None,
    timeout: float = UPDATE_LOCK_TIMEOUT,
    ordered_keys: list[str] | None = None,
) -> dict[str, str]:
    """Точечная запись под manager_lock: defaults → remove → changes.

    ordered_keys сохраняет контракт переходных писателей до унификации
    поведения. Неизвестные ключи сохраняются. Полная запись остаётся write_env.
    defaults заполняет только отсутствующие ключи, сохраняя пустые значения.
    """
    # locks → clock → settings: локальный импорт разрывает цикл.
    from xray_vps_manager.core.locks import manager_lock

    path = paths().server_env if path is None else path
    with manager_lock(timeout, purpose="server.env"):
        values = read_env(path)
        for key, value in (defaults or {}).items():
            values.setdefault(key, value)
        for key in remove:
            values.pop(key, None)
        values.update(changes)
        write_env(values, path=path, ordered_keys=ordered_keys)
    return values
