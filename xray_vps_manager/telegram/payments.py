"""Telegram payment calculation helpers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING


def decimal_storage_value(value):
    return format(Decimal(value).normalize(), "f")


def parse_payment_value(value):
    raw = str(value or "").strip().replace(",", ".")
    if not raw or raw == "0":
        return "", "₽"
    if any(ch in raw for ch in "\r\n\t"):
        raise ValueError("Сумма оплаты должна быть одной строкой.")
    parts = raw.split()
    amount_raw = parts[0]
    currency = parts[1] if len(parts) > 1 else "₽"
    if currency not in ("₽", "$", "€"):
        raise ValueError("Валюта должна быть одной из: ₽, $, €.")
    try:
        amount = Decimal(amount_raw)
    except InvalidOperation as exc:
        raise ValueError("Сумма оплаты должна быть числом.") from exc
    if amount <= 0:
        return "", currency
    if amount > Decimal("1000000000"):
        raise ValueError("Сумма оплаты слишком большая.")
    return decimal_storage_value(amount), currency


def format_decimal_amount(amount):
    if not amount:
        return ""
    value = Decimal(str(amount))
    if value == value.to_integral_value():
        return format(value.quantize(Decimal("1")), "f")
    if value.as_tuple().exponent >= -2:
        return format(value.quantize(Decimal("0.01")), "f")
    return format(value.normalize(), "f")


def format_payment_amount(amount, currency):
    if not amount:
        return "не указана"
    return f"{format_decimal_amount(amount)} {currency}"


def parse_payment_rounding_step(value):
    raw = str(value or "").strip().replace(",", ".")
    if not raw:
        return "10"
    try:
        step = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Шаг округления должен быть числом.") from exc
    if step <= 0:
        raise ValueError("Шаг округления должен быть больше 0.")
    if step > Decimal("1000000000"):
        raise ValueError("Шаг округления слишком большой.")
    return decimal_storage_value(step)


def normalize_payment_rounding_mode(value):
    raw = str(value or "").strip().lower()
    if raw in ("", "none", "no", "off", "0", "без", "без округления"):
        return "none"
    if raw in ("step", "ceil", "up", "round", "round-up", "1", "шаг", "округлять"):
        return "step"
    raise ValueError("Режим округления должен быть none или step.")


def payment_rounding_settings(db):
    mode = db.get("paymentRoundingMode", "none")
    if mode not in ("none", "step"):
        mode = "none"
    try:
        step = parse_payment_rounding_step(db.get("paymentRoundingStep", "10"))
    except ValueError:
        step = "10"
    return mode, step


def payment_rounding_label(db):
    mode, step = payment_rounding_settings(db)
    if mode == "step":
        return f"вверх до {format_decimal_amount(step)}"
    return "без округления"


def payment_share_amount(total_amount, paid_count, rounding_mode="none", rounding_step="10"):
    if not total_amount or paid_count <= 0:
        return ""
    total = Decimal(str(total_amount))
    share = total / Decimal(paid_count)
    if normalize_payment_rounding_mode(rounding_mode) == "step":
        step = Decimal(parse_payment_rounding_step(rounding_step))
        rounded = (share / step).to_integral_value(rounding=ROUND_CEILING) * step
        return decimal_storage_value(rounded)
    return decimal_storage_value(share.quantize(Decimal("0.01"), rounding=ROUND_CEILING))
