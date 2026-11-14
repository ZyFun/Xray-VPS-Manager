"""Диспетчер xray-client и адаптер сценариев изменения клиентов."""

import sys

from xray_vps_manager.clients import access as client_access
from xray_vps_manager.clients import limits as client_limits
from xray_vps_manager.clients import payments as client_payments
from xray_vps_manager.clients import service as client_service
from xray_vps_manager.clients import settings as client_settings
from xray_vps_manager.clients.service import sync_traffic
from xray_vps_manager.commands import client_arguments as args
from xray_vps_manager.commands import client_connections as connections
from xray_vps_manager.commands import client_reports as reports
from xray_vps_manager.commands.client_arguments import usage
from xray_vps_manager.commands.client_output import (
    access_key_for_client_id, client_change_errors, connection_display_name, format_traffic,
    link_for, policy_errors, print_access_update, print_expiry, print_limit_enforcement,
    print_payment_summary, print_route_options,
)
from xray_vps_manager.commands.presenters import backup_text
from xray_vps_manager.runner import command_entry
from xray_vps_manager.system import timezone, unit_sync
from xray_vps_manager.traffic import limits as traffic_limits
from xray_vps_manager.traffic import repository as traffic_repository


def prompt_access_days():
    if not sys.stdin.isatty():
        return None
    print()
    print("ACCESS_DAYS: количество календарных дней доступа.")
    print(f"Клиент будет автоматически отключён в 00:00 по часовому поясу менеджера: {client_settings.manager_timezone_label()}.")
    print("Нажми Enter или введи 0, чтобы добавить клиента бессрочно.")
    value = input("ACCESS_DAYS [бессрочно]: ").strip()
    return args.parse_access_days(value)


def cmd_add(name, access_days=None, prompt_for_access=True, connection_tag=None, protocol="", payment_type="free"):
    with client_change_errors():
        existing = None
        if prompt_for_access:
            existing = client_service.prepare_add_client(
                name, connection_tag=connection_tag, protocol=protocol, payment_type=payment_type,
            )
            if not existing:
                access_days = prompt_access_days()
        change = client_service.add_client(
            name, access_days, connection_tag=connection_tag, protocol=protocol,
            payment_type=payment_type, expected_existing=existing,
        )
    result, config, db = change.result, change.config, change.db

    print(("Added client: " if result.added_client else "Added credential for client: ") + name)
    print(f"Connection: {connection_display_name(config, db, result.connection_tag)} ({result.connection_tag})")
    print(f"Payment type: {client_payments.payment_type_label(result.entry)}")
    print(f"Created: {result.created}")
    print(f"Access until: {client_access.format_access_until(result.entry.get('expiresAt', ''))}")
    print(f"Access key: {access_key_for_client_id(result.client_id)}")
    print(f"Backup: {backup_text(change.backup)}")
    print_payment_summary()
    print(link_for(config, result.credential_id, name, result.connection_tag, db))


def cmd_set_payment(name, payment_value):
    with client_change_errors():
        entry = client_service.set_payment(name, payment_value).result
    print(f"Client: {name}")
    print(f"Payment type: {client_payments.payment_type_label(entry)}")
    print_payment_summary()


def cmd_sync_routes():
    with client_change_errors():
        change = client_service.sync_routes()
    if change.result:
        print("Client cascade routes synchronized.")
        print(f"Backup: {backup_text(change.backup)}")
    else:
        print("Client cascade routes are already synchronized.")
    print_route_options(change.config, change.db)


def cmd_route(name, route_value=None):
    with client_change_errors():
        change = client_service.set_route(name, route_value)
    tag, label = change.result.tag, change.result.label
    print(f"Client: {name}")
    if route_value is None:
        print(f"Current country: {label}")
        print(f"Current cascade: {tag or '-'}")
        print_route_options(change.config, change.db)
        return
    if not change.result.changed:
        print(f"Country already selected: {label}")
        print(f"Cascade: {tag}")
        return
    print(f"Selected country: {label}")
    print(f"Cascade: {tag}")
    print(f"Backup: {backup_text(change.backup)}")
    print("Runtime route updated without Xray restart.")


def cmd_remove(name):
    with client_change_errors():
        change = client_service.remove_client(name)

    print(f"Removed client: {change.result.name}")
    print("Removed traffic history.")
    print(f"Backup: {backup_text(change.backup)}")


def cmd_disable(name, connection_tag=None):
    with client_change_errors():
        change = client_service.disable_client(name, connection_tag)
    result = change.result

    print(f"Disabled client: {result.name}")
    if result.connection_tags:
        print("Disabled credentials: " + ", ".join(result.connection_tags))
    print(f"Backup: {backup_text(change.backup)}")


def cmd_enable(name, connection_tag=None):
    with client_change_errors():
        change = client_service.enable_client(name, connection_tag, limit_status=traffic_limits.traffic_limit_status)
    result, config, db = change.result, change.config, change.db

    print(f"Enabled client: {result.name}")
    if result.connection_tags:
        print("Enabled credentials: " + ", ".join(result.connection_tags))
    if result.connection_tag:
        print(f"Connection: {connection_display_name(config, db, result.connection_tag)} ({result.connection_tag})")
    print(f"Backup: {backup_text(change.backup)}")
    if result.connection_tag:
        print(link_for(config, result.credential_id, result.name, result.connection_tag, db))


def cmd_move_connection(name, target_connection_identifier):
    with client_change_errors():
        change = client_service.move_client(name, target_connection_identifier)
    result, config, db = change.result, change.config, change.db

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
    if change.backup:
        print(f"Backup: {backup_text(change.backup)}")
    else:
        print("Xray restart is not required for disabled client.")
    print("New link:")
    print(link_for(config, result.credential_id, result.name, result.target_connection_tag, db))
    print("Выдай клиенту новую ссылку: после переноса параметры подключения меняются.")


def cmd_set_days(name, days_value):
    days = args.parse_access_days(days_value)
    with client_change_errors():
        change = client_service.set_days(name, days, limit_status=traffic_limits.traffic_limit_status)
    print_access_update(name, change)


def cmd_extend_days(name, days_value):
    days = args.parse_extend_days(days_value)
    with client_change_errors():
        change = client_service.extend_days(name, days, limit_status=traffic_limits.traffic_limit_status)
    print_access_update(name, change)


def cmd_set_limit(name, period_value, limit_gb_value):
    period = args.validate_limit_period(period_value)
    limit_bytes = args.parse_limit_gb(limit_gb_value)
    with client_change_errors():
        result = client_service.set_limit(name, period, limit_bytes).result

    print(f"Client: {name}")
    if result.limit_bytes is None:
        print("Traffic limit: без лимита")
        return

    sync_traffic()
    traffic_db = traffic_repository.load_traffic_db_for_read()
    status = traffic_limits.traffic_limit_status(result.entry, traffic_repository.traffic_entry(traffic_db, name))
    print(f"Traffic limit: {format_traffic(result.limit_bytes)}/{client_limits.traffic_limit_period_label(result.period)}")
    if status:
        print(f"Current usage: {format_traffic(status['usedBytes'])}")
        print(f"Remaining: {format_traffic(status['remainingBytes'])}")
        print(f"Reset: {status['resetAt']}")
        if status["exceeded"]:
            print("Traffic limit is already exceeded. Checking active clients now.")
            cmd_enforce_limits(quiet=False, sync_first=False)


def cmd_clear_limit(name):
    with client_change_errors():
        result = client_service.clear_limit(name).result

    print(f"Client: {name}")
    print("Traffic limit: без лимита")
    if result.was_disabled_by_limit:
        print("Status: disabled by previous traffic limit. Use xray-client enable NAME if the client should be enabled now.")


def cmd_rotate_trojan_password(name, connection_tag=None):
    with client_change_errors():
        change = client_service.rotate_trojan_password(name, connection_tag)
    result, config, db, backup = change.result, change.config, change.db, change.backup

    print("Trojan password rotated.")
    print(f"Client: {name}")
    print(f"Connection: {connection_display_name(config, db, result.connection_tag)} ({result.connection_tag})")
    if backup:
        print(f"Backup: {backup_text(backup)}")
    else:
        print("Backup: not created; credential is not active in config.")
    print("Client must reimport this Trojan link:")
    print(link_for(config, result.credential_id, name, result.connection_tag, db))


def cmd_enforce_limits(quiet=False, sync_first=False):
    with policy_errors("enforce-limits", quiet):
        change = client_service.enforce_limits(limit_status=traffic_limits.traffic_limit_status, sync_first=sync_first)
        if not quiet:
            print_limit_enforcement(change)


def cmd_expire_due(quiet=False):
    with policy_errors("expire-due", quiet):
        change = client_service.expire_due()
        if not quiet:
            print_expiry(change)


def cmd_set_timezone(value):
    with client_change_errors():
        name, normalized, warnings = timezone.set_manager_timezone(value)
    print(f"MANAGER_TIMEZONE: {name or 'server local time'}")
    print(f"Current time: {client_access.local_now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    if normalized:
        print(f"Normalized access deadlines: {normalized}")
    for warning in warnings:
        print(f"WARN: {warning}")
    try:
        result = unit_sync.sync_raw_log_timer()
        print(f"Raw log rotation timer: {result['onCalendar']}")
    except Exception as exc:
        print(f"WARN: raw log rotation timer sync failed: {exc}", file=sys.stderr)


@command_entry('client')
def main():
    if len(sys.argv) < 2:
        usage()
        sys.exit(1)
    command = sys.argv[1]
    if command == "list":
        reports.cmd_list()
    elif command == "connection-list":
        connections.cmd_connection_list()
    elif command == "add-connection":
        connections.cmd_connection_add(*args.parse_connection_add_args(sys.argv[2:]))
    elif command == "add-trojan-connection":
        connections.cmd_trojan_connection_add(*args.parse_trojan_connection_add_args(sys.argv[2:]))
    elif command in ("update-trojan-connection", "trojan-connection-update"):
        connections.cmd_trojan_connection_update(*args.parse_trojan_connection_update_args(sys.argv[2:]))
    elif command in ("connection-transport", "set-connection-transport"):
        connections.cmd_connection_transport(*args.parse_connection_transport_args(sys.argv[2:]))
    elif command in ("connection-xhttp-extra", "set-connection-xhttp-extra"):
        connections.cmd_connection_xhttp_extra(*args.parse_connection_xhttp_extra_args(sys.argv[2:]))
    elif command in ("connection-rename", "rename-connection") and len(sys.argv) == 4:
        connections.cmd_connection_rename(sys.argv[2], sys.argv[3])
    elif command in ("remove-connection", "delete-connection") and len(sys.argv) == 3:
        connections.cmd_connection_remove(sys.argv[2])
    elif command == "add":
        name, access_days, prompt_for_access, connection_tag, protocol, payment_type = args.parse_add_args(sys.argv[2:])
        cmd_add(
            name,
            access_days,
            prompt_for_access=prompt_for_access,
            connection_tag=connection_tag,
            protocol=protocol,
            payment_type=payment_type,
        )
    elif command in ("disable", "off"):
        name, connection_tag = args.parse_name_connection_args(sys.argv[2:])
        cmd_disable(name, connection_tag)
    elif command in ("enable", "on"):
        name, connection_tag = args.parse_name_connection_args(sys.argv[2:])
        cmd_enable(name, connection_tag)
    elif command in ("move-connection", "move-client", "move") and len(sys.argv) == 4:
        cmd_move_connection(sys.argv[2], sys.argv[3])
    elif command in ("remove", "delete", "del", "rm") and len(sys.argv) == 3:
        cmd_remove(sys.argv[2])
    elif command == "link":
        name, connection_tag = args.parse_name_connection_args(sys.argv[2:])
        reports.cmd_link(name, connection_tag)
    elif command == "key" and len(sys.argv) == 3:
        reports.cmd_key(sys.argv[2])
    elif command in ("trojan-password-check", "validate-trojan-passwords") and len(sys.argv) == 2:
        reports.cmd_trojan_password_check()
    elif command in ("rotate-trojan-password", "trojan-password-rotate"):
        name, connection_tag = args.parse_name_connection_args(sys.argv[2:])
        cmd_rotate_trojan_password(name, connection_tag)
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
        reports.cmd_traffic_summary(sys.argv[2] if len(sys.argv) == 3 else None)
    elif command == "traffic-day" and len(sys.argv) in (3, 4):
        reports.cmd_traffic_day(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    elif command == "traffic-week" and len(sys.argv) in (3, 4):
        reports.cmd_traffic_week(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    elif command == "traffic-month" and len(sys.argv) in (3, 4):
        reports.cmd_traffic_month(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    elif command == "traffic-period" and len(sys.argv) == 5:
        reports.cmd_traffic_period(sys.argv[2], sys.argv[3], sys.argv[4])
    elif command == "limit-list" and len(sys.argv) == 2:
        reports.cmd_limit_list()
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
        reports.cmd_timezone()
    elif command == "set-timezone" and len(sys.argv) == 3:
        cmd_set_timezone(sys.argv[2])
    else:
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
