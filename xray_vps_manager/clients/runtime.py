"""Client runtime state helpers."""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Any

from xray_vps_manager.traffic.repository import traffic_entry

ONLINE_WINDOW_SECONDS = 300


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
    if row["status"] != "enabled":
        return "offline", format_time(last_online, display_timezone)
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age = (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    state = "online" if age <= ONLINE_WINDOW_SECONDS else "offline"
    return state, format_time(last_online, display_timezone)


def traffic_updated_at(row: dict[str, Any], traffic_db: dict[str, Any], display_timezone: tzinfo) -> str:
    entry = traffic_entry(traffic_db, row["name"])
    return format_time(entry.get("updated", ""), display_timezone)
