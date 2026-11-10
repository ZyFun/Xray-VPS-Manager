"""Применение GeoIP bypass: config, точечная БД, затем server.env."""

from xray_vps_manager.core import clock, settings
from xray_vps_manager.core.compensation import compensate
from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import bypass
from xray_vps_manager.xray.apply import ApplyResult, apply_config, restore_config_backup
from xray_vps_manager.xray.config import ensure_api_rule_first


def _restore_records(records):
    with database.write_session(database.database_path()) as connection:
        for tag, record in records.items():
            if record is None:
                bypass.delete_route(connection, tag)
            else:
                bypass.upsert_route(connection, tag, record)


def apply_route(config, record, env_values, *, conflict=None, delete=False):
    with manager_lock(purpose="apply bypass"):
        tag = record["tag"]
        tags = [tag] + ([conflict["tag"]] if conflict else [])
        with database.session(database.database_path()) as connection:
            previous = {key: bypass.get_route(connection, key) for key in tags}
        original_env = settings.env_bytes()
        if config is not None:
            ensure_api_rule_first(config)
        applied = apply_config(config) if config is not None else ApplyResult(False)
        env_started = False
        try:
            with database.write_session(database.database_path()) as connection:
                if conflict:
                    bypass.set_enabled(connection, conflict["tag"], False, clock.utc_stamp())
                if delete:
                    bypass.delete_route(connection, tag)
                else:
                    bypass.upsert_route(connection, tag, record)
            env_started = True
            settings.write_env(env_values)
        except BaseException as error:
            actions = [("Метаданные bypass", lambda: _restore_records(previous)),
                       ("Конфигурация Xray", lambda: restore_config_backup(applied.backup))]
            if env_started:
                actions.append(("server.env", lambda: settings.restore_env(original_env)))
            compensate(error, actions)
        return applied
