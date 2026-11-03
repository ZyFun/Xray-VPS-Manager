"""Repository facade for client and managed connection state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients.payments import normalize_payment_type
from xray_vps_manager.core.errors import ConflictError
from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.core.paths import paths
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import cascades as sqlite_cascades
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.storage import SQLiteReadUnavailable


def db_clients(db: dict[str, Any]) -> dict[str, Any]:
    return db.setdefault("clients", {})


def db_managed_connections(db: dict[str, Any]) -> dict[str, Any]:
    return db.setdefault("connections", {})


def db_connections(db: dict[str, Any]) -> dict[str, Any]:
    """Compatibility alias for the legacy Reality-named helper."""
    return db_managed_connections(db)


def db_cascade_routes(db: dict[str, Any]) -> dict[str, Any]:
    return db.setdefault("cascadeRoutes", {})


def _normalize_client(entry):
    entry["paymentType"] = normalize_payment_type(entry.get("paymentType", "free"))
    entry.setdefault("selectedCascadeTag", "")
    for credential in client_credentials.normalize_entry_credentials(entry).values():
        default = "trojan" if credential["client"].get("password") else "vless"
        credential["protocol"] = str(credential.get("protocol") or default).strip().lower()
    return entry


def normalize_client_defaults(db: dict[str, Any]) -> dict[str, Any]:
    for entry in db_clients(db).values():
        if isinstance(entry, dict):
            _normalize_client(entry)
    return db


def get_client(conn, name):
    entry = sqlite_clients.get_client(conn, name)
    return _normalize_client(entry) if entry is not None else None


def list_clients(conn):
    return {name: _normalize_client(entry) for name, entry in sqlite_clients.list_clients(conn).items()}


def save_client(conn, name, entry):
    """Сохраняет запись и переданные credentials. Отсутствие ключа не означает удаление."""
    sqlite_clients.upsert_client(conn, name, _normalize_client(entry))


delete_client = sqlite_clients.delete_client
get_connection = sqlite_connections.get_connection
save_connection = sqlite_connections.upsert_connection
list_connections = sqlite_connections.list_connections
delete_connection = sqlite_connections.delete_connection
get_route = sqlite_cascades.get_route
save_route = sqlite_cascades.upsert_route
list_routes = sqlite_cascades.list_routes
delete_route = sqlite_cascades.delete_route


def get_credential(conn, name, tag):
    stored = sqlite_clients.get_client_credential(conn, name, tag)
    if stored is not None:
        return stored
    return list_credentials(conn, name).get(tag)


def list_credentials(conn, name):
    entry = get_client(conn, name)
    return entry["credentials"] if entry is not None else {}


def save_credential(conn, name, tag, credential):
    tag = client_credentials.credential_key(tag)
    if not tag:
        raise ValueError("Connection tag is required.")
    with database.transaction(conn):
        entry = get_client(conn, name)
        if entry is None:
            raise ValueError(f"Client not found: {name}")
        entry["credentials"][tag] = dict(credential, connection=tag)
        _normalize_client(entry)
        # В старой базе основной ключ может существовать только в полях клиента.
        stored = sqlite_clients.list_client_credentials(conn, name)
        entry["credentials"] = {
            key: item for key, item in entry["credentials"].items() if key == tag or key not in stored
        }
        sqlite_clients.upsert_client(conn, name, entry)


def delete_credential(conn, name, tag):
    with database.transaction(conn):
        entry = get_client(conn, name)
        if entry is None or tag not in entry["credentials"]:
            return False
        del entry["credentials"][tag]
        if not entry["credentials"]:
            raise ConflictError("Use delete_client to remove the last credential.")
        client_credentials.sync_legacy_fields(entry)
        del entry["credentials"]
        sqlite_clients.upsert_client(conn, name, entry)
        return sqlite_clients.delete_client_credential(conn, name, tag)


class _Snapshot(dict):
    """Исходные значения нужны, чтобы отличить изменение от устаревшей копии строки."""

    def __init__(self, records, *, original=None):
        super().__init__(records)
        self.original = deepcopy(records) if original is None else original

    def copy(self):
        return _Snapshot(self, original=self.original)


@dataclass(frozen=True)
class Deletions:
    clients: tuple[str, ...] = ()
    credentials: tuple[tuple[str, str], ...] = ()
    connections: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()

    def apply(self, conn):
        for name, tag in self.credentials:
            delete_credential(conn, name, tag)
        for name in self.clients:
            delete_client(conn, name)
        for tag in self.connections:
            delete_connection(conn, tag)
        for tag in self.routes:
            delete_route(conn, tag)


def load_db(*, db_path: str | Path | None = None) -> dict[str, Any]:
    target = database.database_path(db_path)
    if not database.database_file_exists(target):
        raise database.unavailable_database(target)
    with manager_lock(purpose="load clients", path=target.with_name(paths().manager_lock.name)):
        return load_db_sql(db_path=target)


def save_db(
    db: dict[str, Any],
    *,
    db_path: str | Path | None = None,
    deletions: Deletions | None = None,
) -> None:
    """Для загруженного снимка пишет изменения с проверкой исходных значений.

    Обычный словарь задаёт явные upsert переданных записей. Удаления в обоих
    случаях задаёт сценарий под той же manager.lock, где он загрузил данные.
    """
    write_db_to_sqlite_for_write(db, db_path=db_path, strict=True, deletions=deletions)


def load_db_sql(
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    target = database.database_path(db_path)
    try:
        with database.session(target) as conn, database.transaction(conn):
            return _Snapshot(load_db_from_sqlite(conn))
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite clients cannot be read: {exc}") from exc


def load_db_from_sqlite(connection) -> dict[str, Any]:
    return {
        "connections": list_connections(connection),
        "cascadeRoutes": list_routes(connection),
        "clients": list_clients(connection),
    }


def _write_records(conn, db, deletions):
    original = db.original if isinstance(db, _Snapshot) else None
    if deletions and original is not None and load_db_from_sqlite(conn) != original:
        raise ConflictError("Client database changed before deletion. Reload it and retry.")
    for section, get_record, save_record in (
        ("connections", get_connection, save_connection),
        ("cascadeRoutes", get_route, save_route),
        ("clients", get_client, save_client),
    ):
        records = db.get(section)
        if not isinstance(records, dict):
            continue
        for key, record in records.items():
            if not isinstance(record, dict):
                continue
            if original is not None:
                before = original[section].get(key)
                if record == before:
                    continue
                if get_record(conn, key) != before:
                    raise ConflictError("Client database record changed. Reload it and retry.")
            save_record(conn, str(key), record)
    if deletions:
        deletions.apply(conn)


def write_db_to_sqlite_for_write(
    db: dict[str, Any],
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
    deletions: Deletions | None = None,
) -> bool:
    target = database.database_path(db_path)
    if not database.database_file_exists(target):
        if strict:
            raise database.unavailable_database(target)
        return False
    try:
        with manager_lock(purpose="save clients", path=target.with_name(paths().manager_lock.name)):
            with database.write_session(target) as conn:
                _write_records(conn, db, deletions)
                if isinstance(db, _Snapshot):
                    committed = load_db_from_sqlite(conn)
            if isinstance(db, _Snapshot):
                db.update(committed)
                db.original = deepcopy(committed)
        return True
    except Exception:
        if strict:
            raise
        return False


def restore_after_update(committed, previous):
    """Отменяет именно завершённое обновление при ошибке последующего шага Caddy."""
    undo = _Snapshot(dict(previous), original=committed)
    deletions = Deletions(
        connections=tuple(committed["connections"].keys() - previous["connections"].keys()),
        routes=tuple(committed["cascadeRoutes"].keys() - previous["cascadeRoutes"].keys()),
    )
    save_db(undo, deletions=deletions)
