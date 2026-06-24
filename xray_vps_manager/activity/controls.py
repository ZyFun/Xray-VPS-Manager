"""Activity control operations for parser state and limits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from xray_vps_manager.activity import repository
from xray_vps_manager.activity import settings
from xray_vps_manager.activity import sync
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.activity.constants import CONFIG_PATH, DETAIL_MODE_ALL, DETAIL_MODE_OFF


def load_activity_db() -> dict:
    return repository.load_activity_db(settings.retention_days(), settings.activity_enabled())


def access_log_setting() -> str:
    config = repository.load_json(CONFIG_PATH, {})
    return config.get("log", {}).get("access", "")


def access_log_available_for_parsing() -> bool:
    setting = access_log_setting()
    return bool(setting and setting != "none")


def set_enabled(value: bool) -> None:
    env = settings.with_activity_defaults(settings.server_env_values())
    env["ACTIVITY_LOGGING_ENABLED"] = "true" if value else "false"
    settings.write_server_env(env)


def prune_activity(db: dict, force: bool = False) -> int:
    return repository.prune_activity(
        db,
        settings.retention_days(),
        activity_time.today_utc_date(),
        activity_time.utc_now(),
        force=force,
    )


def set_retention_days(value: str) -> tuple[int, int]:
    days = settings.parse_retention_days(value)
    env = settings.with_activity_defaults(settings.server_env_values())
    env["ACTIVITY_RETENTION_DAYS"] = str(days)
    settings.write_server_env(env)
    db = load_activity_db()
    db["retentionDays"] = days
    removed = prune_activity(db, force=True)
    repository.save_activity_db(db)
    return days, removed


def _set_env_value(key: str, value: str) -> None:
    env = settings.with_activity_defaults(settings.server_env_values())
    env[key] = value
    settings.write_server_env(env)


def _cutoff_for_retention(days: int):
    cutoff_date = activity_time.today_utc_date() - timedelta(days=days - 1)
    return datetime.combine(cutoff_date, datetime.min.time(), tzinfo=timezone.utc)


def set_alert_retention_days(value: str) -> tuple[int, int]:
    days = settings.parse_retention_days(value)
    _set_env_value("ACTIVITY_ALERT_RETENTION_DAYS", str(days))
    removed = repository.prune_alerts_for_write(_cutoff_for_retention(days), strict=True)
    return days, removed


def set_alert_detection_enabled(value: bool) -> bool:
    _set_env_value("ACTIVITY_ALERTS_ENABLED", "true" if value else "false")
    return value


def set_xray_error_event_retention_days(value: str) -> tuple[int, int]:
    days = settings.parse_retention_days(value)
    _set_env_value("XRAY_ERROR_EVENT_RETENTION_DAYS", str(days))
    removed = repository.prune_xray_errors_for_write(_cutoff_for_retention(days), strict=True)
    return days, removed


def set_raw_log_retention_days(kind: str, value: str) -> int:
    days = settings.parse_retention_days(value)
    if kind == "access":
        _set_env_value("XRAY_ACCESS_LOG_RETENTION_DAYS", str(days))
        return days
    if kind == "error":
        _set_env_value("XRAY_ERROR_LOG_RETENTION_DAYS", str(days))
        return days
    raise ValueError("Raw log kind must be access or error.")


def set_raw_log_rotate_time(value: str) -> str:
    parsed = str(value or "").strip()
    if settings.raw_log_rotate_time({"XRAY_RAW_LOG_ROTATE_TIME": parsed}) != parsed:
        raise ValueError("Rotate time must be in HH:MM format.")
    _set_env_value("XRAY_RAW_LOG_ROTATE_TIME", parsed)
    return parsed


def set_risk_limits(
    burst_events: str,
    burst_window_minutes: str,
    unique_hosts: str,
    unique_ports: str,
) -> dict[str, int]:
    values = settings.risk_limit_env_values(burst_events, burst_window_minutes, unique_hosts, unique_ports)
    env = settings.with_activity_defaults(settings.server_env_values())
    for key, value in values.items():
        env[key] = str(value)
    settings.write_server_env(env)
    return settings.risk_limits(env)


def risk_limit_rows() -> list[list[object]]:
    limits = settings.risk_limits()
    return [
        ["Burst events", limits["burstEvents"]],
        ["Burst window", f"{limits['burstWindowMinutes']} minutes"],
        ["Unique hosts", limits["uniqueHosts"]],
        ["Unique ports", limits["uniquePorts"]],
    ]


def enable_activity() -> list[str]:
    repository.ensure_dirs()
    set_enabled(True)
    repository.set_detail_mode_for_write(DETAIL_MODE_ALL)
    db = load_activity_db()
    db["enabled"] = True
    db["detailMode"] = DETAIL_MODE_ALL
    db["retentionDays"] = settings.retention_days()
    sync.initialize_access_offset(db)
    repository.save_activity_db(db)
    messages = [
        "Activity log parsing enabled.",
        "Collection starts from the current access.log position; older access log lines are not imported.",
    ]
    if not access_log_available_for_parsing():
        messages.append("WARN: Xray access log is not configured. Parser is enabled, but no events will be collected until access log exists.")
    return messages


def disable_activity() -> list[str]:
    set_enabled(False)
    repository.set_detail_mode_for_write(DETAIL_MODE_OFF)
    db = load_activity_db()
    db["enabled"] = False
    db["detailMode"] = DETAIL_MODE_OFF
    repository.save_activity_db(db)
    return [
        "Detailed activity logging disabled.",
        "Alert-log, lightweight counters, and Xray access log config were not changed. Existing activity logs were kept.",
    ]
