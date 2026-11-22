"""Применение изменений управляемых подключений и связанных файлов."""

from contextlib import ExitStack, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory

from xray_vps_manager.caddy import model as caddy_model
from xray_vps_manager.caddy import service as caddy_service
from xray_vps_manager.caddy import sites as caddy_sites
from xray_vps_manager.clients import connections, credentials, links, repository, routes, service, settings
from xray_vps_manager.core import fs, process
from xray_vps_manager.core.compensation import compensate
from xray_vps_manager.core.errors import ApplyConfigError, ConflictError, ManagerError, StateError, ValidationError
from xray_vps_manager.core.locks import activity_lock, current_manager_operation, manager_lock, traffic_lock
from xray_vps_manager.core.paths import paths
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import client_traffic_handoff
from xray_vps_manager.xray import apply as xray_apply
from xray_vps_manager.xray import config as xray


@dataclass(frozen=True)
class ConnectionChange[T]:
    result: T
    backup: Path | None = None
    site: Path | None = None
    link_error: OSError | None = None


@dataclass(frozen=True)
class RealityEndpoint:
    port: int
    sni: str
    dest: str


@dataclass(frozen=True)
class _Edit[T]:
    result: T
    deletions: repository.Deletions | None = None
    caddy_site: caddy_service.SiteCandidate | None = None
    install_caddy: bool = False
    env_tag: str = ""


def _retire_connection_traffic(conn, config, previous, result, owner):
    def emails(names):
        retired = {}
        for name in names:
            entry = previous["clients"].get(name)
            if entry is not None:
                credential = credentials.credential_for_connection(entry, result.tag)
                if credential is not None:
                    retired[name] = credentials.credential_email(name, entry, credential)
        return retired

    retained = emails(result.detached_client_names)
    prefer_retired_without_events = set()
    if retained:
        active_names = set()
        for inbound in config.get("inbounds", []):
            protocol = inbound.get("protocol")
            if protocol not in ("vless", "trojan"):
                continue
            settings = inbound.get("settings", {})
            items = settings.get("clients") or (settings.get("users") if protocol == "trojan" else None) or []
            active_names.update(xray.client_name(item) for item in items)
        # Без событий access.log ExecStop сначала видел активные credentials
        # нового конфига, затем старые записи SQLite в порядке тегов.
        prefer_retired_without_events = {
            name for name in retained.keys() - active_names
            if credentials.sorted_credentials(previous["clients"][name])[0]["connection"] == result.tag
        }
    return client_traffic_handoff.start_connection_removal(
        conn, owner=owner, tag=result.tag,
        removed=emails(result.removed_client_names), retained=retained,
        prefer_retired_without_events=prefer_retired_without_events,
    )


def _prepare_edit(edit, config, db, *, preview=False):
    try:
        change = edit(config, db)
        if preview:
            return change, None
        env = _values_for_connection(config, db, change.env_tag) if change.env_tag else None
        routes.ensure_all_client_route_config(config, db)
        xray.ensure_api_rule_first(config)
        return change, env
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValidationError(str(exc)) from exc


def _change(purpose, edit, *, refresh_link=False, retires_traffic=False, precheck_caddy=False):
    with manager_lock(purpose=purpose), ExitStack() as guards:
        config = service.load_working_config()
        validated_site = None
        caddy_package = None
        parser_locked = False
        if precheck_caddy or retires_traffic:
            # Проверка Caddy и ожидание парсера проходят до пишущей транзакции.
            preview_db = repository.load_db_sql()
            client_names = set(preview_db["clients"]) if retires_traffic else set()
            preview, _ = _prepare_edit(edit, deepcopy(config), preview_db, preview=True)
            if retires_traffic and client_names.intersection(preview.deletions.clients):
                guards.enter_context(activity_lock(purpose=purpose))
                parser_locked = True
            if precheck_caddy and preview.caddy_site is not None:
                if preview.install_caddy and not caddy_service.binary_available():
                    directory = guards.enter_context(TemporaryDirectory(prefix="xray-caddy-package-"))
                    caddy_package = caddy_service.stage_caddy_package(Path(directory))
                preview.caddy_site.validate(binary=caddy_package.binary if caddy_package else "caddy")
                validated_site = preview.caddy_site

        undo = None
        applied = None
        handoff_started = False
        settling = False
        post_commit_error = None
        owner = current_manager_operation() if retires_traffic else ""

        def remember_apply(result):
            nonlocal applied
            applied = result

        def settle_db(*, restore=False):
            guard = traffic_lock(purpose=purpose) if handoff_started else nullcontext()
            with guard, database.report_errors(), database.write_session() as conn:
                if restore:
                    undo.restore(conn)
                if handoff_started:
                    client_traffic_handoff.finish(conn, owner=owner, restore_credentials=restore)

        site_update = None
        try:
            writer_guard = traffic_lock(purpose=purpose) if retires_traffic else nullcontext()
            with writer_guard, database.report_errors(), database.write_session() as conn:
                # Конфиг маршрутов строится по тем же данным, которые использует запись.
                db = repository.load_db_from_sqlite(conn)
                previous = deepcopy(db)
                change, env = _prepare_edit(edit, config, db)
                if change.caddy_site != validated_site:
                    raise ConflictError("Connection settings changed after Caddy validation. Reload them and retry.")
                if retires_traffic:
                    if not parser_locked and any(name in previous["clients"] for name in change.deletions.clients):
                        raise ConflictError("Client records changed before connection removal. Reload them and retry.")
                    handoff_started = _retire_connection_traffic(conn, config, previous, change.result, owner)
                undo = repository.write_client_change(
                    conn, db, previous, undo_resources=guards, deletions=change.deletions,
                )
            try:
                applied = xray_apply.apply_config(config, on_applied=remember_apply)
            except ApplyConfigError:
                # Механизм применения сам восстанавливает рабочий файл.
                applied = None
                raise
            if change.caddy_site is not None:
                if caddy_package is not None:
                    caddy_package.install()
                site_update = change.caddy_site.prepare_update(runner=process.run_stream)
                site_update.apply()
            if handoff_started:
                settling = True
                settle_db()
        except BaseException as error:
            if undo is None:
                raise
            if settling:
                with database.report_errors(), database.session() as conn:
                    if client_traffic_handoff.load(conn).get("owner") != owner:
                        # SQLite и Xray уже согласованы. Обновляем зависимые
                        # файлы до того, как передадим отмену вызывающему коду.
                        post_commit_error = error
            if post_commit_error is None:
                actions = site_update.rollback_actions(
                    restore_activation=caddy_package is None,
                ) if site_update is not None else []
                if caddy_package is not None:
                    actions.extend(caddy_package.rollback_actions())
                if applied is not None and applied.backup is not None:
                    actions.append(("Конфигурация Xray", lambda: xray_apply.restore_config_backup(applied.backup)))
                actions.append(("Данные подключений SQLite", lambda: settle_db(restore=True)))
                compensate(error, actions)

        if site_update is not None:
            site_update.prune_backups()
        link_error = None
        try:
            if refresh_link:
                _write_initial_link(config, db, fingerprint=env.get("FINGERPRINT") if env else None)
        except OSError as exc:
            link_error = exc
        finally:
            if env is not None:
                _update_connection_env(env)
        if post_commit_error is not None:
            raise post_commit_error
        return ConnectionChange(
            change.result, applied.backup if applied else None, site_update.target if site_update else None,
            link_error,
        )


def _update_connection_env(values, *, metadata_only=False):
    try:
        settings.update_server_env_values(values)
    except OSError as exc:
        changed = "Connection metadata was updated" if metadata_only else "Xray configuration was applied"
        raise StateError(f"{changed}, but server.env could not be updated.",
                         hint="Run xray-test.", detail=str(exc)) from exc


def _require_caddy_site_available(config, domain, public_port):
    conflicts = connections.public_port_conflicts(config, public_port)
    if conflicts:
        tags = ", ".join(inbound.get("tag") or "(no-tag)" for inbound in conflicts)
        raise ValidationError(
            f"Caddy cannot listen on public port {public_port}: it is already used by Xray inbound(s): {tags}. "
            "Move those connections to another port before installing Caddy."
        )
    caddy_sites.require_site_config_absent(domain)


def _site_candidate(result, *, trojan=False, previous_domain=None):
    content = caddy_model.caddy_site_block(
        result.public_host,
        result.local_port,
        tls_min_version=result.tls_min_version,
        tls_max_version=result.tls_max_version,
        upstream_transport="http" if trojan else "xhttp",
        route_path=result.ws_path if trojan else "",
    )
    return caddy_service.SiteCandidate(result.public_host, content, previous_domain)


def add_connection(name, port, sni, fingerprint, **transport):
    def edit(config, db):
        return _Edit(connections.add_connection(config, db, name, port, sni, fingerprint, **transport))

    return _change("add-connection", edit)


def add_tls_xhttp_connection(name, domain, *, local_port, public_port, install_caddy, **options):
    def edit(config, db):
        tls_domain = caddy_model.validate_domain(domain)
        if install_caddy:
            _require_caddy_site_available(config, tls_domain, public_port)
        result = connections.add_tls_xhttp_connection(
            config, db, name, tls_domain, local_port=local_port, public_port=public_port,
            caddy_enabled=install_caddy, **options,
        )
        return _Edit(result, caddy_site=_site_candidate(result) if install_caddy else None,
                     install_caddy=install_caddy)

    return _change("add-connection", edit, precheck_caddy=install_caddy)


def add_trojan_tls_connection(name, port, domain, cert_file, key_file, fingerprint):
    def edit(config, db):
        return _Edit(connections.add_trojan_tls_connection(
            config, db, name, port, domain, cert_file, key_file, fingerprint,
        ))

    return _change("add-trojan-connection", edit)


def add_trojan_caddy_connection(name, domain, *, local_port, public_port, install_caddy, **options):
    def edit(config, db):
        tls_domain = caddy_model.validate_domain(domain)
        if install_caddy:
            _require_caddy_site_available(config, tls_domain, public_port)
        result = connections.add_trojan_caddy_connection(
            config, db, name, tls_domain, local_port=local_port, public_port=public_port,
            caddy_enabled=install_caddy, **options,
        )
        return _Edit(result, caddy_site=_site_candidate(result, trojan=True) if install_caddy else None,
                     install_caddy=install_caddy)

    return _change("add-trojan-connection", edit, precheck_caddy=install_caddy)


def update_transport(identifier, transport, *, refresh_link=False, **options):
    def edit(config, db):
        result = connections.update_connection_transport(config, db, identifier, transport, **options)
        return _Edit(result, env_tag=result.tag if result.tag == xray.INBOUND_TAG else "")

    return _change("connection-transport", edit, refresh_link=refresh_link)


def update_xhttp_extra(identifier, extra, *, refresh_link=False):
    def edit(config, db):
        return _Edit(connections.update_connection_xhttp_extra(config, db, identifier, extra))

    return _change("connection-xhttp-extra", edit, refresh_link=refresh_link)


def check_reality_port(config: dict, tag: str, port: int) -> None:
    """Проверка для меню до следующего вопроса и для сценария под блокировкой."""
    if connections.port_used_by_other_inbound(config, tag, port):
        raise ConflictError(f"PORT is already used by another inbound: {port}")
    listen = xray.changed_port_listen(config, tag, port)
    if listen is not None and process.tcp_port_in_use(port, listen):
        raise ConflictError(f"PORT is already in use on this server by another process: {port}")


def update_reality_endpoint(tag: str, *, port: int | None = None, sni: str | None = None) -> ConnectionChange[RealityEndpoint]:
    if port is None and sni is None:
        raise ValidationError("Specify PORT or REALITY_SNI.")
    if port is None:
        purpose = "update-sni"
    elif sni is None:
        purpose = "update-port"
    else:
        purpose = "update-port-sni"
    return _update_reality(purpose, tag, port=port, sni=sni)


def update_reality_fingerprint(tag: str, fingerprint: str) -> None:
    try:
        fingerprint = connections.validate_connection_fingerprint(fingerprint)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if not fingerprint:
        raise ValidationError("FINGERPRINT must not be empty.")
    _update_reality("update-fingerprint", tag, fingerprint=fingerprint)


def _update_reality(purpose: str, tag: str, *, port: int | None = None, sni: str | None = None,
                    fingerprint: str | None = None) -> ConnectionChange[RealityEndpoint]:
    with manager_lock(purpose=purpose), ExitStack() as resources:
        config = service.load_working_config()
        try:
            inbound = xray.find_inbound_by_tag(config, tag)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        stream = inbound.get("streamSettings", {})
        if stream.get("security") != "reality":
            raise ValidationError(f"Connection is not Reality: {tag}")

        reality = stream.get("realitySettings", {})
        if port is not None:
            check_reality_port(config, tag, port)
            inbound["port"] = port
        if sni is not None:
            reality["serverNames"] = [sni]
        port = int(inbound.get("port", 443))
        sni = xray.reality_sni(reality)
        dest = xray.reality_dest(sni)
        if fingerprint is None:
            reality["dest"] = dest
            xray.ensure_api_rule_first(config)
        else:
            dest = reality.get("dest", dest)
        result = RealityEndpoint(port, sni, dest)
        env = None
        if tag == xray.INBOUND_TAG:
            env = {"PORT": str(port), "REALITY_SNI": sni, "REALITY_DEST": dest,
                   "FINGERPRINT": fingerprint if fingerprint is not None else settings.fingerprint_or_default()}

        undo = None
        applied = None

        def remember_apply(result):
            nonlocal applied
            applied = result

        def restore_connection():
            with database.report_errors(), database.write_session() as conn:
                undo.restore(conn)

        try:
            with database.report_errors(), database.write_session() as conn:
                record = repository.get_connection(conn, tag)
                updated = dict(record or {"tag": tag, "name": xray.connection_name_from_tag(tag)})
                updated.update(port=port, sni=sni, dest=dest)
                if fingerprint is not None:
                    updated["fingerprint"] = fingerprint
                previous = {"connections": {tag: record} if record is not None else {},
                            "clients": {}, "cascadeRoutes": {}}
                changed = {**previous, "connections": {tag: updated}}
                undo = repository.write_client_change(conn, changed, previous, undo_resources=resources)
            if fingerprint is None:
                applied = xray_apply.apply_config(config, on_applied=remember_apply)
        except BaseException as error:
            if undo is None:
                raise
            actions = []
            if not isinstance(error, ApplyConfigError) and applied is not None and applied.backup is not None:
                actions.append(("Конфигурация Xray", lambda: xray_apply.restore_config_backup(applied.backup)))
            actions.append(("Данные подключения SQLite", restore_connection))
            compensate(error, actions)

        # Ссылка для первого клиента строится по сохранённому состоянию, в том
        # числе при редактировании дополнительного подключения этого клиента.
        try:
            pending_fingerprint = env["FINGERPRINT"] if env else None
            _write_initial_link(config, pending_env_fingerprint=pending_fingerprint)
        finally:
            if env is not None:
                _update_connection_env(env, metadata_only=fingerprint is not None)
        return ConnectionChange(result, applied.backup if applied else None)


def update_trojan_connection(identifier, **options):
    # Повторное построение плана использует ту же проверку для того же адреса.
    port_in_use = cache(process.tcp_port_in_use)

    def edit(config, db):
        result = connections.update_trojan_connection(config, db, identifier, **options)
        if result.caddy_enabled and result.public_host != result.previous_public_host:
            caddy_sites.require_site_config_absent(result.public_host)
        if result.local_port != result.previous_local_port:
            inbound = xray.find_inbound_by_tag(config, result.tag)
            if port_in_use(result.local_port, str(inbound.get("listen") or "")):
                raise ValidationError(f"LOCAL_PORT is already in use on this server by another process: {result.local_port}")
        site = None
        if result.caddy_enabled:
            site = _site_candidate(result, trojan=True, previous_domain=result.previous_public_host)
        return _Edit(result, caddy_site=site)

    return _change("update-trojan-connection", edit, precheck_caddy=True)


def rename_connection(identifier, new_name):
    with manager_lock(purpose="rename-connection"):
        config = service.load_working_config()
        with database.report_errors(), database.write_session() as conn:
            previous = repository.list_connections(conn)
            db = {"connections": deepcopy(previous)}
            try:
                result = connections.rename_connection(config, db, identifier, new_name)
            except (OSError, ValueError, RuntimeError) as exc:
                raise ValidationError(str(exc)) from exc
            record = db["connections"][result.tag]
            if record != previous.get(result.tag):
                repository.save_connection(conn, result.tag, record)
            return result


def remove_connection(identifier, *, refresh_link=False):
    def edit(config, db):
        result = connections.remove_connection(config, db, identifier)
        deletions = repository.Deletions(
            clients=tuple(result.removed_client_names),
            credentials=tuple((name, result.tag) for name in result.detached_client_names),
            connections=(result.tag,),
        )
        return _Edit(result, deletions=deletions, env_tag=result.env_switch_tag)

    return _change("remove-connection", edit, refresh_link=refresh_link, retires_traffic=True)


def _values_for_connection(config, db, tag):
    inbound = xray.find_inbound_by_tag(config, tag)
    transport = xray.connection_settings_from_inbound(inbound)
    values = {
        "PORT": str(transport["port"]),
        "REALITY_SNI": transport["sni"],
        "REALITY_DEST": transport["dest"],
        "FINGERPRINT": connections.connection_fingerprint(config, db, tag),
        "REALITY_TRANSPORT": transport["transport"],
        "GRPC_SERVICE_NAME": "",
        "XHTTP_PATH": "",
        "XHTTP_MODE": "",
    }
    if transport["transport"] == "grpc":
        values["GRPC_SERVICE_NAME"] = transport["grpcServiceName"]
    elif transport["transport"] == "xhttp":
        values["XHTTP_PATH"] = transport["xhttpPath"]
        values["XHTTP_MODE"] = transport["xhttpMode"]
    return values


def _write_initial_link(config, db=None, *, fingerprint=None, pending_env_fingerprint=None):
    inbound = xray.find_inbound(config)
    clients = inbound.get("settings", {}).get("clients", [])
    if not clients:
        return
    name = clients[0].get("email", "starter").split("|created=", 1)[0]
    try:
        service.validate_name(name)
    except ValidationError:
        return
    if db is None:
        active_tags = {tag for tag, _, _ in credentials.active_items_for_client(config, name)}
        db = repository.load_client_link_state(name, active_tags)
    try:
        uri = "\n".join(links.client_link_lines(
            config, db, name, default_fingerprint=pending_env_fingerprint,
        )).strip()
    except (ValueError, RuntimeError, ManagerError):
        return
    if pending_env_fingerprint is not None:
        fingerprint = pending_env_fingerprint
    elif fingerprint is None:
        fingerprint = settings.server_env_value("FINGERPRINT", "chrome").lower()
    if fingerprint not in settings.FINGERPRINTS:
        fingerprint = "chrome"
    transport = xray.connection_settings_from_inbound(inbound)
    content = links.initial_reality_link_content(uri, transport, fingerprint)
    fs.replace_file(paths().initial_link, content.encode(), prepare=lambda path: path.chmod(0o600))
