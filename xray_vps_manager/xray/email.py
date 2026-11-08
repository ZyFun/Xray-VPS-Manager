"""Разбор и формирование email клиентов Xray без проверки формата."""

from typing import Any


def email_metadata(email: str) -> tuple[str, dict[str, str]]:
    parts = str(email or "").split("|")
    name = parts[0]
    metadata: dict[str, str] = {}
    for part in parts[1:]:
        key, separator, value = part.partition("=")
        if separator and key:
            metadata[key] = value
    return name, metadata


def split_email(email: str) -> tuple[str, str]:
    name, metadata = email_metadata(email)
    return name, metadata.get("created", "")


def email_for_client(name: str, created: str = "", *, connection_tag: str = "") -> str:
    parts = [str(name)]
    if created:
        parts.append(f"created={created}")
    if connection_tag:
        parts.append(f"connection={connection_tag}")
    return "|".join(parts)


def client_name(item: dict[str, Any]) -> str:
    return split_email(item.get("email", ""))[0]
