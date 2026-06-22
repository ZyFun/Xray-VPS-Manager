#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from xray_vps_manager.clients import access as client_access
from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.clients import limits as client_limits
from xray_vps_manager.clients import listing as client_listing
from xray_vps_manager.clients import links as client_links
from xray_vps_manager.clients import payments as client_payments
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.clients import runtime as client_runtime
from xray_vps_manager.clients import settings as client_settings
from xray_vps_manager.clients import status as client_status
from xray_vps_manager.core.process import restart_systemd_unit
from xray_vps_manager.core.time import utc_stamp
from xray_vps_manager.core.terminal import print_table
from xray_vps_manager.traffic import formatting as traffic_formatting
from xray_vps_manager.traffic import reports as traffic_reports
from xray_vps_manager.traffic import repository as traffic_repository
from xray_vps_manager.xray import cascade as cascade_config
from xray_vps_manager.xray import caddy as xray_caddy
from xray_vps_manager.xray import client_routes
from xray_vps_manager.xray import config as xray_config

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
STATS_SERVER = "127.0.0.1:10085"
TRAFFIC_SYNC = Path("/usr/local/sbin/xray-traffic-sync")
XRAY_TELEGRAM = Path("/usr/local/sbin/xray-telegram")
CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
CONNECTION_NAME_RE = re.compile(r"^[^\r\n|]{1,64}$")
FINGERPRINTS = {
    "chrome",
    "firefox",
    "safari",
    "ios",
    "android",
    "edge",
    "360",
    "qq",
    "random",
    "randomized",
}
GREEN = "\033[92m"
RED = "\033[31m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def run(command):
    subprocess.run(command, check=True)


def run_capture(command, timeout=5):
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def parse_access_days(value):
    try:
        return client_access.parse_access_days(value)
    except ValueError as exc:
        die(str(exc))


def parse_extend_days(value):
    try:
        return client_access.parse_extend_days(value)
    except ValueError as exc:
        die(str(exc))


def normalize_payment_type(value):
    try:
        return client_payments.normalize_payment_type(value)
    except ValueError as exc:
        die(str(exc))


def set_entry_payment_type(entry, value):
    try:
        return client_payments.set_payment_type(entry, value)
    except ValueError as exc:
        die(str(exc))


def parse_limit_gb(value):
    try:
        return client_limits.parse_limit_gb(value)
    except ValueError as exc:
        die(str(exc))


def validate_limit_period(value):
    try:
        return client_limits.validate_limit_period(value)
    except ValueError as exc:
        die(str(exc))


def set_client_traffic_limit(entry, period, limit_bytes):
    try:
        return client_limits.set_client_traffic_limit(entry, period, limit_bytes, utc_stamp)
    except ValueError as exc:
        die(str(exc))


def clear_client_traffic_limit(entry):
    try:
        return client_limits.clear_client_traffic_limit(entry, utc_stamp)
    except ValueError as exc:
        die(str(exc))


def prompt_access_days():
    if not sys.stdin.isatty():
        return None
    print()
    print("ACCESS_DAYS: количество календарных дней доступа.")
    print(f"Клиент будет автоматически отключён в 00:00 по часовому поясу менеджера: {client_settings.manager_timezone_label()}.")
    print("Нажми Enter или введи 0, чтобы добавить клиента бессрочно.")
    value = input("ACCESS_DAYS [бессрочно]: ").strip()
    return parse_access_days(value)


def load_config():
    try:
        return xray_config.load_config(CONFIG_PATH)
    except FileNotFoundError as exc:
        die(str(exc))


def save_config(config):
    return xray_config.save_config(config, CONFIG_PATH)


def load_db():
    try:
        return client_repository.load_db_sql()
    except ValueError as exc:
        die(str(exc))


def load_db_readonly():
    try:
        return client_repository.load_db_sql_result()
    except ValueError as exc:
        die(str(exc))


def save_db(db):
    client_repository.save_db(db)


def restart_xray_with_config_test():
    run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
    restart_systemd_unit("xray")


def restore_config_backup(backup):
    shutil.copy2(backup, CONFIG_PATH)
    shutil.chown(CONFIG_PATH, user="root", group="xray")
    os.chmod(CONFIG_PATH, 0o640)
    restart_systemd_unit("xray")


def restore_config_file(backup):
    shutil.copy2(backup, CONFIG_PATH)
    shutil.chown(CONFIG_PATH, user="root", group="xray")
    os.chmod(CONFIG_PATH, 0o640)


def save_config_restart_xray_and_db(config, db):
    client_routes.ensure_all_client_route_config(config, db)
    backup = save_config(config)
    try:
        restart_xray_with_config_test()
        save_db(db)
    except subprocess.CalledProcessError:
        restore_config_backup(backup)
        die(f"New config failed. Restored backup: {backup}")
    return backup


def normalize_access_deadlines(tz):
    db = load_db()
    changed = 0
    for entry in client_repository.db_clients(db).values():
        expires_at = entry.get("expiresAt", "")
        normalized = client_access.access_deadline_at_midnight(expires_at, tz)
        if normalized and normalized != expires_at:
            entry["expiresAt"] = normalized
            changed += 1
    if changed:
        save_db(db)
    return changed


def save_server_env_values(values):
    client_settings.save_server_env_values(values, SERVER_ENV_PATH)


def ensure_connections(config, db):
    try:
        client_connections.ensure_connections(config, db)
    except ValueError as exc:
        die(str(exc))


def connection_entry(config, db, tag):
    try:
        return client_connections.connection_entry(config, db, tag)
    except ValueError as exc:
        die(str(exc))


def connection_display_name(config, db, tag):
    try:
        return client_connections.connection_display_name(config, db, tag)
    except ValueError as exc:
        die(str(exc))


def connection_rows(config, db):
    try:
        return client_connections.connection_rows(config, db)
    except ValueError as exc:
        die(str(exc))


def validate_name(name):
    if not CLIENT_NAME_RE.match(name):
        die("Client name must be 1-64 chars: A-Z a-z 0-9 _ . @ -")


def validate_connection_name(name):
    if not CONNECTION_NAME_RE.match((name or "").strip()):
        die("Connection name must be 1-64 chars and must not contain new lines or |")
    return name.strip()


def validate_port(value):
    if not re.fullmatch(r"[0-9]+", value or ""):
        die("PORT must be a number from 1 to 65535.")
    port = int(value, 10)
    if port < 1 or port > 65535:
        die("PORT must be a number from 1 to 65535.")
    return port


def validate_host(value, label="SNI"):
    if not value or "/" in value or ":" in value or not re.fullmatch(r"[A-Za-z0-9.-]+", value):
        die(f"{label} must be a domain without https://, path, or port.")
    return value


def validate_fingerprint(value):
    value = (value or "").strip().lower()
    if value not in FINGERPRINTS:
        die("FINGERPRINT must be one of: " + ", ".join(sorted(FINGERPRINTS)))
    return value


def validate_reality_transport(value):
    try:
        return xray_config.normalize_reality_transport(value)
    except ValueError as exc:
        die(str(exc))


def validate_connection_security(value):
    security = (value or "reality").strip().lower()
    if security not in ("reality", "tls"):
        die("SECURITY must be reality or tls.")
    return security


def validate_tls_version(value, default="tls1.2"):
    try:
        return xray_caddy.normalize_tls_version(value, default)
    except ValueError as exc:
        die(str(exc))


def validate_grpc_service_name(value):
    try:
        return xray_config.normalize_grpc_service_name(value)
    except ValueError as exc:
        die(str(exc))


def validate_xhttp_path(value):
    try:
        return xray_config.normalize_xhttp_path(value)
    except ValueError as exc:
        die(str(exc))


def validate_xhttp_mode(value):
    try:
        return xray_config.normalize_xhttp_mode(value)
    except ValueError as exc:
        die(str(exc))


def validate_xhttp_extra_json(value):
    try:
        return xray_config.normalize_xhttp_extra_json(value)
    except ValueError as exc:
        die(str(exc))


def color_label(value, text):
    if value == "free":
        return f"{GREEN}{text}{RESET}"
    if value == "paid":
        return f"{YELLOW}{text}{RESET}"
    if not sys.stdout.isatty():
        return text
    if value in ("enabled", "online", "бессрочно"):
        return f"{GREEN}{text}{RESET}"
    if value in ("disabled", "offline"):
        return f"{RED}{text}{RESET}"
    return text


def normalize_timezone(value):
    try:
        return client_settings.normalize_timezone(value)
    except ValueError as exc:
        die(str(exc))


def link_for(config, client_id, name, connection_tag=None, db=None):
    try:
        return client_links.link_for(config, client_id, name, connection_tag=connection_tag, db=db, db_loader=load_db)
    except (RuntimeError, ValueError) as exc:
        die(str(exc))


def query_user_stats():
    try:
        result = run_capture(
            [
                "/usr/local/bin/xray",
                "api",
                "statsquery",
                f"--server={STATS_SERVER}",
                "-pattern",
                "user>>>",
            ],
            timeout=4,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    stats = {}
    for name, value in re.findall(r'name:\s*"([^"]+)".*?value:\s*([0-9]+)', result.stdout, re.S):
        stats[name] = int(value)
    return stats


def sync_traffic():
    if TRAFFIC_SYNC.exists():
        run_capture([str(TRAFFIC_SYNC), "--quiet"], timeout=8)


def notify_access_updated(name):
    if not XRAY_TELEGRAM.exists():
        return
    result = run_capture([str(XRAY_TELEGRAM), "notify-access", name, "--quiet"], timeout=20)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        print(f"WARN: Telegram access notification failed: {detail}", file=sys.stderr)


def print_payment_summary():
    if not XRAY_TELEGRAM.exists():
        return
    result = run_capture([str(XRAY_TELEGRAM), "payment-amount"], timeout=10)
    if result.returncode == 0 and result.stdout.strip():
        print(result.stdout.strip())
    elif result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        print(f"WARN: Payment summary unavailable: {detail}", file=sys.stderr)


def resolve_cascade_route_tag(config, db, value):
    raw = str(value or "").strip()
    if not raw:
        die("Cascade route is required.")
    options = client_routes.route_options(db, config)
    by_tag = {item["tag"]: item for item in options}
    if raw in by_tag:
        return raw
    try:
        tag = cascade_config.cascade_tag(raw)
    except ValueError:
        tag = ""
    if tag and tag in by_tag:
        return tag
    country_matches = [
        item["tag"]
        for item in options
        if str(item.get("country") or "").casefold() == raw.casefold()
        or str(item.get("display") or "").casefold() == raw.casefold()
    ]
    if len(country_matches) == 1:
        return country_matches[0]
    if len(country_matches) > 1:
        die("Country matches more than one cascade route. Use cascade tag.")
    die(f"Cascade route not found: {raw}")


def print_route_options(config, db):
    rows = []
    active = cascade_config.active_cascade_tag(config)
    for item in client_routes.route_options(db, config):
        tag = item["tag"]
        rows.append(
            [
                item.get("display") or "-",
                tag,
                "yes" if tag == active else "-",
            ]
        )
    print_table(["COUNTRY", "TAG", "GLOBAL ACTIVE"], rows, empty_message="Cascade routes are not configured.")


def load_traffic_db():
    return traffic_repository.load_traffic_db_for_read()


def load_traffic_db_readonly():
    return traffic_repository.load_traffic_db_for_read()


def remove_traffic_clients(names):
    return traffic_repository.remove_traffic_clients(names)


def format_traffic(value):
    return traffic_formatting.format_traffic(value, none_label="n/a")


def parse_date_value(value, label="DATE"):
    try:
        return date.fromisoformat(value)
    except ValueError:
        die(f"{label} must be in YYYY-MM-DD format.")


def parse_month_value(value=None):
    value = value or client_access.local_now().strftime("%Y-%m")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        die("MONTH must be in YYYY-MM format.")
    year, month = (int(part, 10) for part in value.split("-", 1))
    if month < 1 or month > 12:
        die("MONTH must be in YYYY-MM format.")
    return f"{year:04d}-{month:02d}"


def traffic_limit_status(db_entry, traffic_db_entry, now=None):
    try:
        return client_limits.traffic_limit_status(db_entry, traffic_db_entry, now)
    except ValueError as exc:
        die(str(exc))


def print_client_table(rows):
    headers = ["NAME", "STATUS", "PAYMENT", "COUNTRY", "ONLINE", "IN", "OUT", "TOTAL", "TRAFFIC UPDATED", "LIMIT", "LAST ONLINE", "ACCESS UNTIL", "CREATED"]
    color_columns = {headers.index("STATUS"), headers.index("PAYMENT"), headers.index("ONLINE"), headers.index("ACCESS UNTIL")}
    print_table(headers, rows, empty_message=None, color_columns=color_columns, colorizer=color_label)


def print_connection_title(config, db, tag):
    entry = connection_entry(config, db, tag)
    name = entry.get("name") or xray_config.connection_name_from_tag(tag)
    port = entry.get("port", "")
    sni = entry.get("sni", "")
    security = entry.get("security") or "reality"
    print()
    print(f"Connection: {name}  |  SECURITY={security}  |  PORT={port}  |  SNI={sni}  |  TAG={tag}")


def cmd_connection_list():
    config = load_config()
    read_result = load_db_readonly()
    db = read_result.db
    ensure_connections(config, db)
    if read_result.source == "json":
        save_db(db)
    headers = ["NAME", "TAG", "SECURITY", "PORT", "SNI", "TRANSPORT", "FINGERPRINT", "CREATED"]
    print_table(headers, connection_rows(config, db), empty_message=None)


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
        die("TLS connections support only xhttp transport.")
    grpc_service_name = validate_grpc_service_name(grpc_service_name) if transport == "grpc" else ""
    xhttp_path = validate_xhttp_path(xhttp_path) if transport == "xhttp" else ""
    xhttp_mode = validate_xhttp_mode(xhttp_mode) if transport == "xhttp" else ""
    xhttp_extra = validate_xhttp_extra_json(xhttp_extra_json) if transport == "xhttp" else {}
    public_port = validate_port(public_port_value) if public_port_value else xray_config.DEFAULT_XHTTP_TLS_PUBLIC_PORT
    tls_min_version = validate_tls_version(tls_min_version, "tls1.2")
    tls_max_version = validate_tls_version(tls_max_version, tls_min_version)
    config = load_config()
    db = load_db()
    try:
        if security == "tls":
            if install_caddy:
                conflicts = client_connections.public_port_conflicts(config, public_port)
                if conflicts:
                    tags = ", ".join(inbound.get("tag") or "(no-tag)" for inbound in conflicts)
                    die(
                        f"Caddy cannot listen on public port {public_port}: it is already used by Xray inbound(s): {tags}. "
                        "Move those connections to another port before installing Caddy."
                    )
            result = client_connections.add_tls_xhttp_connection(
                config,
                db,
                name,
                xray_caddy.validate_domain(sni),
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
    except (ValueError, RuntimeError) as exc:
        die(str(exc))

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
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            die(
                "TLS-XHTTP connection was added, but Caddy setup failed. "
                f"Config backup: {backup}. Detail: {exc}"
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
    print(f"Backup: {backup}")


def cmd_connection_transport(identifier, transport_value, grpc_service_name="", xhttp_path="", xhttp_mode="", xhttp_extra_json=None):
    transport = validate_reality_transport(transport_value)
    grpc_service_name = validate_grpc_service_name(grpc_service_name) if transport == "grpc" else ""
    xhttp_path = validate_xhttp_path(xhttp_path) if transport == "xhttp" else ""
    xhttp_mode = validate_xhttp_mode(xhttp_mode) if transport == "xhttp" else ""
    xhttp_extra = validate_xhttp_extra_json(xhttp_extra_json) if transport == "xhttp" and xhttp_extra_json is not None else None
    config = load_config()
    db = load_db()
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
        die(str(exc))

    backup = save_config_restart_xray_and_db(config, db)
    if result.env_update is not None:
        save_server_env_values(result.env_update)

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
    print(f"Backup: {backup}")
    print("Выведи клиентам новые ссылки через xray-client link NAME.")


def cmd_connection_xhttp_extra(identifier, xhttp_extra_json=None, clear=False):
    xhttp_extra = {} if clear else validate_xhttp_extra_json(xhttp_extra_json)
    config = load_config()
    db = load_db()
    try:
        result = client_connections.update_connection_xhttp_extra(config, db, identifier, xhttp_extra)
    except (ValueError, RuntimeError) as exc:
        die(str(exc))

    backup = save_config_restart_xray_and_db(config, db)
    extra_json = xray_config.xhttp_extra_json(result.xhttp_extra)
    print(f"Connection: {result.display_name} ({result.tag})")
    print("XHTTP_EXTRA: " + (extra_json if extra_json else "default"))
    print(f"Backup: {backup}")
    print("Выведи клиентам новые ссылки через xray-client link NAME.")


def cmd_connection_rename(identifier, new_name):
    new_name = validate_connection_name(new_name)
    config = load_config()
    db = load_db()
    try:
        result = client_connections.rename_connection(config, db, identifier, new_name)
    except ValueError as exc:
        die(str(exc))
    save_db(db)
    print(f"Connection renamed: {result.old_name} -> {result.new_name}")
    print(f"Tag: {result.tag}")
    print("Xray restart is not required.")


def cmd_connection_remove(identifier):
    config = load_config()
    db = load_db()
    try:
        result = client_connections.remove_connection(config, db, identifier)
    except ValueError as exc:
        die(str(exc))

    backup = save_config_restart_xray_and_db(config, db)
    if result.env_update is not None:
        save_server_env_values(result.env_update)
    traffic_removed = remove_traffic_clients(result.removed_client_names)

    print(f"Removed connection: {result.display_name}")
    print(f"Tag: {result.tag}")
    print("Removed clients: " + (", ".join(result.removed_client_names) if result.removed_client_names else "none"))
    if traffic_removed:
        print("Removed traffic history for deleted clients.")
    if result.env_switch_tag:
        print(f"server.env switched to connection: {result.env_switch_name} ({result.env_switch_tag})")
    print(f"Backup: {backup}")


def cmd_list():
    config = load_config()
    read_result = load_db_readonly()
    db = read_result.db
    ensure_connections(config, db)
    if read_result.source == "json":
        save_db(db)
    rows = client_listing.client_rows(config, db)
    if not rows:
        print("No clients.")
        return
    sync_traffic()
    traffic_db = load_traffic_db_readonly()
    stats = query_user_stats()
    display_timezone = client_settings.manager_timezone()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["connection"], []).append(row)

    for tag in client_repository.db_connections(db):
        group_rows = grouped.get(tag, [])
        if not group_rows:
            continue
        print_connection_title(config, db, tag)
        table_rows = []
        for row in group_rows:
            incoming, outgoing = client_runtime.traffic_for(traffic_db, stats, row)
            total = None if incoming is None or outgoing is None else incoming + outgoing
            online, last_online = client_runtime.online_state(row, traffic_db, display_timezone)
            db_entry = client_repository.db_clients(db).get(row["name"], {})
            traffic_updated = client_runtime.traffic_updated_at(row, traffic_db, display_timezone)
            table_rows.append(
                [
                    row["name"],
                    row["status"],
                    row["paymentType"],
                    row["cascade"],
                    online,
                    format_traffic(incoming),
                    format_traffic(outgoing),
                    format_traffic(total),
                    traffic_updated,
                    client_limits.format_traffic_limit(db_entry),
                    last_online,
                    client_access.format_access_until(row["expiresAt"]),
                    row["created"],
                ]
            )
        print_client_table(table_rows)


def known_for_traffic_report(config, db, traffic_db, name):
    if name in client_repository.db_clients(db):
        return True
    if name in traffic_db.get("clients", {}):
        return True
    return any(row["name"] == name for row in client_listing.client_rows(config, db))


def traffic_report_context(name):
    validate_name(name)
    sync_traffic()
    config = load_config()
    db = load_db_readonly().db
    ensure_connections(config, db)
    traffic_db = load_traffic_db_readonly()
    if not known_for_traffic_report(config, db, traffic_db, name):
        die(f"Client not found: {name}")
    return config, db, traffic_db, traffic_repository.traffic_entry(traffic_db, name)


def cmd_traffic_summary(month_value=None):
    month_key = parse_month_value(month_value)
    sync_traffic()
    config = load_config()
    read_result = load_db_readonly()
    db = read_result.db
    ensure_connections(config, db)
    if read_result.source == "json":
        save_db(db)
    rows = client_listing.client_rows(config, db)
    if not rows:
        print("No clients.")
        return

    traffic_db = load_traffic_db_readonly()
    table_rows = traffic_reports.month_summary_rows(
        rows,
        traffic_db,
        client_repository.db_clients(db),
        month_key,
        connection_label=lambda row: connection_display_name(config, db, row["connection"]),
        limit_label=client_limits.format_traffic_limit,
        today=client_access.local_now().date(),
    )

    print(f"Month: {month_key}")
    print_table(["NAME", "STATUS", "CONNECTION", "IN", "OUT", "TOTAL", "LIMIT", "ALL TIME"], table_rows, empty_message=None)


def cmd_traffic_day(name, day_value=None):
    day = parse_date_value(day_value or client_access.local_now().date().isoformat())
    _, _, _, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Day: {day.isoformat()} (timezone: {client_settings.manager_timezone_label()})")
    print_table(["HOUR", "IN", "OUT", "TOTAL"], traffic_reports.day_hour_rows(entry, day), empty_message=None)


def cmd_traffic_week(name, start_value=None):
    start = parse_date_value(start_value or (client_access.local_now().date() - timedelta(days=6)).isoformat(), "START_DATE")
    end = start + timedelta(days=6)
    _, _, _, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Period: {start.isoformat()}..{end.isoformat()} (timezone: {client_settings.manager_timezone_label()})")
    print_table(["DATE", "IN", "OUT", "TOTAL"], traffic_reports.period_day_rows(entry, start, end), empty_message=None)


def cmd_traffic_month(name, month_value=None):
    month_key = parse_month_value(month_value)
    start, end = traffic_reports.month_bounds(month_key, today=client_access.local_now().date())
    _, _, _, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Month: {month_key} (timezone: {client_settings.manager_timezone_label()})")
    print_table(["DATE", "IN", "OUT", "TOTAL"], traffic_reports.period_day_rows(entry, start, end), empty_message=None)


def cmd_traffic_period(name, start_value, end_value):
    start = parse_date_value(start_value, "START_DATE")
    end = parse_date_value(end_value, "END_DATE")
    if end < start:
        die("END_DATE must be the same as or later than START_DATE.")
    _, _, _, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Period: {start.isoformat()}..{end.isoformat()} (timezone: {client_settings.manager_timezone_label()})")
    print_table(["DATE", "IN", "OUT", "TOTAL"], traffic_reports.period_day_rows(entry, start, end), empty_message=None)


def db_entry_for_existing_client(config, db, name):
    validate_name(name)
    try:
        return client_crud.db_entry_for_existing_client(config, db, name)
    except ValueError as exc:
        die(str(exc))


def cmd_add(name, access_days=None, prompt_for_access=True, connection_tag=None, payment_type="free"):
    validate_name(name)
    payment_type = normalize_payment_type(payment_type)
    config = load_config()
    db = load_db()
    try:
        connection_tag = client_crud.prepare_add_client(config, db, name, connection_tag)
    except ValueError as exc:
        die(str(exc))

    if prompt_for_access:
        access_days = prompt_access_days()
    try:
        result = client_crud.add_client(config, db, name, access_days, connection_tag, payment_type)
    except ValueError as exc:
        die(str(exc))

    backup = save_config_restart_xray_and_db(config, db)

    print(f"Added client: {name}")
    print(f"Connection: {connection_display_name(config, db, result.connection_tag)} ({result.connection_tag})")
    print(f"Payment type: {client_payments.payment_type_label(result.entry)}")
    print(f"Created: {result.created}")
    print(f"Access until: {client_access.format_access_until(result.entry.get('expiresAt', ''))}")
    print(f"Backup: {backup}")
    print_payment_summary()
    print(link_for(config, result.client_id, name, result.connection_tag, db))


def cmd_set_payment(name, payment_value):
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    entry = db_entry_for_existing_client(config, db, name)
    set_entry_payment_type(entry, payment_value)
    client_repository.db_clients(db)[name] = entry
    save_db(db)
    print(f"Client: {name}")
    print(f"Payment type: {client_payments.payment_type_label(entry)}")
    print_payment_summary()


def cmd_sync_routes():
    config = load_config()
    db = load_db()
    changed = client_routes.ensure_all_client_route_config(config, db)
    if changed:
        backup = save_config_restart_xray_and_db(config, db)
        print("Client cascade routes synchronized.")
        print(f"Backup: {backup}")
    else:
        save_db(db)
        print("Client cascade routes are already synchronized.")
    print_route_options(config, db)


def cmd_route(name, route_value=None):
    validate_name(name)
    config = load_config()
    db = load_db()
    client_routes.sync_routes_from_config(config, db)
    entry = db_entry_for_existing_client(config, db, name)
    current_tag = client_routes.selected_route_tag(entry)

    if route_value is None:
        print(f"Client: {name}")
        print(f"Current country: {client_routes.selected_route_label(db, entry)}")
        print(f"Current cascade: {current_tag or '-'}")
        print_route_options(config, db)
        save_db(db)
        return

    tag = resolve_cascade_route_tag(config, db, route_value)
    if current_tag == tag:
        save_db(db)
        print(f"Client: {name}")
        print(f"Country already selected: {client_routes.selected_route_label(db, entry)}")
        print(f"Cascade: {tag}")
        return

    next_entry = dict(entry)
    next_entry["selectedCascadeTag"] = tag
    client_routes.ensure_client_route_config(config, name, next_entry, tag)
    backup = save_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
    except subprocess.CalledProcessError:
        restore_config_file(backup)
        die(f"New route config failed. Restored backup: {backup}")

    ok, detail = client_routes.apply_runtime_override(name, next_entry, tag, run_capture)
    if not ok:
        restore_config_file(backup)
        die("Runtime route switch failed. Run xray-client sync-routes first and check Xray RoutingService. Detail: " + detail)

    client_repository.db_clients(db)[name] = next_entry
    save_db(db)
    print(f"Client: {name}")
    print(f"Selected country: {client_routes.selected_route_label(db, next_entry)}")
    print(f"Cascade: {tag}")
    print(f"Backup: {backup}")
    print("Runtime route updated without Xray restart.")


def cmd_remove(name):
    validate_name(name)
    config = load_config()
    db = load_db()
    try:
        result = client_crud.remove_client(config, db, name)
    except ValueError as exc:
        die(str(exc))

    backup = save_config_restart_xray_and_db(config, db)

    print(f"Removed client: {result.name}")
    if remove_traffic_clients([result.name]):
        print("Removed traffic history.")
    print(f"Backup: {backup}")


def cmd_disable(name):
    validate_name(name)
    config = load_config()
    db = load_db()
    try:
        result = client_crud.disable_client(config, db, name)
    except ValueError as exc:
        die(str(exc))

    backup = save_config_restart_xray_and_db(config, db)

    print(f"Disabled client: {result.name}")
    print(f"Backup: {backup}")


def cmd_enable(name):
    validate_name(name)
    sync_traffic()
    config = load_config()
    db = load_db()
    traffic_db = load_traffic_db()
    try:
        result = client_crud.enable_client(config, db, traffic_db, name)
    except client_crud.EnableTrafficLimitExceeded as exc:
        traffic_status = exc.traffic_status
        die(
            "Traffic limit is exhausted for the current "
            f"{client_limits.traffic_limit_period_label(traffic_status['period'])}. "
            f"Used {format_traffic(traffic_status['usedBytes'])} of {format_traffic(traffic_status['limitBytes'])}. "
            f"Can be enabled after reset: {traffic_status['resetAt']}"
        )
    except ValueError as exc:
        die(str(exc))

    backup = save_config_restart_xray_and_db(config, db)

    print(f"Enabled client: {result.name}")
    print(f"Connection: {connection_display_name(config, db, result.connection_tag)} ({result.connection_tag})")
    print(f"Backup: {backup}")
    print(link_for(config, result.client_id, result.name, result.connection_tag, db))


def cmd_move_connection(name, target_connection_identifier):
    validate_name(name)
    config = load_config()
    db = load_db()
    try:
        result = client_crud.move_client_to_connection(config, db, name, target_connection_identifier)
    except ValueError as exc:
        die(str(exc))

    backup = None
    if result.config_changed:
        backup = save_config_restart_xray_and_db(config, db)
    else:
        save_db(db)

    print(f"Moved client: {result.name}")
    print(
        "From: "
        f"{connection_display_name(config, db, result.source_connection_tag)} ({result.source_connection_tag})"
    )
    print(
        "To: "
        f"{connection_display_name(config, db, result.target_connection_tag)} ({result.target_connection_tag})"
    )
    print("Status: " + ("enabled" if result.enabled else "disabled"))
    if backup:
        print(f"Backup: {backup}")
    else:
        print("Xray restart is not required for disabled client.")
    print("New link:")
    print(link_for(config, result.client_id, result.name, result.target_connection_tag, db))
    print("Выдай клиенту новую ссылку: после переноса параметры подключения меняются.")


def run_access_update(name, result_factory):
    validate_name(name)
    sync_traffic()
    config = load_config()
    db = load_db()
    traffic_db = load_traffic_db()
    try:
        result = result_factory(config, db, traffic_db)
    except ValueError as exc:
        die(str(exc))
    entry = result.entry

    backup = None
    if result.config_changed:
        backup = save_config_restart_xray_and_db(config, db)
    else:
        save_db(db)

    print(f"Client: {name}")
    print(f"Access until: {client_access.format_access_until(entry.get('expiresAt', ''))}")
    if result.status == "enabled":
        print("Status: enabled")
    elif result.status == "disabled-expired":
        print("Status: disabled by expired access")
    elif result.status == "disabled-traffic-limit":
        traffic_status = result.traffic_status
        print(
            "Status: disabled by traffic limit. "
            f"Used {format_traffic(traffic_status['usedBytes'])} of {format_traffic(traffic_status['limitBytes'])}; "
            f"can be enabled after reset: {traffic_status['resetAt']}"
        )
    if backup:
        print(f"Backup: {backup}")
    notify_access_updated(name)


def enforce_traffic_limits(config, db, traffic_db):
    try:
        return client_status.enforce_traffic_limits(config, db, traffic_db, stamp=utc_stamp())
    except ValueError as exc:
        die(str(exc))


def cmd_set_days(name, days_value):
    days = parse_access_days(days_value)
    run_access_update(
        name,
        lambda config, db, traffic_db: client_status.set_access_days(config, db, traffic_db, name, days),
    )


def cmd_extend_days(name, days_value):
    days = parse_extend_days(days_value)
    run_access_update(
        name,
        lambda config, db, traffic_db: client_status.extend_access_days(config, db, traffic_db, name, days),
    )


def cmd_set_limit(name, period_value, limit_gb_value):
    period = validate_limit_period(period_value)
    limit_bytes = parse_limit_gb(limit_gb_value)
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    entry = db_entry_for_existing_client(config, db, name)
    result = set_client_traffic_limit(entry, period, limit_bytes)
    client_repository.db_clients(db)[name] = result.entry
    save_db(db)

    print(f"Client: {name}")
    if result.limit_bytes is None:
        print("Traffic limit: без лимита")
        return

    sync_traffic()
    traffic_db = load_traffic_db()
    status = traffic_limit_status(result.entry, traffic_repository.traffic_entry(traffic_db, name))
    print(f"Traffic limit: {format_traffic(result.limit_bytes)}/{client_limits.traffic_limit_period_label(result.period)}")
    if status:
        print(f"Current usage: {format_traffic(status['usedBytes'])}")
        print(f"Remaining: {format_traffic(status['remainingBytes'])}")
        print(f"Reset: {status['resetAt']}")
        if status["exceeded"]:
            print("Traffic limit is already exceeded. Checking active clients now.")
            cmd_enforce_limits(quiet=False, sync_first=False)


def cmd_clear_limit(name):
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    entry = db_entry_for_existing_client(config, db, name)
    result = clear_client_traffic_limit(entry)
    client_repository.db_clients(db)[name] = result.entry
    save_db(db)

    print(f"Client: {name}")
    print("Traffic limit: без лимита")
    if result.was_disabled_by_limit:
        print("Status: disabled by previous traffic limit. Use xray-client enable NAME if the client should be enabled now.")


def cmd_limit_list():
    sync_traffic()
    config = load_config()
    read_result = load_db_readonly()
    db = read_result.db
    ensure_connections(config, db)
    if read_result.source == "json":
        save_db(db)
    traffic_db = load_traffic_db_readonly()
    rows = client_listing.client_rows(config, db)
    if not rows:
        print("No clients.")
        return
    table_rows = [
        client_limits.traffic_limit_row(
            row,
            client_repository.db_clients(db),
            traffic_db,
            connection_display_name(config, db, row["connection"]),
        )
        for row in rows
    ]
    print_table(["NAME", "STATUS", "CONNECTION", "LIMIT", "USED", "REMAINING", "RESET"], table_rows, empty_message=None)


def cmd_enforce_limits(quiet=False, sync_first=False):
    if sync_first:
        sync_traffic()

    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    traffic_db = load_traffic_db()
    result = enforce_traffic_limits(config, db, traffic_db)

    if not result.has_changes:
        if not quiet:
            print("No traffic limits exceeded or reset.")
        return

    backup = save_config_restart_xray_and_db(config, db)

    if not quiet:
        if result.reactivated_names:
            print("Re-enabled clients after traffic limit reset: " + ", ".join(result.reactivated_names))
        if result.due_names:
            print("Disabled clients by traffic limit: " + ", ".join(result.due_names))
        print(f"Backup: {backup}")


def cmd_expire_due(quiet=False):
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    try:
        result = client_status.expire_due_clients(config, db, stamp=utc_stamp())
    except ValueError as exc:
        die(str(exc))

    if not result.has_changes:
        if not quiet:
            print("No expired clients.")
        return

    backup = save_config_restart_xray_and_db(config, db)

    if not quiet:
        print("Disabled expired clients: " + ", ".join(result.due_names))
        print(f"Backup: {backup}")


def cmd_timezone():
    name = client_settings.configured_timezone_name()
    current = client_access.local_now()
    print(f"MANAGER_TIMEZONE: {name or 'server local time'}")
    print(f"Current time: {current.strftime('%Y-%m-%d %H:%M:%S %Z')}")


def cmd_set_timezone(value):
    name = normalize_timezone(value)
    values = client_settings.server_env_values(SERVER_ENV_PATH)
    values["MANAGER_TIMEZONE"] = name
    save_server_env_values(values)
    normalized = normalize_access_deadlines(client_settings.manager_timezone())
    print(f"MANAGER_TIMEZONE: {name or 'server local time'}")
    print(f"Current time: {client_access.local_now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    if normalized:
        print(f"Normalized access deadlines: {normalized}")


def usage():
    print("""Usage:
  xray-client list
  xray-client connection-list
  xray-client add-connection NAME PORT SNI [FINGERPRINT] [TRANSPORT] [--transport tcp|grpc|xhttp] [--grpc-service-name NAME] [--xhttp-path PATH] [--xhttp-mode MODE] [--xhttp-extra-json JSON]
  xray-client add-connection NAME LOCAL_PORT DOMAIN [FINGERPRINT] --security tls --transport xhttp [--xhttp-path PATH] [--xhttp-mode MODE] [--xhttp-extra-json JSON] [--public-port PORT] [--install-caddy] [--tls-min-version tls1.2|tls1.3|default] [--tls-max-version tls1.2|tls1.3|default]
  xray-client connection-rename NAME_OR_TAG NEW_NAME
  xray-client connection-transport NAME_OR_TAG tcp|grpc|xhttp [--grpc-service-name NAME] [--xhttp-path PATH] [--xhttp-mode MODE] [--xhttp-extra-json JSON]
  xray-client connection-xhttp-extra NAME_OR_TAG --xhttp-extra-json JSON|--clear-xhttp-extra
  xray-client remove-connection NAME_OR_TAG
  xray-client add NAME [DAYS] [--connection TAG] [--payment paid|free]
  xray-client disable NAME
  xray-client enable NAME
  xray-client move-connection NAME CONNECTION_NAME_OR_TAG
  xray-client remove NAME
  xray-client link NAME
  xray-client set-days NAME DAYS
  xray-client extend-days NAME DAYS
  xray-client set-payment NAME paid|free
  xray-client sync-routes
  xray-client route NAME [COUNTRY_OR_TAG]
  xray-client expire-due [--quiet]
  xray-client traffic-summary [YYYY-MM]
  xray-client traffic-day NAME [YYYY-MM-DD]
  xray-client traffic-week NAME [START_DATE]
  xray-client traffic-month NAME [YYYY-MM]
  xray-client traffic-period NAME START_DATE END_DATE
  xray-client limit-list
  xray-client set-limit NAME daily|monthly LIMIT_GB
  xray-client clear-limit NAME
  xray-client enforce-limits [--quiet] [--sync]
  xray-client timezone
  xray-client set-timezone TIMEZONE|server
""")


def parse_add_args(args):
    if not args:
        usage()
        sys.exit(1)
    name = args[0]
    rest = list(args[1:])
    connection_tag = None
    payment_type = "free"
    if "--connection" in rest:
        index = rest.index("--connection")
        if index + 1 >= len(rest):
            die("--connection requires TAG")
        connection_tag = rest[index + 1]
        del rest[index:index + 2]
    if "--payment" in rest:
        index = rest.index("--payment")
        if index + 1 >= len(rest):
            die("--payment requires paid or free")
        payment_type = normalize_payment_type(rest[index + 1])
        del rest[index:index + 2]
    if len(rest) > 1:
        usage()
        sys.exit(1)
    if rest:
        return name, parse_access_days(rest[0]), False, connection_tag, payment_type
    return name, None, True, connection_tag, payment_type


def parse_connection_add_args(args):
    grpc_service_name = ""
    xhttp_path = ""
    xhttp_mode = ""
    xhttp_extra_json = ""
    transport = ""
    security = "reality"
    public_port = ""
    install_caddy = False
    tls_min_version = "tls1.2"
    tls_max_version = "tls1.2"
    rest = list(args)
    index = 0
    positional = []
    while index < len(rest):
        item = rest[index]
        if item == "--security":
            if index + 1 >= len(rest):
                die("--security requires reality or tls")
            security = rest[index + 1]
            index += 2
            continue
        if item == "--transport":
            if index + 1 >= len(rest):
                die("--transport requires tcp, grpc, or xhttp")
            transport = rest[index + 1]
            index += 2
            continue
        if item == "--grpc-service-name":
            if index + 1 >= len(rest):
                die("--grpc-service-name requires value")
            grpc_service_name = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-path":
            if index + 1 >= len(rest):
                die("--xhttp-path requires value")
            xhttp_path = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-mode":
            if index + 1 >= len(rest):
                die("--xhttp-mode requires value")
            xhttp_mode = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-extra-json":
            if index + 1 >= len(rest):
                die("--xhttp-extra-json requires JSON value")
            xhttp_extra_json = rest[index + 1]
            index += 2
            continue
        if item == "--public-port":
            if index + 1 >= len(rest):
                die("--public-port requires PORT")
            public_port = rest[index + 1]
            index += 2
            continue
        if item == "--install-caddy":
            install_caddy = True
            index += 1
            continue
        if item == "--tls-min-version":
            if index + 1 >= len(rest):
                die("--tls-min-version requires tls1.2, tls1.3, or default")
            tls_min_version = rest[index + 1]
            index += 2
            continue
        if item == "--tls-max-version":
            if index + 1 >= len(rest):
                die("--tls-max-version requires tls1.2, tls1.3, or default")
            tls_max_version = rest[index + 1]
            index += 2
            continue
        if item.startswith("--"):
            die(f"Unknown option: {item}")
        positional.append(item)
        index += 1

    if len(positional) < 3 or len(positional) > 5:
        usage()
        sys.exit(1)

    name, port, sni = positional[:3]
    fingerprint_value = "chrome"
    if len(positional) >= 4:
        maybe_value = positional[3]
        if maybe_value.lower() in xray_config.REALITY_TRANSPORTS:
            transport = maybe_value
        else:
            fingerprint_value = maybe_value
    if len(positional) == 5:
        transport = positional[4]

    transport = transport or ("xhttp" if validate_connection_security(security) == "tls" else "tcp")
    return (
        name,
        port,
        sni,
        fingerprint_value,
        transport,
        grpc_service_name,
        xhttp_path,
        xhttp_mode,
        xhttp_extra_json,
        security,
        public_port,
        install_caddy,
        tls_min_version,
        tls_max_version,
    )


def parse_connection_transport_args(args):
    if len(args) < 2:
        usage()
        sys.exit(1)
    identifier = args[0]
    transport = args[1]
    grpc_service_name = ""
    xhttp_path = ""
    xhttp_mode = ""
    xhttp_extra_json = None
    rest = list(args[2:])
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "--grpc-service-name":
            if index + 1 >= len(rest):
                die("--grpc-service-name requires value")
            grpc_service_name = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-path":
            if index + 1 >= len(rest):
                die("--xhttp-path requires value")
            xhttp_path = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-mode":
            if index + 1 >= len(rest):
                die("--xhttp-mode requires value")
            xhttp_mode = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-extra-json":
            if index + 1 >= len(rest):
                die("--xhttp-extra-json requires JSON value")
            xhttp_extra_json = rest[index + 1]
            index += 2
            continue
        die(f"Unknown option: {item}")
    return identifier, transport, grpc_service_name, xhttp_path, xhttp_mode, xhttp_extra_json


def parse_connection_xhttp_extra_args(args):
    if len(args) < 2:
        usage()
        sys.exit(1)
    identifier = args[0]
    rest = list(args[1:])
    clear = False
    xhttp_extra_json = None
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "--clear-xhttp-extra":
            clear = True
            index += 1
            continue
        if item == "--xhttp-extra-json":
            if index + 1 >= len(rest):
                die("--xhttp-extra-json requires JSON value")
            xhttp_extra_json = rest[index + 1]
            index += 2
            continue
        die(f"Unknown option: {item}")
    if clear and xhttp_extra_json is not None:
        die("Use either --xhttp-extra-json or --clear-xhttp-extra.")
    if not clear and xhttp_extra_json is None:
        die("connection-xhttp-extra requires --xhttp-extra-json JSON or --clear-xhttp-extra.")
    return identifier, xhttp_extra_json, clear


def cmd_link(name):
    validate_name(name)
    config = load_config()
    db = load_db_readonly().db
    ensure_connections(config, db)
    for row in client_listing.client_rows(config, db):
        if row["name"] == name:
            print(link_for(config, row["id"], name, row["connection"], db))
            return
    die(f"Client not found: {name}")


def main():
    if os.geteuid() != 0:
        die("Run this script as root.")
    if len(sys.argv) < 2:
        usage()
        sys.exit(1)
    command = sys.argv[1]
    if command == "list":
        cmd_list()
    elif command == "connection-list":
        cmd_connection_list()
    elif command == "add-connection":
        cmd_connection_add(*parse_connection_add_args(sys.argv[2:]))
    elif command in ("connection-transport", "set-connection-transport"):
        cmd_connection_transport(*parse_connection_transport_args(sys.argv[2:]))
    elif command in ("connection-xhttp-extra", "set-connection-xhttp-extra"):
        cmd_connection_xhttp_extra(*parse_connection_xhttp_extra_args(sys.argv[2:]))
    elif command in ("connection-rename", "rename-connection") and len(sys.argv) == 4:
        cmd_connection_rename(sys.argv[2], sys.argv[3])
    elif command in ("remove-connection", "delete-connection") and len(sys.argv) == 3:
        cmd_connection_remove(sys.argv[2])
    elif command == "add":
        name, access_days, prompt_for_access, connection_tag, payment_type = parse_add_args(sys.argv[2:])
        cmd_add(
            name,
            access_days,
            prompt_for_access=prompt_for_access,
            connection_tag=connection_tag,
            payment_type=payment_type,
        )
    elif command in ("disable", "off") and len(sys.argv) == 3:
        cmd_disable(sys.argv[2])
    elif command in ("enable", "on") and len(sys.argv) == 3:
        cmd_enable(sys.argv[2])
    elif command in ("move-connection", "move-client", "move") and len(sys.argv) == 4:
        cmd_move_connection(sys.argv[2], sys.argv[3])
    elif command in ("remove", "delete", "del", "rm") and len(sys.argv) == 3:
        cmd_remove(sys.argv[2])
    elif command == "link" and len(sys.argv) == 3:
        cmd_link(sys.argv[2])
    elif command in ("set-days", "access-days") and len(sys.argv) == 4:
        cmd_set_days(sys.argv[2], sys.argv[3])
    elif command in ("extend-days", "prolong-days") and len(sys.argv) == 4:
        cmd_extend_days(sys.argv[2], sys.argv[3])
    elif command in ("set-payment", "payment-type") and len(sys.argv) == 4:
        cmd_set_payment(sys.argv[2], sys.argv[3])
    elif command == "sync-routes" and len(sys.argv) == 2:
        cmd_sync_routes()
    elif command == "route" and len(sys.argv) in (3, 4):
        cmd_route(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    elif command == "expire-due" and len(sys.argv) in (2, 3):
        quiet = len(sys.argv) == 3 and sys.argv[2] == "--quiet"
        if len(sys.argv) == 3 and not quiet:
            usage()
            sys.exit(1)
        cmd_expire_due(quiet=quiet)
    elif command == "traffic-summary" and len(sys.argv) in (2, 3):
        cmd_traffic_summary(sys.argv[2] if len(sys.argv) == 3 else None)
    elif command == "traffic-day" and len(sys.argv) in (3, 4):
        cmd_traffic_day(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    elif command == "traffic-week" and len(sys.argv) in (3, 4):
        cmd_traffic_week(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    elif command == "traffic-month" and len(sys.argv) in (3, 4):
        cmd_traffic_month(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    elif command == "traffic-period" and len(sys.argv) == 5:
        cmd_traffic_period(sys.argv[2], sys.argv[3], sys.argv[4])
    elif command == "limit-list" and len(sys.argv) == 2:
        cmd_limit_list()
    elif command == "set-limit" and len(sys.argv) == 5:
        cmd_set_limit(sys.argv[2], sys.argv[3], sys.argv[4])
    elif command == "clear-limit" and len(sys.argv) == 3:
        cmd_clear_limit(sys.argv[2])
    elif command == "enforce-limits":
        allowed = {"--quiet", "--sync"}
        options = set(sys.argv[2:])
        if not options.issubset(allowed):
            usage()
            sys.exit(1)
        cmd_enforce_limits(quiet="--quiet" in options, sync_first="--sync" in options)
    elif command == "timezone" and len(sys.argv) == 2:
        cmd_timezone()
    elif command == "set-timezone" and len(sys.argv) == 3:
        cmd_set_timezone(sys.argv[2])
    else:
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
