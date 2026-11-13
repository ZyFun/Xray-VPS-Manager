"""Синхронизация клиентских маршрутов и применение runtime override."""

from typing import Any

from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.core.errors import ValidationError
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.xray import cascade as cascade_config
from xray_vps_manager.xray import client_routes


def sync_routes_from_config(config: dict[str, Any], db: dict[str, Any]) -> bool:
    """Заполнить метаданные каскадов и выбор клиентов по умолчанию в снимке."""
    changed = sync_route_metadata_from_config(config, db)
    return assign_default_client_routes(config, db) or changed


def sync_route_metadata_from_config(config: dict[str, Any], db: dict[str, Any]) -> bool:
    """Заполнить метаданные каскадов, сохраняя исходный выбор каждого клиента."""

    changed = False
    routes = client_repository.db_cascade_routes(db)
    stamp = utc_stamp()
    active_tag = cascade_config.active_cascade_tag(config)
    for tag in cascade_config.cascade_tags(config):
        existing = routes.get(tag, {})
        if not isinstance(existing, dict):
            existing = {}
        record = dict(existing)
        changed_record = False
        if record.get("tag") != tag:
            record["tag"] = tag
            changed_record = True
        if not record.get("label"):
            record["label"] = cascade_config.cascade_name_from_tag(tag) or tag
            changed_record = True
        if not record.get("country"):
            record["country"] = client_routes.DEFAULT_ACTIVE_COUNTRY if tag == active_tag else client_routes.DEFAULT_INACTIVE_COUNTRY
            changed_record = True
        if not record.get("created"):
            record["created"] = stamp
            changed_record = True
        if changed_record:
            record["updated"] = stamp
        if routes.get(tag) != record:
            routes[tag] = record
            changed = True

    return changed


def assign_default_client_routes(config: dict[str, Any], db: dict[str, Any]) -> bool:
    active = cascade_config.active_cascade_tag(config)
    if not active:
        return False
    configured = set(cascade_config.cascade_tags(config))
    changed = False
    for entry in client_repository.db_clients(db).values():
        if not isinstance(entry, dict):
            continue
        selected = str(entry.get("selectedCascadeTag") or "").strip()
        if selected and selected in configured:
            continue
        entry["selectedCascadeTag"] = active
        changed = True
    return changed


def upsert_route_country(db: dict[str, Any], tag: str, country: str, *, label: str = "") -> None:
    routes = client_repository.db_cascade_routes(db)
    existing = routes.get(tag, {})
    if not isinstance(existing, dict):
        existing = {}
    stamp = utc_stamp()
    record = dict(existing)
    record["tag"] = tag
    record["country"] = client_routes.normalize_country(country)
    record["label"] = label or record.get("label") or cascade_config.cascade_name_from_tag(tag) or tag
    record.setdefault("created", stamp)
    record["updated"] = stamp
    routes[tag] = record


def route_options(db: dict[str, Any], config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    configured = set(cascade_config.cascade_tags(config)) if config is not None else None
    options = []
    for tag, record in client_repository.db_cascade_routes(db).items():
        if configured is not None and tag not in configured:
            continue
        if not isinstance(record, dict):
            continue
        item = dict(record)
        item["tag"] = tag
        item["display"] = client_routes.route_label(item, tag)
        options.append(item)
    return sorted(options, key=lambda item: (str(item.get("display") or "").lower(), str(item.get("tag") or "")))


def selected_route_label(db: dict[str, Any], entry: dict[str, Any] | None) -> str:
    tag = client_routes.selected_route_tag(entry)
    if not tag:
        return "не выбрана"
    record = client_repository.db_cascade_routes(db).get(tag, {})
    return client_routes.route_label(record, tag)


def ensure_all_client_route_config(config: dict[str, Any], db: dict[str, Any]) -> bool:
    changed = sync_routes_from_config(config, db)
    if not client_routes.client_routes_allowed(config):
        return client_routes.remove_all_client_route_config(config) or changed
    expected_balancers = {
        client_routes.client_balancer_tag(name, entry)
        for name, entry in client_repository.db_clients(db).items()
        if isinstance(entry, dict)
    }
    changed = client_routes.remove_stale_client_route_config(config, expected_balancers) or changed
    for name, entry in client_repository.db_clients(db).items():
        if isinstance(entry, dict):
            changed = ensure_client_route_config(config, name, entry) or changed
    return changed


def resolve_route_tag(config, db, value):
    raw = str(value or "").strip()
    if not raw:
        raise ValidationError("Cascade route is required.")
    options = route_options(db, config)
    by_tag = {item["tag"] for item in options}
    if raw in by_tag:
        return raw
    try:
        tag = cascade_config.cascade_tag(raw)
    except ValueError:
        tag = ""
    if tag in by_tag:
        return tag
    country_matches = [
        item["tag"]
        for item in options
        if str(item.get("country") or "").casefold() == raw.casefold()
        or str(item.get("display") or "").casefold() == raw.casefold()
    ]
    if len(country_matches) == 1:
        return country_matches[0]
    if len(country_matches) > 1:
        raise ValidationError("Country matches more than one cascade route. Use cascade tag.")
    raise ValidationError(f"Cascade route not found: {raw}")


def ensure_client_route_config(
    config: dict[str, Any], name: str, entry: dict[str, Any], selected_tag: str | None = None
) -> bool:
    prepared = client_routes.prepare_client_route_config(config, name, entry, selected_tag)
    if isinstance(prepared, bool):
        return prepared
    emails = client_credentials.client_emails(name, entry)
    return client_routes.complete_client_route_config(config, entry, prepared, emails)
