"""Применение маршрутов с точечной компенсацией клиентских метаданных."""

from copy import deepcopy

from xray_vps_manager.clients import repository
from xray_vps_manager.core.compensation import compensate
from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.xray.apply import apply_config, restore_config_backup
from xray_vps_manager.xray.config import ensure_api_rule_first


def load_route_db():
    """Метаданные необходимы для согласованного изменения маршрутов."""
    return repository.load_db_sql()


def _restore_route_records(before, desired, deletions):
    # Только изменённые записи. Трафик, история и прочее состояние БД не входят
    # в снимок и не перезаписываются. Эти сценарии не создают/удаляют клиентов.
    undo = {}
    removed_new_routes = []
    for section in ("clients", "cascadeRoutes", "connections"):
        old = before.get(section, {})
        new = desired.get(section, {})
        keys = set(old) | set(new)
        if section == "cascadeRoutes" and deletions:
            keys.update(deletions.routes)
        undo[section] = {key: old[key] for key in keys if key in old and old.get(key) != new.get(key)}
        if section == "cascadeRoutes":
            removed_new_routes = list(new.keys() - old.keys())
    repository.save_db(undo, deletions=repository.Deletions(routes=tuple(removed_new_routes)))


def apply_route_config(config, db=None, *, deletions=None):
    """Временный порядок config → БД по решению VIREI-125 от 08.11.2026."""
    with manager_lock(purpose="apply route"):
        previous = deepcopy(db.original) if db is not None else None
        desired = deepcopy(dict(db)) if db is not None else None
        ensure_api_rule_first(config)
        applied = apply_config(config)
        try:
            if db is not None:
                repository.save_db(db, deletions=deletions)
        except BaseException as error:
            compensate(error, [
                ("Метаданные маршрутов", lambda: _restore_route_records(previous, desired, deletions)),
                ("Конфигурация Xray", lambda: restore_config_backup(applied.backup)),
            ])
        return applied
