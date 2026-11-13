"""Чтение записей из снимка трафика SQLite."""


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
