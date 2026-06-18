"""Global activity blocklist management and reports."""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import reports as activity_reports
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.activity.constants import EXCEPTION_VALUE_RE
from xray_vps_manager.activity.parser import parse_target
from xray_vps_manager.core.paths import CONFIG_PATH, XRAY_BIN
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity_blocklist as sqlite_blocklist
from xray_vps_manager.db.storage import SQLiteReadUnavailable, sqlite_read_ready
from xray_vps_manager.xray import blocklist as xray_blocklist
from xray_vps_manager.xray.config import load_config as load_xray_config
from xray_vps_manager.xray.config import save_config

FOREVER_DURATION_VALUES = {"", "0", "forever", "unlimited", "never", "none", "бессрочно"}


def normalize_block_value(value: str, fatal: bool = True) -> str:
    def fail(message: str) -> None:
        raise ValueError(message)

    raw = str(value or "").strip()
    if not raw:
        fail("Blocklist value must not be empty.")

    if raw.startswith(("tcp:", "udp:")):
        _network, host, _port = parse_target(raw)
        raw = host or raw

    if "://" in raw:
        parsed = urlparse(raw)
        raw = parsed.hostname or raw

    for prefix in ("domain:", "full:"):
        if raw.lower().startswith(prefix):
            raw = raw.split(":", 1)[1]
            break

    raw = raw.strip().strip("[]").strip().lower()
    if not raw:
        fail("Blocklist value must contain a domain, IP, CIDR, or wildcard mask.")

    if "/" not in raw and raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if port.isdigit():
            raw = host.strip("[]")

    if not EXCEPTION_VALUE_RE.fullmatch(raw):
        fail("Blocklist value may contain only letters, digits, dot, dash, underscore, *, ?, /, and :.")
    return raw


def classify_block_value(value: str, fatal: bool = True) -> tuple[str, str]:
    normalized = normalize_block_value(value, fatal=fatal)
    if "/" in normalized:
        try:
            ipaddress.ip_network(normalized, strict=False)
            return normalized, "cidr"
        except ValueError:
            if fatal:
                raise ValueError("CIDR block must be a valid IP network, for example 203.0.113.0/24.")
            raise
    try:
        ipaddress.ip_address(normalized)
        return normalized, "ip"
    except ValueError:
        pass
    if ":" in normalized:
        raise ValueError("Blocklist value is not a valid IP address or domain.")
    if "*" in normalized or "?" in normalized:
        return normalized, "mask"
    return normalized, "domain"


def normalize_source(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@:-]+", "_", str(value or "manual")).strip("_") or "manual"


def expires_at_from_duration(duration: str | int | None) -> str:
    raw = str(duration if duration is not None else "").strip().lower()
    if raw in FOREVER_DURATION_VALUES:
        return ""
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError("Block duration must be 'forever' or a positive number of days.")
    days = int(raw, 10)
    if days < 1:
        return ""
    return (activity_time.utc_now() + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def open_ready_database(db_path: str | Path | None = None):
    if not database.database_file_exists(db_path):
        raise SQLiteReadUnavailable("SQLite manager database is missing.")
    connection = database.open_database(db_path)
    if not sqlite_read_ready(connection):
        connection.close()
        raise SQLiteReadUnavailable("SQLite database is not marked ready.")
    return connection


def add_block(
    value: str,
    *,
    source_client: str = "",
    duration: str | int | None = None,
    comment: str = "",
    source_event_id: int | None = None,
    source: str = "manual",
    db_path: str | Path | None = None,
) -> dict:
    normalized, kind = classify_block_value(value)
    expires_at = expires_at_from_duration(duration)
    connection = open_ready_database(db_path)
    try:
        item = sqlite_blocklist.upsert_block(
            connection,
            {
                "value": normalized,
                "kind": kind,
                "sourceClient": source_client.strip() or None,
                "sourceEventId": source_event_id,
                "source": normalize_source(source),
                "comment": comment.strip(),
                "createdAt": activity_time.utc_stamp(),
                "expiresAt": expires_at,
                "enabled": True,
            },
        )
        return item
    finally:
        connection.close()


def delete_block(value_or_id: str, *, db_path: str | Path | None = None) -> dict:
    connection = open_ready_database(db_path)
    try:
        item = sqlite_blocklist.delete_block(connection, value_or_id)
        if item is None:
            raise KeyError(value_or_id)
        return item
    finally:
        connection.close()


def block_items(*, db_path: str | Path | None = None) -> list[dict]:
    connection = open_ready_database(db_path)
    try:
        return sqlite_blocklist.list_blocks(connection)
    finally:
        connection.close()


def active_block_items(*, db_path: str | Path | None = None) -> list[dict]:
    connection = open_ready_database(db_path)
    try:
        return sqlite_blocklist.active_blocks(connection, activity_time.utc_stamp())
    finally:
        connection.close()


def item_status(item: dict, now_iso: str | None = None) -> str:
    if not item.get("enabled", True):
        return "disabled"
    expires_at = item.get("expiresAt") or ""
    if expires_at and expires_at <= (now_iso or activity_time.utc_stamp()):
        return "expired"
    return "active"


def list_block_rows(*, db_path: str | Path | None = None) -> list[dict]:
    now_iso = activity_time.utc_stamp()
    stats_by_id = {int(item["id"]): item for item in block_stats_rows(db_path=db_path)}
    return [
        {
            **item,
            "status": item_status(item, now_iso),
            "expiresAt": item.get("expiresAt") or "forever",
            "lastHitAt": stats_by_id.get(int(item["id"]), {}).get("lastSeen", ""),
        }
        for item in block_items(db_path=db_path)
    ]


def host_for_match(host: str) -> str:
    value = str(host or "").strip().strip("[]").lower()
    if not value:
        return ""
    if "/" not in value and value.count(":") == 1:
        candidate, port = value.rsplit(":", 1)
        if port.isdigit():
            value = candidate.strip("[]")
    return value


def block_matches_host(item: dict, host: str) -> bool:
    value = item.get("value", "")
    kind = item.get("kind", "")
    host_value = host_for_match(host)
    if not value or not host_value:
        return False
    if kind == "cidr":
        try:
            return ipaddress.ip_address(host_value) in ipaddress.ip_network(value, strict=False)
        except ValueError:
            return False
    if kind == "ip":
        return host_value == value
    if kind == "mask":
        return fnmatch.fnmatchcase(host_value, value)
    if kind == "domain":
        return host_value == value or host_value.endswith("." + value)
    return False


def event_is_blocked(event: dict) -> bool:
    outbound = str(event.get("outbound") or "").lower()
    risks = {str(risk).lower() for risk in event.get("risks", [])}
    return outbound == "blocked" or "blocked" in risks


def record_blocked_event_hit(connection, event: dict) -> dict | None:
    if not event_is_blocked(event):
        return None
    client_name = str(event.get("client") or event.get("client_name") or "").strip()
    host = str(event.get("host") or "").strip()
    event_time = str(event.get("time") or event.get("event_time") or activity_time.utc_stamp())
    if not client_name or not host:
        return None
    for item in sqlite_blocklist.active_blocks(connection, event_time):
        if block_matches_host(item, host):
            sqlite_blocklist.record_hit(connection, int(item["id"]), client_name, event_time)
            return item
    return None


def event_has_geoip_region(event: dict, region: str) -> bool:
    expected = region.upper()
    for risk in activity_reports.geoip_risks_for_event(event):
        if risk.split(":", 1)[1].upper() == expected:
            return True
    return False


def block_candidate_rows(client_name: str, days_value: str = "7", region: str = "RU") -> list[dict]:
    days = int(days_value or "7", 10)
    start, end = activity_time.date_range_from_days(days)
    active_values = {item["value"] for item in active_block_items()}
    candidates: dict[str, dict] = {}
    for event in activity_repository.iter_events_for_read(client_name, start, end, activity_time.parse_time):
        if not event_has_geoip_region(event, region):
            continue
        host = event.get("host") or ""
        if not host:
            continue
        try:
            value, kind = classify_block_value(host, fatal=False)
        except ValueError:
            continue
        if value in active_values:
            continue
        row = candidates.setdefault(
            value,
            {
                "value": value,
                "kind": kind,
                "events": 0,
                "ports": {},
                "lastSeen": "",
                "sampleTarget": event.get("target") or host,
                "sourceEventId": int(event.get("id") or 0),
            },
        )
        row["events"] += 1
        if event.get("port"):
            port = str(event.get("port"))
            row["ports"][port] = row["ports"].get(port, 0) + 1
        if event.get("time", "") > row["lastSeen"]:
            row["lastSeen"] = event.get("time", "")
            row["sampleTarget"] = event.get("target") or host
            row["sourceEventId"] = int(event.get("id") or 0)
    return sorted(candidates.values(), key=lambda row: (row["events"], row["value"]), reverse=True)


def block_stats_rows(*, db_path: str | Path | None = None) -> list[dict]:
    connection = open_ready_database(db_path)
    try:
        return sqlite_blocklist.list_hit_stats(connection)
    finally:
        connection.close()


def apply_config(config: dict) -> Path:
    backup = save_config(config)
    try:
        subprocess.run([str(XRAY_BIN), "run", "-test", "-config", str(CONFIG_PATH)], check=True)
        subprocess.run(["systemctl", "restart", "xray"], check=True)
    except subprocess.CalledProcessError as exc:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        subprocess.run(["systemctl", "restart", "xray"], check=False)
        raise RuntimeError(f"New config failed. Restored backup: {backup}") from exc
    return backup


def reconcile_xray_config(
    *,
    removed_items: list[dict] | None = None,
    db_path: str | Path | None = None,
) -> Path | None:
    connection = open_ready_database(db_path)
    try:
        now_iso = activity_time.utc_stamp()
        known_items = sqlite_blocklist.list_blocks(connection)
        active_items = sqlite_blocklist.active_blocks(connection, now_iso)
    finally:
        connection.close()

    config = load_xray_config()
    changed = xray_blocklist.sync_blocklist_rules(
        config,
        active_items,
        known_items=known_items,
        removed_items=removed_items,
    )
    if not changed:
        return None
    return apply_config(config)
