"""Status helpers for the interactive menu header."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone

from xray_vps_manager.core import clock
from xray_vps_manager.core.paths import SERVER_ENV_PATH, XRAY_ASSET_DIR
from xray_vps_manager.core.settings import update_env
from xray_vps_manager.core.settings import ORDERED_ENV_KEYS, read_env as read_server_env

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


def normalize_timezone(value: str | None) -> str:
    return clock.normalize_timezone_name(value)


def configured_timezone_name() -> str:
    return clock.manager_timezone(server_env()).name


def manager_timezone():
    return clock.manager_timezone(server_env()).tz


def manager_timezone_label() -> str:
    result = clock.manager_timezone(server_env())
    if result.name:
        return result.label
    suffix = clock.now().astimezone(result.tz).tzname() or "server local time"
    return f"{result.label} ({suffix})"


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
    return clock.now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    return clock.now() - last_run >= timedelta(days=SECURITY_AUDIT_STALE_DAYS)


def security_audit_header_warning() -> str:
    if not security_audit_is_stale():
        return ""
    return "Рекомендуется запустить: Безопасность -> Проверить безопасность сервера."


def record_security_audit_run() -> datetime | None:
    stamp = utc_stamp()
    update_env(
        {SECURITY_AUDIT_ENV_KEY: stamp},
        defaults={key: "" for key in MENU_ENV_REQUIRED_KEYS},
        path=SERVER_ENV_PATH,
        ordered_keys=ORDERED_ENV_KEYS,
    )
    return parse_utc_timestamp(stamp)
