#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.clients import access as client_access
from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.clients import limits as client_limits
from xray_vps_manager.clients import listing as client_listing
from xray_vps_manager.clients import links as client_links
from xray_vps_manager.clients import models as client_models
from xray_vps_manager.clients import payments as client_payments
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.clients import settings as client_settings
from xray_vps_manager.clients import status as client_status
from xray_vps_manager.core.terminal import print_table
from xray_vps_manager.traffic import formatting as traffic_formatting
from xray_vps_manager.traffic import reports as traffic_reports
from xray_vps_manager.traffic import repository as traffic_repository
from xray_vps_manager.xray import config as xray_config
from xray_vps_manager.xray import crypto as xray_crypto

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
CLIENT_DB_PATH = Path("/usr/local/etc/xray/clients.json")
SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
TRAFFIC_PATH = Path("/usr/local/etc/xray/traffic.json")
INBOUND_TAG = "vless-reality"
DEFAULT_CONNECTION_NAME = "default"
STATS_SERVER = "127.0.0.1:10085"
TRAFFIC_SYNC = Path("/usr/local/sbin/xray-traffic-sync")
XRAY_TELEGRAM = Path("/usr/local/sbin/xray-telegram")
DEFAULT_SERVER_ADDR = ""
DEFAULT_SERVER_NAME = "Xray"
ONLINE_WINDOW_SECONDS = 300
BYTES_IN_GB = 1024 ** 3
PAYMENT_TYPES = {"paid", "free"}
CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
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


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_iso():
    return utc_now().isoformat().replace("+00:00", "Z")


def local_now():
    return client_access.local_now()


def parse_datetime(value):
    return client_access.parse_datetime(value)


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


def expires_at_from_days(days):
    return client_access.expires_at_from_days(days)


def set_entry_expiry(entry, days):
    client_access.set_entry_expiry(entry, days)


def normalize_payment_type(value):
    try:
        return client_payments.normalize_payment_type(value)
    except ValueError as exc:
        die(str(exc))


def payment_type_label(entry):
    return client_payments.payment_type_label(entry)


def set_entry_payment_type(entry, value):
    try:
        return client_payments.set_payment_type(entry, value)
    except ValueError as exc:
        die(str(exc))


def extended_expires_at(entry, days):
    return client_access.extended_expires_at(entry, days)


def extend_entry_expiry(entry, days):
    try:
        client_access.extend_entry_expiry(entry, days)
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


def set_entry_traffic_limit(entry, period, limit_bytes):
    try:
        client_limits.set_entry_traffic_limit(entry, period, limit_bytes, utc_now_iso)
    except ValueError as exc:
        die(str(exc))


def traffic_limit(entry):
    return client_limits.traffic_limit(entry)


def traffic_limit_period_key(period, now=None):
    try:
        return client_limits.traffic_limit_period_key(period, now)
    except ValueError as exc:
        die(str(exc))


def traffic_limit_reset_time(period, now=None):
    try:
        return client_limits.traffic_limit_reset_time(period, now)
    except ValueError as exc:
        die(str(exc))


def traffic_limit_period_label(period):
    return client_limits.traffic_limit_period_label(period)


def format_traffic_limit(entry):
    return client_limits.format_traffic_limit(entry)


def prompt_access_days():
    if not sys.stdin.isatty():
        return None
    print()
    print("ACCESS_DAYS: количество календарных дней доступа.")
    print(f"Клиент будет автоматически отключён в 00:00 по часовому поясу менеджера: {manager_timezone_label()}.")
    print("Нажми Enter или введи 0, чтобы добавить клиента бессрочно.")
    value = input("ACCESS_DAYS [бессрочно]: ").strip()
    return parse_access_days(value)


def access_expired(entry, now=None):
    return client_access.access_expired(entry, now)


def format_access_until(value):
    return client_access.format_access_until(value)


def access_deadline_at_midnight(value, tz):
    return client_access.access_deadline_at_midnight(value, tz)


def load_config():
    try:
        return xray_config.load_config(CONFIG_PATH)
    except FileNotFoundError as exc:
        die(str(exc))


def save_config(config):
    return xray_config.save_config(config, CONFIG_PATH)


def load_db():
    try:
        return client_repository.load_db(CLIENT_DB_PATH)
    except ValueError as exc:
        die(str(exc))


def save_db(db):
    client_repository.save_db(db, CLIENT_DB_PATH)


def normalize_access_deadlines(tz):
    if not CLIENT_DB_PATH.exists():
        return 0
    db = load_db()
    changed = 0
    for entry in db_clients(db).values():
        expires_at = entry.get("expiresAt", "")
        normalized = access_deadline_at_midnight(expires_at, tz)
        if normalized and normalized != expires_at:
            entry["expiresAt"] = normalized
            changed += 1
    if changed:
        save_db(db)
    return changed


def save_server_env_values(values):
    client_settings.save_server_env_values(values, SERVER_ENV_PATH)


def find_inbound(config):
    try:
        return xray_config.find_inbound(config)
    except ValueError as exc:
        die(str(exc))


def reality_inbounds(config):
    return xray_config.reality_inbounds(config)


def inbound_tag(inbound):
    return xray_config.inbound_tag(inbound)


def find_inbound_by_tag(config, tag):
    try:
        return xray_config.find_inbound_by_tag(config, tag)
    except ValueError as exc:
        die(str(exc))


def default_connection_tag(config):
    try:
        return xray_config.default_connection_tag(config)
    except ValueError as exc:
        die(str(exc))


def db_connections(db):
    return client_repository.db_connections(db)


def connection_name_from_tag(tag):
    return xray_config.connection_name_from_tag(tag)


def connection_settings_from_inbound(inbound):
    return xray_config.connection_settings_from_inbound(inbound)


def reality_dest(sni):
    return xray_config.reality_dest(sni)


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


def connection_fingerprint(config, db, tag):
    try:
        return client_connections.connection_fingerprint(config, db, tag)
    except ValueError as exc:
        die(str(exc))


def connection_rows(config, db):
    try:
        return client_connections.connection_rows(config, db)
    except ValueError as exc:
        die(str(exc))


def resolve_connection_identifier(config, db, value):
    try:
        return client_connections.resolve_connection_identifier(config, db, value)
    except ValueError as exc:
        die(str(exc))


def clients(inbound):
    return xray_config.clients(inbound)


def split_email(email):
    return client_models.split_email(email)


def client_name(item):
    return client_models.client_name(item)


def active_client(inbound, name):
    return xray_config.active_client(inbound, name)


def active_client_any(config, name):
    return xray_config.active_client_any(config, name)


def db_clients(db):
    return client_repository.db_clients(db)


def db_entry_from_client(item, created="", enabled=True, previous=None):
    try:
        return client_models.db_entry_from_client(item, created=created, enabled=enabled, previous=previous)
    except ValueError as exc:
        die(str(exc))


def client_from_db_entry(name, entry):
    try:
        return client_models.client_from_db_entry(name, entry)
    except ValueError as exc:
        die(str(exc))


def clear_disabled_state(entry):
    client_status.clear_disabled_state(entry)


def enable_db_client(config, db, name, entry):
    try:
        return client_status.enable_db_client(config, name, entry)
    except ValueError as exc:
        die(str(exc))


def remove_active_client(config, name):
    return client_status.remove_active_client(config, name)


def clear_traffic_limit_exceeded_state(entry):
    client_status.clear_traffic_limit_exceeded_state(entry)


def disabled_entry_for_policy(config, name, entry):
    try:
        return client_status.disabled_entry_for_policy(config, name, entry)
    except ValueError as exc:
        die(str(exc))


def reconcile_client_access_status(config, db, traffic_db, name, entry, now=None):
    try:
        return client_status.reconcile_client_access_status(config, db, traffic_db, name, entry, now)
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


def xray_uuid():
    return xray_crypto.xray_uuid()


def xray_x25519_keys():
    try:
        return xray_crypto.xray_x25519_keys()
    except RuntimeError as exc:
        die(str(exc))


def random_short_id():
    return xray_crypto.random_short_id()


def reality_public_key(private_key):
    try:
        return xray_crypto.reality_public_key(private_key)
    except RuntimeError as exc:
        die(str(exc))


def server_env_values():
    return client_settings.server_env_values(SERVER_ENV_PATH)


def server_env_value(key, default=""):
    return client_settings.server_env_value(key, default)


def normalize_timezone(value):
    try:
        return client_settings.normalize_timezone(value)
    except ValueError as exc:
        die(str(exc))


def configured_timezone_name():
    return client_settings.configured_timezone_name()


def manager_timezone():
    return client_settings.manager_timezone()


def manager_timezone_label():
    return client_settings.manager_timezone_label()


def server_addr():
    try:
        return client_settings.server_addr()
    except ValueError as exc:
        die(str(exc))


def server_name():
    try:
        return client_settings.server_name()
    except ValueError as exc:
        die(str(exc))


def fingerprint():
    try:
        return client_settings.fingerprint()
    except ValueError as exc:
        die(str(exc))


def link_for(config, client_id, name, connection_tag=None, db=None):
    try:
        return client_links.link_for(config, client_id, name, connection_tag=connection_tag, db=db, db_loader=load_db)
    except (RuntimeError, ValueError) as exc:
        die(str(exc))


def client_rows(config, db):
    return client_listing.client_rows(config, db)


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


def load_traffic_db():
    return traffic_repository.load_traffic_db(TRAFFIC_PATH)


def save_traffic_db(db):
    traffic_repository.save_traffic_db(db, TRAFFIC_PATH)


def remove_traffic_clients(names):
    return traffic_repository.remove_traffic_clients(names, TRAFFIC_PATH)


def runtime_traffic_for(stats, email):
    if stats is None:
        return None, None
    uplink = stats.get(f"user>>>{email}>>>traffic>>>uplink", 0)
    downlink = stats.get(f"user>>>{email}>>>traffic>>>downlink", 0)
    return uplink, downlink


def traffic_for(traffic_db, stats, row):
    entry = traffic_db.get("clients", {}).get(row["name"])
    if entry:
        return int(entry.get("incoming", 0)), int(entry.get("outgoing", 0))
    return runtime_traffic_for(stats, row["email"])


def format_traffic(value):
    return traffic_formatting.format_traffic(value, none_label="n/a")


def parse_date_value(value, label="DATE"):
    try:
        return date.fromisoformat(value)
    except ValueError:
        die(f"{label} must be in YYYY-MM-DD format.")


def parse_month_value(value=None):
    value = value or local_now().strftime("%Y-%m")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        die("MONTH must be in YYYY-MM format.")
    year, month = (int(part, 10) for part in value.split("-", 1))
    if month < 1 or month > 12:
        die("MONTH must be in YYYY-MM format.")
    return f"{year:04d}-{month:02d}"


def traffic_entry(traffic_db, name):
    return traffic_repository.traffic_entry(traffic_db, name)


def traffic_limit_usage(entry, period, now=None):
    try:
        return client_limits.traffic_limit_usage(entry, period, now)
    except ValueError as exc:
        die(str(exc))


def traffic_limit_status(db_entry, traffic_db_entry, now=None):
    try:
        return client_limits.traffic_limit_status(db_entry, traffic_db_entry, now)
    except ValueError as exc:
        die(str(exc))


def parse_time(value):
    if not value or value in ("never", "unknown"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_time(value):
    parsed = parse_time(value)
    if parsed is None:
        return "never"
    local = parsed.astimezone(manager_timezone())
    return local.strftime("%Y-%m-%d %H:%M %Z")


def online_state(row, traffic_db):
    if row["status"] != "enabled":
        return "offline", "never"
    entry = traffic_db.get("clients", {}).get(row["name"], {})
    last_online = entry.get("lastOnline", "")
    parsed = parse_time(last_online)
    if parsed is None:
        return "offline", "never"
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    state = "online" if age <= ONLINE_WINDOW_SECONDS else "offline"
    return state, format_time(last_online)


def traffic_updated_at(row, traffic_db):
    entry = traffic_db.get("clients", {}).get(row["name"], {})
    return format_time(entry.get("updated", ""))


def print_client_table(rows):
    headers = ["NAME", "STATUS", "PAYMENT", "ONLINE", "IN", "OUT", "TOTAL", "TRAFFIC UPDATED", "LIMIT", "LAST ONLINE", "ACCESS UNTIL", "CREATED"]
    color_columns = {headers.index("STATUS"), headers.index("PAYMENT"), headers.index("ONLINE"), headers.index("ACCESS UNTIL")}
    print_table(headers, rows, empty_message=None, color_columns=color_columns, colorizer=color_label)


def print_plain_table(headers, rows):
    print_table(headers, rows, empty_message=None)


def print_connection_title(config, db, tag):
    entry = connection_entry(config, db, tag)
    name = entry.get("name") or connection_name_from_tag(tag)
    port = entry.get("port", "")
    sni = entry.get("sni", "")
    print()
    print(f"Connection: {name}  |  PORT={port}  |  SNI={sni}  |  TAG={tag}")


def used_ports(config):
    return client_connections.used_ports(config)


def next_connection_tag(config):
    return client_connections.next_connection_tag(config)


def make_reality_inbound(tag, port, sni, private_key, short_id):
    return client_connections.make_reality_inbound(tag, port, sni, private_key, short_id)


def cmd_connection_list():
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    save_db(db)
    headers = ["NAME", "TAG", "PORT", "SNI", "FINGERPRINT", "CREATED"]
    print_table(headers, connection_rows(config, db), empty_message=None)


def cmd_connection_add(name, port_value, sni_value, fingerprint_value="chrome"):
    name = validate_connection_name(name)
    port = validate_port(port_value)
    sni = validate_host(sni_value, "REALITY_SNI")
    fp = validate_fingerprint(fingerprint_value or "chrome")
    config = load_config()
    db = load_db()
    try:
        result = client_connections.add_connection(config, db, name, port, sni, fp)
    except (ValueError, RuntimeError) as exc:
        die(str(exc))

    backup = save_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
        save_db(db)
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")

    print(f"Added connection: {result.name}")
    print(f"Tag: {result.tag}")
    print(f"PORT: {result.port}")
    print(f"REALITY_SNI: {result.sni}")
    print(f"REALITY_DEST: {result.dest}")
    print(f"FINGERPRINT: {result.fingerprint}")
    print(f"Backup: {backup}")


def connection_client_names(config, db, tag):
    try:
        return client_connections.connection_client_names(config, db, tag)
    except ValueError as exc:
        die(str(exc))


def server_env_values_for_connection(config, db, tag):
    try:
        return client_connections.server_env_values_for_connection(config, db, tag)
    except ValueError as exc:
        die(str(exc))


def cmd_connection_remove(identifier):
    config = load_config()
    db = load_db()
    try:
        result = client_connections.remove_connection(config, db, identifier)
    except ValueError as exc:
        die(str(exc))

    backup = save_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
        save_db(db)
        if result.env_update is not None:
            save_server_env_values(result.env_update)
        traffic_removed = remove_traffic_clients(result.removed_client_names)
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")

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
    db = load_db()
    ensure_connections(config, db)
    save_db(db)
    rows = client_rows(config, db)
    if not rows:
        print("No clients.")
        return
    sync_traffic()
    traffic_db = load_traffic_db()
    stats = query_user_stats()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["connection"], []).append(row)

    for tag in db_connections(db):
        group_rows = grouped.get(tag, [])
        if not group_rows:
            continue
        print_connection_title(config, db, tag)
        table_rows = []
        for row in group_rows:
            incoming, outgoing = traffic_for(traffic_db, stats, row)
            total = None if incoming is None or outgoing is None else incoming + outgoing
            online, last_online = online_state(row, traffic_db)
            db_entry = db_clients(db).get(row["name"], {})
            traffic_updated = traffic_updated_at(row, traffic_db)
            table_rows.append(
                [
                    row["name"],
                    row["status"],
                    row["paymentType"],
                    online,
                    format_traffic(incoming),
                    format_traffic(outgoing),
                    format_traffic(total),
                    traffic_updated,
                    format_traffic_limit(db_entry),
                    last_online,
                    format_access_until(row["expiresAt"]),
                    row["created"],
                ]
            )
        print_client_table(table_rows)


def known_for_traffic_report(config, db, traffic_db, name):
    if name in db_clients(db):
        return True
    if name in traffic_db.get("clients", {}):
        return True
    return any(row["name"] == name for row in client_rows(config, db))


def traffic_report_context(name):
    validate_name(name)
    sync_traffic()
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    traffic_db = load_traffic_db()
    if not known_for_traffic_report(config, db, traffic_db, name):
        die(f"Client not found: {name}")
    return config, db, traffic_db, traffic_entry(traffic_db, name)


def cmd_traffic_summary(month_value=None):
    month_key = parse_month_value(month_value)
    sync_traffic()
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    save_db(db)
    rows = client_rows(config, db)
    if not rows:
        print("No clients.")
        return

    traffic_db = load_traffic_db()
    table_rows = traffic_reports.month_summary_rows(
        rows,
        traffic_db,
        db_clients(db),
        month_key,
        connection_label=lambda row: connection_display_name(config, db, row["connection"]),
        limit_label=format_traffic_limit,
        today=local_now().date(),
    )

    print(f"Month: {month_key}")
    print_plain_table(["NAME", "STATUS", "CONNECTION", "IN", "OUT", "TOTAL", "LIMIT", "ALL TIME"], table_rows)


def cmd_traffic_day(name, day_value=None):
    day = parse_date_value(day_value or local_now().date().isoformat())
    _, _, _, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Day: {day.isoformat()} (timezone: {manager_timezone_label()})")
    print_plain_table(["HOUR", "IN", "OUT", "TOTAL"], traffic_reports.day_hour_rows(entry, day))


def cmd_traffic_week(name, start_value=None):
    start = parse_date_value(start_value or (local_now().date() - timedelta(days=6)).isoformat(), "START_DATE")
    end = start + timedelta(days=6)
    _, _, _, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Period: {start.isoformat()}..{end.isoformat()} (timezone: {manager_timezone_label()})")
    print_plain_table(["DATE", "IN", "OUT", "TOTAL"], traffic_reports.period_day_rows(entry, start, end))


def cmd_traffic_month(name, month_value=None):
    month_key = parse_month_value(month_value)
    start, end = traffic_reports.month_bounds(month_key, today=local_now().date())
    _, _, _, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Month: {month_key} (timezone: {manager_timezone_label()})")
    print_plain_table(["DATE", "IN", "OUT", "TOTAL"], traffic_reports.period_day_rows(entry, start, end))


def cmd_traffic_period(name, start_value, end_value):
    start = parse_date_value(start_value, "START_DATE")
    end = parse_date_value(end_value, "END_DATE")
    if end < start:
        die("END_DATE must be the same as or later than START_DATE.")
    _, _, _, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Period: {start.isoformat()}..{end.isoformat()} (timezone: {manager_timezone_label()})")
    print_plain_table(["DATE", "IN", "OUT", "TOTAL"], traffic_reports.period_day_rows(entry, start, end))


def resolve_connection_for_add(config, db, connection_tag=None):
    try:
        return client_crud.resolve_connection_for_add(config, db, connection_tag)
    except ValueError as exc:
        die(str(exc))


def all_client_names(config, db):
    return client_crud.all_client_names(config, db)


def db_entry_for_existing_client(config, db, name):
    validate_name(name)
    entry = db_clients(db).get(name)
    if entry:
        return entry

    inbound, item = active_client_any(config, name)
    if item is None:
        die(f"Client not found: {name}")
    _, created = split_email(item.get("email", ""))
    entry = db_entry_from_client(item, created=created, enabled=True)
    entry["connection"] = inbound_tag(inbound)
    db_clients(db)[name] = entry
    return entry


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

    backup = save_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
        save_db(db)
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")

    print(f"Added client: {name}")
    print(f"Connection: {connection_display_name(config, db, result.connection_tag)} ({result.connection_tag})")
    print(f"Payment type: {payment_type_label(result.entry)}")
    print(f"Created: {result.created}")
    print(f"Access until: {format_access_until(result.entry.get('expiresAt', ''))}")
    print(f"Backup: {backup}")
    print_payment_summary()
    print(link_for(config, result.client_id, name, result.connection_tag, db))


def cmd_set_payment(name, payment_value):
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    entry = db_entry_for_existing_client(config, db, name)
    set_entry_payment_type(entry, payment_value)
    db_clients(db)[name] = entry
    save_db(db)
    print(f"Client: {name}")
    print(f"Payment type: {payment_type_label(entry)}")
    print_payment_summary()


def cmd_remove(name):
    validate_name(name)
    config = load_config()
    db = load_db()
    try:
        result = client_crud.remove_client(config, db, name)
    except ValueError as exc:
        die(str(exc))

    backup = save_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
        save_db(db)
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")

    print(f"Removed client: {result.name}")
    if remove_traffic_clients([result.name]):
        print("Removed traffic history.")
    print(f"Backup: {backup}")


def cmd_disable(name):
    validate_name(name)
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    inbound, item = active_client_any(config, name)
    if item is None:
        if name in db_clients(db) and db["clients"][name].get("enabled") is False:
            die(f"Client already disabled: {name}")
        die(f"Enabled client not found: {name}")

    _, created = split_email(item.get("email", ""))
    previous = db_clients(db).get(name, {})
    if name in db_clients(db):
        created = previous.get("created", created)
    entry = db_entry_from_client(item, created=created, enabled=False, previous=previous)
    entry["connection"] = previous.get("connection") or inbound_tag(inbound)
    entry["disabledAt"] = utc_now_iso()
    db_clients(db)[name] = entry
    inbound["settings"]["clients"] = [client for client in clients(inbound) if client_name(client) != name]

    backup = save_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
        save_db(db)
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")

    print(f"Disabled client: {name}")
    print(f"Backup: {backup}")


def cmd_enable(name):
    validate_name(name)
    sync_traffic()
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    if active_client_any(config, name)[1] is not None:
        die(f"Client already enabled: {name}")
    entry = db_clients(db).get(name)
    if not entry:
        die(f"Client not found: {name}")
    if access_expired(entry):
        die(f"Access expired for client: {name}. Extend it first: xray-client extend-days {name} DAYS")
    traffic_status = traffic_limit_status(entry, traffic_entry(load_traffic_db(), name))
    if traffic_status and traffic_status["exceeded"]:
        die(
            "Traffic limit is exhausted for the current "
            f"{traffic_limit_period_label(traffic_status['period'])}. "
            f"Used {format_traffic(traffic_status['usedBytes'])} of {format_traffic(traffic_status['limitBytes'])}. "
            f"Can be enabled after reset: {traffic_status['resetAt']}"
        )

    enable_db_client(config, db, name, entry)
    client = entry["client"]
    connection_tag = entry["connection"]
    entry["enabled"] = True
    clear_disabled_state(entry)
    db_clients(db)[name] = entry

    backup = save_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
        save_db(db)
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")

    print(f"Enabled client: {name}")
    print(f"Connection: {connection_display_name(config, db, connection_tag)} ({connection_tag})")
    print(f"Backup: {backup}")
    print(link_for(config, client["id"], name, connection_tag, db))


def apply_access_update(name, update_entry):
    validate_name(name)
    sync_traffic()
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    entry = db_clients(db).get(name)

    if not entry:
        inbound, item = active_client_any(config, name)
        if item is None:
            die(f"Client not found: {name}")
        _, created = split_email(item.get("email", ""))
        entry = db_entry_from_client(item, created=created, enabled=True)
        entry["connection"] = inbound_tag(inbound)

    update_entry(entry)
    traffic_db = load_traffic_db()
    entry, config_changed, status, traffic_status = reconcile_client_access_status(config, db, traffic_db, name, entry)

    backup = None
    if config_changed:
        backup = save_config(config)
        try:
            run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
            run(["systemctl", "restart", "xray"])
            save_db(db)
        except subprocess.CalledProcessError:
            shutil.copy2(backup, CONFIG_PATH)
            shutil.chown(CONFIG_PATH, user="root", group="xray")
            os.chmod(CONFIG_PATH, 0o640)
            run(["systemctl", "restart", "xray"])
            die(f"New config failed. Restored backup: {backup}")
    else:
        save_db(db)

    print(f"Client: {name}")
    print(f"Access until: {format_access_until(entry.get('expiresAt', ''))}")
    if status == "enabled":
        print("Status: enabled")
    elif status == "disabled-expired":
        print("Status: disabled by expired access")
    elif status == "disabled-traffic-limit":
        print(
            "Status: disabled by traffic limit. "
            f"Used {format_traffic(traffic_status['usedBytes'])} of {format_traffic(traffic_status['limitBytes'])}; "
            f"can be enabled after reset: {traffic_status['resetAt']}"
        )
    if backup:
        print(f"Backup: {backup}")
    notify_access_updated(name)


def cmd_set_days(name, days_value):
    days = parse_access_days(days_value)
    apply_access_update(name, lambda entry: set_entry_expiry(entry, days))


def cmd_extend_days(name, days_value):
    days = parse_extend_days(days_value)
    apply_access_update(name, lambda entry: extend_entry_expiry(entry, days))


def cmd_set_limit(name, period_value, limit_gb_value):
    period = validate_limit_period(period_value)
    limit_bytes = parse_limit_gb(limit_gb_value)
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    entry = db_entry_for_existing_client(config, db, name)
    set_entry_traffic_limit(entry, period, limit_bytes)
    db_clients(db)[name] = entry
    save_db(db)

    print(f"Client: {name}")
    if limit_bytes is None:
        print("Traffic limit: без лимита")
        return

    sync_traffic()
    traffic_db = load_traffic_db()
    status = traffic_limit_status(entry, traffic_entry(traffic_db, name))
    print(f"Traffic limit: {format_traffic(limit_bytes)}/{traffic_limit_period_label(period)}")
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
    was_disabled_by_limit = entry.get("enabled") is False and entry.get("disabledReason") == "traffic-limit"
    set_entry_traffic_limit(entry, "daily", None)
    db_clients(db)[name] = entry
    save_db(db)

    print(f"Client: {name}")
    print("Traffic limit: без лимита")
    if was_disabled_by_limit:
        print("Status: disabled by previous traffic limit. Use xray-client enable NAME if the client should be enabled now.")


def traffic_limit_row(config, db, traffic_db, row):
    return client_limits.traffic_limit_row(
        row,
        db_clients(db),
        traffic_db,
        connection_display_name(config, db, row["connection"]),
    )


def cmd_limit_list():
    sync_traffic()
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    save_db(db)
    traffic_db = load_traffic_db()
    rows = client_rows(config, db)
    if not rows:
        print("No clients.")
        return
    table_rows = [traffic_limit_row(config, db, traffic_db, row) for row in rows]
    print_plain_table(["NAME", "STATUS", "CONNECTION", "LIMIT", "USED", "REMAINING", "RESET"], table_rows)


def cmd_enforce_limits(quiet=False, sync_first=False):
    if sync_first:
        sync_traffic()

    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    traffic_db = load_traffic_db()
    now = local_now()
    reactivated_names = []
    due_names = []
    due_clients = {}
    due_statuses = {}

    for name, entry in db_clients(db).items():
        if entry.get("enabled") is not False or entry.get("disabledReason") != "traffic-limit":
            continue
        status = traffic_limit_status(entry, traffic_entry(traffic_db, name), now)
        if not status or status["exceeded"]:
            continue
        exceeded_period = entry.get("trafficLimitExceededPeriod", "")
        if exceeded_period and exceeded_period == status["periodKey"]:
            continue
        if access_expired(entry, now):
            continue

        enable_db_client(config, db, name, entry)
        entry["enabled"] = True
        clear_disabled_state(entry)
        db_clients(db)[name] = entry
        reactivated_names.append(name)

    for inbound in reality_inbounds(config):
        tag = inbound_tag(inbound)
        for item in clients(inbound):
            name = client_name(item)
            entry = db_clients(db).get(name, {})
            status = traffic_limit_status(entry, traffic_entry(traffic_db, name), now)
            if status and status["exceeded"]:
                due_names.append(name)
                due_clients[name] = (tag, item)
                due_statuses[name] = status

    if not reactivated_names and not due_names:
        if not quiet:
            print("No traffic limits exceeded or reset.")
        return

    stamp = utc_now_iso()
    for name in due_names:
        tag, item = due_clients[name]
        status = due_statuses[name]
        _, created = split_email(item.get("email", ""))
        previous = db_clients(db).get(name, {})
        entry = db_entry_from_client(item, created=created, enabled=False, previous=previous)
        entry["connection"] = previous.get("connection") or tag
        entry["disabledAt"] = stamp
        entry["disabledReason"] = "traffic-limit"
        entry["trafficLimitExceededAt"] = stamp
        entry["trafficLimitExceededPeriod"] = status["periodKey"]
        entry["trafficLimitExceededBytes"] = status["usedBytes"]
        entry["trafficLimitResetAt"] = status["resetAt"]
        db_clients(db)[name] = entry

    due_set = set(due_names)
    for inbound in reality_inbounds(config):
        inbound["settings"]["clients"] = [item for item in clients(inbound) if client_name(item) not in due_set]

    backup = save_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
        save_db(db)
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")

    if not quiet:
        if reactivated_names:
            print("Re-enabled clients after traffic limit reset: " + ", ".join(reactivated_names))
        if due_names:
            print("Disabled clients by traffic limit: " + ", ".join(due_names))
        print(f"Backup: {backup}")


def cmd_expire_due(quiet=False):
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    now = local_now()
    due_names = []
    due_clients = {}
    current_by_tag = {}

    for inbound in reality_inbounds(config):
        tag = inbound_tag(inbound)
        current_by_tag[tag] = clients(inbound)
        for item in clients(inbound):
            name = client_name(item)
            entry = db_clients(db).get(name, {})
            if access_expired(entry, now):
                due_names.append(name)
                due_clients[name] = (tag, item)

    if not due_names:
        if not quiet:
            print("No expired clients.")
        return

    stamp = utc_now_iso()
    for name in due_names:
        tag, item = due_clients[name]
        _, created = split_email(item.get("email", ""))
        previous = db_clients(db).get(name, {})
        entry = db_entry_from_client(item, created=created, enabled=False, previous=previous)
        entry["connection"] = previous.get("connection") or tag
        entry["disabledAt"] = stamp
        entry["expiredAt"] = stamp
        entry["disabledReason"] = "expired"
        db_clients(db)[name] = entry

    due_set = set(due_names)
    for inbound in reality_inbounds(config):
        inbound["settings"]["clients"] = [item for item in clients(inbound) if client_name(item) not in due_set]

    backup = save_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
        save_db(db)
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")

    if not quiet:
        print("Disabled expired clients: " + ", ".join(due_names))
        print(f"Backup: {backup}")


def cmd_timezone():
    name = configured_timezone_name()
    current = local_now()
    print(f"MANAGER_TIMEZONE: {name or 'server local time'}")
    print(f"Current time: {current.strftime('%Y-%m-%d %H:%M:%S %Z')}")


def cmd_set_timezone(value):
    name = normalize_timezone(value)
    values = server_env_values()
    values["MANAGER_TIMEZONE"] = name
    save_server_env_values(values)
    normalized = normalize_access_deadlines(manager_timezone())
    print(f"MANAGER_TIMEZONE: {name or 'server local time'}")
    print(f"Current time: {local_now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    if normalized:
        print(f"Normalized access deadlines: {normalized}")


def usage():
    print("""Usage:
  xray-client list
  xray-client connection-list
  xray-client add-connection NAME PORT SNI [FINGERPRINT]
  xray-client remove-connection NAME_OR_TAG
  xray-client add NAME [DAYS] [--connection TAG] [--payment paid|free]
  xray-client disable NAME
  xray-client enable NAME
  xray-client remove NAME
  xray-client link NAME
  xray-client set-days NAME DAYS
  xray-client extend-days NAME DAYS
  xray-client set-payment NAME paid|free
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


def cmd_link(name):
    validate_name(name)
    config = load_config()
    db = load_db()
    ensure_connections(config, db)
    for row in client_rows(config, db):
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
    elif command == "add-connection" and len(sys.argv) in (5, 6):
        cmd_connection_add(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] if len(sys.argv) == 6 else "chrome")
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
