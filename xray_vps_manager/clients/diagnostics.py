"""Client diagnostics helpers used by server checks."""

from __future__ import annotations

from typing import Any

from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients.models import split_email
from xray_vps_manager.xray.config import inbound_tag


def _inbound_clients(inbound: dict[str, Any]) -> list[dict[str, Any]]:
    items = inbound.get("settings", {}).get("clients", [])
    return items if isinstance(items, list) else []


def _name_from_email(email: str) -> str:
    return split_email(email)[0]


def active_managed_client_rows(inbounds: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for inbound in inbounds:
        tag = inbound_tag(inbound)
        protocol = str(inbound.get("protocol") or "").strip().lower()
        for item in _inbound_clients(inbound):
            email = str(item.get("email") or "").strip()
            name = _name_from_email(email)
            if not email or not name:
                continue
            rows.append(
                {
                    "name": name,
                    "connection": tag,
                    "protocol": protocol,
                    "email": email,
                }
            )
    return rows


def cross_protocol_duplicate_rows(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    rows_by_name: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        rows_by_name.setdefault(row["name"], []).append(row)
    return {
        name: items
        for name, items in rows_by_name.items()
        if {"vless", "trojan"}.issubset({item["protocol"] for item in items})
    }


def duplicate_active_client_name_issues(
    rows: list[dict[str, str]],
    db_clients: dict[str, dict[str, Any]],
) -> list[str]:
    rows_by_email: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        rows_by_email.setdefault(row["email"], []).append(row)

    issues = []
    for email, email_rows in rows_by_email.items():
        connection_tags = {row["connection"] for row in email_rows}
        protocols = {row["protocol"] for row in email_rows}
        if len(connection_tags) > 1 and {"vless", "trojan"}.issubset(protocols):
            issue_rows = ", ".join(f"{row['connection']}:{row['protocol']}" for row in email_rows)
            issues.append(
                f"{email_rows[0]['name']}: same active email is reused across "
                f"VLESS/Trojan credentials: {email} ({issue_rows})"
            )

    for name, name_rows in cross_protocol_duplicate_rows(rows).items():
        entry = db_clients.get(name)
        if not isinstance(entry, dict):
            issues.append(f"{name}: active VLESS/Trojan duplicate is missing from SQLite clients")
            continue
        credentials = entry.get("credentials", {})
        if not isinstance(credentials, dict):
            credentials = {}
        active_tags = {row["connection"] for row in name_rows}
        missing_tags = sorted(active_tags - set(credentials))
        if missing_tags:
            issues.append(f"{name}: active VLESS/Trojan credentials missing from SQLite: " + ", ".join(missing_tags))
            continue
        for row in name_rows:
            credential = credentials.get(row["connection"], {})
            if not isinstance(credential, dict):
                issues.append(f"{name}: invalid SQLite credential for {row['connection']}")
                continue
            if credential.get("enabled") is False:
                issues.append(f"{name}: SQLite credential is disabled but active in config: {row['connection']}")
                continue
            expected_email = client_credentials.credential_email(name, entry, credential)
            if expected_email != row["email"]:
                issues.append(
                    f"{name}: active email mismatch for {row['connection']}: "
                    f"config={row['email']} sqlite={expected_email}"
                )
    return issues
