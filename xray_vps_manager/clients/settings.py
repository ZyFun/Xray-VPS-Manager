"""Server/client settings read from server.env."""

from __future__ import annotations

import re
from pathlib import Path

from xray_vps_manager.core import clock
from xray_vps_manager.core.paths import SERVER_ENV_PATH
from xray_vps_manager.core.settings import read_env as read_server_env, write_env as write_server_env, env_value

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


def server_env_values(path: Path | None = None) -> dict[str, str]:
    return read_server_env(path)


def server_env_value(key: str, default: str = "", path: Path | None = None) -> str:
    return env_value(key, default, path)


def save_server_env_values(values: dict[str, str], path: Path | None = None) -> None:
    ordered = [
        "SERVER_ADDR",
        "SERVER_NAME",
        "PORT",
        "REALITY_SNI",
        "REALITY_DEST",
        "FINGERPRINT",
        "REALITY_TRANSPORT",
        "GRPC_SERVICE_NAME",
        "XHTTP_PATH",
        "XHTTP_MODE",
        "MANAGER_TIMEZONE",
    ]
    updated = dict(values)
    for key in ordered:
        updated.setdefault(key, "")
    write_server_env(updated, path=path, ordered_keys=ordered)


def normalize_timezone(value: str | None) -> str:
    return clock.normalize_timezone_name(value)


def manager_timezone_info() -> clock.ManagerTimezone:
    # Сохраняем прежний приоритет env_value для клиентских настроек.
    value = server_env_value("MANAGER_TIMEZONE", "")
    return clock.manager_timezone({"MANAGER_TIMEZONE": value})


def configured_timezone_name() -> str:
    return manager_timezone_info().name


def manager_timezone():
    return manager_timezone_info().tz


def manager_timezone_label() -> str:
    result = manager_timezone_info()
    if result.name:
        return result.label
    suffix = clock.now().astimezone(result.tz).tzname() or "server local time"
    return f"{result.label} ({suffix})"


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
