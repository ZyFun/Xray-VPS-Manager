"""Access period helpers."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any

from xray_vps_manager.clients.settings import manager_timezone


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_access_days(value: str | None) -> int | None:
    raw = (value or "").strip().lower()
    if raw in ("", "0", "none", "never", "no", "unlimited", "forever", "бессрочно", "без срока"):
        return None
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError("Access days must be a positive number. Empty or 0 means unlimited access.")
    days = int(raw, 10)
    if days < 1:
        return None
    if days > 36500:
        raise ValueError("Access days is too large. Use a value up to 36500.")
    return days


def parse_extend_days(value: str | None) -> int:
    raw = (value or "").strip().lower().lstrip("+")
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError("Extend days must be a positive number.")
    days = int(raw, 10)
    if days < 1:
        raise ValueError("Extend days must be a positive number.")
    if days > 36500:
        raise ValueError("Extend days is too large. Use a value up to 36500.")
    return days


def local_now() -> datetime:
    return datetime.now(manager_timezone()).replace(microsecond=0)


def expires_at_from_days(days: int | None) -> str:
    if days is None:
        return ""
    now = local_now()
    expire_date = now.date() + timedelta(days=days)
    expire_at = datetime.combine(expire_date, time.min, tzinfo=now.tzinfo)
    return expire_at.isoformat(timespec="seconds")


def set_entry_expiry(entry: dict[str, Any], days: int | None) -> None:
    expires_at = expires_at_from_days(days)
    if expires_at:
        entry["expiresAt"] = expires_at
        entry["accessDays"] = days
        entry.pop("expiredAt", None)
    else:
        entry.pop("expiresAt", None)
        entry.pop("accessDays", None)
        entry.pop("expiredAt", None)


def extended_expires_at(entry: dict[str, Any], days: int) -> str:
    now = local_now()
    current = parse_datetime(entry.get("expiresAt", ""))
    if current is None:
        base_date = now.date()
    else:
        current_date = current.astimezone(now.tzinfo).date()
        base_date = max(current_date, now.date())
    expire_date = base_date + timedelta(days=days)
    return datetime.combine(expire_date, time.min, tzinfo=now.tzinfo).isoformat(timespec="seconds")


def extend_entry_expiry(entry: dict[str, Any], days: int) -> None:
    if days < 1:
        raise ValueError("Extend days must be a positive number.")
    entry["expiresAt"] = extended_expires_at(entry, days)
    try:
        previous_days = int(entry.get("accessDays", 0) or 0)
    except (TypeError, ValueError):
        previous_days = 0
    entry["accessDays"] = previous_days + days if previous_days > 0 else days
    entry.pop("expiredAt", None)


def access_expired(entry: dict[str, Any], now: datetime | None = None) -> bool:
    expires_at = parse_datetime(entry.get("expiresAt", ""))
    if expires_at is None:
        return False
    now = now or local_now()
    return now >= expires_at.astimezone(now.tzinfo)


def format_access_until(value: str | None) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return "бессрочно"
    return parsed.astimezone(manager_timezone()).strftime("%Y-%m-%d %H:%M")


def access_deadline_at_midnight(value: str | None, tz) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return ""
    local = parsed.astimezone(tz)
    return datetime.combine(local.date(), time.min, tzinfo=tz).isoformat(timespec="minutes")
