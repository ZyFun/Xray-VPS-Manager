"""Client paid/free status helpers."""

from __future__ import annotations

from typing import Any

from xray_vps_manager.clients import models


def normalize_payment_type(value: str | None) -> str:
    return models.normalize_payment_type(value)


def payment_type_label(entry: dict[str, Any]) -> str:
    return models.payment_type_label(entry)


def set_payment_type(entry: dict[str, Any], value: str | None) -> str:
    payment_type = normalize_payment_type(value)
    entry["paymentType"] = payment_type
    return payment_type
