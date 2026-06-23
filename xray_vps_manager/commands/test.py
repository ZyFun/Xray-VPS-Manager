#!/usr/bin/env python3
import json
import os
import re
import socket
import stat
import subprocess
import sys
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.core.server_env import read_server_env
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.db import database as sqlite_database
from xray_vps_manager.db import schema as sqlite_schema
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import activity_blocklist as sqlite_blocklist
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.repositories import connections as sqlite_connections
from xray_vps_manager.db.repositories import telegram as sqlite_telegram
from xray_vps_manager.db.repositories import traffic as sqlite_traffic
from xray_vps_manager.db.storage import sqlite_read_ready
from xray_vps_manager.traffic import consistency as traffic_consistency
from xray_vps_manager.xray import blocklist as xray_blocklist
from xray_vps_manager.xray import cascade as cascade_config

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
MANAGER_DB_PATH = Path("/usr/local/etc/xray/manager.db")
XRAY_BIN = Path("/usr/local/bin/xray")
XRAY_ASSET_DIR = Path("/usr/local/share/xray")
STATS_SERVER = "127.0.0.1:10085"
WARP_OUTBOUND_TAG = "warp-out"
DIRECT_TAG = "direct"
BLOCKED_TAG = "blocked"
GEOIP_WARNING_PREFIX = "geoip-warning-"
HELPER_SCRIPTS = [
    "/usr/local/sbin/xray-client",
    "/usr/local/sbin/xray-menu",
    "/usr/local/sbin/xray-activity",
    "/usr/local/sbin/xray-traffic-sync",
    "/usr/local/sbin/xray-set-cascade",
    "/usr/local/sbin/xray-update",
    "/usr/local/sbin/xray-backup",
    "/usr/local/sbin/xray-test",
    "/usr/local/sbin/xray-warp",
    "/usr/local/sbin/xray-telegram",
    "/usr/local/sbin/xray-vps-manager",
    "/usr/local/sbin/xray-manager-update",
]
MANAGER_PACKAGE_DIR = Path("/usr/local/lib/xray-vps-manager/xray_vps_manager")
TRAFFIC_SERVICE_COMMANDS = [
    "ExecStart=/usr/local/sbin/xray-traffic-sync --quiet",
    "ExecStart=/usr/local/sbin/xray-activity sync --quiet",
    "ExecStart=/usr/local/sbin/xray-telegram notify-geoip --quiet",
    "ExecStart=/usr/local/sbin/xray-client enforce-limits --quiet",
    "ExecStart=/usr/local/sbin/xray-client expire-due --quiet",
    "ExecStart=/usr/local/sbin/xray-telegram notify-expiry --quiet",
    "ExecStart=/usr/local/sbin/xray-telegram notify-daily-summary --quiet",
]
TELEGRAM_POLLER_SERVICE_COMMAND = "ExecStart=/usr/local/sbin/xray-telegram run-poller"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def color(text, code):
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{RESET}"


def line_ok(message):
    print(color(f"OK: {message}", GREEN))


def line_warn(message):
    print(color(f"WARN: {message}", YELLOW))


def line_fail(message):
    print(color(f"FAIL: {message}", RED))


def compact_output(result):
    detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
    return "\n".join(detail.splitlines()[:8])


def run(command, timeout=20, env=None):
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
    )


def run_ok(command, timeout=20, env=None):
    result = run(command, timeout=timeout, env=env)
    if result.returncode != 0:
        raise RuntimeError(compact_output(result))
    return result.stdout.strip() or " ".join(command)


def load_json(path):
    if not path.exists():
        raise RuntimeError(f"not found: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}")


def parse_xray_version(output):
    match = re.search(r"(\d+(?:\.\d+){1,3})", output or "")
    return match.group(1) if match else ""


def is_active(unit):
    result = run(["systemctl", "is-active", unit], timeout=10)
    return result.returncode == 0 and result.stdout.strip() == "active"


def check_tcp(host, port, timeout=2):
    with socket.create_connection((host, port), timeout=timeout):
        return True


def reality_inbounds(config):
    return [
        inbound
        for inbound in config.get("inbounds", [])
        if inbound.get("protocol") == "vless"
        and inbound.get("streamSettings", {}).get("security") == "reality"
    ]


def vless_inbounds(config):
    return [inbound for inbound in config.get("inbounds", []) if inbound.get("protocol") == "vless"]


def trojan_inbounds(config):
    return [
        inbound
        for inbound in config.get("inbounds", [])
        if inbound.get("protocol") == "trojan"
        and inbound_tag(inbound).startswith("trojan-tls")
    ]


def managed_connection_inbounds(config):
    return vless_inbounds(config) + trojan_inbounds(config)


def inbound_tag(inbound):
    return inbound.get("tag") or "vless-reality"


def clients(inbound):
    if inbound.get("protocol") == "trojan":
        return inbound.get("settings", {}).get("clients", [])
    return inbound.get("settings", {}).get("clients", [])


def client_name(email):
    if "|created=" in email:
        return email.split("|created=", 1)[0]
    return email


def routing_rules(config):
    return config.get("routing", {}).get("rules", [])


def rule_values(rule, key):
    value = rule.get(key, [])
    if isinstance(value, str):
        return [value]
    return value if isinstance(value, list) else []


class Diagnostics:
    def __init__(self):
        self.context = {}
        self.failures = []
        self.warnings = []

    def check(self, label, func, fatal=True):
        try:
            detail = func()
            line_ok(detail or label)
        except Exception as exc:
            message = f"{label}: {exc}"
            if fatal:
                self.failures.append(message)
                line_fail(message)
            else:
                self.warnings.append(message)
                line_warn(message)

    def summary(self):
        print()
        if self.failures:
            line_fail(f"Diagnostics failed: {len(self.failures)} critical issue(s), {len(self.warnings)} warning(s).")
            return 1
        if self.warnings:
            line_warn(f"Diagnostics finished with {len(self.warnings)} warning(s), no critical failures.")
            return 0
        line_ok("All diagnostics passed.")
        return 0


def check_root():
    if os.geteuid() != 0:
        raise RuntimeError("run xray-test as root")
    return "root privileges"


def check_xray_binary(diag):
    if not XRAY_BIN.exists():
        raise RuntimeError(f"not found: {XRAY_BIN}")
    result = run([str(XRAY_BIN), "version"], timeout=10)
    if result.returncode != 0:
        raise RuntimeError(compact_output(result))
    version = parse_xray_version(result.stdout.splitlines()[0] if result.stdout else "")
    if not version:
        raise RuntimeError("could not parse Xray version")
    diag.context["xray_version"] = version
    return f"{XRAY_BIN} version {version}"


def check_assets():
    missing = [name for name in ("geoip.dat", "geosite.dat") if not (XRAY_ASSET_DIR / name).exists()]
    if missing:
        raise RuntimeError("missing " + ", ".join(missing))
    return "geoip.dat and geosite.dat exist"


def check_helpers():
    missing = []
    for path in HELPER_SCRIPTS:
        item = Path(path)
        if not item.exists() or not os.access(item, os.X_OK):
            missing.append(path)
    if missing:
        raise RuntimeError("missing or not executable: " + ", ".join(missing))
    return "helper scripts are installed and executable"


def check_helper_syntax():
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = "/tmp/xray-test-pycache"
    run_ok(["python3", "-m", "py_compile", *HELPER_SCRIPTS], timeout=30, env=env)
    return "helper Python scripts compile"


def check_manager_package():
    if not MANAGER_PACKAGE_DIR.is_dir():
        raise RuntimeError(f"manager package not installed: {MANAGER_PACKAGE_DIR}")
    files = manager_package_python_files()
    if not files:
        raise RuntimeError(f"manager package has no Python files: {MANAGER_PACKAGE_DIR}")
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = "/tmp/xray-test-pycache"
    run_ok(["python3", "-m", "py_compile", *files], timeout=60, env=env)
    return f"manager Python package installed and compiles: {len(files)} files"


def manager_package_python_files():
    return sorted(
        str(path)
        for path in MANAGER_PACKAGE_DIR.rglob("*.py")
        if not path.name.startswith("._")
    )


def check_config_json(diag):
    config = load_json(CONFIG_PATH)
    diag.context["config"] = config
    return f"{CONFIG_PATH} parsed"


def check_client_db(diag):
    connection = sqlite_ready_connection()
    try:
        db = {
            "connections": sqlite_connections.list_connections(connection),
            "clients": sqlite_clients.list_clients(connection),
        }
    finally:
        connection.close()
    diag.context["client_db"] = db
    diag.context["client_db_source"] = "SQLite"
    return f"{MANAGER_DB_PATH} clients loaded from SQLite"


def check_traffic_db(diag):
    connection = sqlite_ready_connection()
    try:
        diag.context["traffic_db"] = {"clients": sqlite_traffic.list_traffic_entries(connection)}
    finally:
        connection.close()
    diag.context["traffic_db_source"] = "SQLite"
    return f"{MANAGER_DB_PATH} traffic loaded from SQLite"


def check_activity_exceptions_db(diag):
    connection = sqlite_ready_connection()
    try:
        diag.context["activity_exceptions_db"] = {"items": sqlite_activity.list_exceptions(connection)}
    finally:
        connection.close()
    return f"{MANAGER_DB_PATH} activity exceptions loaded from SQLite"


def check_activity_blocklist_db(diag):
    connection = sqlite_ready_connection()
    try:
        items = sqlite_blocklist.list_blocks(connection)
        stats = sqlite_blocklist.list_hit_stats(connection)
    finally:
        connection.close()
    diag.context["activity_blocklist_db"] = {"items": items, "stats": stats}
    return f"{MANAGER_DB_PATH} activity blocklist loaded from SQLite: entries={len(items)}, stats={len(stats)}"


def check_telegram_bot_db(diag):
    connection = sqlite_ready_connection()
    try:
        db = sqlite_telegram_settings_db(connection)
    finally:
        connection.close()
    if not isinstance(db, dict):
        raise RuntimeError("Telegram settings must contain an object")
    mode = db.get("routeMode", "direct")
    if mode not in ("direct", "cascade"):
        raise RuntimeError("Telegram routeMode must be direct or cascade")
    if not isinstance(db.get("botName", "Vireika"), str):
        raise RuntimeError("Telegram botName must be a string")
    if not isinstance(db.get("paymentAmount", ""), str):
        raise RuntimeError("Telegram paymentAmount must be a string")
    if not isinstance(db.get("paymentTotalAmount", ""), str):
        raise RuntimeError("Telegram paymentTotalAmount must be a string")
    if not isinstance(db.get("paymentDomainAnnualAmount", ""), str):
        raise RuntimeError("Telegram paymentDomainAnnualAmount must be a string")
    if db.get("paymentCurrency", "₽") not in ("₽", "$", "€"):
        raise RuntimeError("Telegram paymentCurrency must be ₽, $, or €")
    if db.get("paymentRoundingMode", "none") not in ("none", "step"):
        raise RuntimeError("Telegram paymentRoundingMode must be none or step")
    if not isinstance(db.get("paymentRoundingStep", "10"), str):
        raise RuntimeError("Telegram paymentRoundingStep must be a string")
    if db.get("paymentTransferMethod", "none") not in ("none", "phone", "card", "bank-account"):
        raise RuntimeError("Telegram paymentTransferMethod must be none, phone, card, or bank-account")
    for key in ("paymentPhone", "paymentBank", "paymentCard", "paymentBankAccount"):
        if not isinstance(db.get(key, ""), str):
            raise RuntimeError(f"Telegram {key} must be a string")
    if not isinstance(db.get("clientSubscriptions", {}), dict):
        raise RuntimeError("Telegram clientSubscriptions must be an object")
    if not isinstance(db.get("clientSubscriptionState", {}), dict):
        raise RuntimeError("Telegram clientSubscriptionState must be an object")
    if not isinstance(db.get("dailySummaryState", {}), dict):
        raise RuntimeError("Telegram dailySummaryState must be an object")
    if not isinstance(db.get("adminState", {}), dict):
        raise RuntimeError("Telegram adminState must be an object")
    diag.context["telegram_bot_db"] = db
    return f"{MANAGER_DB_PATH} Telegram settings loaded from SQLite"


def sqlite_ready_connection():
    if not MANAGER_DB_PATH.exists():
        raise RuntimeError(f"not found: {MANAGER_DB_PATH}")
    connection = sqlite_database.open_database(MANAGER_DB_PATH, initialize=False)
    try:
        if not sqlite_read_ready(connection):
            raise RuntimeError("SQLite read-ready metadata is not true")
        return connection
    except Exception:
        connection.close()
        raise


def sqlite_telegram_settings_db(connection):
    db = {
        "routeMode": "direct",
        "botName": "Vireika",
        "paymentAmount": "",
        "paymentTotalAmount": "",
        "paymentDomainAnnualAmount": "",
        "paymentCurrency": "₽",
        "paymentRoundingMode": "none",
        "paymentRoundingStep": "10",
        "paymentTransferMethod": "none",
        "paymentPhone": "",
        "paymentBank": "",
        "paymentCard": "",
        "paymentBankAccount": "",
        "clientSubscriptions": {},
        "clientSubscriptionState": {},
        "dailySummaryState": {},
        "adminState": {},
    }
    for key, value in sqlite_telegram.list_settings(connection).items():
        db[key] = value
    for item in sqlite_telegram.list_subscriptions(connection):
        chat_id = str(item.get("chatId") or "")
        if chat_id:
            db["clientSubscriptions"][chat_id] = {
                "clientId": item.get("clientUuid", ""),
                "client": item.get("clientName", ""),
            }
    return db


def table_count(connection, table):
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def table_values(connection, table, column):
    rows = connection.execute(f"SELECT {column} FROM {table}").fetchall()
    return {str(row[column]) for row in rows}


def check_sqlite_database(diag, full_integrity=False):
    if not MANAGER_DB_PATH.exists():
        raise RuntimeError(f"not found: {MANAGER_DB_PATH}; run install.sh or restore a backup with manager.db")

    connection = sqlite_database.open_database(MANAGER_DB_PATH, initialize=False)
    try:
        quick_check_status = "skipped; run xray-test --all for full SQLite integrity scan"
        if full_integrity:
            quick_check = sqlite_database.quick_check(connection)
            if quick_check != "ok":
                raise RuntimeError(f"PRAGMA quick_check returned: {quick_check}")
            quick_check_status = "ok"

        version = sqlite_schema.schema_version(connection)
        if version != sqlite_schema.CURRENT_SCHEMA_VERSION:
            raise RuntimeError(f"schema version {version}, expected {sqlite_schema.CURRENT_SCHEMA_VERSION}")

        ready = sqlite_read_ready(connection)
        if not ready:
            raise RuntimeError("SQLite read-ready metadata is not true")

        counts = {
            "connections": table_count(connection, "reality_connections"),
            "clients": table_count(connection, "clients"),
            "traffic": table_count(connection, "traffic_totals"),
            "activityEvents": table_count(connection, "activity_events"),
            "activityExceptions": table_count(connection, "activity_exceptions"),
            "activityBlocklist": table_count(connection, "activity_blocklist"),
            "activityBlocklistHits": table_count(connection, "activity_blocklist_hits"),
            "telegramSubscriptions": table_count(connection, "telegram_subscriptions"),
        }
        diag.context["sqlite_counts"] = counts
        if ready:
            issues = sqlite_alignment_issues(diag, connection)
            if issues:
                raise RuntimeError("; ".join(issues[:8]))

        count_text = ", ".join(f"{key}={value}" for key, value in counts.items())
        return f"{MANAGER_DB_PATH} schema={version}, quick_check={quick_check_status}, sqliteReady=yes, {count_text}"
    finally:
        connection.close()


def sqlite_alignment_issues(diag, connection):
    issues = []

    client_db = diag.context.get("client_db", {})
    clients_section = client_db.get("clients", {}) if isinstance(client_db.get("clients", {}), dict) else {}
    connections_section = client_db.get("connections", {}) if isinstance(client_db.get("connections", {}), dict) else {}
    for name, entry in clients_section.items():
        if not isinstance(entry, dict):
            issues.append(f"SQLite client record is invalid: {name}")
            continue
        connection_tag = str(entry.get("connection") or "").strip()
        if connection_tag and connection_tag not in connections_section:
            issues.append(f"SQLite client has missing connection: {name} -> {connection_tag}")

    sqlite_clients = table_values(connection, "clients", "name")
    sqlite_traffic = table_values(connection, "traffic_totals", "client_name")
    stale_traffic = sqlite_traffic - sqlite_clients
    if stale_traffic:
        issues.append(format_set_difference("SQLite traffic has unknown clients", set(), stale_traffic))

    sqlite_subscriptions = {
        value
        for value in table_values(connection, "telegram_subscriptions", "client_name")
        if value
    }
    stale_subscriptions = sqlite_subscriptions - sqlite_clients
    if stale_subscriptions:
        issues.append(format_set_difference("SQLite Telegram subscriptions have unknown clients", set(), stale_subscriptions))

    return [issue for issue in issues if issue]


def format_set_difference(label, expected, actual):
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    parts = []
    if missing:
        parts.append("missing: " + ", ".join(missing[:8]))
    if extra:
        parts.append("extra: " + ", ".join(extra[:8]))
    return f"{label} ({'; '.join(parts)})" if parts else ""


def check_server_env(diag):
    values = read_server_env(SERVER_ENV_PATH, strict=True, require_exists=True)
    diag.context["server_env"] = values
    missing = [key for key in ("SERVER_ADDR", "SERVER_NAME", "PORT", "REALITY_SNI", "REALITY_DEST", "FINGERPRINT") if key not in values]
    if missing:
        raise RuntimeError("missing keys: " + ", ".join(missing))
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", values.get("SERVER_NAME", "")):
        raise RuntimeError("SERVER_NAME must be 1-64 chars: A-Z a-z 0-9 _ . @ -")
    if values.get("REALITY_SNI") and values.get("REALITY_DEST") != f"{values['REALITY_SNI']}:443":
        raise RuntimeError("REALITY_DEST must be REALITY_SNI:443")
    return f"{SERVER_ENV_PATH} parsed"


def check_manager_timezone(diag):
    value = (diag.context.get("server_env", {}).get("MANAGER_TIMEZONE") or "").strip()
    if not value:
        return "MANAGER_TIMEZONE uses server local time"
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        raise RuntimeError(f"invalid MANAGER_TIMEZONE: {value}")
    return f"MANAGER_TIMEZONE={value}"


def check_reality_inbounds(diag):
    config = diag.context["config"]
    inbounds = reality_inbounds(config)
    if not inbounds:
        raise RuntimeError("VLESS Reality inbound not found")

    seen_tags = set()
    seen_ports = set()
    names = set()
    summary = []
    for inbound in inbounds:
        tag = inbound_tag(inbound)
        if tag in seen_tags:
            raise RuntimeError(f"duplicate inbound tag: {tag}")
        seen_tags.add(tag)

        try:
            port = int(inbound.get("port", 0))
        except (TypeError, ValueError):
            raise RuntimeError(f"{tag}: invalid port")
        if port < 1 or port > 65535:
            raise RuntimeError(f"{tag}: invalid port {port}")
        if port in seen_ports:
            raise RuntimeError(f"duplicate Reality port: {port}")
        seen_ports.add(port)

        reality = inbound.get("streamSettings", {}).get("realitySettings", {})
        sni = (reality.get("serverNames") or [""])[0]
        dest = reality.get("dest", "")
        private_key = reality.get("privateKey", "")
        short_ids = reality.get("shortIds", [])
        if not sni or not dest or not private_key or not short_ids:
            raise RuntimeError(f"{tag}: incomplete Reality settings")
        if dest != f"{sni}:443":
            raise RuntimeError(f"{tag}: Reality dest must be SNI:443, got {dest}")
        stream = inbound.get("streamSettings", {})
        network = (stream.get("network") or "tcp").lower()
        if network not in ("tcp", "grpc", "xhttp"):
            raise RuntimeError(f"{tag}: unsupported Reality transport: {network}")
        if network == "grpc" and "serviceName" not in (stream.get("grpcSettings") or {}):
            raise RuntimeError(f"{tag}: grpcSettings.serviceName is required")
        if network == "xhttp":
            xhttp = stream.get("xhttpSettings") or {}
            if not xhttp.get("path") or not xhttp.get("mode"):
                raise RuntimeError(f"{tag}: xhttpSettings.path and mode are required")

        for client in clients(inbound):
            client_id = client.get("id", "")
            email = client.get("email", "")
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", client_id):
                raise RuntimeError(f"{tag}: client has invalid UUID")
            if not email:
                raise RuntimeError(f"{tag}: client has empty email")
            if network == "tcp" and client.get("flow") != "xtls-rprx-vision":
                raise RuntimeError(f"{tag}: TCP Vision client has invalid flow")
            if network != "tcp" and client.get("flow"):
                raise RuntimeError(f"{tag}: {network} client must not use Vision flow")
            name = client_name(email)
            if name in names:
                raise RuntimeError(f"duplicate active client name: {name}")
            names.add(name)
        summary.append(f"{tag}:{port}")

    diag.context["reality_inbounds"] = inbounds
    diag.context["reality_tags"] = seen_tags
    diag.context["reality_ports"] = sorted(seen_ports)
    return "Reality connections OK: " + ", ".join(summary)


def check_reality_ports(diag):
    ports = diag.context.get("reality_ports", [])
    for port in ports:
        check_tcp("127.0.0.1", port)
    return "Reality TCP ports accept local connections: " + ", ".join(str(port) for port in ports)


def check_config_test():
    run_ok([str(XRAY_BIN), "run", "-test", "-config", str(CONFIG_PATH)], timeout=30)
    return "Xray configuration OK"


def check_xray_service():
    if not is_active("xray"):
        raise RuntimeError("xray.service is not active")
    return "xray.service active"


def check_stats_config(diag):
    config = diag.context["config"]
    services = config.get("api", {}).get("services", [])
    if "StatsService" not in services:
        raise RuntimeError("api.services must include StatsService")
    if not isinstance(config.get("stats"), dict):
        raise RuntimeError("stats object is missing")
    level0 = config.get("policy", {}).get("levels", {}).get("0", {})
    if level0.get("statsUserUplink") is not True or level0.get("statsUserDownlink") is not True:
        raise RuntimeError("policy.levels.0 must enable user traffic stats")
    if not any(item.get("tag") == "api" for item in config.get("inbounds", [])):
        raise RuntimeError("api inbound is missing")
    api_rule = any("api" in rule_values(rule, "inboundTag") and rule.get("outboundTag") == "api" for rule in routing_rules(config))
    if not api_rule:
        raise RuntimeError("routing rule for api inbound is missing")
    return "Stats API config is enabled"


def check_stats_runtime():
    run_ok([str(XRAY_BIN), "api", "statsquery", f"--server={STATS_SERVER}", "-pattern", "user>>>"], timeout=10)
    return f"Stats API responds on {STATS_SERVER}"


def check_timers():
    inactive = [unit for unit in ("xray-traffic-sync.timer", "xray-client-expire.timer") if not is_active(unit)]
    if inactive:
        raise RuntimeError("inactive timers: " + ", ".join(inactive))
    return "traffic and expiry timers active"


def check_traffic_sync_service():
    result = run(["systemctl", "cat", "xray-traffic-sync.service"], timeout=10)
    if result.returncode != 0:
        raise RuntimeError(compact_output(result))
    missing = [line for line in TRAFFIC_SERVICE_COMMANDS if line not in result.stdout]
    if missing:
        raise RuntimeError("missing service command(s): " + ", ".join(missing))
    return "xray-traffic-sync.service runs traffic sync, activity sync, Telegram notifications, limits, expiry checks, and daily summaries"


def check_telegram_poller_service():
    result = run(["systemctl", "cat", "xray-telegram-poller.service"], timeout=10)
    if result.returncode != 0:
        raise RuntimeError(compact_output(result))
    if TELEGRAM_POLLER_SERVICE_COMMAND not in result.stdout:
        raise RuntimeError("missing service command: " + TELEGRAM_POLLER_SERVICE_COMMAND)
    if not is_active("xray-telegram-poller.service"):
        raise RuntimeError("xray-telegram-poller.service is not active")
    return "xray-telegram-poller.service active for Telegram user long polling"


def check_traffic_sync_runtime():
    run_ok(["/usr/local/sbin/xray-traffic-sync", "--quiet"], timeout=15)
    return "xray-traffic-sync runs successfully"


def check_activity_runtime():
    run_ok(["/usr/local/sbin/xray-activity", "status"], timeout=20)
    return "xray-activity status runs successfully"


def check_telegram_runtime():
    run_ok(["/usr/local/sbin/xray-telegram", "status"], timeout=20)
    return "xray-telegram status runs successfully"


def check_client_list_runtime():
    run_ok(["/usr/local/sbin/xray-client", "list"], timeout=20)
    return "xray-client list runs successfully"


def check_client_db_alignment(diag):
    db = diag.context.get("client_db", {})
    inbounds = managed_connection_inbounds(diag.context.get("config", {}))
    known_tags = {inbound_tag(inbound) for inbound in inbounds}
    active_names = set()
    for inbound in inbounds:
        for client in clients(inbound):
            active_names.add(client_name(client.get("email", "")))

    problems = []
    for name, entry in db.get("clients", {}).items():
        connection = entry.get("connection", "")
        if connection and connection not in known_tags:
            problems.append(f"{name}: unknown connection {connection}")
        if entry.get("enabled") is not False and name not in active_names:
            problems.append(f"{name}: enabled in SQLite but absent from config")
    if problems:
        raise RuntimeError("; ".join(problems[:8]))
    source = diag.context.get("client_db_source", "SQLite")
    return f"{source} matches active managed connections"


def check_traffic_db_alignment(diag):
    client_names = set(diag.context.get("client_db", {}).get("clients", {}).keys())
    traffic_names = set(diag.context.get("traffic_db", {}).get("clients", {}).keys())
    stale = sorted(traffic_names - client_names)
    diag.context["stale_traffic_clients"] = stale
    if stale:
        source = diag.context.get("traffic_db_source", "SQLite")
        raise RuntimeError(f"{source} has stale clients: " + ", ".join(stale[:8]))
    source = diag.context.get("traffic_db_source", "SQLite")
    return f"{source} client rows are known"


def manager_timezone(diag):
    value = (diag.context.get("server_env", {}).get("MANAGER_TIMEZONE") or "").strip()
    if not value:
        return datetime.now().astimezone().tzinfo
    return ZoneInfo(value)


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def access_expired(entry, now):
    expires_at = parse_datetime(entry.get("expiresAt", ""))
    if expires_at is None:
        return False
    return now >= expires_at.astimezone(now.tzinfo)


def traffic_bucket_totals(bucket):
    if not isinstance(bucket, dict):
        return 0
    return int(bucket.get("incoming", 0) or 0) + int(bucket.get("outgoing", 0) or 0)


def day_total(entry, day):
    history = entry.get("history", {})
    if not isinstance(history, dict):
        return 0
    hours = history.get(day.isoformat(), {})
    if not isinstance(hours, dict):
        return 0
    return sum(traffic_bucket_totals(bucket) for bucket in hours.values())


def month_total(entry, month_key):
    year, month = (int(part, 10) for part in month_key.split("-", 1))
    current = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    total = 0
    while current <= end:
        total += day_total(entry, current)
        current = date.fromordinal(current.toordinal() + 1)
    return total


def subtract_months(day, months):
    month = day.month - months
    year = day.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def check_traffic_history_consistency(diag):
    client_db = diag.context.get("client_db", {})
    traffic_db = diag.context.get("traffic_db", {})
    now = datetime.now(manager_timezone(diag)).replace(microsecond=0)
    cutoff = subtract_months(now.date(), 6)
    gaps = traffic_consistency.retained_history_gaps(
        traffic_db,
        client_db.get("clients", {}),
        cutoff,
    )
    if gaps:
        details = ", ".join(f"{gap.name}: missing {gap.missing_total} bytes" for gap in gaps[:8])
        raise RuntimeError("traffic history is behind retained totals: " + details)
    return "traffic history matches retained totals"


def traffic_limit_status(client_entry, traffic_entry, now):
    limit = client_entry.get("trafficLimit")
    if not isinstance(limit, dict):
        return None
    period = limit.get("period")
    try:
        limit_bytes = int(limit.get("bytes", 0) or 0)
    except (TypeError, ValueError):
        return None
    if period not in ("daily", "monthly") or limit_bytes <= 0:
        return None

    if period == "daily":
        period_key = now.date().isoformat()
        used = day_total(traffic_entry, now.date())
    else:
        period_key = now.strftime("%Y-%m")
        used = month_total(traffic_entry, period_key)
    return {
        "periodKey": period_key,
        "limitBytes": limit_bytes,
        "usedBytes": used,
        "exceeded": used >= limit_bytes,
    }


def check_traffic_limit_reset_state(diag):
    client_db = diag.context.get("client_db", {})
    traffic_db = diag.context.get("traffic_db", {})
    traffic_clients = traffic_db.get("clients", {})
    now = datetime.now(manager_timezone(diag)).replace(microsecond=0)
    stale = []

    for name, entry in client_db.get("clients", {}).items():
        if entry.get("enabled") is not False or entry.get("disabledReason") != "traffic-limit":
            continue
        status = traffic_limit_status(entry, traffic_clients.get(name, {}), now)
        if not status or status["exceeded"] or access_expired(entry, now):
            continue
        exceeded_period = entry.get("trafficLimitExceededPeriod", "")
        if exceeded_period and exceeded_period == status["periodKey"]:
            continue
        stale.append(f"{name}: current period {status['periodKey']} uses {status['usedBytes']} of {status['limitBytes']} bytes")

    if stale:
        raise RuntimeError("traffic-limit clients are eligible for auto re-enable: " + "; ".join(stale[:8]))
    return "traffic-limit reset state OK"


def check_torrent_policy(diag):
    blocked = any("bittorrent" in rule_values(rule, "protocol") and rule.get("outboundTag") == BLOCKED_TAG for rule in routing_rules(diag.context["config"]))
    if blocked:
        return "BitTorrent traffic is blocked"
    return "BitTorrent traffic is allowed by current settings"


def check_cascade_config(diag):
    config = diag.context["config"]
    outbounds = cascade_config.cascade_outbounds(config)
    if not outbounds:
        return "Cascade outbound is not configured"
    tags = {item.get("tag") for item in outbounds}
    for outbound in outbounds:
        tag = outbound.get("tag") or "cascade-unknown"
        if outbound.get("protocol") != "vless":
            raise RuntimeError(f"{tag} must use protocol=vless")
        vnext = outbound.get("settings", {}).get("vnext", [])
        if not vnext or not vnext[0].get("address") or not vnext[0].get("port"):
            raise RuntimeError(f"{tag} has incomplete vnext settings")
    active = cascade_config.active_cascade_tag(config)
    catchall = cascade_config.current_catchall_tag(config)
    if cascade_config.is_cascade_tag(catchall) and catchall not in tags:
        raise RuntimeError(f"active cascade route points to missing outbound: {catchall}")
    if active:
        return f"Cascade config is present; active route: {active}; deep network test remains in Cascade menu"
    return f"Cascade config is present but not active; configured: {len(outbounds)}"


def is_catchall_rule(rule, tag):
    if rule.get("type") != "field" or rule.get("outboundTag") != tag:
        return False
    if rule.get("network") != "tcp,udp":
        return False
    for key in ("domain", "ip", "protocol", "inboundTag", "port", "source", "sourcePort", "attrs"):
        if key in rule:
            return False
    return True


def check_warp_config(diag):
    config = diag.context["config"]
    outbound = next((item for item in config.get("outbounds", []) if item.get("tag") == WARP_OUTBOUND_TAG), None)
    route = any(is_catchall_rule(rule, WARP_OUTBOUND_TAG) for rule in routing_rules(config))
    if not outbound and not route:
        return "WARP outbound is not configured"
    if not outbound:
        raise RuntimeError("WARP route is enabled, but warp-out outbound is missing")
    if outbound.get("protocol") != "wireguard":
        raise RuntimeError("warp-out must use protocol=wireguard")
    settings = outbound.get("settings", {})
    if not settings.get("secretKey") or not settings.get("address") or not settings.get("peers"):
        raise RuntimeError("warp-out has incomplete WireGuard settings")
    peer = settings.get("peers", [{}])[0]
    if not peer.get("endpoint") or not peer.get("publicKey"):
        raise RuntimeError("warp-out peer must include endpoint and publicKey")
    if route:
        return "WARP config is present and enabled as catch-all"
    return "WARP config is present but disabled"


def is_geoip_warning_tag(tag):
    return str(tag or "").startswith(GEOIP_WARNING_PREFIX)


def check_geoip_warning_routing(diag):
    config = diag.context["config"]
    env = diag.context.get("server_env", {})
    old_code = (env.get("ACTIVITY_GEOIP_WARNING_CODE") or "").strip()
    code = (env.get("ACTIVITY_XRAY_GEOIP_WARNING_CODE") or "").strip().upper()
    outbounds = config.get("outbounds", [])
    outbound_tags = {item.get("tag") for item in outbounds if is_geoip_warning_tag(item.get("tag"))}
    rules = [rule for rule in routing_rules(config) if is_geoip_warning_tag(rule.get("outboundTag"))]

    if old_code:
        raise RuntimeError("old ACTIVITY_GEOIP_WARNING_CODE is still present in server.env")
    if not code and not outbound_tags and not rules:
        return "GeoIP routing warnings are disabled"
    if not code:
        raise RuntimeError("geoip-warning route/outbound exists, but ACTIVITY_XRAY_GEOIP_WARNING_CODE is empty")
    strategy = config.get("routing", {}).get("domainStrategy", "")
    if strategy != "IPOnDemand":
        raise RuntimeError(f"GeoIP warning routing requires routing.domainStrategy=IPOnDemand, got {strategy or 'empty'}")

    expected_tag = f"{GEOIP_WARNING_PREFIX}{code}"
    expected_ip = f"geoip:{code.lower()}"
    extra_tags = sorted(tag for tag in outbound_tags if tag != expected_tag)
    if extra_tags:
        raise RuntimeError("unexpected GeoIP warning outbound(s): " + ", ".join(extra_tags))
    if expected_tag not in outbound_tags:
        raise RuntimeError(f"missing GeoIP warning outbound: {expected_tag}")

    matching_rules = [
        rule
        for rule in rules
        if rule.get("outboundTag") == expected_tag and expected_ip in rule_values(rule, "ip")
    ]
    if not matching_rules:
        raise RuntimeError(f"missing routing rule {expected_ip} -> {expected_tag}")
    return f"GeoIP routing warnings enabled for {code}"


def blocked_rule_index_before_geoip(config, key, value):
    rules = routing_rules(config)
    geoip_indexes = [index for index, rule in enumerate(rules) if is_geoip_warning_tag(rule.get("outboundTag"))]
    first_geoip = min(geoip_indexes) if geoip_indexes else len(rules)
    for index, rule in enumerate(rules):
        if index >= first_geoip:
            break
        if rule.get("outboundTag") == BLOCKED_TAG and value in rule_values(rule, key):
            return index
    return None


def check_activity_blocklist_routing(diag):
    config = diag.context["config"]
    connection = sqlite_ready_connection()
    try:
        active_items = sqlite_blocklist.active_blocks(connection, activity_time.utc_stamp())
    finally:
        connection.close()
    if not active_items:
        return "Activity global blocklist is empty"

    domains, ips = xray_blocklist.split_rule_values(active_items)
    missing = []
    for value in domains:
        if blocked_rule_index_before_geoip(config, "domain", value) is None:
            missing.append(f"domain {value}")
    for value in ips:
        if blocked_rule_index_before_geoip(config, "ip", value) is None:
            missing.append(f"ip {value}")
    if missing:
        raise RuntimeError("active blocklist entries are missing before GeoIP warning routing: " + ", ".join(missing[:8]))
    return f"Activity global blocklist routing is active: entries={len(active_items)}"


def check_file_permissions():
    checked = []
    for path in (
        CONFIG_PATH,
        SERVER_ENV_PATH,
        MANAGER_DB_PATH,
    ):
        if not path.exists():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o007:
            raise RuntimeError(f"{path} is readable by others: mode {mode:o}")
        checked.append(f"{path.name}={mode:o}")
    return "sensitive file permissions OK: " + ", ".join(checked)


def run_diagnostics(full_integrity=False):
    diag = Diagnostics()
    diag.check("Root privileges", check_root)
    diag.check("Xray binary", lambda: check_xray_binary(diag))
    diag.check("Xray assets", check_assets)
    diag.check("Helper scripts", check_helpers)
    diag.check("Helper script syntax", check_helper_syntax)
    diag.check("Manager package", check_manager_package)
    diag.check("Config JSON", lambda: check_config_json(diag))
    diag.check("Clients DB", lambda: check_client_db(diag))
    diag.check("Traffic DB", lambda: check_traffic_db(diag))
    diag.check("Activity exceptions DB", lambda: check_activity_exceptions_db(diag))
    diag.check("Activity blocklist DB", lambda: check_activity_blocklist_db(diag))
    diag.check("Telegram bot DB", lambda: check_telegram_bot_db(diag))
    diag.check("Manager SQLite DB", lambda: check_sqlite_database(diag, full_integrity=full_integrity), fatal=False)
    diag.check("server.env", lambda: check_server_env(diag))
    diag.check("Manager timezone", lambda: check_manager_timezone(diag))
    diag.check("Reality connections", lambda: check_reality_inbounds(diag))
    diag.check("Xray config test", check_config_test)
    diag.check("xray.service", check_xray_service)
    diag.check("Reality TCP ports", lambda: check_reality_ports(diag))
    diag.check("Stats API config", lambda: check_stats_config(diag))
    diag.check("Stats API runtime", check_stats_runtime)
    diag.check("Systemd timers", check_timers)
    diag.check("Traffic sync service", check_traffic_sync_service)
    diag.check("Telegram poller service", check_telegram_poller_service)
    diag.check("Traffic sync runtime", check_traffic_sync_runtime)
    diag.check("Activity log runtime", check_activity_runtime)
    diag.check("Telegram bot runtime", check_telegram_runtime)
    diag.check("xray-client list", check_client_list_runtime)
    diag.check("Client DB alignment", lambda: check_client_db_alignment(diag), fatal=False)
    diag.check("Traffic DB alignment", lambda: check_traffic_db_alignment(diag), fatal=False)
    diag.check("Traffic history consistency", lambda: check_traffic_history_consistency(diag), fatal=False)
    diag.check("Traffic limit reset state", lambda: check_traffic_limit_reset_state(diag))
    diag.check("Torrent policy", lambda: check_torrent_policy(diag))
    diag.check("Cascade config", lambda: check_cascade_config(diag))
    diag.check("WARP config", lambda: check_warp_config(diag))
    diag.check("GeoIP warning routing", lambda: check_geoip_warning_routing(diag), fatal=False)
    diag.check("Activity blocklist routing", lambda: check_activity_blocklist_routing(diag), fatal=False)
    diag.check("Sensitive file permissions", check_file_permissions, fatal=False)
    return diag.summary()


def usage():
    print("""Usage:
  xray-test
  xray-test --all

Default mode skips the full SQLite PRAGMA quick_check scan.
Use --all when you need a deep SQLite physical integrity check.
""")


def main():
    args = sys.argv[1:]
    if len(args) > 1 or any(arg not in ("--all", "all") for arg in args):
        usage()
        sys.exit(1)
    sys.exit(run_diagnostics(full_integrity=bool(args)))


if __name__ == "__main__":
    main()
