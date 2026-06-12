"""Activity settings stored in server.env."""

from __future__ import annotations

import os
import re

from xray_vps_manager.activity.constants import (
    DEFAULT_RETENTION_DAYS,
    DEFAULT_RISK_BURST_EVENTS,
    DEFAULT_RISK_BURST_WINDOW_MINUTES,
    DEFAULT_RISK_UNIQUE_HOSTS,
    DEFAULT_RISK_UNIQUE_PORTS,
    SERVER_ENV_PATH,
)
from xray_vps_manager.activity.repository import chown_xray

ORDERED_ENV_KEYS = [
    "SERVER_ADDR",
    "SERVER_NAME",
    "PORT",
    "REALITY_SNI",
    "REALITY_DEST",
    "FINGERPRINT",
    "MANAGER_TIMEZONE",
    "SECURITY_AUDIT_LAST_RUN",
    "ACTIVITY_LOGGING_ENABLED",
    "ACTIVITY_RETENTION_DAYS",
    "ACTIVITY_RISK_BURST_EVENTS",
    "ACTIVITY_RISK_BURST_WINDOW_MINUTES",
    "ACTIVITY_RISK_UNIQUE_HOSTS",
    "ACTIVITY_RISK_UNIQUE_PORTS",
    "ACTIVITY_XRAY_GEOIP_WARNING_CODE",
    "ACTIVITY_XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY",
]


def server_env_values() -> dict[str, str]:
    values = {}
    if SERVER_ENV_PATH.exists():
        for line in SERVER_ENV_PATH.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"').strip("'")
    return values


def write_server_env(values: dict[str, str]) -> None:
    values.pop("ACTIVITY_GEOIP_WARNING_CODE", None)
    lines = [f"{key}={values.get(key, '')}" for key in ORDERED_ENV_KEYS if key in values]
    for key in sorted(values):
        if key not in ORDERED_ENV_KEYS:
            lines.append(f"{key}={values[key]}")
    tmp = SERVER_ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    chown_xray(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(SERVER_ENV_PATH)


def with_activity_defaults(env: dict[str, str]) -> dict[str, str]:
    env.setdefault("ACTIVITY_LOGGING_ENABLED", "false")
    env.setdefault("ACTIVITY_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    env.setdefault("ACTIVITY_RISK_BURST_EVENTS", str(DEFAULT_RISK_BURST_EVENTS))
    env.setdefault("ACTIVITY_RISK_BURST_WINDOW_MINUTES", str(DEFAULT_RISK_BURST_WINDOW_MINUTES))
    env.setdefault("ACTIVITY_RISK_UNIQUE_HOSTS", str(DEFAULT_RISK_UNIQUE_HOSTS))
    env.setdefault("ACTIVITY_RISK_UNIQUE_PORTS", str(DEFAULT_RISK_UNIQUE_PORTS))
    env.setdefault("ACTIVITY_XRAY_GEOIP_WARNING_CODE", "")
    return env


def activity_enabled(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else server_env_values()
    return (env.get("ACTIVITY_LOGGING_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "y")


def xray_geoip_warning_code(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else server_env_values()
    return (env.get("ACTIVITY_XRAY_GEOIP_WARNING_CODE") or "").strip().upper()


def retention_days(env: dict[str, str] | None = None) -> int:
    env = env if env is not None else server_env_values()
    raw = (env.get("ACTIVITY_RETENTION_DAYS") or str(DEFAULT_RETENTION_DAYS)).strip()
    try:
        value = int(raw, 10)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return max(1, value)


def parse_retention_days(value: str) -> int:
    raw = str(value or "").strip()
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError("Retention days must be a number from 1 to 3650.")
    days = int(raw, 10)
    if days < 1 or days > 3650:
        raise ValueError("Retention days must be a number from 1 to 3650.")
    return days


def env_int(env: dict[str, str], name: str, default: int, minimum: int = 1, maximum: int = 1000000) -> int:
    raw = (env.get(name) or str(default)).strip()
    try:
        value = int(raw, 10)
    except ValueError:
        return default
    if value < minimum or value > maximum:
        return default
    return value


def risk_limits(env: dict[str, str] | None = None) -> dict[str, int]:
    env = env if env is not None else server_env_values()
    return {
        "burstEvents": env_int(env, "ACTIVITY_RISK_BURST_EVENTS", DEFAULT_RISK_BURST_EVENTS, 1, 1000000),
        "burstWindowMinutes": env_int(env, "ACTIVITY_RISK_BURST_WINDOW_MINUTES", DEFAULT_RISK_BURST_WINDOW_MINUTES, 1, 1440),
        "uniqueHosts": env_int(env, "ACTIVITY_RISK_UNIQUE_HOSTS", DEFAULT_RISK_UNIQUE_HOSTS, 1, 1000000),
        "uniquePorts": env_int(env, "ACTIVITY_RISK_UNIQUE_PORTS", DEFAULT_RISK_UNIQUE_PORTS, 1, 65535),
    }


def parse_limit_value(label: str, value: str, minimum: int, maximum: int) -> int:
    raw = str(value or "").strip()
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"{label} must be a number from {minimum} to {maximum}.")
    parsed = int(raw, 10)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be a number from {minimum} to {maximum}.")
    return parsed


def risk_limit_env_values(
    burst_events: str,
    burst_window_minutes: str,
    unique_hosts: str,
    unique_ports: str,
) -> dict[str, int]:
    return {
        "ACTIVITY_RISK_BURST_EVENTS": parse_limit_value("BURST_EVENTS", burst_events, 1, 1000000),
        "ACTIVITY_RISK_BURST_WINDOW_MINUTES": parse_limit_value("BURST_WINDOW_MINUTES", burst_window_minutes, 1, 1440),
        "ACTIVITY_RISK_UNIQUE_HOSTS": parse_limit_value("UNIQUE_HOSTS", unique_hosts, 1, 1000000),
        "ACTIVITY_RISK_UNIQUE_PORTS": parse_limit_value("UNIQUE_PORTS", unique_ports, 1, 65535),
    }

