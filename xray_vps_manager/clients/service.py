"""Сценарии изменения клиента и согласование SQLite с конфигурацией Xray."""

from copy import deepcopy
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
import re

from xray_vps_manager.clients import credentials, crud, payments, repository, routes
from xray_vps_manager.core import process
from xray_vps_manager.core.compensation import compensate
from xray_vps_manager.core.errors import ConflictError, StateError, ValidationError
from xray_vps_manager.core.locks import activity_lock, current_manager_operation, manager_lock, traffic_lock
from xray_vps_manager.core.paths import paths
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import client_traffic_handoff
from xray_vps_manager.xray.apply import apply_config, restore_config_backup
from xray_vps_manager.xray.config import ensure_api_rule_first, load_config


_CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


@dataclass(frozen=True)
class ClientChange[T]:
    result: T
    config: dict
    db: dict
    backup: Path | None


@dataclass(frozen=True)
class _Mutation[T]:
    result: T
    deletions: repository.Deletions | None = None
    apply: bool = True


def validate_name(name):
    if not _CLIENT_NAME_RE.match(name):
        raise ValidationError("Client name must be 1-64 chars: A-Z a-z 0-9 _ . @ -")


def _load_config():
    try:
        return load_config(paths().config)
    except FileNotFoundError as exc:
        raise StateError(str(exc)) from exc


def _retire_traffic(conn, before, mutation, owner):
    if not mutation.apply or mutation.deletions is None:
        return False
    name = mutation.result.name
    entry = before["clients"].get(name)
    if entry is None:
        return False
    stored = credentials.normalize_entry_credentials(entry)
    if mutation.deletions.clients:
        tags = stored.keys()
        target = None
    else:
        tags = [tag for _, tag in mutation.deletions.credentials if tag in stored]
        target = mutation.result.target_connection_tag
    retired = {tag: credentials.credential_email(name, entry, stored[tag]) for tag in tags}
    return client_traffic_handoff.start(conn, owner=owner, name=name, retired=retired, target=target)


def _change(purpose, mutate, *, retires_traffic=False, removes_client=False):
    parser_guard = activity_lock(purpose=purpose) if removes_client else nullcontext()
    with manager_lock(purpose=purpose), parser_guard:
        config = _load_config()
        undo = None
        applied = None
        owner = current_manager_operation() if retires_traffic else ""
        handoff_started = False
        settling_traffic = False

        def remember_apply(result):
            nonlocal applied
            applied = result

        def settle_traffic_and_db(*, restore=False):
            guard = traffic_lock(purpose=purpose) if handoff_started else nullcontext()
            with guard, database.report_errors(), database.write_session() as conn:
                if restore:
                    undo.restore(conn)
                if handoff_started:
                    client_traffic_handoff.finish(conn, owner=owner, restore_credentials=restore)

        try:
            writer_guard = traffic_lock(purpose=purpose) if retires_traffic else nullcontext()
            try:
                with writer_guard, database.report_errors(), database.write_session() as conn:
                    with database.report_errors(message="SQLite clients cannot be read"):
                        db = repository.load_db_from_sqlite(conn)
                    previous = deepcopy(db)
                    mutation = mutate(config, db, conn)
                    if mutation.apply:
                        routes.ensure_all_client_route_config(config, db)
                        ensure_api_rule_first(config)
                    if retires_traffic:
                        handoff_started = _retire_traffic(conn, previous, mutation, owner)
                    undo = repository.write_client_change(
                        conn, db, previous, deletions=mutation.deletions, prepare_undo=mutation.apply,
                    )
                    committed = repository.load_db_from_sqlite(conn)
            except ValidationError:
                raise
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            if mutation.apply:
                # ExecStop получает traffic.lock и открывает собственную пишущую сессию.
                applied = apply_config(config, on_applied=remember_apply)
            if handoff_started:
                settling_traffic = True
                settle_traffic_and_db()
        except BaseException as error:
            if undo is None:
                raise
            if settling_traffic:
                # Дельты и удаление передачи фиксируются вместе. Если COMMIT
                # успел пройти, обе стороны уже применены и согласованы.
                with database.report_errors(), database.session() as conn:
                    if client_traffic_handoff.load(conn).get("owner") != owner:
                        raise error
            actions = []
            if (settling_traffic or isinstance(error, KeyboardInterrupt)) and applied and applied.backup:
                # Apply завершился, а вызов прервали на возврате или после рестарта.
                actions.append(("Конфигурация Xray", lambda: restore_config_backup(applied.backup)))
            actions.append(("Клиентские данные SQLite", lambda: settle_traffic_and_db(restore=True)))
            compensate(error, actions)

        return ClientChange(mutation.result, config, committed, applied.backup if applied else None)


def prepare_add_client(name, *, connection_tag=None, protocol="", payment_type="free"):
    """Проверить параметры и определить, надо ли спросить срок доступа."""
    validate_name(name)
    try:
        if payment_type:
            payments.normalize_payment_type(payment_type)
        config, db = _load_config(), repository.load_db_sql()
        existing = crud.client_exists(config, db, name)
        crud.prepare_add_client(config, db, name, connection_tag, protocol=protocol)
        return existing
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def add_client(name, access_days=None, *, connection_tag=None, protocol="", payment_type="free", expected_existing=None):
    validate_name(name)
    try:
        payment_type = payments.normalize_payment_type(payment_type) if payment_type else ""
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    def mutate(config, db, _conn):
        if expected_existing is not None and crud.client_exists(config, db, name) != expected_existing:
            raise ConflictError(f"Client {name} was added or removed by another operation. Run the command again.")
        tag = crud.prepare_add_client(config, db, name, connection_tag, protocol=protocol)
        return _Mutation(crud.add_client(config, db, name, access_days, tag, payment_type))

    return _change("add", mutate)


def remove_client(name):
    validate_name(name)

    def mutate(config, db, _conn):
        result = crud.remove_client(config, db, name)
        return _Mutation(result, repository.Deletions(clients=(name,)))

    return _change("remove", mutate, retires_traffic=True, removes_client=True)


def disable_client(name, connection_tag=None):
    validate_name(name)
    return _change("disable", lambda config, db, _: _Mutation(crud.disable_client(config, db, name, connection_tag)))


def sync_traffic():
    sync = paths().sbin_dir / "xray-traffic-sync"
    if sync.exists():
        process.run([str(sync), "--quiet"], timeout=8)


def enable_client(name, connection_tag=None):
    validate_name(name)
    sync_traffic()

    def mutate(config, db, conn):
        with database.report_errors(message="SQLite traffic cannot be read"):
            traffic = repository.get_traffic_entry(conn, name)
        return _Mutation(crud.enable_client(config, db, traffic, name, connection_tag))

    return _change("enable", mutate)


def move_client(name, target_connection_identifier):
    validate_name(name)

    def mutate(config, db, _conn):
        result = crud.move_client_to_connection(config, db, name, target_connection_identifier)
        deletions = repository.Deletions(credentials=((name, result.source_connection_tag),))
        return _Mutation(result, deletions, apply=result.config_changed)

    return _change("move-connection", mutate, retires_traffic=True)


def apply_config_and_save_db(config: dict, db: dict, *, deletions=None):
    """Применить конфиг, затем сохранить БД (решение VIREI-124 от 08.11.2026).

    Ошибку apply откатывает сам механизм. Ошибка следующей записи БД требует
    отдельной компенсации конфига. Отмена внутри save_db могла прийти после
    COMMIT: как и прежде, один конфиг в этом случае не откатывается (71-R03).
    """
    with manager_lock(purpose="apply client config"):
        routes.ensure_all_client_route_config(config, db)
        ensure_api_rule_first(config)
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
