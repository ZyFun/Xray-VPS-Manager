"""Согласование blocklist с Xray вне activity.lock."""

from xray_vps_manager.activity import blocklist
from xray_vps_manager.core.clock import utc_stamp
from xray_vps_manager.core.compensation import compensate
from xray_vps_manager.core.locks import activity_lock, manager_lock
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity_blocklist as repository
from xray_vps_manager.xray import blocklist as config_rules
from xray_vps_manager.xray.apply import apply_config as apply_xray_config
from xray_vps_manager.xray.config import ensure_api_rule_first, load_config


def apply_config(config):
    with manager_lock(purpose="blocklist config"):
        ensure_api_rule_first(config)
        return apply_xray_config(config).backup


def reconcile_xray_config(*, removed_items=None, db_path=None):
    with manager_lock(purpose="blocklist reconcile"):
        with activity_lock(), database.session(database.database_path(db_path)) as connection:
            known = repository.list_blocks(connection)
            active = repository.active_blocks(connection, utc_stamp())
        config = load_config()
        changed = config_rules.sync_blocklist_rules(config, active, known_items=known, removed_items=removed_items)
        changed = ensure_api_rule_first(config) or changed
        if not changed:
            return None
        return apply_xray_config(config).backup


def _restore(snapshot, value, db_path, deleted):
    with activity_lock(), database.write_session(database.database_path(db_path)) as connection:
        repository.restore_block(connection, snapshot, value, restore_hits=deleted)


def _change(value, *, delete=False, db_path=None, **kwargs):
    with manager_lock(purpose="blocklist change"):
        key = value if delete else blocklist.classify_block_value(value)[0]
        mutation_started = False
        try:
            with activity_lock():
                with database.session(database.database_path(db_path)) as connection:
                    previous = repository.snapshot_block(connection, key, by_value=not delete)
                if delete and previous is None:
                    raise KeyError(value)
                mutation_started = True
                item = (blocklist.delete_block(key, db_path=db_path) if delete else
                        blocklist.add_block(value, db_path=db_path, **kwargs))
            backup = reconcile_xray_config(removed_items=[item] if delete else None, db_path=db_path)
        except BaseException as error:
            if not mutation_started:
                raise
            # apply владеет конфигом при собственном отказе; здесь только БД.
            compensate(error, [("Запись blocklist", lambda: _restore(previous, key, db_path, delete))])
        return item, backup


def add_block(value, **kwargs):
    return _change(value, **kwargs)


def delete_block(value, **kwargs):
    return _change(value, delete=True, **kwargs)
