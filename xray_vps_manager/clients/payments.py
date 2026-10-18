"""Client paid/free status helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from xray_vps_manager.clients import access, models


def normalize_payment_type(value: str | None) -> str:
    return models.normalize_payment_type(value)


def counts_toward_payment_share(entry: Any, now: datetime | None = None) -> bool:
    """A paid client shares the rent only while enabled and with unexpired access."""
    if not isinstance(entry, dict) or entry.get("paymentType") != "paid":
        return False
    if entry.get("enabled") is False:
        return False
    return not access.access_expired(entry, now)


def payment_type_label(entry: dict[str, Any]) -> str:
    return models.payment_type_label(entry)


def set_payment_type(entry: dict[str, Any], value: str | None) -> str:
    payment_type = normalize_payment_type(value)
    entry["paymentType"] = payment_type
    return payment_type
