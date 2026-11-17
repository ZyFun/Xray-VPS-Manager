"""Сценарии изменения клиента и согласование SQLite с конфигурацией Xray."""

from copy import deepcopy
from contextlib import nullcontext
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
import re
from uuid import uuid4

from xray_vps_manager.clients import access, connections, credentials, crud, limits, payments, repository, routes, status
from xray_vps_manager.core import process
from xray_vps_manager.core.compensation import compensate
from xray_vps_manager.core.errors import ApplyConfigError, ConflictError, StateError, ValidationError
from xray_vps_manager.core.locks import activity_lock, current_manager_operation, manager_lock, traffic_lock
from xray_vps_manager.core.paths import paths
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import client_policy_recovery, client_traffic_handoff
from xray_vps_manager.xray.apply import apply_config, restart_installed_config, restore_config_backup
from xray_vps_manager.xray import client_route_runtime, client_routes
from xray_vps_manager.xray.config import active_client_any, ensure_api_rule_first, load_config


_CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_POLICY_LOCK_TIMEOUT = 30


@dataclass(frozen=True)
class ClientChange[T]:
    result: T
    config: dict
    db: dict
    backup: Path | None


@dataclass(frozen=True)
class _Reactivation:
    previous: client_policy_recovery.PendingReactivations
    pending: client_policy_recovery.PendingReactivations
    restart: bool
    keep_denied: bool


@dataclass(frozen=True)
class _Mutation[T]:
    result: T
    deletions: repository.Deletions | None = None
    apply: bool = True
    persist: bool = True
    reactivation: _Reactivation | None = None


@dataclass(frozen=True)
class RouteSelection:
    tag: str
    label: str
    changed: bool


def validate_name(name):
    if not _CLIENT_NAME_RE.match(name):
        raise ValidationError("Client name must be 1-64 chars: A-Z a-z 0-9 _ . @ -")


def _load_config():
    try:
        return load_config(paths().config)
    except FileNotFoundError as exc:
        raise StateError(str(exc)) from exc


def read_state():
    return _load_config(), repository.load_db_sql()


def _repair_candidates(config, conn):
    clients = repository.list_clients(conn, normalize=False)
    return credentials.repair_candidates(config, clients, repository.list_connections(conn))


def preview_repair() -> tuple[credentials.RepairCandidate, ...]:
    config = _load_config()
    with database.report_errors(), database.session() as conn, database.transaction(conn):
        return _repair_candidates(config, conn)


def repair_clients(confirmed: tuple[credentials.RepairCandidate, ...]) -> int:
    with manager_lock(purpose="repair"):
        config = _load_config()
        with database.report_errors(), database.write_session() as conn:
            candidates = _repair_candidates(config, conn)
            if candidates != confirmed:
                raise ConflictError("Repair candidates changed. Run xray-client repair again.")
            for name, group in groupby(candidates, key=lambda candidate: candidate.name):
                items = tuple(group)
                first = items[0]
                client_id = first.client_id or (first.item["id"] if first.protocol == "vless" else str(uuid4()))
                records = {}
                for item in items:
                    if item.protocol == "vless":
                        credential_id = item.item["id"]
                    elif not first.client_id and item is first:
                        credential_id = client_id
                    else:
                        credential_id = (item.legacy or {}).get("id") or str(uuid4())
                    records[item.tag] = item.credential(credential_id)
                if first.client_id:
                    repository.save_restored_credentials(conn, name, records)
                else:
                    repository.save_client(conn, name, {
                        "id": client_id,
                        "created": first.created,
                        "connection": first.tag,
                        "credentials": records,
                    })
    return len(candidates)


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


def _change(purpose, mutate, *, retires_traffic=False, removes_client=False, before_write=None):
    parser_guard = activity_lock(purpose=purpose) if removes_client else nullcontext()
    with manager_lock(purpose=purpose), parser_guard:
        config = _load_config()
        if before_write is not None:
            before_write(config)
        undo = None
        applied = None
        owner = current_manager_operation() if retires_traffic else ""
        handoff_started = False
        settling = False
        reactivation = None
        safe_config_installed = False

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
                if reactivation is not None and (not restore or reactivation.pending != reactivation.previous):
                    client_policy_recovery.save(conn, reactivation.previous if restore else {})

        try:
            writer_guard = traffic_lock(purpose=purpose) if retires_traffic else nullcontext()
            try:
                with writer_guard, database.report_errors(), database.write_session() as conn:
                    with database.report_errors(message="SQLite clients cannot be read"):
                        db = repository.load_db_from_sqlite(conn)
                    previous = deepcopy(db)
                    try:
                        mutation = mutate(config, db, conn)
                        if not mutation.persist:
                            return ClientChange(mutation.result, config, previous, None)
                        if mutation.apply:
                            routes.ensure_all_client_route_config(config, db)
                            ensure_api_rule_first(config)
                    except ValidationError:
                        raise
                    except ValueError as exc:
                        raise ValidationError(str(exc)) from exc
                    if retires_traffic:
                        handoff_started = _retire_traffic(conn, previous, mutation, owner)
                    reactivation = mutation.reactivation
                    undo = repository.write_client_change(
                        conn, db, previous, deletions=mutation.deletions,
                        prepare_undo=mutation.apply or reactivation is not None,
                    )
                    if reactivation is not None and reactivation.pending != reactivation.previous:
                        client_policy_recovery.save(conn, reactivation.pending)
                    committed = repository.load_db_from_sqlite(conn)
            except ValidationError:
                raise
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            if mutation.apply:
                # ExecStop получает traffic.lock и открывает собственную пишущую сессию.
                keep_denied = reactivation is not None and reactivation.keep_denied
                try:
                    applied = apply_config(config, restart=not keep_denied, on_applied=remember_apply)
                except ApplyConfigError:
                    # Ошибка из apply означает, что он уже отвечал за откат файла.
                    applied = None
                    raise
            if reactivation is not None and reactivation.keep_denied:
                # Предыдущий конфиг мог разрешать уже запрещённого клиента.
                # После установки закрывающего варианта сохраняем его и маркер,
                # даже если запуск Xray не подтвердился.
                safe_config_installed = True
            if reactivation is not None and reactivation.restart and not (applied and applied.restarted):
                restart_installed_config(reason="восстановление доступа после сброса лимита")
            if handoff_started or reactivation is not None:
                settling = True
                settle_traffic_and_db()
        except BaseException as error:
            if undo is None or safe_config_installed or (
                reactivation is not None and reactivation.keep_denied and applied is not None
            ):
                raise
            if settling:
                # Дельты и удаление передачи фиксируются вместе. Если COMMIT
                # успел пройти, обе стороны уже применены и согласованы.
                with database.report_errors(), database.session() as conn:
                    if handoff_started and client_traffic_handoff.load(conn).get("owner") != owner:
                        raise error
                    if reactivation is not None and not client_policy_recovery.load(conn):
                        raise error
            actions = []
            if (settling or isinstance(error, KeyboardInterrupt)) and applied and applied.backup:
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


def _reject_config_only_client(config, name):
    if not repository.client_exists_in_sqlite(name):
        crud.reject_config_only_client(config, name)


def enable_client(name, connection_tag=None, *, limit_status):
    validate_name(name)
    # Проверяем согласованный снимок до pre-sync, который работает без manager.lock.
    with manager_lock(purpose="enable"):
        _reject_config_only_client(_load_config(), name)
    sync_traffic()

    def mutate(config, db, conn):
        with database.report_errors(message="SQLite traffic cannot be read"):
            traffic = repository.get_traffic_entry(conn, name)
        return _Mutation(crud.enable_client(config, db, traffic, name, connection_tag, limit_status=limit_status))

    return _change("enable", mutate)


def move_client(name, target_connection_identifier):
    validate_name(name)

    def mutate(config, db, _conn):
        result = crud.move_client_to_connection(config, db, name, target_connection_identifier)
        deletions = repository.Deletions(credentials=((name, result.source_connection_tag),))
        return _Mutation(result, deletions, apply=result.config_changed)

    return _change("move-connection", mutate, retires_traffic=True)


def _update_access(purpose, name, update_entry, limit_status):
    validate_name(name)

    def before_write(config):
        _reject_config_only_client(config, name)
        sync_traffic()

    def mutate(config, db, conn):
        entry = crud.db_entry_for_existing_client(config, db, name)
        with database.report_errors(message="SQLite traffic cannot be read"):
            traffic = repository.get_traffic_entry(conn, name)
        result = status.apply_access_update(config, db, traffic, name, entry, update_entry, limit_status=limit_status)
        return _Mutation(result, apply=result.config_changed)

    return _change(purpose, mutate, before_write=before_write)


def set_days(name, days, *, limit_status):
    return _update_access("set-days", name, lambda entry: access.set_entry_expiry(entry, days), limit_status)


def extend_days(name, days, *, limit_status):
    return _update_access("extend-days", name, lambda entry: access.extend_entry_expiry(entry, days), limit_status)


def _update_client_entry(purpose, name, update_entry):
    validate_name(name)

    def mutate(config, db, _conn):
        connections.ensure_connections(config, db)
        entry = crud.db_entry_for_existing_client(config, db, name)
        result = update_entry(entry)
        repository.db_clients(db)[name] = entry
        return _Mutation(result, apply=False)

    return _change(purpose, mutate)


def set_payment(name, payment_type):
    def update(entry):
        payments.set_payment_type(entry, payment_type)
        return entry

    return _update_client_entry("set-payment", name, update)


def set_limit(name, period, limit_bytes):
    return _update_client_entry(
        "set-limit", name, lambda entry: limits.set_client_traffic_limit(entry, period, limit_bytes, utc_stamp),
    )


def clear_limit(name):
    return _update_client_entry("clear-limit", name, lambda entry: limits.clear_client_traffic_limit(entry, utc_stamp))


def _run_policy(purpose, evaluate):
    def mutate(config, db, conn):
        connections.ensure_connections(config, db)
        return evaluate(config, db, conn)

    with manager_lock(_POLICY_LOCK_TIMEOUT, purpose=purpose):
        config = _load_config()
        with database.report_errors(message="SQLite clients cannot be read"), \
                database.session() as conn, database.transaction(conn):
            db = repository.load_db_from_sqlite(conn)
            previous = deepcopy(db)
            try:
                mutation = mutate(config, db, conn)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        if not mutation.persist:
            return ClientChange(mutation.result, config, previous, None)
        # Трафик мог измениться после читающего снимка. Повторяем расчёт
        # на данных той же транзакции, которая фиксирует клиентские изменения.
        return _change(purpose, mutate)


def enforce_limits(*, limit_status, sync_first=False) -> ClientChange[status.TrafficLimitEnforcementResult]:
    if sync_first:
        sync_traffic()

    def evaluate(config, db, conn):
        with database.report_errors(message="SQLite traffic cannot be read"):
            pending = client_policy_recovery.load(conn)
            entries = {
                name: repository.get_traffic_entry(conn, name)
                for name, entry in repository.db_clients(db).items()
                if name in pending or limits.traffic_limit(entry) is not None
            }
        stamp, now = utc_stamp(), access.local_now()
        retry = status.retry_limit_reactivations(
            config, db, entries, pending, limit_status=limit_status, now=now,
        )
        result = status.enforce_traffic_limits(
            config, db, {"clients": entries}, now=now, stamp=stamp, limit_status=limit_status,
        )
        clients = repository.db_clients(db)
        candidates = {
            name: status.reactivation_fingerprints(clients[name], tags=tags)
            for name, tags in retry.credentials.items()
        }
        candidates.update({
            name: status.reactivation_fingerprints(clients[name])
            for name in result.reactivated_names
        })
        restart = bool(retry.enabled or retry.exceeded or retry.expired)
        keep_denied = bool(retry.exceeded or retry.expired)
        reactivation = _Reactivation(pending, candidates, restart, keep_denied) if pending or candidates else None
        should_apply = result.has_changes or retry.config_changed
        result.reactivated_names[:0] = retry.enabled
        result.due_names[:0] = [name for name in retry.exceeded if name not in result.reactivated_names]
        result.expired_during_recovery = retry.expired
        return _Mutation(
            result, apply=should_apply, persist=should_apply or reactivation is not None, reactivation=reactivation,
        )

    return _run_policy("enforce-limits", evaluate)


def expire_due() -> ClientChange[status.ExpireDueResult]:
    def evaluate(config, db, _conn):
        result = status.expire_due_clients(config, db, stamp=utc_stamp())
        return _Mutation(result, apply=result.has_changes, persist=result.has_changes)

    return _run_policy("expire-due", evaluate)


def rotate_trojan_password(name, connection_tag=None):
    validate_name(name)

    def mutate(config, db, _conn):
        result = crud.rotate_trojan_password(config, db, name, connection_tag)
        return _Mutation(result, apply=result.config_changed)

    return _change("rotate-trojan-password", mutate)


def set_route(name, route_value=None):
    """Синхронизировать выбор и при необходимости переключить runtime без рестарта."""
    validate_name(name)
    if route_value is None:
        config, db = read_state()
        routes.sync_routes_from_config(config, db)
        entry = repository.db_clients(db).get(name)
        if entry is None:
            _, item = active_client_any(config, name)
            if item is None:
                raise ValidationError(f"Client not found: {name}")
            entry = {}
            routes.assign_default_client_routes(config, {"clients": {name: entry}})
        tag = client_routes.selected_route_tag(entry)
        return ClientChange(RouteSelection(tag, routes.selected_route_label(db, entry), False), config, db, None)

    with manager_lock(purpose="route"):
        config = _load_config()
        undo = None
        applied = None
        previous_runtime = None
        attempted_override = False

        def remember_apply(result):
            nonlocal applied
            applied = result

        def restore_db():
            with database.report_errors(), database.write_session() as conn:
                undo.restore(conn)

        def remember_runtime():
            nonlocal previous_runtime
            previous_runtime = client_route_runtime.read_balancer(balancer)

        def restore_runtime():
            if attempted_override:
                client_route_runtime.set_override(balancer, previous_runtime.override)
            if client_route_runtime.read_balancer(balancer) != previous_runtime:
                raise StateError("Xray API вернул другое состояние балансера после восстановления")

        try:
            with database.report_errors(), database.write_session() as conn:
                with database.report_errors(message="SQLite clients cannot be read"):
                    db = repository.load_db_from_sqlite(conn)
                previous = deepcopy(db)
                routes.sync_routes_from_config(config, db)
                entry = crud.db_entry_for_existing_client(config, db, name)
                current = client_routes.selected_route_tag(entry)
                tag = routes.resolve_route_tag(config, db, route_value)
                changed = current != tag
                if changed:
                    if not client_routes.client_routes_allowed(config):
                        raise ValidationError(
                            "Выбор страны недоступен: общий маршрут сейчас не каскад "
                            "(включён WARP или каскадный маршрут отключён). Выбери страну после возврата каскадного маршрута."
                        )
                    entry["selectedCascadeTag"] = tag
                    routes.ensure_client_route_config(config, name, entry, tag)
                    ensure_api_rule_first(config)
                    balancer = client_routes.client_balancer_tag(name, entry)
                    repository.db_clients(db)[name] = entry
                undo = repository.write_client_change(conn, db, previous)
            if changed:
                try:
                    apply_config(config, restart=False, on_applied=remember_apply, before_install=remember_runtime)
                except ApplyConfigError:
                    applied = None  # Применение само отвечает за откат при отказе внутри apply.
                    raise
                attempted_override = True
                client_route_runtime.set_override(balancer, tag)
            result = RouteSelection(tag, routes.selected_route_label(db, entry), changed)
            return ClientChange(result, config, db, applied.backup if applied else None)
        except BaseException as error:
            if undo is None:
                if isinstance(error, ValueError):
                    raise ValidationError(str(error)) from error
                raise
            actions = []
            if applied and applied.backup:
                actions.append(("Конфигурация Xray", lambda: restore_config_backup(applied.backup, restart=False)))
            if previous_runtime is not None:
                actions.append(("Балансер Xray", restore_runtime))
            actions.append(("Клиентские данные SQLite", restore_db))
            if isinstance(error, ApplyConfigError) and error.stage == "rollback":
                error = StateError("Не удалось восстановить конфигурацию Xray; состояние не подтверждено",
                                   detail=error.detail)
            compensate(error, actions)


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


def sync_routes():
    """Явная синхронизация правил со старым порядком применения и записи."""
    with manager_lock(purpose="sync-routes"):
        config, db = read_state()
        changed = routes.ensure_all_client_route_config(config, db)
        backup = apply_config_and_save_db(config, db).backup if changed else None
        if not changed:
            repository.save_db(db)
    return ClientChange(changed, config, db, backup)
