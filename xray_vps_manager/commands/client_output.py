"""Форматирование и обработка ошибок клиентских команд и меню."""

from contextlib import contextmanager
import sys

from xray_vps_manager.clients import access as client_access
from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.clients import limits as client_limits
from xray_vps_manager.clients import links as client_links
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.clients import routes as client_route_service
from xray_vps_manager.clients import service as client_service
from xray_vps_manager.commands.presenters import apply_error_text, backup_text
from xray_vps_manager.core.errors import ApplyConfigError, LockTimeout, ManagerError
from xray_vps_manager.core.paths import paths
from xray_vps_manager.core.process import run_capture
from xray_vps_manager.core.terminal import print_table
from xray_vps_manager.runner import error_exit, fail
from xray_vps_manager.traffic import formatting as traffic_formatting
from xray_vps_manager.xray import cascade as cascade_config


def validate_name(name):
    try:
        client_service.validate_name(name)
    except ManagerError as exc:
        error_exit(exc)


def ensure_connections(config, db):
    try:
        client_connections.ensure_connections(config, db)
    except ValueError as exc:
        fail(str(exc))


def connection_display_name(config, db, tag):
    try:
        return client_connections.connection_display_name(config, db, tag)
    except ValueError as exc:
        fail(str(exc))


def link_for(config, client_id, name, connection_tag=None, db=None):
    try:
        return client_links.link_for(config, client_id, name, connection_tag=connection_tag, db=db, db_loader=client_repository.load_db_sql)
    except (RuntimeError, ValueError) as exc:
        fail(str(exc))


def access_key_for_client_id(client_id):
    return f"vpn-key:{client_id}"


def notify_access_updated(name):
    telegram = paths().sbin_dir / "xray-telegram"
    if not telegram.exists():
        return
    result = run_capture([str(telegram), "notify-access", name, "--quiet"], timeout=20)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        print(f"WARN: Telegram access notification failed: {detail}", file=sys.stderr)


def print_payment_summary():
    telegram = paths().sbin_dir / "xray-telegram"
    if not telegram.exists():
        return
    result = run_capture([str(telegram), "payment-amount"], timeout=10)
    if result.returncode == 0 and result.stdout.strip():
        print(result.stdout.strip())
    elif result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        print(f"WARN: Payment summary unavailable: {detail}", file=sys.stderr)


def print_route_options(config, db):
    rows = []
    active = cascade_config.active_cascade_tag(config)
    for item in client_route_service.route_options(db, config):
        tag = item["tag"]
        rows.append(
            [
                item.get("display") or "-",
                tag,
                "yes" if tag == active else "-",
            ]
        )
    print_table(["COUNTRY", "TAG", "GLOBAL ACTIVE"], rows, empty_message="Cascade routes are not configured.")


def format_traffic(value):
    return traffic_formatting.format_traffic(value, none_label="n/a")


@contextmanager
def client_change_errors():
    try:
        yield
    except client_crud.EnableTrafficLimitExceeded as exc:
        status = exc.traffic_status
        fail(
            "Traffic limit is exhausted for the current "
            f"{client_limits.traffic_limit_period_label(status['period'])}. "
            f"Used {format_traffic(status['usedBytes'])} of {format_traffic(status['limitBytes'])}. "
            f"Can be enabled after reset: {status['resetAt']}"
        )
    except ManagerError as exc:
        if exc.exit_code == 130:
            raise KeyboardInterrupt from exc
        if isinstance(exc, ApplyConfigError):
            fail(apply_error_text(exc))
        error_exit(exc)


@contextmanager
def policy_errors(purpose, quiet):
    with client_change_errors():
        try:
            yield
        except LockTimeout as exc:
            if not quiet:
                raise
            print(f"WARN: {exc.message}. Шаг {purpose} пропущен, следующий запуск таймера повторит его.", file=sys.stderr)


def print_limit_enforcement(change):
    result = change.result
    if not result.has_changes:
        print("No traffic limits exceeded or reset.")
        return
    if result.reactivated_names:
        print("Re-enabled clients after traffic limit reset: " + ", ".join(result.reactivated_names))
    if result.due_names:
        print("Disabled clients by traffic limit: " + ", ".join(result.due_names))
    if result.expired_during_recovery:
        print("Disabled expired clients: " + ", ".join(result.expired_during_recovery))
    print(f"Backup: {backup_text(change.backup)}")


def print_expiry(change):
    if not change.result.has_changes:
        print("No expired clients.")
        return
    print("Disabled expired clients: " + ", ".join(change.result.due_names))
    print(f"Backup: {backup_text(change.backup)}")


def print_access_update(name, change):
    result, backup = change.result, change.backup
    entry = result.entry
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
        print(f"Backup: {backup_text(backup)}")
    notify_access_updated(name)
