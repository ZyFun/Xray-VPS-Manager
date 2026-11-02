"""Сценарии создания, миграции и диагностики SQLite."""

from contextlib import closing
from dataclasses import dataclass
import json
import os
import shutil
from pathlib import Path
from collections.abc import Mapping

from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.core.errors import StateError, ValidationError
from xray_vps_manager.core.fs import replace_file
from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.core.paths import paths
from xray_vps_manager.db import database, schema
from xray_vps_manager.db.repositories import clients, connections, settings
from xray_vps_manager.db.storage import SQLITE_READ_READY_KEY, sqlite_read_ready
from xray_vps_manager.xray.config import load_config


COUNT_TABLES = {
    "connections": "reality_connections",
    "clients": "clients",
    "traffic": "traffic_totals",
    "activity_events": "activity_events",
    "activity_alert_events": "activity_alert_events",
    "activity_client_counters": "activity_client_counters",
    "activity_exceptions": "activity_exceptions",
    "activity_blocklist": "activity_blocklist",
    "activity_blocklist_hits": "activity_blocklist_hits",
    "xray_error_events": "xray_error_events",
    "telegram_subscriptions": "telegram_subscriptions",
}


@dataclass(frozen=True)
class DatabaseStatus:
    version: int
    quick_check: str
    ready: bool
    counts: dict[str, int]


@database.report_errors()
def status() -> DatabaseStatus | None:
    if not paths().manager_db.exists():
        return None
    with closing(database.connect()) as connection:
        return DatabaseStatus(
            schema.schema_version(connection), database.quick_check(connection), sqlite_read_ready(connection),
            {label: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
             for label, table in COUNT_TABLES.items()},
        )


@database.report_errors()
def migrate() -> None:
    database.ensure_initialized(force=True)


def _install_records(config: dict, env: Mapping[str, str]) -> dict:
    client_name = env["INSTALL_CLIENT_NAME"]
    created = env["INSTALL_CREATED"]
    initial_protocol = env["INSTALL_INITIAL_PROTOCOL"]
    transport = env["INSTALL_REALITY_TRANSPORT"]
    db = {"connections": {}, "clients": {}}

    if initial_protocol not in {"vless", "trojan", "both"}:
        raise ValidationError("Invalid INSTALL_INITIAL_PROTOCOL.")

    if initial_protocol in {"vless", "both"}:
        connection_record = {
            "tag": "vless-reality",
            "name": "default",
            "created": created,
            "port": int(env["INSTALL_PORT"]),
            "sni": env["INSTALL_REALITY_SNI"],
            "dest": env["INSTALL_REALITY_DEST"],
            "fingerprint": env["INSTALL_FINGERPRINT"],
            "publicKey": env["INSTALL_PUBLIC_KEY"],
            "shortId": env["INSTALL_SHORT_ID"],
            "transport": transport,
        }
        if transport == "grpc":
            connection_record["grpcServiceName"] = env["INSTALL_GRPC_SERVICE_NAME"]
        elif transport == "xhttp":
            connection_record["xhttpPath"] = env["INSTALL_XHTTP_PATH"]
            connection_record["xhttpMode"] = env["INSTALL_XHTTP_MODE"]
        db["connections"]["vless-reality"] = connection_record

    if initial_protocol in {"trojan", "both"}:
        db["connections"]["trojan-tls"] = {
            "tag": "trojan-tls",
            "name": env["INSTALL_TROJAN_CONNECTION_NAME"],
            "created": created,
            "protocol": "trojan",
            "security": "tls",
            "transport": "ws",
            "port": 443,
            "publicPort": 443,
            "localPort": int(env["INSTALL_TROJAN_LOCAL_PORT"]),
            "publicHost": env["INSTALL_TROJAN_DOMAIN"],
            "sni": env["INSTALL_TROJAN_DOMAIN"],
            "dest": "",
            "fingerprint": env["INSTALL_FINGERPRINT"],
            "publicKey": "",
            "shortId": "",
            "caddy": True,
            "wsPath": env["INSTALL_TROJAN_WS_PATH"],
            "tlsMinVersion": env["INSTALL_TROJAN_TLS_MIN_VERSION"],
            "tlsMaxVersion": env["INSTALL_TROJAN_TLS_MAX_VERSION"],
        }

    uuids = iter((env["INSTALL_UUID"], env["INSTALL_TROJAN_UUID"]))
    for tag in tuple(db["connections"]):
        client_crud.add_client(
            config, db, client_name, access_days=None, connection_tag=tag,
            uuid_factory=lambda: next(uuids), password_factory=lambda: env["INSTALL_TROJAN_PASSWORD"],
        )
    return db


def _prepare_config(path: Path) -> None:
    shutil.chown(path, user="root", group="xray")
    os.chmod(path, 0o640)


@database.report_errors()
def initialize(*, from_install: bool = False) -> None:
    if not from_install:
        with closing(database.initialize()) as connection, database.transaction(connection, immediate=True):
            settings.set_metadata(connection, SQLITE_READ_READY_KEY, "true")
        return

    with manager_lock(purpose="sqlite init --from-install"):
        config_path = paths().config
        try:
            config = load_config(config_path)
            records = _install_records(config, os.environ)
        except KeyError as exc:
            raise ValidationError(f"Missing install parameter: {exc}") from exc
        except (ValueError, OSError) as exc:
            raise ValidationError(f"Cannot prepare initial Xray configuration: {exc}") from exc

        with closing(database.initialize()) as connection:
            with database.transaction(connection, immediate=True):
                if (settings.get_metadata(connection, SQLITE_READ_READY_KEY)
                        or clients.list_clients(connection) or connections.list_connections(connection)):
                    raise StateError("Installation requires an empty manager database.")
                for tag, record in records["connections"].items():
                    connections.upsert_connection(connection, tag, record)
                for name, entry in records["clients"].items():
                    client_repository.save_client(connection, name, entry)
                settings.set_metadata(connection, SQLITE_READ_READY_KEY, "true")
            try:
                data = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode()
                replace_file(config_path, data, prepare=_prepare_config)
            except (OSError, LookupError) as exc:
                with database.transaction(connection, immediate=True):
                    for name in records["clients"]:
                        clients.delete_client(connection, name)
                    for tag in records["connections"]:
                        connections.delete_connection(connection, tag)
                    settings.delete_metadata(connection, SQLITE_READ_READY_KEY)
                raise StateError(f"Cannot save initial Xray configuration: {exc}") from exc
