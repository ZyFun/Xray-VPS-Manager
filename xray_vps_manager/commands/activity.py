#!/usr/bin/env python3
import fcntl
import os
import shlex
import signal
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from xray_vps_manager.activity.constants import (
    DETAIL_MODE_ALL,
    DETAIL_MODE_OFF,
    DETAIL_MODE_SELECTED,
    EXPORT_DIR,
    LOCK_PATH,
)
from xray_vps_manager.activity import blocklist as activity_blocklist
from xray_vps_manager.activity import backfill as activity_backfill
from xray_vps_manager.activity import bypass as activity_bypass
from xray_vps_manager.activity import client_reports as activity_client_reports
from xray_vps_manager.activity import controls as activity_controls
from xray_vps_manager.activity import exception_reports as activity_exception_reports
from xray_vps_manager.activity import exports as activity_exports
from xray_vps_manager.activity import parser as activity_parser
from xray_vps_manager.activity import raw_logs as activity_raw_logs
from xray_vps_manager.activity import reports as activity_reports
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import settings as activity_settings
from xray_vps_manager.activity import status as activity_status
from xray_vps_manager.activity import sync as activity_sync
from xray_vps_manager.core.time import manager_timezone
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


def print_backfill_stats(stats, applied=False):
    print(f"Target: {stats.get('target') or '-'}")
    print(f"Period: {stats.get('start') or '-'} .. {stats.get('end') or '-'}")
    print(f"Mode: {'apply' if applied else 'dry-run'}")
    print(f"Files: {len(stats.get('files') or [])}")
    for path in stats.get("files") or []:
        print(f"- {path}")
    print(f"Raw lines scanned: {stats.get('rawLines', 0)}")
    print(f"Parsed events: {stats.get('parsedEvents', 0)}")
    print(f"Matched events: {stats.get('matched', 0)}")
    print(f"Inserted events: {stats.get('inserted', 0)}")
    print(f"Duplicates: {stats.get('duplicates', 0)}")
    print(f"Unknown clients: {stats.get('unknownClients', 0)}")
    print(f"Retention skipped: {stats.get('retentionSkipped', 0)}")
    if stats.get("fileStats"):
        print_table(
            ["FILE", "LINES", "PARSED", "MATCHED"],
            [
                [
                    Path(item.get("file") or "").name,
                    item.get("rawLines", 0),
                    item.get("parsedEvents", 0),
                    item.get("matchedEvents", 0),
                ]
                for item in stats["fileStats"]
            ],
        )
    if stats.get("clients"):
        print_table(["CLIENT", "EVENTS"], sorted(stats["clients"].items()))
    if stats.get("risks"):
        print_table(["RISK", "EVENTS"], sorted(stats["risks"].items()))
    print("Backfill applied." if applied else "Dry-run only. No events were inserted.")


def backfill_activity(client_name, start_value, end_value, mode_args):
    start = parse_date(start_value, "START_DATE")
    end = parse_date(end_value, "END_DATE")
    if end < start:
        die("END_DATE must not be earlier than START_DATE.")
    dry_run = "--dry-run" in mode_args
    apply = "--apply" in mode_args
    yes = "--yes" in mode_args
    if dry_run == apply:
        die("Backfill requires exactly one mode: --dry-run or --apply.")
    if apply and not yes:
        die("Refusing to apply backfill without --yes.")
    clients = activity_sync.known_clients()
    if client_name != "all" and not any(item.get("client") == client_name for item in clients.values()):
        die(f"Unknown client in active config/client db: {client_name}")
    stats = activity_backfill.run_backfill(
        clients,
        client_name=client_name,
        start=start,
        end=end,
        apply=apply,
    )
    print_backfill_stats(stats, applied=apply)


def set_retention_days(value):
    try:
        days, removed = activity_controls.set_retention_days(value)
    except ValueError as exc:
        die(str(exc))
    print(f"Activity retention set to {days} days.")
    print(f"Pruned old activity events: {removed}")


def set_alert_retention_days(value):
    try:
        days, removed = activity_controls.set_alert_retention_days(value)
    except ValueError as exc:
        die(str(exc))
    print(f"Activity alert-log retention set to {days} days.")
    print(f"Pruned old alert events: {removed}")


def set_alert_detection(value):
    normalized = str(value or "").strip().lower()
    if normalized not in ("on", "off", "true", "false", "1", "0", "yes", "no"):
        die("Alert detection must be on or off.")
    enabled = normalized in ("on", "true", "1", "yes")
    activity_controls.set_alert_detection_enabled(enabled)
    print(f"Activity alert detection: {'enabled' if enabled else 'disabled'}")


def print_alert_detection():
    print(f"Activity alert detection: {'enabled' if activity_settings.alerts_enabled() else 'disabled'}")


def set_error_event_retention_days(value):
    try:
        days, removed = activity_controls.set_xray_error_event_retention_days(value)
    except ValueError as exc:
        die(str(exc))
    print(f"Xray error event retention set to {days} days.")
    print(f"Pruned old Xray error events: {removed}")


def set_raw_log_retention(kind, value):
    try:
        days = activity_controls.set_raw_log_retention_days(kind, value)
    except ValueError as exc:
        die(str(exc))
    print(f"Raw Xray {kind}.log retention set to {days} days.")


def set_raw_log_rotate_time(value):
    try:
        parsed = activity_controls.set_raw_log_rotate_time(value)
    except ValueError as exc:
        die(str(exc))
    print(f"Raw Xray log rotate time set to {parsed}.")
    sync_raw_log_timer()


def sync_raw_log_timer(no_systemctl=False):
    try:
        result = activity_raw_logs.sync_raw_log_timer(run_systemctl=not no_systemctl)
    except RuntimeError as exc:
        die(f"Raw Xray log rotate timer sync failed: {exc}")
    print(f"Raw Xray log rotate service synced: {result['servicePath']}")
    print(f"Raw Xray log rotate timer synced: {result['timerPath']}")
    print(f"OnCalendar: {result['onCalendar']}")
    print(f"systemctl: {result['systemctl']}")


def set_risk_limits(burst_events, burst_window_minutes, unique_hosts, unique_ports):
    try:
        activity_controls.set_risk_limits(burst_events, burst_window_minutes, unique_hosts, unique_ports)
    except ValueError as exc:
        die(str(exc))
    print("Activity suspicious limits updated.")
    print_risk_limits()


def print_risk_limits():
    print_table(["LIMIT", "VALUE"], activity_controls.risk_limit_rows())


def print_geoip_status():
    code = activity_settings.xray_geoip_warning_code()
    print_table(
        ["SETTING", "VALUE"],
        [
            ["Alert detection", "enabled" if activity_settings.alerts_enabled() else "disabled"],
            ["Xray route GeoIP warnings", code or "disabled"],
            ["GeoIP risk prefix", "xray-geoip:CODE"],
        ],
    )


def print_retention_overview():
    print_table(
        ["LOG", "RETENTION"],
        [
            ["Detailed activity", f"{activity_settings.retention_days()} days"],
            ["Alert-log", f"{activity_settings.alert_retention_days()} days"],
            ["Xray error events", f"{activity_settings.xray_error_event_retention_days()} days"],
            ["Raw access.log", f"{activity_settings.xray_access_log_retention_days()} days"],
            ["Raw error.log", f"{activity_settings.xray_error_log_retention_days()} days"],
        ],
    )


def enable_activity():
    for message in activity_controls.enable_activity():
        print(message)


def disable_activity():
    for message in activity_controls.disable_activity():
        print(message)


def detail_mode(value=None):
    if value is None:
        try:
            status = activity_repository.detail_capture_status_for_read(
                legacy_enabled=activity_settings.activity_enabled()
            )
        except Exception as exc:
            die(str(exc))
        selected = status.get("selectedClients") or []
        print_table(
            ["SETTING", "VALUE"],
            [
                ["Detailed mode", status.get("mode") or DETAIL_MODE_OFF],
                ["Selected clients", ", ".join(selected) if selected else "-"],
                ["Legacy ACTIVITY_LOGGING_ENABLED", "true" if activity_settings.activity_enabled() else "false"],
                ["Alert-log enabled", "true" if activity_settings.alerts_enabled() else "false"],
            ],
        )
        return
    normalized = str(value or "").strip().lower()
    if normalized not in (DETAIL_MODE_OFF, DETAIL_MODE_ALL, DETAIL_MODE_SELECTED):
        die("Detail mode must be one of: off, all, selected.")
    try:
        mode = activity_repository.set_detail_mode_for_write(normalized)
    except Exception as exc:
        die(str(exc))
    activity_controls.set_enabled(mode != DETAIL_MODE_OFF)
    print(f"Detailed activity mode: {mode}")
    if mode == DETAIL_MODE_SELECTED:
        print("Selected clients can be changed with: xray-activity detail-clients set CLIENT...")


def detail_clients(args):
    command = args[0] if args else "list"
    try:
        if command == "list":
            status = activity_repository.detail_capture_status_for_read(
                legacy_enabled=activity_settings.activity_enabled()
            )
            selected = status.get("selectedClients") or []
            if not selected:
                print("No selected clients for detailed activity mode.")
                return
            for name in selected:
                print(name)
            return
        if command == "set":
            activity_repository.set_detail_clients_for_write(args[1:])
            print(f"Selected clients updated: {', '.join(args[1:]) if args[1:] else '-'}")
            return
        if command == "clear":
            activity_repository.set_detail_clients_for_write([])
            print("Selected clients cleared.")
            return
    except Exception as exc:
        die(str(exc))
    usage()
    sys.exit(1)


def report_client(name, days_value="7"):
    report = activity_client_reports.client_report(name, days_value)
    print(f"Activity report for client: {report['name']}")
    print(f"Period: {report['start'].isoformat()} - {report['end'].isoformat()} UTC")
    print_table(["DATE", "EVENTS", "HOSTS", "PORTS", "OUTBOUNDS", "RISKS", "BYPASS", "EXCEPTIONS", "TOP HOSTS"], report["rows"])
    credential_rows = report.get("credentialRows") or []
    if len([row for row in credential_rows if row and row[0] != "TOTAL"]) > 1:
        print()
        print("Credentials")
        print_table(
            ["CONNECTION", "EVENTS", "HOSTS", "PORTS", "OUTBOUNDS", "RISKS", "BYPASS", "EXCEPTIONS", "TOP HOSTS"],
            credential_rows,
        )
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


def bypass_status():
    rows = activity_bypass.status_rows()
    if not rows:
        print("No GeoIP bypass routes found.")
        return
    print_table(["TAG", "REGION", "LABEL", "STATUS", "CONFIGURED OUTBOUND"], rows)


def bypass_events(days_value="7"):
    report = activity_bypass.event_rows(days_value)
    print(f"GeoIP bypass events: {report['start'].isoformat()} - {report['end'].isoformat()} UTC")
    rows = report.get("rows") or []
    if not rows:
        print("No GeoIP bypass events found.")
        return
    print_table(
        ["CLIENT", "LAST", "HOST/IP", "PORT", "REGION", "BYPASS", "EVENTS"],
        [
            [
                row.get("client", ""),
                row.get("last", ""),
                row.get("host", ""),
                row.get("port", ""),
                row.get("region", ""),
                row.get("bypassTag", ""),
                row.get("events", 0),
            ]
            for row in rows
        ],
    )


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


def list_blocklist(plain=False):
    rows = activity_blocklist.list_block_rows()
    if plain:
        for item in rows:
            print("\t".join([
                str(item.get("id", "")),
                item.get("value", ""),
                item.get("kind", ""),
                item.get("sourceClient", ""),
                item.get("createdAt", ""),
                item.get("expiresAt", ""),
                item.get("status", ""),
                item.get("lastHitAt", ""),
                item.get("comment", ""),
            ]))
        return
    if not rows:
        print("No activity blocklist entries configured.")
        return
    print_table(
        ["ID", "CLIENT", "VALUE", "KIND", "STATUS", "EXPIRES", "LAST HIT", "COMMENT"],
        [
            [
                item.get("id", ""),
                item.get("sourceClient") or "-",
                item.get("value", ""),
                item.get("kind", ""),
                item.get("status", ""),
                item.get("expiresAt", ""),
                item.get("lastHitAt") or "-",
                item.get("comment", ""),
            ]
            for item in rows
        ],
    )


def print_block_candidates(client_name, days_value="7", region="RU", plain=False):
    rows = activity_blocklist.block_candidate_rows(client_name, days_value, region)
    if plain:
        for row in rows:
            print("\t".join([
                row["value"],
                row["kind"],
                str(row["events"]),
                activity_reports.top_items(row["ports"], limit=5),
                row["lastSeen"],
                row["sampleTarget"],
                str(row["sourceEventId"]),
            ]))
        return
    if not rows:
        print(f"No GeoIP {region.upper()} candidates found for client: {client_name}")
        return
    print_table(
        ["VALUE", "KIND", "EVENTS", "PORTS", "LAST SEEN"],
        [
            [
                row["value"],
                row["kind"],
                row["events"],
                activity_reports.top_items(row["ports"], limit=5),
                row["lastSeen"],
            ]
            for row in rows
        ],
    )


def add_block(value, source_client="", duration="forever", comment="", source_event_id=""):
    event_id = int(source_event_id) if str(source_event_id or "").isdigit() else None
    try:
        item = activity_blocklist.add_block(
            value,
            source_client=source_client,
            duration=duration,
            comment=comment,
            source_event_id=event_id,
            source="geoip-menu" if source_client else "manual",
        )
        backup = activity_blocklist.reconcile_xray_config()
    except ValueError as exc:
        die(str(exc))
    except RuntimeError as exc:
        die(str(exc))
    print(f"Added activity block: {item['value']}")
    print(f"Kind: {item['kind']}")
    print(f"Source client: {item.get('sourceClient') or '-'}")
    print(f"Expires: {item.get('expiresAt') or 'forever'}")
    print(f"Backup: {backup}" if backup else "Xray routing already up to date.")


def delete_block(value_or_id):
    try:
        item = activity_blocklist.delete_block(value_or_id)
        backup = activity_blocklist.reconcile_xray_config(removed_items=[item])
    except KeyError:
        die(f"Activity blocklist entry not found: {value_or_id}")
    except RuntimeError as exc:
        die(str(exc))
    print(f"Deleted activity block: {item['value']}")
    print(f"Backup: {backup}" if backup else "Xray routing already up to date.")


def sync_blocklist():
    try:
        backup = activity_blocklist.reconcile_xray_config()
    except RuntimeError as exc:
        die(str(exc))
    print(f"Backup: {backup}" if backup else "Activity blocklist routing already up to date.")


def block_stats(plain=False):
    rows = activity_blocklist.block_stats_rows()
    if plain:
        for row in rows:
            print("\t".join([
                row["value"],
                row["kind"],
                str(row["totalHits"]),
                activity_reports.top_items(row["clients"], limit=20),
                row["firstSeen"],
                row["lastSeen"],
                row["comment"],
            ]))
        return
    if not rows:
        print("No activity blocklist entries configured.")
        return
    print_table(
        ["VALUE", "KIND", "HITS", "CLIENTS", "FIRST SEEN", "LAST SEEN", "COMMENT"],
        [
            [
                row["value"],
                row["kind"],
                row["totalHits"],
                activity_reports.top_items(row["clients"], limit=10),
                row["firstSeen"] or "-",
                row["lastSeen"] or "-",
                row["comment"],
            ]
            for row in rows
        ],
    )


def alert_log(limit_value="50", risk_prefix=""):
    try:
        limit = max(1, int(limit_value))
    except ValueError:
        die("LIMIT must be a number.")
    risk = risk_prefix.strip()
    if risk.lower() == "geoip":
        risk = "xray-geoip:"
    rows = activity_repository.alert_events_for_read(risk_prefix=risk or None, limit=limit)
    if not rows:
        print("No activity alert events found.")
        return
    print_table(
        ["TIME", "CLIENT", "RISK", "TARGET", "EVENTS", "LAST", "OUTBOUND"],
        [
            [
                row.get("time", ""),
                row.get("client", ""),
                row.get("risk", ""),
                f"{row.get('host') or '-'}:{row.get('port') or '-'}",
                row.get("event_count", 1),
                row.get("last_seen_at", ""),
                row.get("outbound", ""),
            ]
            for row in rows
        ],
    )


def local_today() -> date:
    tzinfo, _label = manager_timezone()
    return datetime.now(timezone.utc).astimezone(tzinfo).date()


def counter_window(days: int) -> tuple[str, str]:
    today = local_today()
    start = today - timedelta(days=max(1, days) - 1)
    end = today + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def print_counter_rows(rows):
    print_table(
        ["BUCKET", "CLIENT", "EVENTS", "GEOIP", "SUSPICIOUS", "BLOCKED", "HOSTS", "PORTS", "LAST"],
        [
            [
                row.get("bucketStart", ""),
                row.get("client", ""),
                row.get("totalEvents", 0),
                row.get("geoipEvents", 0),
                row.get("suspiciousEvents", 0),
                row.get("blockedEvents", 0),
                row.get("uniqueHosts", 0),
                row.get("uniquePorts", 0),
                row.get("lastSeen", ""),
            ]
            for row in rows
        ],
    )


def format_growth_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def print_counter_growth_rows(rows):
    print_table(
        [
            "CLIENT",
            "LATEST",
            "BASE DAYS",
            "EVENTS",
            "AVG EVENTS",
            "DELTA EVENTS",
            "HOSTS",
            "AVG HOSTS",
            "DELTA HOSTS",
            "PORTS",
            "AVG PORTS",
            "DELTA PORTS",
        ],
        [
            [
                row.get("client", ""),
                row.get("bucketStart", ""),
                row.get("baselineBuckets", 0),
                row.get("totalEvents", 0),
                format_growth_number(row.get("avgTotalEvents", 0)),
                format_growth_number(row.get("totalEventsDelta", 0)),
                row.get("uniqueHosts", 0),
                format_growth_number(row.get("avgUniqueHosts", 0)),
                format_growth_number(row.get("uniqueHostsDelta", 0)),
                row.get("uniquePorts", 0),
                format_growth_number(row.get("avgUniquePorts", 0)),
                format_growth_number(row.get("uniquePortsDelta", 0)),
            ]
            for row in rows
        ],
    )


def counter_log(bucket_type="day", limit_value="50", client_name="", days_value=""):
    if bucket_type not in ("day", "hour"):
        die("BUCKET must be day or hour.")
    try:
        limit = max(1, int(limit_value))
    except ValueError:
        die("LIMIT must be a number.")
    start = end = None
    if days_value:
        try:
            days = max(1, int(days_value))
        except ValueError:
            die("DAYS must be a number.")
        start, end = counter_window(days)
    rows = activity_repository.client_counters_for_read(
        bucket_type=bucket_type,
        client_name=client_name or None,
        start=start,
        end=end,
        limit=limit,
    )
    if not rows:
        print("No lightweight activity counters found.")
        return
    print_counter_rows(rows)


def counter_growth(limit_value="50"):
    try:
        limit = max(1, int(limit_value))
    except ValueError:
        die("LIMIT must be a number.")
    start, end = counter_window(7)
    rows = activity_repository.client_counters_for_read(bucket_type="day", start=start, end=end, limit=10000)
    growth_rows = activity_client_reports.counter_growth_rows(rows, limit=limit)
    if not growth_rows:
        print("No lightweight activity counter growth found.")
        return
    print_counter_growth_rows(growth_rows)


def error_log(limit_value="50", level=""):
    try:
        limit = max(1, int(limit_value))
    except ValueError:
        die("LIMIT must be a number.")
    rows = activity_repository.xray_error_events_for_read(level=level or None, limit=limit)
    if not rows:
        print("No Xray error events found.")
        return
    print_table(
        ["TIME", "LEVEL", "SOURCE", "COMPONENT", "COUNT", "MESSAGE"],
        [
            [
                row.get("time", ""),
                row.get("level", ""),
                row.get("source", ""),
                row.get("component", "") or "-",
                row.get("eventCount", 0),
                row.get("message", ""),
            ]
            for row in rows
        ],
    )


def error_summary(days_value="7"):
    try:
        days = max(1, int(days_value))
    except ValueError:
        die("DAYS must be a number.")
    start_day = date.today() - timedelta(days=days - 1)
    rows = activity_repository.xray_error_events_for_read(start=f"{start_day.isoformat()}T00:00:00Z", limit=1000)
    if not rows:
        print("No Xray error events found.")
        return
    summary = {}
    for row in rows:
        key = (row.get("level", ""), row.get("source", ""))
        item = summary.setdefault(key, {"level": key[0], "source": key[1], "rows": 0, "events": 0, "last": ""})
        item["rows"] += 1
        item["events"] += int(row.get("eventCount") or 0)
        item["last"] = max(item["last"], row.get("lastSeen") or row.get("time") or "")
    print_table(
        ["LEVEL", "SOURCE", "ROWS", "EVENTS", "LAST"],
        [[item["level"], item["source"], item["rows"], item["events"], item["last"]] for item in summary.values()],
    )


def error_log_days(days_value="7", limit_value="50", level=""):
    try:
        days = max(1, int(days_value))
    except ValueError:
        die("DAYS must be a number.")
    try:
        limit = max(1, int(limit_value))
    except ValueError:
        die("LIMIT must be a number.")
    start_day = date.today() - timedelta(days=days - 1)
    rows = activity_repository.xray_error_events_for_read(
        level=level or None,
        start=f"{start_day.isoformat()}T00:00:00Z",
        limit=limit,
    )
    if not rows:
        print("No Xray error events found.")
        return
    print_table(
        ["ID", "TIME", "LEVEL", "SOURCE", "COMPONENT", "COUNT", "MESSAGE"],
        [
            [
                row.get("id", ""),
                row.get("time", ""),
                row.get("level", ""),
                row.get("source", ""),
                row.get("component", "") or "-",
                row.get("eventCount", 0),
                row.get("message", ""),
            ]
            for row in rows
        ],
    )


def error_detail(value):
    try:
        event_id = int(value)
    except ValueError:
        die("ERROR_ID must be a number.")
    row = activity_repository.xray_error_event_for_read(event_id)
    if not row:
        die(f"Xray error event not found: {event_id}")
    print_table(
        ["FIELD", "VALUE"],
        [
            ["ID", row.get("id", "")],
            ["Time", row.get("time", "")],
            ["Level", row.get("level", "")],
            ["Source", row.get("source", "")],
            ["Component", row.get("component", "") or "-"],
            ["Count", row.get("eventCount", 0)],
            ["First seen", row.get("firstSeen", "")],
            ["Last seen", row.get("lastSeen", "")],
            ["Message", row.get("message", "")],
            ["Raw line", row.get("rawLine", "")],
        ],
    )


def raw_logs_status():
    print_table(["SETTING", "VALUE"], activity_raw_logs.raw_log_rows())


def raw_log_archives(plain=False):
    rows = activity_raw_logs.raw_log_archive_rows()
    if plain:
        for row in rows:
            print("\t".join([row["path"], row["type"], row["modified"], row["size"]]))
        return
    if not rows:
        print("No raw Xray log archives found.")
        return
    print_table(
        ["TYPE", "FILE", "MODIFIED", "SIZE"],
        [[row["type"], row["file"], row["modified"], row["size"]] for row in rows],
    )


def rotate_raw_logs(only_if_due=False):
    return activity_raw_logs.rotate_raw_logs(only_if_due=only_if_due, log=log)


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
  xray-activity detail-mode [off|all|selected]
  xray-activity detail-clients [list|set CLIENT...|clear]
  xray-activity sync [--quiet]
  xray-activity alerts [LIMIT] [RISK_PREFIX|geoip]
  xray-activity counters [day|hour] [LIMIT]
  xray-activity counters-today [LIMIT]
  xray-activity counters-week [LIMIT]
  xray-activity counters-growth [LIMIT]
  xray-activity errors [LIMIT] [LEVEL]
  xray-activity errors-summary [DAYS]
  xray-activity errors-days [DAYS] [LIMIT] [LEVEL]
  xray-activity error-detail ERROR_ID
  xray-activity raw-logs
  xray-activity raw-log-archives [--plain]
  xray-activity rotate-raw-logs [--due]
  xray-activity backfill CLIENT|all START_DATE END_DATE --dry-run
  xray-activity backfill CLIENT|all START_DATE END_DATE --apply --yes
  xray-activity client NAME [DAYS]
  xray-activity suspicious [DAYS]
  xray-activity geoip-risks [DAYS]
  xray-activity bypass-events [DAYS]
  xray-activity bypass-status
  xray-activity exception-candidates [DAYS] [--plain]
  xray-activity exceptions [--plain]
  xray-activity exception-add VALUE [SOURCE]
  xray-activity exception-delete VALUE
  xray-activity exception-delete-all --yes
  xray-activity blocklist [--plain]
  xray-activity block-candidates CLIENT [DAYS] [REGION] [--plain]
  xray-activity block-add VALUE SOURCE_CLIENT DURATION COMMENT [SOURCE_EVENT_ID]
  xray-activity block-delete VALUE_OR_ID
  xray-activity block-sync
  xray-activity block-stats [--plain]
  xray-activity export NAME START_DATE END_DATE [--path-only]
  xray-activity export-list [--plain]
  xray-activity export-delete ARCHIVE_PATH_OR_NAME
  xray-activity export-delete-all --yes
  xray-activity download-command ARCHIVE_PATH_OR_NAME [SSH_TARGET_OR_USER_HOST] [LOCAL_DIR]
  xray-activity alert-detection [on|off]
  xray-activity geoip-status
  xray-activity retention-overview
  xray-activity retention [DAYS]
  xray-activity alert-retention [DAYS]
  xray-activity error-retention [DAYS]
  xray-activity raw-log-retention access|error [DAYS]
  xray-activity raw-log-rotate-time [HH:MM]
  xray-activity raw-log-timer-sync [--no-systemctl]
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
        elif command == "detail-mode" and len(args) in (1, 2):
            detail_mode(args[1] if len(args) == 2 else None)
        elif command == "detail-clients" and len(args) >= 1:
            detail_clients(args[1:])
        elif command == "sync":
            sys.exit(sync_activity())
        elif command == "backfill" and len(args) in (5, 6):
            backfill_activity(args[1], args[2], args[3], args[4:])
        elif command == "alerts" and len(args) in (1, 2, 3):
            alert_log(args[1] if len(args) >= 2 else "50", args[2] if len(args) == 3 else "")
        elif command == "alert-detection" and len(args) in (1, 2):
            if len(args) == 1:
                print_alert_detection()
            else:
                set_alert_detection(args[1])
        elif command == "geoip-status" and len(args) == 1:
            print_geoip_status()
        elif command == "retention-overview" and len(args) == 1:
            print_retention_overview()
        elif command == "counters" and len(args) in (1, 2, 3, 4):
            counter_log(
                args[1] if len(args) >= 2 else "day",
                args[2] if len(args) >= 3 else "50",
                args[3] if len(args) == 4 else "",
            )
        elif command == "counters-today" and len(args) in (1, 2):
            counter_log("day", args[1] if len(args) == 2 else "50", days_value="1")
        elif command == "counters-week" and len(args) in (1, 2):
            counter_log("day", args[1] if len(args) == 2 else "50", days_value="7")
        elif command == "counters-growth" and len(args) in (1, 2):
            counter_growth(args[1] if len(args) == 2 else "50")
        elif command == "errors" and len(args) in (1, 2, 3):
            error_log(args[1] if len(args) >= 2 else "50", args[2] if len(args) == 3 else "")
        elif command == "errors-summary" and len(args) in (1, 2):
            error_summary(args[1] if len(args) == 2 else "7")
        elif command == "errors-days" and len(args) in (1, 2, 3, 4):
            error_log_days(
                args[1] if len(args) >= 2 else "7",
                args[2] if len(args) >= 3 else "50",
                args[3] if len(args) == 4 else "",
            )
        elif command == "error-detail" and len(args) == 2:
            error_detail(args[1])
        elif command == "raw-logs" and len(args) == 1:
            raw_logs_status()
        elif command == "raw-log-archives" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--plain":
                usage()
                sys.exit(1)
            raw_log_archives(plain=len(args) == 2)
        elif command == "rotate-raw-logs" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--due":
                usage()
                sys.exit(1)
            sys.exit(rotate_raw_logs(only_if_due=len(args) == 2))
        elif command == "client" and len(args) in (2, 3):
            report_client(args[1], args[2] if len(args) == 3 else "7")
        elif command == "suspicious" and len(args) in (1, 2):
            suspicious(args[1] if len(args) == 2 else "7")
        elif command == "geoip-risks" and len(args) in (1, 2):
            geoip_risk_details(args[1] if len(args) == 2 else "7")
        elif command == "bypass-events" and len(args) in (1, 2):
            bypass_events(args[1] if len(args) == 2 else "7")
        elif command == "bypass-status" and len(args) == 1:
            bypass_status()
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
        elif command == "blocklist" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--plain":
                usage()
                sys.exit(1)
            list_blocklist(plain=len(args) == 2)
        elif command == "block-candidates" and len(args) in (2, 3, 4, 5):
            plain = "--plain" in args
            values = [arg for arg in args[1:] if arg != "--plain"]
            print_block_candidates(
                values[0],
                values[1] if len(values) >= 2 else "7",
                values[2] if len(values) >= 3 else "RU",
                plain=plain,
            )
        elif command == "block-add" and len(args) in (5, 6):
            add_block(args[1], args[2], args[3], args[4], args[5] if len(args) == 6 else "")
        elif command == "block-delete" and len(args) == 2:
            delete_block(args[1])
        elif command == "block-sync" and len(args) == 1:
            sync_blocklist()
        elif command == "block-stats" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--plain":
                usage()
                sys.exit(1)
            block_stats(plain=len(args) == 2)
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
        elif command == "alert-retention" and len(args) in (1, 2):
            if len(args) == 1:
                print(f"Activity alert-log retention: {activity_settings.alert_retention_days()} days")
            else:
                set_alert_retention_days(args[1])
        elif command == "error-retention" and len(args) in (1, 2):
            if len(args) == 1:
                print(f"Xray error event retention: {activity_settings.xray_error_event_retention_days()} days")
            else:
                set_error_event_retention_days(args[1])
        elif command == "raw-log-retention" and len(args) in (2, 3):
            if args[1] not in ("access", "error"):
                usage()
                sys.exit(1)
            if len(args) == 2:
                days = (
                    activity_settings.xray_access_log_retention_days()
                    if args[1] == "access"
                    else activity_settings.xray_error_log_retention_days()
                )
                print(f"Raw Xray {args[1]}.log retention: {days} days")
            else:
                set_raw_log_retention(args[1], args[2])
        elif command == "raw-log-rotate-time" and len(args) in (1, 2):
            if len(args) == 1:
                print(f"Raw Xray log rotate time: {activity_settings.raw_log_rotate_time()}")
            else:
                set_raw_log_rotate_time(args[1])
        elif command == "raw-log-timer-sync" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--no-systemctl":
                usage()
                sys.exit(1)
            sync_raw_log_timer(no_systemctl=len(args) == 2)
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
