"""Traffic report settings stored in server.env."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from xray_vps_manager.core.paths import SERVER_ENV_PATH
from xray_vps_manager.core.server_env import ORDERED_ENV_KEYS, read_server_env, write_server_env as write_server_env_file

TOTAL_MULTIPLIER_ENABLED_KEY = "TRAFFIC_TOTAL_MULTIPLIER_ENABLED"
TOTAL_MULTIPLIER_KEY = "TRAFFIC_TOTAL_MULTIPLIER"
DEFAULT_TOTAL_MULTIPLIER = Decimal("2")
MIN_TOTAL_MULTIPLIER = Decimal("0.01")
MAX_TOTAL_MULTIPLIER = Decimal("100")
TRUTHY_VALUES = {"1", "true", "yes", "y", "on", "вкл", "да"}


def server_env_values(path: Path = SERVER_ENV_PATH) -> dict[str, str]:
    return read_server_env(path)


def write_server_env(values: dict[str, str], path: Path = SERVER_ENV_PATH) -> None:
    write_server_env_file(values, path=path, ordered_keys=ORDERED_ENV_KEYS)


def with_total_multiplier_defaults(env: dict[str, str]) -> dict[str, str]:
    updated = dict(env)
    updated.setdefault(TOTAL_MULTIPLIER_ENABLED_KEY, "false")
    updated.setdefault(TOTAL_MULTIPLIER_KEY, format_total_multiplier(DEFAULT_TOTAL_MULTIPLIER))
    return updated


def total_multiplier_enabled(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else server_env_values()
    return (env.get(TOTAL_MULTIPLIER_ENABLED_KEY) or "false").strip().lower() in TRUTHY_VALUES


def parse_total_multiplier(value: str | Decimal | int | float) -> Decimal:
    raw = str(value or "").strip().replace(",", ".")
    if not raw:
        raise ValueError("MULTIPLIER must be a number from 0.01 to 100.")
    try:
        multiplier = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("MULTIPLIER must be a number from 0.01 to 100.") from exc
    if not multiplier.is_finite():
        raise ValueError("MULTIPLIER must be a number from 0.01 to 100.")
    if multiplier < MIN_TOTAL_MULTIPLIER or multiplier > MAX_TOTAL_MULTIPLIER:
        raise ValueError("MULTIPLIER must be a number from 0.01 to 100.")
    return multiplier


def total_multiplier(env: dict[str, str] | None = None) -> Decimal:
    env = env if env is not None else server_env_values()
    try:
        return parse_total_multiplier(env.get(TOTAL_MULTIPLIER_KEY) or format_total_multiplier(DEFAULT_TOTAL_MULTIPLIER))
    except ValueError:
        return DEFAULT_TOTAL_MULTIPLIER


def format_total_multiplier(multiplier: Decimal | str | int | float) -> str:
    value = parse_total_multiplier(multiplier) if not isinstance(multiplier, Decimal) else multiplier
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def total_multiplier_label(multiplier: Decimal | str | int | float | None = None) -> str:
    value = total_multiplier() if multiplier is None else multiplier
    return f"x{format_total_multiplier(value)}"


def multiplied_total_bytes(total_bytes: int, multiplier: Decimal | str | int | float) -> int:
    value = parse_total_multiplier(multiplier) if not isinstance(multiplier, Decimal) else multiplier
    return int((Decimal(int(total_bytes or 0)) * value).to_integral_value(rounding=ROUND_HALF_UP))
