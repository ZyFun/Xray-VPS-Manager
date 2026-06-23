"""Client runtime state helpers."""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Any

from xray_vps_manager.traffic.repository import credential_traffic_entry, traffic_entry

ONLINE_WINDOW_SECONDS = 300


def normalize_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def runtime_traffic_for(stats: dict[str, int] | None, email: str) -> tuple[int | None, int | None]:
    if stats is None:
        return None, None
    uplink = stats.get(f"user>>>{email}>>>traffic>>>uplink", 0)
    downlink = stats.get(f"user>>>{email}>>>traffic>>>downlink", 0)
    return uplink, downlink


def traffic_for(
    traffic_db: dict[str, Any],
    stats: dict[str, int] | None,
    row: dict[str, Any],
) -> tuple[int | None, int | None]:
    entry = traffic_entry(traffic_db, row["name"])
    if entry:
        return int(entry.get("incoming", 0)), int(entry.get("outgoing", 0))
    return runtime_traffic_for(stats, row["email"])


def credential_traffic_for(
    traffic_db: dict[str, Any],
    stats: dict[str, int] | None,
    row: dict[str, Any],
) -> tuple[int | None, int | None]:
    entry = credential_traffic_entry(traffic_db, row["name"], row["connection"])
    if entry:
        return int(entry.get("incoming", 0)), int(entry.get("outgoing", 0))
    return runtime_traffic_for(stats, row["email"])


def parse_time(value: str | None) -> datetime | None:
    if not value or value in ("never", "unknown"):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_time(value: str | None, display_timezone: tzinfo) -> str:
    parsed = parse_time(value)
    if parsed is None:
        return "never"
    local = parsed.astimezone(display_timezone)
    return local.strftime("%Y-%m-%d %H:%M %Z")


def last_online_display(
    entry: dict[str, Any],
    display_timezone: tzinfo,
    now_utc: datetime | None = None,
) -> str:
    last_online = entry.get("lastOnline", "")
    parsed = parse_time(last_online)
    if parsed is None:
        return "never"
    now = normalize_utc(now_utc or datetime.now(timezone.utc))
    if parsed.astimezone(timezone.utc) > now:
        updated = entry.get("updated", "")
        updated_parsed = parse_time(updated)
        if updated_parsed is not None and updated_parsed.astimezone(timezone.utc) <= now:
            return format_time(updated, display_timezone)
        return "never"
    return format_time(last_online, display_timezone)


def online_state(
    row: dict[str, Any],
    traffic_db: dict[str, Any],
    display_timezone: tzinfo,
    now_utc: datetime | None = None,
) -> tuple[str, str]:
    entry = traffic_entry(traffic_db, row["name"])
    last_online = entry.get("lastOnline", "")
    parsed = parse_time(last_online)
    if parsed is None:
        return "offline", "never"
    now = normalize_utc(now_utc or datetime.now(timezone.utc))
    display_value = last_online_display(entry, display_timezone, now)
    if row["status"] != "enabled":
        return "offline", display_value
    age = (now - parsed.astimezone(timezone.utc)).total_seconds()
    state = "online" if 0 <= age <= ONLINE_WINDOW_SECONDS else "offline"
    return state, display_value


def credential_online_state(
    row: dict[str, Any],
    traffic_db: dict[str, Any],
    display_timezone: tzinfo,
    now_utc: datetime | None = None,
) -> tuple[str, str]:
    entry = credential_traffic_entry(traffic_db, row["name"], row["connection"])
    last_online = entry.get("lastOnline", "")
    parsed = parse_time(last_online)
    if parsed is None:
        return "offline", "never"
    now = normalize_utc(now_utc or datetime.now(timezone.utc))
    display_value = last_online_display(entry, display_timezone, now)
    if row["status"] != "enabled":
        return "offline", display_value
    age = (now - parsed.astimezone(timezone.utc)).total_seconds()
    state = "online" if 0 <= age <= ONLINE_WINDOW_SECONDS else "offline"
    return state, display_value


def traffic_updated_at(row: dict[str, Any], traffic_db: dict[str, Any], display_timezone: tzinfo) -> str:
    entry = traffic_entry(traffic_db, row["name"])
    return format_time(entry.get("updated", ""), display_timezone)


def credential_traffic_updated_at(row: dict[str, Any], traffic_db: dict[str, Any], display_timezone: tzinfo) -> str:
    entry = credential_traffic_entry(traffic_db, row["name"], row["connection"])
    return format_time(entry.get("updated", ""), display_timezone)
