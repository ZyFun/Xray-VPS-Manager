"""Activity control operations for parser state and limits."""

from __future__ import annotations

from xray_vps_manager.activity import repository
from xray_vps_manager.activity import settings
from xray_vps_manager.activity import sync
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.activity.constants import CONFIG_PATH


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
    db = load_activity_db()
    db["enabled"] = True
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
    db = load_activity_db()
    db["enabled"] = False
    repository.save_activity_db(db)
    return [
        "Activity log parsing disabled.",
        "Xray access log config was not changed. Existing activity logs were kept.",
    ]
