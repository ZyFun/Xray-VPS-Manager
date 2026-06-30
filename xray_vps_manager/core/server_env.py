"""Shared helpers for reading and writing server.env."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from xray_vps_manager.core.paths import SERVER_ENV_PATH

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


def read_server_env(
    path: Path = SERVER_ENV_PATH,
    *,
    strict: bool = False,
    require_exists: bool = False,
) -> dict[str, str]:
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


def server_env_values(path: Path = SERVER_ENV_PATH) -> dict[str, str]:
    return read_server_env(path)


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


def write_server_env(
    values: dict[str, str],
    path: Path = SERVER_ENV_PATH,
    ordered_keys: list[str] | None = None,
) -> None:
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
