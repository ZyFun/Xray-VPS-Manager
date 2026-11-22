"""Аргументы и вывод команд управления подключениями."""

from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import connections_service, service as client_service
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
from xray_vps_manager.commands.client_output import client_change_errors, ensure_connections
from xray_vps_manager.commands.presenters import backup_text
from xray_vps_manager.core.terminal import print_table
from xray_vps_manager.runner import fail
from xray_vps_manager.caddy import model as caddy_model
from xray_vps_manager.xray import config as xray_config


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
    with client_change_errors():
        if security == "tls":
            change = connections_service.add_tls_xhttp_connection(
                name, sni, local_port=port, public_port=public_port, fingerprint_value=fp,
                xhttp_path=xhttp_path, xhttp_mode=xhttp_mode, xhttp_extra=xhttp_extra,
                tls_min_version=tls_min_version, tls_max_version=tls_max_version, install_caddy=install_caddy,
            )
        else:
            change = connections_service.add_connection(
                name, port, sni, fp, transport=transport, grpc_service_name=grpc_service_name,
                xhttp_path=xhttp_path, xhttp_mode=xhttp_mode, xhttp_extra=xhttp_extra,
            )
    print_added_connection(change)


def print_added_connection(change):
    result, backup, caddy_site = change.result, change.backup, change.site

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
    with client_change_errors():
        if transport == "ws":
            local_port = validate_port(port_value)
            public_port = validate_port(public_port_value) if public_port_value else xray_config.DEFAULT_TROJAN_TLS_PUBLIC_PORT
            path = validate_trojan_ws_path(ws_path)
            tls_min_version = validate_tls_version(tls_min_version, xray_config.DEFAULT_TROJAN_TLS_MIN_VERSION)
            tls_max_version = validate_tls_version(tls_max_version, tls_min_version)
            change = connections_service.add_trojan_caddy_connection(
                name, domain, local_port=local_port, public_port=public_port, fingerprint_value=fp,
                ws_path=path, tls_min_version=tls_min_version, tls_max_version=tls_max_version,
                install_caddy=install_caddy,
            )
        else:
            port = validate_port(port_value)
            cert_file = validate_absolute_path(cert_file_value, "CERT_FILE")
            key_file = validate_absolute_path(key_file_value, "KEY_FILE")
            change = connections_service.add_trojan_tls_connection(name, port, domain, cert_file, key_file, fp)
    print_added_trojan_connection(change)


def print_added_trojan_connection(change):
    result, backup, caddy_site = change.result, change.backup, change.site

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


def cmd_connection_transport(
    identifier, transport_value, grpc_service_name="", xhttp_path="", xhttp_mode="", xhttp_extra_json=None,
):
    transport = validate_reality_transport(transport_value)
    grpc_service_name = validate_grpc_service_name(grpc_service_name) if transport == "grpc" else ""
    xhttp_path = validate_xhttp_path(xhttp_path) if transport == "xhttp" else ""
    xhttp_mode = validate_xhttp_mode(xhttp_mode) if transport == "xhttp" else ""
    xhttp_extra = validate_xhttp_extra_json(xhttp_extra_json) if transport == "xhttp" and xhttp_extra_json is not None else None
    with client_change_errors():
        change = connections_service.update_transport(
            identifier, transport, grpc_service_name=grpc_service_name, xhttp_path=xhttp_path,
            xhttp_mode=xhttp_mode, xhttp_extra=xhttp_extra,
        )
    print_transport_change(change)


def print_transport_change(change):
    result, backup = change.result, change.backup

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
    with client_change_errors():
        change = connections_service.update_xhttp_extra(identifier, xhttp_extra)
    print_xhttp_extra_change(change)


def print_xhttp_extra_change(change):
    result, backup = change.result, change.backup
    extra_json = xray_config.xhttp_extra_json(result.xhttp_extra)
    print(f"Connection: {result.display_name} ({result.tag})")
    print("XHTTP_EXTRA: " + (extra_json if extra_json else "default"))
    print(f"Backup: {backup_text(backup)}")
    print("Выведи клиентам новые ссылки через xray-client link NAME.")


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
    domain = caddy_model.validate_domain(domain_value) if domain_value else None
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

    with client_change_errors():
        change = connections_service.update_trojan_connection(
            identifier, domain=domain, local_port=local_port, public_port=public_port,
            ws_path=ws_path, fingerprint_value=fp, tls_min_version=tls_min, tls_max_version=tls_max,
        )
    print_trojan_connection_change(change)


def print_trojan_connection_change(change):
    result, backup, caddy_site = change.result, change.backup, change.site

    print(f"Trojan connection updated: {result.display_name}")
    print(f"Tag: {result.tag}")
    print(f"TLS_DOMAIN: {result.public_host}")
    print(f"PUBLIC_PORT: {result.public_port}")
    print(f"LOCAL_PORT: {result.local_port}")
    print(f"WS_PATH: {result.ws_path}")
    print(f"FINGERPRINT: {result.fingerprint or '-'}")
    print(f"TLS_MIN_VERSION: {result.tls_min_version}")
    print(f"TLS_MAX_VERSION: {result.tls_max_version}")
    if caddy_site:
        print(f"CADDY_SITE: {caddy_site}")
    elif result.caddy_enabled:
        print("CADDY_SITE: not updated")
    else:
        print("CADDY_SITE: disabled for this connection")
    print(f"Backup: {backup_text(backup)}")
    print("Выведи клиентам новые Trojan-ссылки через xray-client link NAME: параметры подключения изменились.")


def cmd_connection_rename(identifier, new_name):
    new_name = validate_connection_name(new_name)
    with client_change_errors():
        result = connections_service.rename_connection(identifier, new_name)
    print_renamed_connection(result)


def print_renamed_connection(result):
    print(f"Connection renamed: {result.old_name} -> {result.new_name}")
    print(f"Tag: {result.tag}")
    print("Xray restart is not required.")


def cmd_connection_remove(identifier):
    with client_change_errors():
        change = connections_service.remove_connection(identifier)
    print_removed_connection(change)


def print_removed_connection(change):
    result, backup = change.result, change.backup

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
