"""Client data helpers and lightweight models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PAYMENT_TYPES = {"paid", "free"}


@dataclass
class ClientRecord:
    name: str
    client_id: str
    created: str = ""
    enabled: bool = True
    connection: str = ""
    payment_type: str = "free"

    @classmethod
    def from_entry(cls, name: str, entry: dict[str, Any]) -> "ClientRecord":
        return cls(
            name=name,
            client_id=str(entry.get("id", "")),
            created=str(entry.get("created", "")),
            enabled=entry.get("enabled") is not False,
            connection=str(entry.get("connection", "")),
            payment_type=payment_type_label(entry),
        )


@dataclass
class RealityConnection:
    tag: str
    name: str
    port: int
    sni: str
    dest: str
    fingerprint: str
    created: str = ""

    @classmethod
    def from_entry(cls, tag: str, entry: dict[str, Any]) -> "RealityConnection":
        return cls(
            tag=tag,
            name=str(entry.get("name", "")),
            port=int(entry.get("port", 0) or 0),
            sni=str(entry.get("sni", "")),
            dest=str(entry.get("dest", "")),
            fingerprint=str(entry.get("fingerprint", "")),
            created=str(entry.get("created", "")),
        )


def split_email(email: str) -> tuple[str, str]:
    if "|created=" in str(email):
        name, created = str(email).split("|created=", 1)
        return name, created
    return str(email), ""


def client_name(item: dict[str, Any]) -> str:
    return split_email(item.get("email", ""))[0]


def normalize_payment_type(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in ("paid", "pay", "yes", "y", "1", "платный", "платно", "да"):
        return "paid"
    if raw in ("", "free", "no", "n", "0", "бесплатный", "бесплатно", "нет"):
        return "free"
    raise ValueError("Payment type must be paid or free.")


def payment_type_label(entry: dict[str, Any]) -> str:
    return "paid" if entry.get("paymentType") == "paid" else "free"


def db_entry_from_client(
    item: dict[str, Any],
    created: str = "",
    enabled: bool = True,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name, email_created = split_email(item.get("email", ""))
    previous = previous or {}
    created = created or previous.get("created", "") or email_created
    entry: dict[str, Any] = {
        "id": item.get("id", ""),
        "created": created,
        "enabled": enabled,
        "client": dict(item),
    }
    for key in (
        "expiresAt",
        "accessDays",
        "expiredAt",
        "disabledReason",
        "disabledAt",
        "trafficLimit",
        "trafficLimitExceededAt",
        "trafficLimitExceededPeriod",
        "trafficLimitExceededBytes",
        "trafficLimitResetAt",
        "paymentType",
    ):
        if key in previous:
            entry[key] = previous[key]
    entry["paymentType"] = normalize_payment_type(entry.get("paymentType", "free"))
    if "connection" in previous:
        entry["connection"] = previous["connection"]
    if not entry["client"].get("email") and name:
        entry["client"]["email"] = f"{name}|created={created}" if created else name
    return entry


def client_from_db_entry(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    created = entry.get("created", "")
    client = dict(entry.get("client") or {})
    client.setdefault("id", entry.get("id", ""))
    client.setdefault("flow", "xtls-rprx-vision")
    client.setdefault("level", 0)
    client["email"] = f"{name}|created={created}" if created else name
    if not client.get("id"):
        raise ValueError(f"Client has no UUID in database: {name}")
    return client
