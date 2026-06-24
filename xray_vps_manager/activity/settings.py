"""Activity settings stored in server.env."""

from __future__ import annotations

import re

from xray_vps_manager.activity.constants import (
    DEFAULT_ALERT_RETENTION_DAYS,
    DEFAULT_XRAY_ACCESS_LOG_RETENTION_DAYS,
    DEFAULT_XRAY_ERROR_LOG_RETENTION_DAYS,
    DEFAULT_XRAY_ERROR_RETENTION_DAYS,
    DEFAULT_XRAY_RAW_LOG_ROTATE_TIME,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_RISK_BURST_EVENTS,
    DEFAULT_RISK_BURST_WINDOW_MINUTES,
    DEFAULT_RISK_UNIQUE_HOSTS,
    DEFAULT_RISK_UNIQUE_PORTS,
    SERVER_ENV_PATH,
)
from xray_vps_manager.core.server_env import ORDERED_ENV_KEYS, read_server_env, write_server_env as write_server_env_file


def server_env_values() -> dict[str, str]:
    return read_server_env(SERVER_ENV_PATH)


def write_server_env(values: dict[str, str]) -> None:
    write_server_env_file(values, path=SERVER_ENV_PATH, ordered_keys=ORDERED_ENV_KEYS)


def with_activity_defaults(env: dict[str, str]) -> dict[str, str]:
    env.setdefault("ACTIVITY_LOGGING_ENABLED", "false")
    env.setdefault("ACTIVITY_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    env.setdefault("ACTIVITY_RISK_BURST_EVENTS", str(DEFAULT_RISK_BURST_EVENTS))
    env.setdefault("ACTIVITY_RISK_BURST_WINDOW_MINUTES", str(DEFAULT_RISK_BURST_WINDOW_MINUTES))
    env.setdefault("ACTIVITY_RISK_UNIQUE_HOSTS", str(DEFAULT_RISK_UNIQUE_HOSTS))
    env.setdefault("ACTIVITY_RISK_UNIQUE_PORTS", str(DEFAULT_RISK_UNIQUE_PORTS))
    env.setdefault("ACTIVITY_XRAY_GEOIP_WARNING_CODE", "")
    env.setdefault("ACTIVITY_ALERTS_ENABLED", "true")
    env.setdefault("ACTIVITY_ALERT_RETENTION_DAYS", str(DEFAULT_ALERT_RETENTION_DAYS))
    env.setdefault("XRAY_ERROR_EVENT_RETENTION_DAYS", str(DEFAULT_XRAY_ERROR_RETENTION_DAYS))
    env.setdefault("XRAY_ACCESS_LOG_RETENTION_DAYS", str(DEFAULT_XRAY_ACCESS_LOG_RETENTION_DAYS))
    env.setdefault("XRAY_ERROR_LOG_RETENTION_DAYS", str(DEFAULT_XRAY_ERROR_LOG_RETENTION_DAYS))
    env.setdefault("XRAY_RAW_LOG_ROTATE_TIME", DEFAULT_XRAY_RAW_LOG_ROTATE_TIME)
    return env


def activity_enabled(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else server_env_values()
    return (env.get("ACTIVITY_LOGGING_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "y")


def alerts_enabled(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else server_env_values()
    return (env.get("ACTIVITY_ALERTS_ENABLED") or "true").strip().lower() not in ("0", "false", "no", "n", "off")


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


def _retention_value(env: dict[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or str(default)).strip()
    try:
        value = int(raw, 10)
    except ValueError:
        return default
    return max(1, value)


def alert_retention_days(env: dict[str, str] | None = None) -> int:
    env = env if env is not None else server_env_values()
    return _retention_value(env, "ACTIVITY_ALERT_RETENTION_DAYS", DEFAULT_ALERT_RETENTION_DAYS)


def xray_error_event_retention_days(env: dict[str, str] | None = None) -> int:
    env = env if env is not None else server_env_values()
    return _retention_value(env, "XRAY_ERROR_EVENT_RETENTION_DAYS", DEFAULT_XRAY_ERROR_RETENTION_DAYS)


def xray_access_log_retention_days(env: dict[str, str] | None = None) -> int:
    env = env if env is not None else server_env_values()
    return _retention_value(env, "XRAY_ACCESS_LOG_RETENTION_DAYS", DEFAULT_XRAY_ACCESS_LOG_RETENTION_DAYS)


def xray_error_log_retention_days(env: dict[str, str] | None = None) -> int:
    env = env if env is not None else server_env_values()
    return _retention_value(env, "XRAY_ERROR_LOG_RETENTION_DAYS", DEFAULT_XRAY_ERROR_LOG_RETENTION_DAYS)


def raw_log_rotate_time(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else server_env_values()
    value = (env.get("XRAY_RAW_LOG_ROTATE_TIME") or DEFAULT_XRAY_RAW_LOG_ROTATE_TIME).strip()
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        return DEFAULT_XRAY_RAW_LOG_ROTATE_TIME
    return value


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
