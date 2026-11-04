"""Traffic storage helpers backed by SQLite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import traffic as sqlite_traffic
from xray_vps_manager.db.storage import SQLiteReadUnavailable
from xray_vps_manager.traffic.history import HistoryChanges


def default_db() -> dict[str, Any]:
    return {"clients": {}, "credentials": {}}


def load_traffic_db(*, db_path: str | Path | None = None) -> dict[str, Any]:
    return load_traffic_db_for_read(db_path=db_path)


def load_traffic_db_for_read(
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    try:
        with database.session(database.database_path(db_path), read_snapshot=True) as connection:
            return load_traffic_db_from_sqlite(connection)
    except SQLiteReadUnavailable:
        raise
    except Exception as exc:
        raise SQLiteReadUnavailable(f"SQLite traffic cannot be read: {exc}") from exc


def load_traffic_db_from_sqlite(connection) -> dict[str, Any]:
    db = {
        "clients": sqlite_traffic.list_traffic_entries(connection),
        "credentials": sqlite_traffic.list_credential_traffic_entries(connection),
    }
    access_log_state = sqlite_traffic.get_access_log_state(connection)
    if access_log_state:
        db["accessLog"] = access_log_state
    return db


def save_traffic_db(
    db: dict[str, Any],
    *,
    db_path: str | Path | None = None,
    history_changes: HistoryChanges | None = None,
) -> None:
    write_traffic_db_to_sqlite_for_write(db, db_path=db_path, strict=True, history_changes=history_changes)


def traffic_clients(db: dict | None) -> dict:
    if not isinstance(db, dict):
        return {}
    clients = db.get("clients", {})
    return clients if isinstance(clients, dict) else {}


def traffic_entry(db: dict | None, name: str) -> dict:
    entry = traffic_clients(db).get(name, {})
    return entry if isinstance(entry, dict) else {}


def traffic_credentials(db: dict | None) -> dict:
    if not isinstance(db, dict):
        return {}
    credentials = db.get("credentials", {})
    return credentials if isinstance(credentials, dict) else {}


def credential_traffic_entries(db: dict | None, name: str) -> dict:
    entries = traffic_credentials(db).get(name, {})
    return entries if isinstance(entries, dict) else {}


def credential_traffic_entry(db: dict | None, name: str, connection_tag: str) -> dict:
    entry = credential_traffic_entries(db, name).get(connection_tag, {})
    return entry if isinstance(entry, dict) else {}


def ensure_entry(entries: dict, name: str, email: str) -> dict:
    entry = entries.setdefault(
        name,
        {
            "email": email,
            "incoming": 0,
            "outgoing": 0,
            "last": {},
            "history": {},
        },
    )
    entry["email"] = email
    entry.setdefault("history", {})
    return entry


def ensure_credential_entry(entries: dict, name: str, connection_tag: str, email: str) -> dict:
    client_credentials = entries.setdefault(name, {})
    if not isinstance(client_credentials, dict):
        client_credentials = {}
        entries[name] = client_credentials
    entry = client_credentials.setdefault(
        connection_tag,
        {
            "email": email,
            "incoming": 0,
            "outgoing": 0,
            "last": {},
            "history": {},
        },
    )
    entry["email"] = email
    entry.setdefault("history", {})
    return entry


def remove_traffic_clients(
    names: list[str] | tuple[str, ...] | set[str],
    *,
    db_path: str | Path | None = None,
) -> bool:
    return remove_traffic_clients_from_sqlite_for_write(names, db_path=db_path, strict=True)


def write_traffic_db_to_sqlite_for_write(
    db: dict[str, Any],
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
    history_changes: HistoryChanges | None = None,
) -> bool:
    """Сохраняет итоги и историю трафика одной транзакцией.

    Без `history_changes` непустая история записи заменяет её прежние бакеты точечными операциями.
    С `history_changes` (прогон `xray-traffic-sync`, VIREI-247) записи, которые уже есть в базе,
    получают только дельты изменённых бакетов и общую обрезку старше `cutoff`; целиком пишется
    история только новых записей (например, первой credential, скопировавшей историю клиента).
    """
    entries = traffic_clients(db)
    credential_entries = traffic_credentials(db)
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            mirrorable_clients = {
                name for name, entry in entries.items()
                if isinstance(entry, dict) and sqlite_clients.client_exists(connection, name)
            }
            stored_clients: set[str] = set()
            stored_credentials: set[tuple[str, str]] = set()
            if history_changes is not None:
                # Обрезка до дельт — тот же порядок, что в памяти: prune_history до восстановления пропусков.
                sqlite_traffic.delete_history_before(connection, history_changes.cutoff.isoformat())
                stored_clients = sqlite_traffic.traffic_client_names(connection)
                stored_credentials = sqlite_traffic.credential_traffic_keys(connection)
            for name in sorted(mirrorable_clients):
                if name in stored_clients:
                    sqlite_traffic.upsert_traffic_totals(connection, name, entries[name])
                else:
                    sqlite_traffic.upsert_traffic_entry(connection, name, entries[name])
            written_credentials: set[tuple[str, str]] = set()
            if "credentials" in db:
                for name, credentials in credential_entries.items():
                    if not isinstance(credentials, dict):
                        continue
                    known = sqlite_clients.list_client_credentials(connection, name)
                    for connection_tag, entry in credentials.items():
                        if isinstance(entry, dict) and str(connection_tag) in known:
                            key = (str(name), str(connection_tag))
                            if key in stored_credentials:
                                sqlite_traffic.upsert_credential_traffic_totals(connection, *key, entry)
                            else:
                                sqlite_traffic.upsert_credential_traffic_entry(connection, *key, entry)
                            written_credentials.add(key)
            if history_changes is not None:
                for (name, day_key, hour), (incoming, outgoing) in sorted(history_changes.clients.items()):
                    if name in mirrorable_clients and name in stored_clients:
                        sqlite_traffic.add_history_delta(connection, name, day_key, hour, incoming, outgoing)
                for (name, connection_tag, day_key, hour), (incoming, outgoing) in sorted(
                    history_changes.credentials.items()
                ):
                    key = (name, connection_tag)
                    if key in written_credentials and key in stored_credentials:
                        sqlite_traffic.add_credential_history_delta(
                            connection,
                            name,
                            connection_tag,
                            day_key,
                            hour,
                            incoming,
                            outgoing,
                        )
            sqlite_traffic.upsert_access_log_state(connection, db.get("accessLog"))
        return True
    except Exception:
        if strict:
            raise
        return False


def remove_traffic_clients_from_sqlite_for_write(
    names: list[str] | tuple[str, ...] | set[str],
    *,
    db_path: str | Path | None = None,
    strict: bool = False,
) -> bool:
    try:
        with database.write_session(database.database_path(db_path)) as connection:
            return sqlite_traffic.remove_traffic_clients(connection, names)
    except Exception:
        if strict:
            raise
        return False
