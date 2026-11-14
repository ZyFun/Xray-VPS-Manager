"""Переходные команды подключений до шага 4b.

Здесь остаётся существующий порядок записи Xray, SQLite, Caddy и server.env.
"""

from contextlib import contextmanager
import copy
import subprocess

from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.clients import service as client_service
from xray_vps_manager.clients import settings as client_settings
from xray_vps_manager.commands.client_arguments import (
    validate_absolute_path,
    validate_connection_name,
    validate_connection_security,
    validate_fingerprint,
    validate_grpc_service_name,
    validate_host,
    validate_port,
    validate_reality_transport,
    validate_tls_version,
    validate_trojan_ws_path,
    validate_xhttp_extra_json,
    validate_xhttp_mode,
    validate_xhttp_path,
)
from xray_vps_manager.commands.client_output import ensure_connections
from xray_vps_manager.commands.presenters import apply_error_text, backup_text
from xray_vps_manager.core import process
from xray_vps_manager.core.errors import ApplyConfigError, ManagerError
from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.core.terminal import print_table
from xray_vps_manager.runner import error_exit, fail
from xray_vps_manager.xray import apply as xray_apply
from xray_vps_manager.xray import caddy as xray_caddy
from xray_vps_manager.xray import config as xray_config


def save_config_restart_xray_and_db(config, db, *, deletions=None):
    try:
        return client_service.apply_config_and_save_db(config, db, deletions=deletions).backup
    except ApplyConfigError as exc:
        if exc.exit_code == 130:
            raise KeyboardInterrupt from exc
        fail(apply_error_text(exc))
    except ManagerError as exc:
        error_exit(exc)


def cmd_connection_list():
    config, db = client_service.read_state()
    ensure_connections(config, db)
    headers = ["NAME", "TAG", "PROTOCOL", "SECURITY", "PORT", "SNI", "TRANSPORT", "FINGERPRINT", "CREATED"]
    try:
        rows = client_connections.connection_rows(config, db)
    except ValueError as exc:
        fail(str(exc))
    print_table(headers, rows, empty_message=None)


def cmd_connection_add(
    name,
    port_value,
    sni_value,
    fingerprint_value="chrome",
    transport_value="tcp",
    grpc_service_name="",
    xhttp_path="",
    xhttp_mode="",
    xhttp_extra_json="",
    security_value="reality",
    public_port_value="",
    install_caddy=False,
    tls_min_version="tls1.2",
    tls_max_version="tls1.2",
):
    name = validate_connection_name(name)
    security = validate_connection_security(security_value)
    port = validate_port(port_value)
    sni = validate_host(sni_value, "REALITY_SNI" if security == "reality" else "TLS_DOMAIN")
    fp = validate_fingerprint(fingerprint_value or "chrome")
    transport = validate_reality_transport(transport_value or ("xhttp" if security == "tls" else "tcp"))
    if security == "tls" and transport != "xhttp":
        fail("TLS connections support only xhttp transport.")
    grpc_service_name = validate_grpc_service_name(grpc_service_name) if transport == "grpc" else ""
    xhttp_path = validate_xhttp_path(xhttp_path) if transport == "xhttp" else ""
    xhttp_mode = validate_xhttp_mode(xhttp_mode) if transport == "xhttp" else ""
    xhttp_extra = validate_xhttp_extra_json(xhttp_extra_json) if transport == "xhttp" else {}
    public_port = validate_port(public_port_value) if public_port_value else xray_config.DEFAULT_XHTTP_TLS_PUBLIC_PORT
    tls_min_version = validate_tls_version(tls_min_version, "tls1.2")
    tls_max_version = validate_tls_version(tls_max_version, tls_min_version)
    with manager_lock(purpose="add-connection"):
        config, db = client_service.read_state()
        try:
            if security == "tls":
                tls_domain = xray_caddy.validate_domain(sni)
                if install_caddy:
                    conflicts = client_connections.public_port_conflicts(config, public_port)
                    if conflicts:
                        tags = ", ".join(inbound.get("tag") or "(no-tag)" for inbound in conflicts)
                        fail(
                            f"Caddy cannot listen on public port {public_port}: it is already used by Xray inbound(s): {tags}. "
                            "Move those connections to another port before installing Caddy."
                        )
                    xray_caddy.require_site_config_absent(tls_domain)
                result = client_connections.add_tls_xhttp_connection(
                    config,
                    db,
                    name,
                    tls_domain,
                    local_port=port,
                    public_port=public_port,
                    fingerprint_value=fp,
                    xhttp_path=xhttp_path,
                    xhttp_mode=xhttp_mode,
                    xhttp_extra=xhttp_extra,
                    tls_min_version=tls_min_version,
                    tls_max_version=tls_max_version,
                    caddy_enabled=install_caddy,
                )
            else:
                result = client_connections.add_connection(
                    config,
                    db,
                    name,
                    port,
                    sni,
                    fp,
                    transport=transport,
                    grpc_service_name=grpc_service_name,
                    xhttp_path=xhttp_path,
                    xhttp_mode=xhttp_mode,
                    xhttp_extra=xhttp_extra,
                )
        except (OSError, ValueError, RuntimeError) as exc:
            fail(str(exc))

        backup = save_config_restart_xray_and_db(config, db)
        caddy_site = None
        if result.security == "tls" and install_caddy:
            try:
                caddy_site = xray_caddy.setup_caddy_for_xhttp(
                    result.public_host,
                    result.local_port,
                    tls_min_version=result.tls_min_version,
                    tls_max_version=result.tls_max_version,
                )
            except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                fail(
                    "TLS-XHTTP connection was added, but Caddy setup failed. "
                    f"Config backup: {backup_text(backup)}. Detail: {exc}"
                )

    print(f"Added connection: {result.name}")
    print(f"Tag: {result.tag}")
    print(f"SECURITY: {result.security}")
    if result.security == "tls":
        print(f"PUBLIC_HOST: {result.public_host}")
        print(f"PUBLIC_PORT: {result.public_port}")
        print(f"LOCAL_PORT: {result.local_port}")
        print(f"FINGERPRINT: {result.fingerprint or '-'}")
        print(f"TLS_MIN_VERSION: {result.tls_min_version}")
        print(f"TLS_MAX_VERSION: {result.tls_max_version}")
        if caddy_site:
            print(f"CADDY_SITE: {caddy_site}")
    else:
        print(f"PORT: {result.port}")
        print(f"REALITY_SNI: {result.sni}")
        print(f"REALITY_DEST: {result.dest}")
        print(f"FINGERPRINT: {result.fingerprint}")
    print(f"TRANSPORT: {result.transport}")
    if result.transport == "grpc":
        print(f"GRPC_SERVICE_NAME: {result.grpc_service_name}")
    elif result.transport == "xhttp":
        print(f"XHTTP_PATH: {result.xhttp_path}")
        print(f"XHTTP_MODE: {result.xhttp_mode}")
        extra_json = xray_config.xhttp_extra_json(result.xhttp_extra)
        print("XHTTP_EXTRA: " + (extra_json if extra_json else "default"))
    print(f"Backup: {backup_text(backup)}")


def cmd_trojan_connection_add(
    name,
    port_value,
    domain_value,
    cert_file_value="",
    key_file_value="",
    fingerprint_value="chrome",
    transport_value="ws",
    ws_path="",
    public_port_value="",
    install_caddy=True,
    tls_min_version=xray_config.DEFAULT_TROJAN_TLS_MIN_VERSION,
    tls_max_version=xray_config.DEFAULT_TROJAN_TLS_MAX_VERSION,
):
    name = validate_connection_name(name)
    domain = validate_host(domain_value, "TLS_DOMAIN")
    fp = validate_fingerprint(fingerprint_value or "chrome")
    transport = (transport_value or "ws").strip().lower()
    if transport not in ("tcp", "ws"):
        fail("Trojan TRANSPORT must be tcp or ws.")
    with manager_lock(purpose="add-trojan-connection"):
        config, db = client_service.read_state()
        try:
            if transport == "ws":
                local_port = validate_port(port_value)
                public_port = validate_port(public_port_value) if public_port_value else xray_config.DEFAULT_TROJAN_TLS_PUBLIC_PORT
                path = validate_trojan_ws_path(ws_path)
                tls_min_version = validate_tls_version(tls_min_version, xray_config.DEFAULT_TROJAN_TLS_MIN_VERSION)
                tls_max_version = validate_tls_version(tls_max_version, tls_min_version)
                tls_domain = xray_caddy.validate_domain(domain)
                if install_caddy:
                    conflicts = client_connections.public_port_conflicts(config, public_port)
                    if conflicts:
                        tags = ", ".join(inbound.get("tag") or "(no-tag)" for inbound in conflicts)
                        fail(
                            f"Caddy cannot listen on public port {public_port}: it is already used by Xray inbound(s): {tags}. "
                            "Move those connections to another port before installing Caddy."
                        )
                    xray_caddy.require_site_config_absent(tls_domain)
                result = client_connections.add_trojan_caddy_connection(
                    config,
                    db,
                    name,
                    tls_domain,
                    local_port=local_port,
                    public_port=public_port,
                    fingerprint_value=fp,
                    ws_path=path,
                    tls_min_version=tls_min_version,
                    tls_max_version=tls_max_version,
                    caddy_enabled=install_caddy,
                )
            else:
                port = validate_port(port_value)
                cert_file = validate_absolute_path(cert_file_value, "CERT_FILE")
                key_file = validate_absolute_path(key_file_value, "KEY_FILE")
                result = client_connections.add_trojan_tls_connection(
                    config,
                    db,
                    name,
                    port,
                    domain,
                    cert_file,
                    key_file,
                    fp,
                )
        except (OSError, ValueError, RuntimeError) as exc:
            fail(str(exc))

        backup = save_config_restart_xray_and_db(config, db)
        caddy_site = None
        if result.transport == "ws" and install_caddy:
            try:
                caddy_site = xray_caddy.setup_caddy_for_trojan_ws(
                    result.public_host,
                    result.local_port,
                    result.ws_path,
                    tls_min_version=result.tls_min_version,
                    tls_max_version=result.tls_max_version,
                )
            except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                fail(
                    "Trojan Caddy connection was added, but Caddy setup failed. "
                    f"Config backup: {backup_text(backup)}. Detail: {exc}"
                )

    print(f"Added Trojan connection: {result.name}")
    print(f"Tag: {result.tag}")
    print("PROTOCOL: trojan")
    print(f"SECURITY: {result.security}")
    print(f"TLS_DOMAIN: {result.sni}")
    print(f"FINGERPRINT: {result.fingerprint}")
    print(f"TRANSPORT: {result.transport}")
    if result.transport == "ws":
        print(f"PUBLIC_PORT: {result.public_port}")
        print(f"LOCAL_PORT: {result.local_port}")
        print(f"WS_PATH: {result.ws_path}")
        print(f"TLS_MIN_VERSION: {result.tls_min_version}")
        print(f"TLS_MAX_VERSION: {result.tls_max_version}")
        if caddy_site:
            print(f"CADDY_SITE: {caddy_site}")
    else:
        print(f"PORT: {result.port}")
        print(f"CERT_FILE: {result.cert_file}")
        print(f"KEY_FILE: {result.key_file}")
    print(f"Backup: {backup_text(backup)}")


def cmd_connection_transport(identifier, transport_value, grpc_service_name="", xhttp_path="", xhttp_mode="", xhttp_extra_json=None):
    transport = validate_reality_transport(transport_value)
    grpc_service_name = validate_grpc_service_name(grpc_service_name) if transport == "grpc" else ""
    xhttp_path = validate_xhttp_path(xhttp_path) if transport == "xhttp" else ""
    xhttp_mode = validate_xhttp_mode(xhttp_mode) if transport == "xhttp" else ""
    xhttp_extra = validate_xhttp_extra_json(xhttp_extra_json) if transport == "xhttp" and xhttp_extra_json is not None else None
    with manager_lock(purpose="connection-transport"):
        config, db = client_service.read_state()
        try:
            result = client_connections.update_connection_transport(
                config,
                db,
                identifier,
                transport,
                grpc_service_name=grpc_service_name,
                xhttp_path=xhttp_path,
                xhttp_mode=xhttp_mode,
                xhttp_extra=xhttp_extra,
            )
        except (ValueError, RuntimeError) as exc:
            fail(str(exc))

        backup = save_config_restart_xray_and_db(config, db)
        if result.env_update is not None:
            client_settings.save_server_env_values(result.env_update)

    print(f"Connection: {result.display_name} ({result.tag})")
    print(f"TRANSPORT: {result.transport}")
    if result.transport == "grpc":
        print(f"GRPC_SERVICE_NAME: {result.grpc_service_name}")
    elif result.transport == "xhttp":
        print(f"XHTTP_PATH: {result.xhttp_path}")
        print(f"XHTTP_MODE: {result.xhttp_mode}")
        extra_json = xray_config.xhttp_extra_json(result.xhttp_extra)
        print("XHTTP_EXTRA: " + (extra_json if extra_json else "default"))
    print("Updated clients: " + (", ".join(result.updated_clients or []) if result.updated_clients else "none"))
    print(f"Backup: {backup_text(backup)}")
    print("Выведи клиентам новые ссылки через xray-client link NAME.")


def cmd_connection_xhttp_extra(identifier, xhttp_extra_json=None, clear=False):
    xhttp_extra = {} if clear else validate_xhttp_extra_json(xhttp_extra_json)
    with manager_lock(purpose="connection-xhttp-extra"):
        config, db = client_service.read_state()
        try:
            result = client_connections.update_connection_xhttp_extra(config, db, identifier, xhttp_extra)
        except (ValueError, RuntimeError) as exc:
            fail(str(exc))

        backup = save_config_restart_xray_and_db(config, db)
    extra_json = xray_config.xhttp_extra_json(result.xhttp_extra)
    print(f"Connection: {result.display_name} ({result.tag})")
    print("XHTTP_EXTRA: " + (extra_json if extra_json else "default"))
    print(f"Backup: {backup_text(backup)}")
    print("Выведи клиентам новые ссылки через xray-client link NAME.")


def update_trojan_caddy_site(result):
    if not result.caddy_enabled:
        return None
    if result.public_host == result.previous_public_host:
        return xray_caddy.update_site_config(
            result.public_host,
            result.local_port,
            tls_min_version=result.tls_min_version,
            tls_max_version=result.tls_max_version,
            upstream_transport="http",
            route_path=result.ws_path,
        )

    old_path = xray_caddy.site_config_path(result.previous_public_host)
    new_path = xray_caddy.site_config_path(result.public_host)
    old_backup = xray_caddy.backup_file(old_path)
    new_backup = xray_caddy.backup_file(new_path)
    try:
        xray_caddy.write_site_config(
            result.public_host,
            result.local_port,
            tls_min_version=result.tls_min_version,
            tls_max_version=result.tls_max_version,
            upstream_transport="http",
            route_path=result.ws_path,
        )
        if old_path.exists():
            old_path.unlink()
        xray_caddy.validate_and_reload_caddy()
    except Exception:
        xray_caddy.restore_file(old_backup, old_path)
        xray_caddy.restore_file(new_backup, new_path)
        try:
            xray_caddy.validate_and_reload_caddy()
        except Exception:
            pass
        raise
    xray_caddy.prune_site_backups(old_path, new_path, protect=(old_backup, new_backup))
    return xray_caddy.SiteWriteResult(new_path, old_backup)


def cmd_trojan_connection_update(
    identifier,
    domain_value=None,
    local_port_value=None,
    public_port_value=None,
    ws_path_value=None,
    fingerprint_value=None,
    tls_min_version_value=None,
    tls_max_version_value=None,
):
    domain = xray_caddy.validate_domain(domain_value) if domain_value else None
    local_port = validate_port(local_port_value) if local_port_value else None
    public_port = validate_port(public_port_value) if public_port_value else None
    ws_path = validate_trojan_ws_path(ws_path_value) if ws_path_value else None
    fp = validate_fingerprint(fingerprint_value) if fingerprint_value else None
    tls_min = (
        validate_tls_version(tls_min_version_value, xray_config.DEFAULT_TROJAN_TLS_MIN_VERSION)
        if tls_min_version_value
        else None
    )
    tls_max = (
        validate_tls_version(tls_max_version_value, tls_min or xray_config.DEFAULT_TROJAN_TLS_MAX_VERSION)
        if tls_max_version_value
        else None
    )

    with manager_lock(purpose="update-trojan-connection"):
        config, db = client_service.read_state()
        original_db = copy.deepcopy(db)
        try:
            result = client_connections.update_trojan_connection(
                config,
                db,
                identifier,
                domain=domain,
                local_port=local_port,
                public_port=public_port,
                ws_path=ws_path,
                fingerprint_value=fp,
                tls_min_version=tls_min,
                tls_max_version=tls_max,
            )
            if result.caddy_enabled and result.public_host != result.previous_public_host:
                xray_caddy.require_site_config_absent(result.public_host)
        except (OSError, ValueError, RuntimeError) as exc:
            fail(str(exc))
        if result.local_port != result.previous_local_port:
            # Порт мог занять не Xray-процесс: Xray не стартует на нём, поэтому отказ до записи config.json (XR-REV-010).
            listen = str(xray_config.find_inbound_by_tag(config, result.tag).get("listen") or "")
            if process.tcp_port_in_use(result.local_port, listen):
                fail(f"LOCAL_PORT is already in use on this server by another process: {result.local_port}")

        backup = save_config_restart_xray_and_db(config, db)
        caddy_result = None
        try:
            caddy_result = update_trojan_caddy_site(result)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            xray_apply.restore_config_backup(backup)
            client_repository.restore_after_update(db, original_db)
            fail(
                "Trojan connection was updated, but Caddy update failed. "
                f"Rolled back Xray config and database. Detail: {exc}"
            )

    print(f"Trojan connection updated: {result.display_name}")
    print(f"Tag: {result.tag}")
    print(f"TLS_DOMAIN: {result.public_host}")
    print(f"PUBLIC_PORT: {result.public_port}")
    print(f"LOCAL_PORT: {result.local_port}")
    print(f"WS_PATH: {result.ws_path}")
    print(f"FINGERPRINT: {result.fingerprint or '-'}")
    print(f"TLS_MIN_VERSION: {result.tls_min_version}")
    print(f"TLS_MAX_VERSION: {result.tls_max_version}")
    if caddy_result:
        print(f"CADDY_SITE: {caddy_result.path}")
    elif result.caddy_enabled:
        print("CADDY_SITE: not updated")
    else:
        print("CADDY_SITE: disabled for this connection")
    print(f"Backup: {backup_text(backup)}")
    print("Выведи клиентам новые Trojan-ссылки через xray-client link NAME: параметры подключения изменились.")


def cmd_connection_rename(identifier, new_name):
    new_name = validate_connection_name(new_name)
    with manager_lock(purpose="rename-connection"):
        config, db = client_service.read_state()
        try:
            result = client_connections.rename_connection(config, db, identifier, new_name)
        except ValueError as exc:
            fail(str(exc))
        client_repository.save_db(db)
    print(f"Connection renamed: {result.old_name} -> {result.new_name}")
    print(f"Tag: {result.tag}")
    print("Xray restart is not required.")


def cmd_connection_remove(identifier):
    with manager_lock(purpose="remove-connection"):
        config, db = client_service.read_state()
        try:
            result = client_connections.remove_connection(config, db, identifier)
        except ValueError as exc:
            fail(str(exc))

        deletions = client_repository.Deletions(
            clients=tuple(result.removed_client_names),
            credentials=tuple((name, result.tag) for name in result.detached_client_names),
            connections=(result.tag,),
        )
        backup = save_config_restart_xray_and_db(config, db, deletions=deletions)
        if result.env_update is not None:
            client_settings.save_server_env_values(result.env_update)

    print(f"Removed connection: {result.display_name}")
    print(f"Tag: {result.tag}")
    print("Removed clients: " + (", ".join(result.removed_client_names) if result.removed_client_names else "none"))
    if result.removed_client_names:
        print("Removed traffic history for deleted clients.")
    if result.detached_client_names:
        print("Kept clients with other connections: " + ", ".join(result.detached_client_names))
    if result.env_switch_tag:
        print(f"server.env switched to connection: {result.env_switch_name} ({result.env_switch_tag})")
    print(f"Backup: {backup_text(backup)}")
