"""Control operations for traffic report settings."""

from __future__ import annotations

from decimal import Decimal

from xray_vps_manager.core.settings import update_env
from xray_vps_manager.traffic import settings


def _update_settings(changes: dict[str, str]) -> dict[str, str]:
    return update_env(changes, defaults=settings.with_total_multiplier_defaults({}))


def set_total_multiplier_enabled(value: bool) -> None:
    _update_settings({settings.TOTAL_MULTIPLIER_ENABLED_KEY: "true" if value else "false"})


def set_total_multiplier(value: str) -> Decimal:
    multiplier = settings.parse_total_multiplier(value)
    _update_settings({settings.TOTAL_MULTIPLIER_KEY: settings.format_total_multiplier(multiplier)})
    return multiplier


def total_multiplier_rows() -> list[list[object]]:
    env = settings.with_total_multiplier_defaults(settings.server_env_values())
    multiplier = settings.total_multiplier(env)
    label = settings.total_multiplier_label(multiplier)
    enabled = settings.total_multiplier_enabled(env)
    return [
        [f"Строка TOTAL {label}", "включена" if enabled else "выключена"],
        ["Множитель", label],
    ]
