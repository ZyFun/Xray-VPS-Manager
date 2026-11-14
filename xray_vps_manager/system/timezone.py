"""Смена зоны менеджера и приведение прежних сроков к новой полуночи."""

from xray_vps_manager.clients import access as client_access
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.clients import settings as client_settings
from xray_vps_manager.core.errors import ValidationError
from xray_vps_manager.core.locks import manager_lock


def normalize_access_deadlines(tz, previous_tz=None, now=None):
    db = client_repository.load_db_sql()
    now = now or client_access.local_now()
    changed = 0
    warnings = []
    for name, entry in client_repository.db_clients(db).items():
        expires_at = entry.get("expiresAt", "")
        normalized = client_access.access_deadline_at_midnight(expires_at, tz)
        if normalized:
            warnings.extend(
                client_access.access_deadline_change_warnings(
                    name, expires_at, normalized, previous_tz, now, entry.get("disabledReason", "")
                )
            )
        if normalized and normalized != expires_at:
            entry["expiresAt"] = normalized
            changed += 1
    if changed:
        client_repository.save_db(db)
    return changed, warnings


def set_manager_timezone(value):
    try:
        name = client_settings.normalize_timezone(value)
    except ValueError as error:
        raise ValidationError(str(error)) from error
    with manager_lock(purpose="set-timezone"):
        previous = client_settings.manager_timezone_info()
        # При исправлении неверной настройки прежняя выбранная зона неизвестна.
        previous_tz = previous.tz if previous.valid else None
        values = client_settings.server_env_values()
        values["MANAGER_TIMEZONE"] = name
        client_settings.save_server_env_values(values)
        normalized, warnings = normalize_access_deadlines(client_settings.manager_timezone(), previous_tz)
    return name, normalized, warnings
