"""SQLite schema definition for manager state."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from pathlib import Path

from xray_vps_manager.core.paths import MANAGER_DB_PATH

CURRENT_SCHEMA_VERSION = 5


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    requires_backup: bool = False


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_manager_schema",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS manager_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS reality_connections (
                tag TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
                sni TEXT NOT NULL,
                dest TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                public_key TEXT,
                short_id TEXT,
                created_at TEXT,
                extra_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS clients (
                name TEXT PRIMARY KEY,
                uuid TEXT NOT NULL UNIQUE,
                email TEXT,
                connection_tag TEXT,
                created_at TEXT,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                disabled_reason TEXT,
                disabled_at TEXT,
                expires_at TEXT,
                access_days INTEGER,
                expired_at TEXT,
                payment_type TEXT NOT NULL DEFAULT 'free' CHECK (payment_type IN ('free', 'paid')),
                xray_client_json TEXT NOT NULL DEFAULT '{}',
                extra_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (connection_tag)
                    REFERENCES reality_connections(tag)
                    ON UPDATE CASCADE
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS client_traffic_limits (
                client_name TEXT PRIMARY KEY,
                period TEXT NOT NULL CHECK (period IN ('daily', 'monthly')),
                limit_bytes INTEGER NOT NULL CHECK (limit_bytes >= 0),
                set_at TEXT,
                FOREIGN KEY (client_name)
                    REFERENCES clients(name)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS client_traffic_limit_state (
                client_name TEXT PRIMARY KEY,
                exceeded_at TEXT,
                exceeded_period TEXT,
                exceeded_bytes INTEGER NOT NULL DEFAULT 0 CHECK (exceeded_bytes >= 0),
                reset_at TEXT,
                FOREIGN KEY (client_name)
                    REFERENCES clients(name)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS traffic_totals (
                client_name TEXT PRIMARY KEY,
                email TEXT,
                incoming_bytes INTEGER NOT NULL DEFAULT 0 CHECK (incoming_bytes >= 0),
                outgoing_bytes INTEGER NOT NULL DEFAULT 0 CHECK (outgoing_bytes >= 0),
                last_runtime_uplink INTEGER,
                last_runtime_downlink INTEGER,
                last_online_at TEXT,
                last_online_source TEXT,
                last_accepted_at TEXT,
                updated_at TEXT,
                FOREIGN KEY (client_name)
                    REFERENCES clients(name)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS traffic_history (
                client_name TEXT NOT NULL,
                bucket_date TEXT NOT NULL,
                bucket_hour INTEGER NOT NULL CHECK (bucket_hour BETWEEN 0 AND 23),
                incoming_bytes INTEGER NOT NULL DEFAULT 0 CHECK (incoming_bytes >= 0),
                outgoing_bytes INTEGER NOT NULL DEFAULT 0 CHECK (outgoing_bytes >= 0),
                PRIMARY KEY (client_name, bucket_date, bucket_hour),
                FOREIGN KEY (client_name)
                    REFERENCES clients(name)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS file_offsets (
                name TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                inode INTEGER,
                offset INTEGER NOT NULL DEFAULT 0 CHECK (offset >= 0),
                updated_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS activity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                client_name TEXT NOT NULL,
                email TEXT,
                connection_tag TEXT,
                source TEXT,
                status TEXT,
                network TEXT,
                target TEXT,
                host TEXT,
                port INTEGER,
                inbound TEXT,
                outbound TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (client_name)
                    REFERENCES clients(name)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS activity_event_risks (
                event_id INTEGER NOT NULL,
                risk TEXT NOT NULL,
                PRIMARY KEY (event_id, risk),
                FOREIGN KEY (event_id)
                    REFERENCES activity_events(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS activity_exceptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK (kind IN ('domain', 'ip', 'cidr', 'mask')),
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS telegram_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS telegram_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                chat_label TEXT,
                client_name TEXT,
                client_uuid TEXT NOT NULL,
                connection_tag TEXT,
                link_signature_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                created_at TEXT,
                updated_at TEXT,
                UNIQUE (chat_id, client_uuid),
                FOREIGN KEY (client_name)
                    REFERENCES clients(name)
                    ON UPDATE CASCADE
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS telegram_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS payment_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_clients_connection ON clients(connection_tag)",
            "CREATE INDEX IF NOT EXISTS idx_clients_enabled ON clients(enabled)",
            "CREATE INDEX IF NOT EXISTS idx_clients_expires_at ON clients(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_clients_payment_type ON clients(payment_type)",
            "CREATE INDEX IF NOT EXISTS idx_traffic_history_date ON traffic_history(bucket_date)",
            "CREATE INDEX IF NOT EXISTS idx_traffic_history_client_date ON traffic_history(client_name, bucket_date)",
            "CREATE INDEX IF NOT EXISTS idx_activity_events_time ON activity_events(event_time)",
            "CREATE INDEX IF NOT EXISTS idx_activity_events_client_time ON activity_events(client_name, event_time)",
            "CREATE INDEX IF NOT EXISTS idx_activity_events_host ON activity_events(host)",
            "CREATE INDEX IF NOT EXISTS idx_activity_events_outbound ON activity_events(outbound)",
            "CREATE INDEX IF NOT EXISTS idx_activity_events_port ON activity_events(port)",
            "CREATE INDEX IF NOT EXISTS idx_activity_event_risks_risk ON activity_event_risks(risk)",
            "CREATE INDEX IF NOT EXISTS idx_activity_exceptions_kind ON activity_exceptions(kind)",
            "CREATE INDEX IF NOT EXISTS idx_telegram_subscriptions_chat ON telegram_subscriptions(chat_id)",
            "CREATE INDEX IF NOT EXISTS idx_telegram_subscriptions_client ON telegram_subscriptions(client_name)",
            "CREATE INDEX IF NOT EXISTS idx_telegram_subscriptions_uuid ON telegram_subscriptions(client_uuid)",
            "CREATE INDEX IF NOT EXISTS idx_telegram_subscriptions_enabled ON telegram_subscriptions(enabled)",
        ),
    ),
    Migration(
        version=2,
        name="cascade_client_routes",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS cascade_routes (
                tag TEXT PRIMARY KEY,
                country TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                created_at TEXT,
                updated_at TEXT,
                extra_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "ALTER TABLE clients ADD COLUMN selected_cascade_tag TEXT",
            "CREATE INDEX IF NOT EXISTS idx_clients_selected_cascade ON clients(selected_cascade_tag)",
            "CREATE INDEX IF NOT EXISTS idx_cascade_routes_country ON cascade_routes(country)",
        ),
    ),
    Migration(
        version=3,
        name="telegram_activity_subscriptions",
        statements=(
            """
            ALTER TABLE telegram_subscriptions
            ADD COLUMN activity_notifications_enabled INTEGER NOT NULL DEFAULT 0 CHECK (activity_notifications_enabled IN (0, 1))
            """,
            "CREATE INDEX IF NOT EXISTS idx_telegram_subscriptions_activity ON telegram_subscriptions(activity_notifications_enabled)",
        ),
    ),
    Migration(
        version=4,
        name="activity_global_blocklist",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS activity_blocklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK (kind IN ('domain', 'ip', 'cidr', 'mask')),
                source_client_name TEXT,
                source_event_id INTEGER,
                source TEXT NOT NULL DEFAULT 'manual',
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                expires_at TEXT,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                FOREIGN KEY (source_client_name)
                    REFERENCES clients(name)
                    ON UPDATE CASCADE
                    ON DELETE SET NULL,
                FOREIGN KEY (source_event_id)
                    REFERENCES activity_events(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS activity_blocklist_hits (
                blocklist_id INTEGER NOT NULL,
                client_name TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0 CHECK (hits >= 0),
                first_seen_at TEXT,
                last_seen_at TEXT,
                PRIMARY KEY (blocklist_id, client_name),
                FOREIGN KEY (blocklist_id)
                    REFERENCES activity_blocklist(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (client_name)
                    REFERENCES clients(name)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_activity_blocklist_client ON activity_blocklist(source_client_name)",
            "CREATE INDEX IF NOT EXISTS idx_activity_blocklist_value ON activity_blocklist(value)",
            "CREATE INDEX IF NOT EXISTS idx_activity_blocklist_expires ON activity_blocklist(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_activity_blocklist_enabled ON activity_blocklist(enabled)",
            "CREATE INDEX IF NOT EXISTS idx_activity_blocklist_hits_client ON activity_blocklist_hits(client_name)",
        ),
    ),
    Migration(
        version=5,
        name="client_credentials",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS client_credentials (
                client_name TEXT NOT NULL,
                connection_tag TEXT NOT NULL,
                credential_uuid TEXT,
                protocol TEXT NOT NULL DEFAULT 'vless' CHECK (protocol IN ('vless', 'trojan')),
                security TEXT NOT NULL DEFAULT '',
                transport TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                created_at TEXT,
                xray_client_json TEXT NOT NULL DEFAULT '{}',
                link_metadata_json TEXT NOT NULL DEFAULT '{}',
                extra_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (client_name, connection_tag),
                FOREIGN KEY (client_name)
                    REFERENCES clients(name)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE,
                FOREIGN KEY (connection_tag)
                    REFERENCES reality_connections(tag)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE
            )
            """,
            """
            INSERT OR IGNORE INTO client_credentials(
                client_name, connection_tag, credential_uuid, protocol, enabled, created_at, xray_client_json
            )
            SELECT
                name,
                connection_tag,
                uuid,
                CASE WHEN instr(xray_client_json, '"password"') > 0 THEN 'trojan' ELSE 'vless' END,
                enabled,
                created_at,
                xray_client_json
            FROM clients
            WHERE connection_tag IS NOT NULL AND connection_tag != ''
            """,
            """
            CREATE TABLE IF NOT EXISTS credential_traffic_totals (
                client_name TEXT NOT NULL,
                connection_tag TEXT NOT NULL,
                email TEXT,
                incoming_bytes INTEGER NOT NULL DEFAULT 0 CHECK (incoming_bytes >= 0),
                outgoing_bytes INTEGER NOT NULL DEFAULT 0 CHECK (outgoing_bytes >= 0),
                last_runtime_uplink INTEGER,
                last_runtime_downlink INTEGER,
                last_online_at TEXT,
                last_online_source TEXT,
                last_accepted_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (client_name, connection_tag),
                FOREIGN KEY (client_name, connection_tag)
                    REFERENCES client_credentials(client_name, connection_tag)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS credential_traffic_history (
                client_name TEXT NOT NULL,
                connection_tag TEXT NOT NULL,
                bucket_date TEXT NOT NULL,
                bucket_hour INTEGER NOT NULL CHECK (bucket_hour BETWEEN 0 AND 23),
                incoming_bytes INTEGER NOT NULL DEFAULT 0 CHECK (incoming_bytes >= 0),
                outgoing_bytes INTEGER NOT NULL DEFAULT 0 CHECK (outgoing_bytes >= 0),
                PRIMARY KEY (client_name, connection_tag, bucket_date, bucket_hour),
                FOREIGN KEY (client_name, connection_tag)
                    REFERENCES client_credentials(client_name, connection_tag)
                    ON UPDATE CASCADE
                    ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_client_credentials_client ON client_credentials(client_name)",
            "CREATE INDEX IF NOT EXISTS idx_client_credentials_connection ON client_credentials(connection_tag)",
            "CREATE INDEX IF NOT EXISTS idx_client_credentials_uuid ON client_credentials(credential_uuid)",
            "CREATE INDEX IF NOT EXISTS idx_credential_traffic_history_client_date ON credential_traffic_history(client_name, connection_tag, bucket_date)",
        ),
    ),
)


def connect(path: Path = MANAGER_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    configure_connection(connection)
    return connection


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")


def schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0] if row else 0)


def pending_migrations(connection: sqlite3.Connection) -> tuple[Migration, ...]:
    current = schema_version(connection)
    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"SQLite schema version {current} is newer than supported version {CURRENT_SCHEMA_VERSION}."
        )
    return tuple(migration for migration in MIGRATIONS if migration.version > current)


def pending_migrations_require_backup(migrations: tuple[Migration, ...]) -> bool:
    return any(migration.requires_backup for migration in migrations)


def ensure_schema(connection: sqlite3.Connection) -> None:
    configure_connection(connection)
    for migration in pending_migrations(connection):
        with connection:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT OR REPLACE INTO schema_migrations(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def index_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}
