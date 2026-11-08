"""Клиентские сценарии; переходный порядок config → БД до шага 4."""

from xray_vps_manager.clients import repository, routes
from xray_vps_manager.core.errors import StateError
from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.xray.apply import apply_config, restore_config_backup


def apply_config_and_save_db(config: dict, db: dict, *, deletions=None):
    """Применить конфиг, затем сохранить БД (решение VIREI-124 от 08.11.2026).

    Ошибку apply откатывает сам механизм. Ошибка следующей записи БД требует
    отдельной компенсации конфига. Отмена внутри save_db могла прийти после
    COMMIT: как и прежде, один конфиг в этом случае не откатывается (71-R03).
    """
    with manager_lock(purpose="apply client config"):
        routes.ensure_all_client_route_config(config, db)
        applied = apply_config(config)
        try:
            repository.save_db(db, deletions=deletions)
        except Exception as error:
            try:
                restore_config_backup(applied.backup)
            except Exception as restore_error:
                raise StateError(
                    f"New config failed: {error}. Restoring backup {applied.backup} also failed: {restore_error}",
                    detail=getattr(restore_error, "detail", ""),
                ) from restore_error
            detail = f"Restored backup: {applied.backup}" if applied.backup else "Config was not changed."
            raise StateError(f"New config failed: {error}. {detail}") from error
        return applied
