"""Server/client settings read from server.env."""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.core.paths import SERVER_ENV_PATH

DEFAULT_SERVER_ADDR = ""
DEFAULT_SERVER_NAME = "Xray"
FINGERPRINTS = {
    "chrome",
    "firefox",
    "safari",
    "ios",
    "android",
    "edge",
    "360",
    "qq",
    "random",
    "randomized",
}
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def server_env_values(path: Path = SERVER_ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"').strip("'")
    return values


def server_env_value(key: str, default: str = "") -> str:
    return server_env_values().get(key) or os.environ.get(key, default)


def save_server_env_values(values: dict[str, str], path: Path = SERVER_ENV_PATH) -> None:
    ordered = ["SERVER_ADDR", "SERVER_NAME", "PORT", "REALITY_SNI", "REALITY_DEST", "FINGERPRINT", "MANAGER_TIMEZONE"]
    lines = [f"{key}={values.get(key, '')}" for key in ordered]
    for key in sorted(values):
        if key not in ordered:
            lines.append(f"{key}={values[key]}")

    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(path)


def normalize_timezone(value: str | None) -> str:
    raw = (value or "").strip()
    if raw.lower() in ("", "server", "local", "default", "system", "сервер", "локально", "по умолчанию"):
        return ""
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time.") from exc
    return raw


def configured_timezone_name() -> str:
    return normalize_timezone(server_env_value("MANAGER_TIMEZONE", ""))


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


def server_addr() -> str:
    value = server_env_value("SERVER_ADDR", DEFAULT_SERVER_ADDR)
    if value:
        return value
    raise ValueError(f"SERVER_ADDR is not set. Check {SERVER_ENV_PATH} or set SERVER_ADDR manually.")


def server_name() -> str:
    value = server_env_value("SERVER_NAME", DEFAULT_SERVER_NAME).strip()
    if not value:
        return DEFAULT_SERVER_NAME
    if not SERVER_NAME_RE.fullmatch(value):
        raise ValueError("SERVER_NAME must be 1-64 chars: A-Z a-z 0-9 _ . @ -")
    return value


def fingerprint() -> str:
    value = server_env_value("FINGERPRINT", "chrome").lower()
    if value not in FINGERPRINTS:
        raise ValueError("FINGERPRINT must be one of: " + ", ".join(sorted(FINGERPRINTS)))
    return value
