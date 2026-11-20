"""Чтение и вывод списка клиентов, ключей, лимитов и отчётов трафика."""

from datetime import timedelta
import re
from subprocess import TimeoutExpired
import sys

from xray_vps_manager.clients import access as client_access
from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.clients import listing as client_listing
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.clients import runtime as client_runtime
from xray_vps_manager.clients import service as client_service
from xray_vps_manager.clients import settings as client_settings
from xray_vps_manager.clients.service import sync_traffic
from xray_vps_manager.commands.client_arguments import parse_date_value, parse_month_value
from xray_vps_manager.commands.client_output import (
    access_key_for_client_id, connection_display_name, ensure_connections, format_traffic, link_for, validate_name,
)
from xray_vps_manager.core.paths import paths
from xray_vps_manager.core.process import run_capture
from xray_vps_manager.core.terminal import print_table
from xray_vps_manager.runner import fail
from xray_vps_manager.traffic import limits as traffic_limits
from xray_vps_manager.traffic import reports as traffic_reports
from xray_vps_manager.traffic import repository as traffic_repository

STATS_SERVER = "127.0.0.1:10085"
GREEN = "\033[92m"
RED = "\033[31m"
YELLOW = "\033[93m"
RESET = "\033[0m"


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
    if value == "missing":
        return f"{YELLOW}{text}{RESET}"
    return text


def query_user_stats():
    try:
        result = run_capture(
            [
                str(paths().xray_bin),
                "api",
                "statsquery",
                f"--server={STATS_SERVER}",
                "-pattern",
                "user>>>",
            ],
            timeout=4,
        )
    except (FileNotFoundError, TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    stats = {}
    for name, value in re.findall(r'name:\s*"([^"]+)".*?value:\s*([0-9]+)', result.stdout, re.S):
        stats[name] = int(value)
    return stats


def print_client_table(rows):
    headers = ["NAME", "STATUS", "PAYMENT", "COUNTRY", "ONLINE", "LAST ONLINE", "TOTAL", "LIMIT", "TRAFFIC UPDATED", "ACCESS UNTIL", "CREDS"]
    color_columns = {headers.index("STATUS"), headers.index("PAYMENT"), headers.index("ONLINE"), headers.index("ACCESS UNTIL")}
    print_table(headers, rows, empty_message=None, color_columns=color_columns, colorizer=color_label)


def print_credential_table(rows):
    headers = ["CLIENT", "PROTOCOL", "SECURITY", "TRANSPORT", "CONNECTION", "STATUS", "ONLINE", "TRAFFIC", "TRAFFIC UPDATED", "LAST ONLINE"]
    color_columns = {headers.index("STATUS"), headers.index("ONLINE")}
    print_table(headers, rows, empty_message=None, color_columns=color_columns, colorizer=color_label)


def cmd_list():
    config, db = client_service.read_state()
    ensure_connections(config, db)
    rows = client_listing.client_rows(config, db, resolve_countries=True)
    if not rows:
        print("No clients.")
        return
    sync_traffic()
    traffic_db = traffic_repository.load_traffic_db_for_read()
    stats = query_user_stats()
    display_timezone = client_settings.manager_timezone()
    table_rows = []
    for row in rows:
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
                last_online,
                format_traffic(total),
                traffic_limits.format_traffic_limit(db_entry),
                traffic_updated,
                client_access.format_access_until(row["expiresAt"]),
                f"{row.get('credentialsActive', 0)}/{row.get('credentialsTotal', 0)}",
            ]
        )
    print()
    print("Clients")
    print_client_table(table_rows)

    credential_table_rows = []
    for row in client_listing.credential_rows(config, db):
        incoming, outgoing = client_runtime.credential_traffic_for(traffic_db, stats, row)
        total = None if incoming is None or outgoing is None else incoming + outgoing
        online, last_online = client_runtime.credential_online_state(row, traffic_db, display_timezone)
        traffic_updated = client_runtime.credential_traffic_updated_at(row, traffic_db, display_timezone)
        credential_table_rows.append(
            [
                row["client"],
                row["protocol"],
                row["security"] or "-",
                row["transport"] or "-",
                row["connection"],
                row["status"],
                online,
                format_traffic(total),
                traffic_updated,
                last_online,
            ]
        )
    print()
    print("Credentials")
    print_credential_table(credential_table_rows)


def known_for_traffic_report(config, db, traffic_db, name):
    if name in client_repository.db_clients(db):
        return True
    if name in traffic_db.get("clients", {}):
        return True
    return any(row["name"] == name for row in client_listing.client_rows(config, db))


def traffic_report_context(name):
    validate_name(name)
    sync_traffic()
    config, db = client_service.read_state()
    ensure_connections(config, db)
    traffic_db = traffic_repository.load_traffic_db_for_read()
    if not known_for_traffic_report(config, db, traffic_db, name):
        fail(f"Client not found: {name}")
    return config, db, traffic_db, traffic_repository.traffic_entry(traffic_db, name)


def cmd_traffic_summary(month_value=None):
    month_key = parse_month_value(month_value)
    sync_traffic()
    config, db = client_service.read_state()
    ensure_connections(config, db)
    rows = client_listing.client_rows(config, db)
    if not rows:
        print("No clients.")
        return

    traffic_db = traffic_repository.load_traffic_db_for_read()
    table_rows = traffic_reports.month_summary_rows(
        rows,
        traffic_db,
        client_repository.db_clients(db),
        month_key,
        connection_label=lambda row: connection_display_name(config, db, row["connection"]),
        limit_label=traffic_limits.format_traffic_limit,
        today=client_access.local_now().date(),
    )

    print(f"Month: {month_key}")
    print_table(["NAME", "STATUS", "CONNECTION", "IN", "OUT", "TOTAL", "LIMIT", "ALL TIME"], table_rows, empty_message=None)


def traffic_credential_rows(config, db, name):
    return [row for row in client_listing.credential_rows(config, db) if row["client"] == name]


def print_traffic_credential_period(config, db, traffic_db, name, start, end):
    credentials = traffic_credential_rows(config, db, name)
    if len(credentials) <= 1:
        return
    print()
    print("Credentials")
    print_table(
        ["PROTOCOL", "SECURITY", "TRANSPORT", "CONNECTION", "STATUS", "IN", "OUT", "TOTAL"],
        traffic_reports.credential_period_rows(credentials, traffic_db, start, end),
        empty_message=None,
    )


def cmd_traffic_day(name, day_value=None):
    day = parse_date_value(day_value or client_access.local_now().date().isoformat())
    config, db, traffic_db, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Day: {day.isoformat()} (timezone: {client_settings.manager_timezone_label()})")
    print_table(["HOUR", "IN", "OUT", "TOTAL"], traffic_reports.day_hour_rows(entry, day), empty_message=None)
    print_traffic_credential_period(config, db, traffic_db, name, day, day)


def cmd_traffic_week(name, start_value=None):
    start = parse_date_value(start_value or (client_access.local_now().date() - timedelta(days=6)).isoformat(), "START_DATE")
    end = start + timedelta(days=6)
    config, db, traffic_db, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Period: {start.isoformat()}..{end.isoformat()} (timezone: {client_settings.manager_timezone_label()})")
    print_table(["DATE", "IN", "OUT", "TOTAL"], traffic_reports.period_day_rows(entry, start, end), empty_message=None)
    print_traffic_credential_period(config, db, traffic_db, name, start, end)


def cmd_traffic_month(name, month_value=None):
    month_key = parse_month_value(month_value)
    start, end = traffic_reports.month_bounds(month_key, today=client_access.local_now().date())
    config, db, traffic_db, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Month: {month_key} (timezone: {client_settings.manager_timezone_label()})")
    print_table(["DATE", "IN", "OUT", "TOTAL"], traffic_reports.period_day_rows(entry, start, end), empty_message=None)
    print_traffic_credential_period(config, db, traffic_db, name, start, end)


def cmd_traffic_period(name, start_value, end_value):
    start = parse_date_value(start_value, "START_DATE")
    end = parse_date_value(end_value, "END_DATE")
    if end < start:
        fail("END_DATE must be the same as or later than START_DATE.")
    config, db, traffic_db, entry = traffic_report_context(name)
    print(f"Client: {name}")
    print(f"Period: {start.isoformat()}..{end.isoformat()} (timezone: {client_settings.manager_timezone_label()})")
    print_table(["DATE", "IN", "OUT", "TOTAL"], traffic_reports.period_day_rows(entry, start, end), empty_message=None)
    print_traffic_credential_period(config, db, traffic_db, name, start, end)


def cmd_trojan_password_check():
    config, db = client_service.read_state()
    rows = client_crud.trojan_password_policy_rows(config, db)
    if not rows:
        print("No Trojan credentials.")
        return
    table_rows = [
        [row["client"], row["connection"], row["status"], row["issues"]]
        for row in rows
    ]
    print_table(["CLIENT", "CONNECTION", "STATUS", "ISSUES"], table_rows, empty_message=None)
    if any(row["status"] != "OK" for row in rows):
        fail("Trojan password policy check failed.")


def cmd_limit_list():
    sync_traffic()
    config, db = client_service.read_state()
    ensure_connections(config, db)
    traffic_db = traffic_repository.load_traffic_db_for_read()
    rows = client_listing.client_rows(config, db)
    if not rows:
        print("No clients.")
        return
    table_rows = [
        traffic_limits.traffic_limit_row(
            row,
            client_repository.db_clients(db),
            traffic_db,
            connection_display_name(config, db, row["connection"]),
        )
        for row in rows
    ]
    print_table(["NAME", "STATUS", "CONNECTION", "LIMIT", "USED", "REMAINING", "RESET"], table_rows, empty_message=None)


def cmd_link(name, connection_tag=None):
    validate_name(name)
    config, db = client_service.read_state()
    ensure_connections(config, db)
    links = []
    for row in client_listing.credential_rows(config, db):
        if row["name"] != name:
            continue
        if connection_tag and row["connection"] != connection_tag:
            continue
        links.append(row)
    if links:
        if len(links) == 1:
            row = links[0]
            print(link_for(config, row["id"], name, row["connection"], db))
            return
        for row in links:
            print(f"[{row['protocol']} {row['connection']}]")
            print(link_for(config, row["id"], name, row["connection"], db))
        return
    fail(f"Client not found: {name}")


def cmd_key(name):
    validate_name(name)
    config, db = client_service.read_state()
    ensure_connections(config, db)
    entry = client_repository.db_clients(db).get(name)
    if not entry:
        fail(f"Client not found: {name}")
    client_id = str(entry.get("id") or "").strip()
    if not client_id:
        fail(f"Client has no internal UUID: {name}")
    print(access_key_for_client_id(client_id))


def cmd_timezone():
    zone = client_settings.manager_timezone_info()
    current = client_access.local_now()
    print(f"MANAGER_TIMEZONE: {zone.label}")
    print(f"Current time: {current.strftime('%Y-%m-%d %H:%M:%S %Z')}")
