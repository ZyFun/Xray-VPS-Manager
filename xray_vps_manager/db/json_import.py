"""Import existing JSON/JSONL manager state into SQLite.

The importer is intentionally one-way and non-destructive for the current JSON
files. It can clear and repopulate SQLite tables, but it never deletes or edits
the JSON source files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import sqlite3
from pathlib import Path
from typing import Any

from xray_vps_manager.core.paths import (
    ACTIVITY_DIR,
    ACTIVITY_EXCEPTIONS_PATH,
    ACTIVITY_PATH,
    CLIENT_DB_PATH,
    CLIENT_LOG_DIR,
    MANAGER_DB_PATH,
    TELEGRAM_DB_PATH,
    TRAFFIC_PATH,
)
from xray_vps_manager.activity.exceptions import classify_exception_value
from xray_vps_manager.db import database, schema
from xray_vps_manager.db.repositories import activity, clients, connections, settings, telegram, traffic
from xray_vps_manager.db.repositories.base import encode_json


@dataclass(frozen=True)
class JsonStatePaths:
    clients: Path = CLIENT_DB_PATH
    traffic: Path = TRAFFIC_PATH
    activity: Path = ACTIVITY_PATH
    activity_exceptions: Path = ACTIVITY_EXCEPTIONS_PATH
    activity_dir: Path = ACTIVITY_DIR
    client_activity_dir: Path = CLIENT_LOG_DIR
    telegram: Path = TELEGRAM_DB_PATH


@dataclass
class ImportSummary:
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def increment(self, key: str, value: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + value

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def as_dict(self) -> dict[str, Any]:
        return {"counts": dict(self.counts), "warnings": list(self.warnings)}


STATE_TABLES = (
    "activity_event_risks",
    "activity_events",
    "activity_exceptions",
    "telegram_subscriptions",
    "telegram_state",
    "telegram_settings",
    "payment_settings",
    "traffic_history",
    "traffic_totals",
    "client_traffic_limit_state",
    "client_traffic_limits",
    "clients",
    "reality_connections",
    "file_offsets",
    "manager_metadata",
)


def import_json_files(
    paths: JsonStatePaths | None = None,
    db_path: str | Path | None = None,
    *,
    replace: bool = True,
) -> ImportSummary:
    connection = database.open_database(db_path or MANAGER_DB_PATH)
    try:
        return import_json_state(connection, paths or JsonStatePaths(), replace=replace)
    finally:
        connection.close()


def import_json_state(
    connection: sqlite3.Connection,
    paths: JsonStatePaths | None = None,
    *,
    replace: bool = True,
) -> ImportSummary:
    schema.ensure_schema(connection)
    paths = paths or JsonStatePaths()
    summary = ImportSummary()
    if replace:
        clear_imported_state(connection)

    client_db = read_json(paths.clients, {"clients": {}, "connections": {}}, summary, "clients.json")
    import_clients(connection, client_db, summary)

    traffic_db = read_json(paths.traffic, {"clients": {}}, summary, "traffic.json")
    import_traffic(connection, traffic_db, summary)

    activity_db = read_json(paths.activity, {}, summary, "activity.json")
    import_activity_metadata(connection, activity_db, summary)

    exception_db = read_json(paths.activity_exceptions, {"items": []}, summary, "activity-exceptions.json")
    import_activity_exceptions(connection, exception_db, summary)

    import_activity_events(connection, paths.client_activity_dir, summary)

    telegram_db = read_json(paths.telegram, {}, summary, "telegram-bot.json")
    import_telegram(connection, telegram_db, summary)

    validate_import(connection, summary)
    settings.set_metadata(connection, "jsonImport.completed", "true")
    return summary


def clear_imported_state(connection: sqlite3.Connection) -> None:
    with database.transaction(connection):
        for table in STATE_TABLES:
            connection.execute(f"DELETE FROM {table}")


def read_json(path: Path, default: Any, summary: ImportSummary, label: str) -> Any:
    if not path.exists():
        summary.warn(f"{label} not found: {path}")
        return default
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        summary.warn(f"{label} is not valid JSON: {exc}")
        return default
    return data if data is not None else default


def import_clients(connection: sqlite3.Connection, db: dict[str, Any], summary: ImportSummary) -> None:
    if not isinstance(db, dict):
        summary.warn("clients.json root is not an object")
        return

    connection_records = db.get("connections", {})
    if not isinstance(connection_records, dict):
        connection_records = {}
        summary.warn("clients.json field connections is not an object")
    for tag, record in connection_records.items():
        if not isinstance(record, dict):
            summary.warn(f"connection {tag} skipped: record is not an object")
            continue
        connections.upsert_connection(connection, str(tag), record)
        summary.increment("connections")

    client_records = db.get("clients", {})
    if not isinstance(client_records, dict):
        summary.warn("clients.json field clients is not an object")
        return

    known_connections = set(connections.list_connections(connection))
    for name, entry in client_records.items():
        if not isinstance(entry, dict):
            summary.warn(f"client {name} skipped: record is not an object")
            continue
        normalized = dict(entry)
        connection_tag = normalized.get("connection") or ""
        if connection_tag and connection_tag not in known_connections:
            summary.warn(f"client {name} references missing connection {connection_tag}; imported without connection")
            normalized.pop("connection", None)
        try:
            clients.upsert_client(connection, str(name), normalized)
        except ValueError as exc:
            summary.warn(f"client {name} skipped: {exc}")
            continue
        summary.increment("clients")


def import_traffic(connection: sqlite3.Connection, db: dict[str, Any], summary: ImportSummary) -> None:
    if not isinstance(db, dict):
        summary.warn("traffic.json root is not an object")
        return
    import_file_offset(connection, "traffic-access-log", db.get("accessLog"), summary)
    set_metadata_json(connection, "traffic.sourceMetadata", {
        "version": db.get("version"),
        "historyRetentionMonths": db.get("historyRetentionMonths"),
        "updated": db.get("updated"),
    })
    client_names = set(clients.list_clients(connection))
    entries = db.get("clients", {})
    if not isinstance(entries, dict):
        summary.warn("traffic.json field clients is not an object")
        return
    for name, entry in entries.items():
        if name not in client_names:
            summary.increment("skipped_traffic_clients")
            summary.warn(f"traffic client {name} skipped: client is absent from clients.json")
            continue
        if not isinstance(entry, dict):
            summary.warn(f"traffic client {name} skipped: record is not an object")
            continue
        traffic.upsert_traffic_entry(connection, str(name), entry)
        summary.increment("traffic_clients")
        summary.increment("traffic_history_buckets", count_history_buckets(entry.get("history")))


def import_activity_metadata(connection: sqlite3.Connection, db: dict[str, Any], summary: ImportSummary) -> None:
    if not isinstance(db, dict):
        summary.warn("activity.json root is not an object")
        return
    import_file_offset(connection, "activity-access-log", db.get("accessLog"), summary)
    set_metadata_json(connection, "activity.sourceMetadata", {
        "version": db.get("version"),
        "enabled": db.get("enabled"),
        "retentionDays": db.get("retentionDays"),
        "lastSync": db.get("lastSync"),
        "lastPrune": db.get("lastPrune"),
    })
    if isinstance(db.get("clients"), dict):
        set_metadata_json(connection, "activity.summary", db.get("clients"))
        summary.increment("activity_summary_clients", len(db["clients"]))


def import_activity_exceptions(connection: sqlite3.Connection, db: dict[str, Any], summary: ImportSummary) -> None:
    if not isinstance(db, dict):
        summary.warn("activity-exceptions.json root is not an object")
        return
    items = db.get("items", [])
    if not isinstance(items, list):
        summary.warn("activity-exceptions.json field items is not a list")
        return
    for item in items:
        if isinstance(item, str):
            item = {"value": item, "source": "legacy", "createdAt": ""}
        if not isinstance(item, dict):
            summary.warn("activity exception skipped: item is not an object")
            continue
        value = item.get("value")
        if not value:
            summary.warn("activity exception skipped: value is empty")
            continue
        try:
            normalized_value, kind = classify_exception_value(str(value), fatal=True)
        except ValueError as exc:
            summary.warn(f"activity exception {value} skipped: {exc}")
            continue
        item = dict(item)
        item["value"] = normalized_value
        item["kind"] = item.get("kind") or kind
        activity.upsert_exception(connection, item)
        summary.increment("activity_exceptions")


def import_activity_events(connection: sqlite3.Connection, client_log_dir: Path, summary: ImportSummary) -> None:
    if not client_log_dir.exists():
        summary.warn(f"activity client log directory not found: {client_log_dir}")
        return
    client_names = set(clients.list_clients(connection))
    for path in sorted(client_log_dir.glob("*.jsonl")):
        with database.transaction(connection):
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        summary.warn(f"activity event skipped: invalid JSON in {path.name}:{index}")
                        continue
                    if not isinstance(event, dict):
                        summary.warn(f"activity event skipped: non-object in {path.name}:{index}")
                        continue
                    name = str(event.get("client") or event.get("client_name") or "")
                    if name not in client_names:
                        summary.increment("skipped_activity_events")
                        summary.warn(f"activity event skipped: client {name or '-'} is absent from clients.json")
                        continue
                    activity.add_event(connection, event)
                    summary.increment("activity_events")


def import_telegram(connection: sqlite3.Connection, db: dict[str, Any], summary: ImportSummary) -> None:
    if not isinstance(db, dict):
        summary.warn("telegram-bot.json root is not an object")
        return
    for key in ("version", "enabled", "token", "botName", "chatId", "chatLabel", "routeMode"):
        if key in db:
            telegram.set_setting(connection, key, str(db.get(key) or ""))
            summary.increment("telegram_settings")

    for key in ("paymentAmount", "paymentTotalAmount", "paymentCurrency", "paymentRoundingMode", "paymentRoundingStep"):
        if key in db:
            settings.set_payment_setting(connection, key, str(db.get(key) or ""))
            summary.increment("payment_settings")

    for key in ("geoipState", "clientSubscriptionState", "dailySummaryState", "adminState"):
        value = db.get(key)
        if isinstance(value, dict):
            telegram.set_state(connection, key, value)
            summary.increment("telegram_state")

    import_telegram_subscriptions(connection, db.get("clientSubscriptions", {}), summary)


def import_telegram_subscriptions(
    connection: sqlite3.Connection,
    subscriptions: Any,
    summary: ImportSummary,
) -> None:
    if not isinstance(subscriptions, dict):
        summary.warn("telegram-bot.json field clientSubscriptions is not an object")
        return
    client_entries = clients.list_clients(connection)
    for chat_id, subscription in subscriptions.items():
        if not isinstance(subscription, dict):
            summary.warn(f"telegram subscription {chat_id} skipped: record is not an object")
            continue
        client_name = subscription.get("client") or subscription.get("clientName") or ""
        client_entry = client_entries.get(client_name)
        client_uuid = subscription.get("clientId") or subscription.get("clientUuid") or ""
        if not client_uuid and client_entry:
            client_uuid = client_entry.get("id") or ""
        if not client_uuid:
            summary.increment("skipped_telegram_subscriptions")
            summary.warn(f"telegram subscription {chat_id} skipped: client UUID is empty")
            continue
        telegram.upsert_subscription(
            connection,
            {
                "chatId": str(chat_id),
                "chatLabel": subscription.get("chatLabel") or "",
                "clientName": client_name if client_name in client_entries else "",
                "clientUuid": client_uuid,
                "connection": subscription.get("connection") or "",
                "linkSignature": {"linkHash": subscription.get("linkHash") or ""},
                "enabled": subscription.get("enabled") is not False,
                "createdAt": subscription.get("subscribedAt") or subscription.get("createdAt") or "",
                "updatedAt": subscription.get("updatedAt") or subscription.get("subscribedAt") or "",
            },
        )
        summary.increment("telegram_subscriptions")


def import_file_offset(
    connection: sqlite3.Connection,
    name: str,
    state: Any,
    summary: ImportSummary,
) -> None:
    if not isinstance(state, dict):
        return
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO file_offsets(name, path, inode, offset, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                path = excluded.path,
                inode = excluded.inode,
                offset = excluded.offset,
                updated_at = excluded.updated_at
            """,
            (
                name,
                state.get("path") or "",
                state.get("inode"),
                int(state.get("offset") or 0),
                state.get("updated") or state.get("updated_at") or "",
            ),
        )
    summary.increment("file_offsets")


def set_metadata_json(connection: sqlite3.Connection, key: str, value: Any) -> None:
    settings.set_metadata(connection, key, encode_json(value))


def count_history_buckets(history: Any) -> int:
    if not isinstance(history, dict):
        return 0
    total = 0
    for hours in history.values():
        if isinstance(hours, dict):
            total += sum(1 for bucket in hours.values() if isinstance(bucket, dict))
    return total


def validate_import(connection: sqlite3.Connection, summary: ImportSummary) -> None:
    expected_to_table = {
        "connections": "reality_connections",
        "clients": "clients",
        "traffic_clients": "traffic_totals",
        "activity_events": "activity_events",
        "activity_exceptions": "activity_exceptions",
        "telegram_subscriptions": "telegram_subscriptions",
    }
    for expected_key, table in expected_to_table.items():
        expected = summary.counts.get(expected_key, 0)
        actual = table_count(connection, table)
        if expected != actual:
            summary.warn(f"validation mismatch for {table}: expected {expected}, got {actual}")


def table_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
