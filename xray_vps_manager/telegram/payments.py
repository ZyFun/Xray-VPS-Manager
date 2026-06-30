"""Telegram payment calculation helpers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING

PAYMENT_SETTING_KEYS = (
    "paymentAmount",
    "paymentTotalAmount",
    "paymentDomainAnnualAmount",
    "paymentCurrency",
    "paymentRoundingMode",
    "paymentRoundingStep",
    "paymentTransferMethod",
    "paymentPhone",
    "paymentBank",
    "paymentCard",
    "paymentBankAccount",
)
MONEY_QUANT = Decimal("0.01")

PAYMENT_TRANSFER_METHODS = ("none", "phone", "card", "bank-account")
PAYMENT_PHONE_BANKS = (
    "Т-Банк (Тинькофф)",
    "Сбербанк",
    "ВТБ",
    "Альфа-Банк",
    "Газпромбанк",
)


def decimal_storage_value(value):
    return format(Decimal(value).normalize(), "f")


def parse_payment_value(value, default_currency="₽"):
    raw = str(value or "").strip().replace(",", ".")
    if not raw or raw == "0":
        currency = default_currency if default_currency in ("₽", "$", "€") else "₽"
        return "", currency
    if any(ch in raw for ch in "\r\n\t"):
        raise ValueError("Сумма оплаты должна быть одной строкой.")
    parts = raw.split()
    amount_raw = parts[0]
    currency = parts[1] if len(parts) > 1 else default_currency
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


def _positive_decimal(value) -> Decimal:
    try:
        amount = Decimal(str(value or "").strip() or "0")
    except InvalidOperation:
        return Decimal("0")
    if amount <= 0:
        return Decimal("0")
    return amount


def payment_currency(db):
    currency = db.get("paymentCurrency") or "₽"
    return currency if currency in ("₽", "$", "€") else "₽"


def domain_monthly_amount(db):
    annual = _positive_decimal(db.get("paymentDomainAnnualAmount", ""))
    if annual <= 0:
        return ""
    monthly = (annual / Decimal("12")).quantize(MONEY_QUANT, rounding=ROUND_CEILING)
    return decimal_storage_value(monthly)


def effective_total_amount(db):
    server_monthly = _positive_decimal(db.get("paymentTotalAmount", ""))
    domain_monthly = _positive_decimal(domain_monthly_amount(db))
    total = server_monthly + domain_monthly
    if total <= 0:
        return ""
    return decimal_storage_value(total)


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


def client_entries(client_db):
    if not isinstance(client_db, dict):
        return {}
    clients = client_db.get("clients", {})
    return clients if isinstance(clients, dict) else {}


def paid_client_count(client_db):
    return sum(1 for entry in client_entries(client_db).values() if entry.get("paymentType") == "paid")


def payment_amount_label(db, client_db):
    total = effective_total_amount(db)
    currency = payment_currency(db)
    count = paid_client_count(client_db)
    rounding_mode, rounding_step = payment_rounding_settings(db)
    share = payment_share_amount(total, count, rounding_mode, rounding_step)
    if not total:
        return "не указана"
    if count <= 0:
        return f"не рассчитана: нет платных клиентов (общая сумма: {format_payment_amount(total, currency)})"
    return format_payment_amount(share, currency)


def payment_summary(db, client_db):
    server_monthly = str(db.get("paymentTotalAmount") or "").strip()
    domain_annual = str(db.get("paymentDomainAnnualAmount") or "").strip()
    domain_monthly = domain_monthly_amount(db)
    total = effective_total_amount(db)
    currency = payment_currency(db)
    count = paid_client_count(client_db)
    rounding_mode, rounding_step = payment_rounding_settings(db)
    share = payment_share_amount(total, count, rounding_mode, rounding_step)
    summary = {
        "serverMonthly": format_payment_amount(server_monthly, currency),
        "domainAnnual": format_payment_amount(domain_annual, currency),
        "domainMonthly": format_payment_amount(domain_monthly, currency),
        "total": format_payment_amount(total, currency),
        "paidCount": count,
        "rounding": payment_rounding_label(db),
        "share": format_payment_amount(share, currency) if share else "не рассчитана",
        "transfer": payment_transfer_label(db),
    }
    if not total and count > 0:
        summary["warning"] = (
            "Rent amount is not set. Configure it in "
            "Telegram бот -> Настроить оплату и округление, "
            "or run: xray-telegram payment-amount '500 ₽'"
        )
    return summary


def apply_payment_amount(db, value):
    amount, currency = parse_payment_value(value, payment_currency(db))
    db["paymentTotalAmount"] = amount
    db["paymentCurrency"] = currency
    db["paymentAmount"] = format_payment_amount(amount, currency) if amount else ""
    return amount, currency


def apply_domain_annual_amount(db, value):
    amount, currency = parse_payment_value(value, payment_currency(db))
    db["paymentDomainAnnualAmount"] = amount
    db["paymentCurrency"] = currency
    return amount, currency


def apply_payment_rounding(db, mode_value, step_value=None):
    mode = normalize_payment_rounding_mode(mode_value)
    if mode == "step":
        if not step_value:
            raise ValueError("Для режима step нужно указать шаг округления.")
        db["paymentRoundingStep"] = parse_payment_rounding_step(step_value)
    db["paymentRoundingMode"] = mode
    if "paymentRoundingStep" not in db:
        db["paymentRoundingStep"] = "10"
    return mode, db["paymentRoundingStep"]


def _one_line(value, label, max_length=128):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{label} не может быть пустым.")
    if any(char in raw for char in "\r\n\t"):
        raise ValueError(f"{label} должно быть одной строкой.")
    if len(raw) > max_length:
        raise ValueError(f"{label} слишком длинное.")
    return raw


def normalize_phone_number(value):
    raw = _one_line(value, "Номер телефона", max_length=64)
    digits = "".join(char for char in raw if char.isdigit())
    if len(digits) < 7:
        raise ValueError("Номер телефона слишком короткий.")
    if raw.lstrip().startswith("+"):
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("8"):
        return f"+7{digits[1:]}"
    return f"+{digits}"


def normalize_payment_transfer_method(value):
    raw = str(value or "").strip().lower()
    aliases = {
        "": "none",
        "none": "none",
        "clear": "none",
        "off": "none",
        "no": "none",
        "0": "none",
        "phone": "phone",
        "phone-number": "phone",
        "tel": "phone",
        "1": "phone",
        "card": "card",
        "card-number": "card",
        "2": "card",
        "bank": "bank-account",
        "bank-account": "bank-account",
        "account": "bank-account",
        "3": "bank-account",
    }
    method = aliases.get(raw, raw)
    if method not in PAYMENT_TRANSFER_METHODS:
        raise ValueError("Способ перевода должен быть none, phone, card или bank-account.")
    return method


def normalized_payment_transfer(db):
    try:
        method = normalize_payment_transfer_method(db.get("paymentTransferMethod", "none"))
    except ValueError:
        method = "none"
    if method == "phone":
        phone = str(db.get("paymentPhone") or "").strip()
        bank = str(db.get("paymentBank") or "").strip()
        if phone and bank:
            return method, phone, bank
        return "none", "", ""
    if method == "card":
        card = str(db.get("paymentCard") or "").strip()
        if card:
            return method, card, ""
        return "none", "", ""
    if method == "bank-account":
        account = str(db.get("paymentBankAccount") or "").strip()
        if account:
            return method, account, ""
        return "none", "", ""
    return "none", "", ""


def payment_transfer_label(db):
    method, value, bank = normalized_payment_transfer(db)
    if method == "phone":
        return f"по номеру телефона {value}, банк: {bank}"
    if method == "card":
        return f"по номеру карты {value}"
    if method == "bank-account":
        return f"на банковский счёт {value}"
    return "не указаны"


def payment_transfer_message_lines(db):
    method, value, bank = normalized_payment_transfer(db)
    if method == "phone":
        return [
            "Перевод нужно выполнить по номеру телефона:",
            value,
            f"Банк: {bank}",
        ]
    if method == "card":
        return [f"Перевод нужно выполнить по номеру карты: {value}"]
    if method == "bank-account":
        return [f"Перевод нужно выполнить на банковский счёт: {value}"]
    return []


def clear_payment_transfer(db):
    db["paymentTransferMethod"] = "none"
    db["paymentPhone"] = ""
    db["paymentBank"] = ""
    db["paymentCard"] = ""
    db["paymentBankAccount"] = ""


def apply_payment_transfer(db, method_value, value="", bank=""):
    method = normalize_payment_transfer_method(method_value)
    clear_payment_transfer(db)
    if method == "none":
        return method
    if method == "phone":
        db["paymentTransferMethod"] = method
        db["paymentPhone"] = normalize_phone_number(value)
        db["paymentBank"] = _one_line(bank, "Банк", max_length=64)
        return method
    if method == "card":
        db["paymentTransferMethod"] = method
        db["paymentCard"] = _one_line(value, "Номер карты", max_length=64)
        return method
    if method == "bank-account":
        db["paymentTransferMethod"] = method
        db["paymentBankAccount"] = _one_line(value, "Банковский счёт", max_length=128)
        return method
    raise ValueError("Неизвестный способ перевода.")
