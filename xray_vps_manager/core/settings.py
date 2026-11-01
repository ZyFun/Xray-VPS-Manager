"""Каноническое чтение и запись server.env: нормализация B1.2 / XR-REV-069."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, Mapping

from xray_vps_manager.core.paths import paths
from xray_vps_manager.core.errors import ValidationError

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


def _env_key(key: str) -> str:
    key = key.strip()
    if key.startswith(("export ", "export\t")):
        key = key[6:].strip()
    return key.upper()


def _normalized_values(values: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in values.items():
        key = _env_key(key)
        if key in normalized and normalized[key] != value:
            raise ValidationError(f"conflicting server.env values for key: {key}")
        normalized[key] = value
    return normalized


def _env_value(value: str) -> str:
    """Разбирает только данные: комментарии, одну оболочку и два escape в ней.

    Кавычки открывают группу в начале значения или после пробела/табуляции;
    апострофы внутри слов буквальны. Непарная группа остаётся как есть.
    """
    start = len(value) - len(value.lstrip())
    end = len(value)
    quote = ""
    outer_quote = ""
    outer_close = -1
    index = start
    while index < end:
        char = value[index]
        if quote:
            if quote == '"' and char == "\\" and index + 1 < end and value[index + 1] in '\\"':
                index += 2
                continue
            if char == quote:
                if outer_quote and outer_close < 0:
                    outer_close = index
                quote = ""
        elif char == "#" and index > 0 and value[index - 1] in " \t":
            end = index
            break
        elif char in "\"'" and (index == start or value[index - 1] in " \t"):
            quote = char
            if index == start:
                outer_quote = char
        index += 1

    token = value[start:end].rstrip()
    if not outer_quote or outer_close != start + len(token) - 1:
        return token
    token = token[1:-1]
    if outer_quote == "'":
        return token
    decoded: list[str] = []
    index = 0
    while index < len(token):
        if token[index] == "\\" and index + 1 < len(token) and token[index + 1] in '\\"':
            index += 1
        decoded.append(token[index])
        index += 1
    return "".join(decoded)


def _env_literal(value: str) -> str:
    # Простые строки сохраняют прежний вид; защитная оболочка нужна только
    # там, где чтение иначе снимет пробелы, кавычки или хвостовой комментарий.
    if _env_value(value) == value:
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_env(
    path: Path | None = None,
    *,
    strict: bool = False,
    require_exists: bool = False,
) -> dict[str, str]:
    """Читает нормализованные ключи; при дубликатах последняя строка побеждает.

    strict отклоняет строки без '=' через ValidationError. require_exists
    сохраняет отдельный прежний RuntimeError для отсутствующего файла.
    """
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
                raise ValidationError(f"invalid line without '=': {line}")
            continue
        key, value = line.split("=", 1)
        values[_env_key(key)] = _env_value(value)
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


def env_text(
    values: Mapping[str, str],
    ordered_keys: list[str] | None = None,
) -> str:
    """Общая сериализация для server.env и его переносимой копии в архиве."""
    values = _normalized_values(values)
    for key, value in values.items():
        if "".join(key.splitlines()) != key or "".join(value.splitlines()) != value:
            raise ValidationError(f"server.env key and value must be single lines: {key!r}")
    values.pop("ACTIVITY_GEOIP_WARNING_CODE", None)
    ordered = list(dict.fromkeys(_env_key(key) for key in (ordered_keys or ORDERED_ENV_KEYS)))
    lines = [f"{key}={_env_literal(values[key])}" for key in ordered if key in values]
    for key in sorted(values):
        if key not in ordered:
            lines.append(f"{key}={_env_literal(values[key])}")
    return "\n".join(lines) + "\n"


def write_env(
    values: Mapping[str, str],
    path: Path | None = None,
    ordered_keys: list[str] | None = None,
) -> None:
    path = paths().server_env if path is None else path
    text = env_text(values, ordered_keys)

    tmp = path.with_suffix(".env.tmp")
    tmp.write_text(text)
    chown_server_env(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(path)


def env_value(key: str, default: str = "", path: Path | None = None) -> str:
    """Сохраняет клиентский fallback: пустое значение файла → окружение → default."""
    return read_env(path).get(_env_key(key)) or os.environ.get(key, default)


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
    changes = _normalized_values(changes)
    defaults = _normalized_values(defaults or {})
    with manager_lock(timeout, purpose="server.env"):
        values = read_env(path)
        for key, value in (defaults or {}).items():
            values.setdefault(key, value)
        for key in remove:
            values.pop(_env_key(key), None)
        values.update(changes)
        write_env(values, path=path, ordered_keys=ordered_keys)
    return values
