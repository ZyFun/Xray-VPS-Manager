"""Status helpers for the interactive menu header."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.core.paths import SERVER_ENV_PATH, XRAY_ASSET_DIR
from xray_vps_manager.core.server_env import ORDERED_ENV_KEYS, read_server_env, write_server_env as write_server_env_file

SECURITY_AUDIT_ENV_KEY = "SECURITY_AUDIT_LAST_RUN"
SECURITY_AUDIT_STALE_DAYS = 30
MENU_ENV_REQUIRED_KEYS = [
    "SERVER_ADDR",
    "SERVER_NAME",
    "PORT",
    "REALITY_SNI",
    "REALITY_DEST",
    "FINGERPRINT",
    "MANAGER_TIMEZONE",
]


def die(message: str) -> None:
    raise SystemExit(message)


def current_xray_version() -> str:
    try:
        result = subprocess.run(
            ["/usr/local/bin/xray", "version"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return "unknown"
    if result.returncode != 0 or not result.stdout:
        return "unknown"
    match = re.search(r"(\d+(?:\.\d+){1,3})", result.stdout.splitlines()[0])
    return match.group(1) if match else result.stdout.splitlines()[0]


def server_env() -> dict[str, str]:
    return read_server_env(SERVER_ENV_PATH)


def write_server_env_values(values: dict[str, str]) -> None:
    updated = dict(values)
    for key in MENU_ENV_REQUIRED_KEYS:
        updated.setdefault(key, "")
    write_server_env_file(updated, path=SERVER_ENV_PATH, ordered_keys=ORDERED_ENV_KEYS)


def normalize_timezone(value: str | None) -> str:
    raw = (value or "").strip()
    if raw.lower() in ("", "server", "local", "default", "system", "сервер", "локально", "по умолчанию"):
        return ""
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        die("MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time.")
    return raw


def configured_timezone_name() -> str:
    return normalize_timezone(server_env().get("MANAGER_TIMEZONE", ""))


def manager_timezone():
    name = configured_timezone_name()
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


def manager_timezone_label() -> str:
    name = configured_timezone_name()
    if name:
        return name
    current = datetime.now().astimezone()
    suffix = current.tzname() or "server local time"
    return f"server local time ({suffix})"


def parse_utc_timestamp(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith(" UTC"):
        raw = raw[:-4].strip() + "+00:00"
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_manager_time(moment: datetime) -> str:
    local = moment.astimezone(manager_timezone())
    tz_name = local.tzname() or manager_timezone_label()
    return local.strftime("%Y-%m-%d %H:%M ") + tz_name


def manager_updated_header_value(value: str) -> str:
    moment = parse_utc_timestamp(value)
    if not moment:
        return value
    return format_manager_time(moment)


def asset_mtime_label(name: str) -> str:
    path = XRAY_ASSET_DIR / name
    if not path.exists():
        return f"{name}: missing"
    moment = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return f"{name}: {format_manager_time(moment)}"


def geo_assets_header_value() -> str:
    return "; ".join(asset_mtime_label(name) for name in ("geoip.dat", "geosite.dat"))


def last_security_audit_time() -> datetime | None:
    return parse_utc_timestamp(server_env().get(SECURITY_AUDIT_ENV_KEY, ""))


def security_audit_header_value() -> str:
    last_run = last_security_audit_time()
    if not last_run:
        return "не выполнялась"
    return format_manager_time(last_run)


def security_audit_is_stale() -> bool:
    last_run = last_security_audit_time()
    if not last_run:
        return True
    return datetime.now(timezone.utc) - last_run >= timedelta(days=SECURITY_AUDIT_STALE_DAYS)


def security_audit_header_warning() -> str:
    if not security_audit_is_stale():
        return ""
    return "Рекомендуется запустить: Безопасность -> Проверить безопасность сервера."


def record_security_audit_run() -> datetime | None:
    values = server_env()
    stamp = utc_stamp()
    values[SECURITY_AUDIT_ENV_KEY] = stamp
    write_server_env_values(values)
    return parse_utc_timestamp(stamp)
