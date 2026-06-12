#!/usr/bin/env python3
import fcntl
import os
import shlex
import signal
import sys
from datetime import date

from xray_vps_manager.activity.constants import EXPORT_DIR, LOCK_PATH
from xray_vps_manager.activity import client_reports as activity_client_reports
from xray_vps_manager.activity import controls as activity_controls
from xray_vps_manager.activity import exception_reports as activity_exception_reports
from xray_vps_manager.activity import exports as activity_exports
from xray_vps_manager.activity import parser as activity_parser
from xray_vps_manager.activity import reports as activity_reports
from xray_vps_manager.activity import settings as activity_settings
from xray_vps_manager.activity import status as activity_status
from xray_vps_manager.activity import sync as activity_sync
from xray_vps_manager.core.terminal import print_table

if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def log(message):
    if "--quiet" not in sys.argv:
        print(message)


def require_root():
    if os.geteuid() != 0:
        die("Run this script as root.")


def parse_date(value, label="DATE"):
    try:
        return date.fromisoformat(value)
    except ValueError:
        die(f"{label} must be in YYYY-MM-DD format.")


def sync_activity():
    return activity_sync.sync_activity(log)


def set_retention_days(value):
    try:
        days, removed = activity_controls.set_retention_days(value)
    except ValueError as exc:
        die(str(exc))
    print(f"Activity retention set to {days} days.")
    print(f"Pruned old activity events: {removed}")


def set_risk_limits(burst_events, burst_window_minutes, unique_hosts, unique_ports):
    try:
        activity_controls.set_risk_limits(burst_events, burst_window_minutes, unique_hosts, unique_ports)
    except ValueError as exc:
        die(str(exc))
    print("Activity suspicious limits updated.")
    print_risk_limits()


def print_risk_limits():
    print_table(["LIMIT", "VALUE"], activity_controls.risk_limit_rows())


def enable_activity():
    for message in activity_controls.enable_activity():
        print(message)


def disable_activity():
    for message in activity_controls.disable_activity():
        print(message)


def report_client(name, days_value="7"):
    report = activity_client_reports.client_report(name, days_value)
    print(f"Activity report for client: {report['name']}")
    print(f"Period: {report['start'].isoformat()} - {report['end'].isoformat()} UTC")
    print_table(["DATE", "EVENTS", "HOSTS", "PORTS", "OUTBOUNDS", "RISKS", "EXCEPTIONS", "TOP HOSTS"], report["rows"])
    print(f"Total events: {report['totalEvents']}")


def suspicious(days_value="7"):
    report = activity_client_reports.suspicious_report(days_value)
    print(f"Suspicious activity report: {report['start'].isoformat()} - {report['end'].isoformat()} UTC")
    if not report["rows"]:
        print("No suspicious activity found by current rules.")
        return
    print_table(["CLIENT", "RISKS", "EVENTS", "HOSTS", "PORTS", "DETAILS", "RECOMMENDATION"], report["rows"])


def geoip_risk_details(days_value="7"):
    report = activity_client_reports.geoip_risk_details(days_value)
    print(f"GeoIP risk details: {report['start'].isoformat()} - {report['end'].isoformat()} UTC")
    print(f"Timezone: {report['timezoneLabel']}")
    for client in report["clients"]:
        print()
        print(f"Client: {client['name']}")
        print_table(["TIME", "IP", "DOMAIN", "PORT", "REGION", "OUTBOUND"], client["rows"])
    if not report["clients"]:
        print("No GeoIP risk events found by current rules.")


def add_exception(value, source="manual"):
    try:
        result = activity_exception_reports.add_exception(value, source)
    except ValueError as exc:
        die(str(exc))
    if not result["added"]:
        print(f"Exception already exists: {result['value']}")
        return
    print(f"Added activity exception: {result['value']}")
    print(f"Kind: {result['kind']}")


def delete_exception(value):
    try:
        normalized = activity_exception_reports.delete_exception(value)
    except ValueError as exc:
        die(str(exc))
    except KeyError as exc:
        normalized = exc.args[0]
        die(f"Activity exception not found: {normalized}")
    print(f"Deleted activity exception: {normalized}")


def delete_all_exceptions(confirmed=False):
    if not confirmed:
        die("Refusing to delete all activity exceptions without --yes.")
    count = activity_exception_reports.delete_all_exceptions()
    print(f"Deleted activity exceptions: {count}")


def list_exceptions(plain=False):
    rows = activity_exception_reports.list_exception_rows()
    if plain:
        for item in rows:
            print("\t".join([
                item.get("value", ""),
                item.get("kind", ""),
                item.get("createdAt", ""),
                item.get("source", ""),
            ]))
        return
    if not rows:
        print("No activity exceptions configured.")
        return
    print_table(
        ["VALUE", "KIND", "CREATED", "SOURCE"],
        [[item.get("value", ""), item.get("kind", ""), item.get("createdAt", ""), item.get("source", "")] for item in rows],
    )


def print_exception_candidates(days_value="7", plain=False):
    rows = activity_exception_reports.exception_candidate_rows(days_value)
    if plain:
        for row in rows:
            print("\t".join([
                row["value"],
                row["kind"],
                str(row["events"]),
                activity_reports.top_items(row["clients"], limit=5),
                activity_reports.top_items(row["risks"], limit=5),
                activity_reports.top_items(row["ports"], limit=5),
                row["lastSeen"],
                row["sampleTarget"],
            ]))
        return
    if not rows:
        print("No suspicious activity candidates found for exceptions.")
        return
    print_table(
        ["VALUE", "KIND", "EVENTS", "CLIENTS", "RISKS", "PORTS", "LAST SEEN"],
        [
            [
                row["value"],
                row["kind"],
                row["events"],
                activity_reports.top_items(row["clients"], 3),
                activity_reports.top_items(row["risks"], 3),
                activity_reports.top_items(row["ports"], 3),
                row["lastSeen"],
            ]
            for row in rows
        ],
    )


def export_client(name, start_value, end_value, path_only=False):
    start = parse_date(start_value, "START_DATE")
    end = parse_date(end_value, "END_DATE")
    if end < start:
        die("END_DATE must not be earlier than START_DATE.")
    events = list(activity_client_reports.iter_events(name, start, end))
    aggregate = activity_reports.aggregate_events(events)
    archive = activity_exports.create_client_export(name, start, end, events, aggregate)
    if path_only:
        print(archive)
    else:
        print(f"Export created: {archive}")
        print(f"Events: {len(events)}")
        print(f"Size: {activity_reports.format_size(archive.stat().st_size)}")


def resolve_export_archive(value):
    try:
        return activity_exports.resolve_export_archive(value)
    except FileNotFoundError:
        die(f"Activity export archive not found: {value}")
    except PermissionError as exc:
        die(f"Refusing to use an archive outside {EXPORT_DIR}: {exc}")
    except ValueError:
        die("Refusing to use a file that does not look like a .tar.gz activity export.")


def list_exports(plain=False):
    rows = activity_exports.export_archive_rows(activity_reports.format_size)
    if plain:
        for row in rows:
            print("\t".join([row["path"], row["created"], row["size"], row["client"], row["period"], row["events"]]))
        return
    if not rows:
        print("No activity export archives found.")
        return
    print_table(
        ["FILE", "CREATED", "SIZE", "CLIENT", "PERIOD", "EVENTS"],
        [[row["file"], row["created"], row["size"], row["client"], row["period"], row["events"]] for row in rows],
    )


def delete_export(value):
    archive = resolve_export_archive(value)
    size = archive.stat().st_size
    archive.unlink()
    print(f"Deleted activity export: {archive}")
    print(f"Freed: {activity_reports.format_size(size)}")


def delete_all_exports(confirmed=False):
    if not confirmed:
        die("Refusing to delete all activity exports without --yes.")
    if not EXPORT_DIR.exists() or not activity_exports.export_archives():
        print("No activity export archives found.")
        return
    removed, total_size, warnings = activity_exports.delete_all_exports()
    for warning in warnings:
        print(warning)
    print(f"Deleted activity exports: {removed}")
    print(f"Freed: {activity_reports.format_size(total_size)}")
    print(f"Directory: {EXPORT_DIR}")


def default_ssh_target():
    server_addr = (activity_settings.server_env_values().get("SERVER_ADDR") or os.environ.get("SERVER_ADDR", "")).strip()
    return activity_exports.default_ssh_target(server_addr)


def download_command(value, ssh_target=None, local_path="~/Downloads"):
    archive = resolve_export_archive(value)
    ssh_target = ssh_target or default_ssh_target()
    target = local_path.rstrip("/") + "/"
    print("Run this command on your local computer:")
    print(f"scp {shlex.quote(ssh_target + ':' + str(archive))} {activity_exports.quote_local_path(target)}")


def status():
    rows, warnings = activity_status.status_rows()
    print_table(["SETTING", "VALUE"], rows)
    for warning in warnings:
        print(warning)


def usage():
    print(
        """Usage:
  xray-activity status
  xray-activity enable
  xray-activity disable
  xray-activity sync [--quiet]
  xray-activity client NAME [DAYS]
  xray-activity suspicious [DAYS]
  xray-activity geoip-risks [DAYS]
  xray-activity exception-candidates [DAYS] [--plain]
  xray-activity exceptions [--plain]
  xray-activity exception-add VALUE [SOURCE]
  xray-activity exception-delete VALUE
  xray-activity exception-delete-all --yes
  xray-activity export NAME START_DATE END_DATE [--path-only]
  xray-activity export-list [--plain]
  xray-activity export-delete ARCHIVE_PATH_OR_NAME
  xray-activity export-delete-all --yes
  xray-activity download-command ARCHIVE_PATH_OR_NAME [SSH_TARGET_OR_USER_HOST] [LOCAL_DIR]
  xray-activity retention [DAYS]
  xray-activity risk-limits
  xray-activity risk-limits set BURST_EVENTS BURST_WINDOW_MINUTES UNIQUE_HOSTS UNIQUE_PORTS
  xray-activity geo-list [FILTER]
"""
    )


def main():
    require_root()
    args = [arg for arg in sys.argv[1:] if arg != "--quiet"]
    command = args[0] if args else "status"
    with LOCK_PATH.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if command == "status":
            status()
        elif command == "enable":
            enable_activity()
        elif command == "disable":
            disable_activity()
        elif command == "sync":
            sys.exit(sync_activity())
        elif command == "client" and len(args) in (2, 3):
            report_client(args[1], args[2] if len(args) == 3 else "7")
        elif command == "suspicious" and len(args) in (1, 2):
            suspicious(args[1] if len(args) == 2 else "7")
        elif command == "geoip-risks" and len(args) in (1, 2):
            geoip_risk_details(args[1] if len(args) == 2 else "7")
        elif command == "exception-candidates" and len(args) in (1, 2, 3):
            plain = "--plain" in args
            values = [arg for arg in args[1:] if arg != "--plain"]
            print_exception_candidates(values[0] if values else "7", plain=plain)
        elif command == "exceptions" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--plain":
                usage()
                sys.exit(1)
            list_exceptions(plain=len(args) == 2)
        elif command == "exception-add" and len(args) in (2, 3):
            add_exception(args[1], args[2] if len(args) == 3 else "manual")
        elif command == "exception-delete" and len(args) == 2:
            delete_exception(args[1])
        elif command == "exception-delete-all" and len(args) in (1, 2):
            delete_all_exceptions(confirmed=len(args) == 2 and args[1] == "--yes")
        elif command == "export" and len(args) in (4, 5):
            if len(args) == 5 and args[4] != "--path-only":
                usage()
                sys.exit(1)
            export_client(args[1], args[2], args[3], path_only=len(args) == 5)
        elif command == "export-list" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--plain":
                usage()
                sys.exit(1)
            list_exports(plain=len(args) == 2)
        elif command in ("export-delete", "delete-export") and len(args) == 2:
            delete_export(args[1])
        elif command in ("export-delete-all", "delete-all-exports") and len(args) in (1, 2):
            delete_all_exports(confirmed=len(args) == 2 and args[1] == "--yes")
        elif command == "download-command" and len(args) in (2, 3, 4):
            download_command(args[1], args[2] if len(args) >= 3 else None, args[3] if len(args) >= 4 else "~/Downloads")
        elif command == "retention" and len(args) in (1, 2):
            if len(args) == 1:
                print(f"Activity retention: {activity_settings.retention_days()} days")
            else:
                set_retention_days(args[1])
        elif command == "risk-limits" and len(args) in (1, 6):
            if len(args) == 1:
                print_risk_limits()
            elif args[1] == "set":
                set_risk_limits(args[2], args[3], args[4], args[5])
            else:
                usage()
                sys.exit(1)
        elif command == "geo-list" and len(args) in (1, 2):
            query = args[1].upper() if len(args) == 2 else ""
            for code in activity_parser.available_geoip_codes():
                if not query or query in code:
                    print(code)
        else:
            usage()
            sys.exit(1)


if __name__ == "__main__":
    main()
