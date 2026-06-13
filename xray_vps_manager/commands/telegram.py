#!/usr/bin/env python3
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.core.server_env import read_server_env
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.traffic import repository as traffic_repository
from xray_vps_manager.telegram import admin as telegram_admin
from xray_vps_manager.telegram import api as telegram_api
from xray_vps_manager.telegram import notifications as telegram_notifications
from xray_vps_manager.telegram import payments as telegram_payments
from xray_vps_manager.telegram import poller as telegram_poller
from xray_vps_manager.telegram import setup as telegram_setup
from xray_vps_manager.telegram import settings as telegram_settings
from xray_vps_manager.telegram import subscriptions as telegram_subscriptions

SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
MANAGER_DB_PATH = Path("/usr/local/etc/xray/manager.db")
XRAY_CLIENT = Path("/usr/local/sbin/xray-client")
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
DEFAULT_SERVER_NAME = "Xray"


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def run_capture(command, timeout=20, input_text=None):
    return subprocess.run(
        command,
        check=False,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def require_root():
    if os.geteuid() != 0:
        die("Run this script as root.")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_stamp():
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_time(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_db():
    return telegram_settings.load_db_sql()


def load_db_readonly():
    return telegram_settings.load_db_sql()


def save_db(db):
    telegram_settings.save_db(db)


def save_db_sections(db, sections):
    telegram_settings.save_db_sections(db, sections)


def mask_token(token):
    return telegram_settings.mask_token(token)


def bot_name(db=None):
    return telegram_settings.bot_name(db, loader=load_db_readonly)


def set_bot_name(value):
    telegram_setup.set_bot_name(value)


def server_env_values():
    return read_server_env(SERVER_ENV_PATH)


def server_name_fragment():
    value = (server_env_values().get("SERVER_NAME") or DEFAULT_SERVER_NAME).strip()
    if not value or not SERVER_NAME_RE.fullmatch(value):
        value = DEFAULT_SERVER_NAME
    return quote(value, safe="")


def display_timezone():
    configured = (server_env_values().get("MANAGER_TIMEZONE") or "").strip()
    if configured:
        try:
            return ZoneInfo(configured), configured
        except ZoneInfoNotFoundError:
            return timezone.utc, f"UTC (invalid MANAGER_TIMEZONE: {configured})"
    local = datetime.now().astimezone().tzinfo or timezone.utc
    return local, "server local time"


def format_event_time(value):
    moment = parse_time(value)
    if not moment:
        return value or "-"
    tzinfo, label = display_timezone()
    return f"{moment.astimezone(tzinfo).strftime('%Y-%m-%d %H:%M:%S')} {label}"


def load_client_db():
    return client_repository.load_db_sql()


def load_traffic_db():
    return traffic_repository.load_traffic_db_for_read()


def set_route_mode(mode):
    telegram_setup.set_route_mode(mode)


def curl_json(db, method, payload=None, timeout=30):
    try:
        return telegram_api.curl_json(db, method, payload=payload, timeout=timeout)
    except ValueError as exc:
        die(str(exc))


def send_chat_message(db, chat_id, text, reply_markup=None, parse_mode=None):
    try:
        return telegram_api.send_chat_message(db, chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    except ValueError as exc:
        die(str(exc))


def send_message(db, text, parse_mode=None):
    return send_chat_message(db, db.get("chatId"), text, parse_mode=parse_mode)


def notification_context():
    return telegram_notifications.NotificationContext(
        load_db=load_db,
        save_db_sections=save_db_sections,
        load_client_db=load_client_db,
        load_traffic_db=load_traffic_db,
        display_timezone=display_timezone,
        format_event_time=format_event_time,
        format_access_until=format_access_until,
        parse_time=parse_time,
        utc_now=utc_now,
        utc_stamp=utc_stamp,
        run_capture=run_capture,
        send_chat_message=send_chat_message,
        send_message=send_message,
        bot_name=bot_name,
    )


def admin_context():
    return telegram_admin.AdminContext(
        load_client_db=load_client_db,
        save_db_sections=save_db_sections,
        format_access_until=format_access_until,
        run_capture=run_capture,
        send_chat_message=send_chat_message,
        bot_name=bot_name,
        notification_context=notification_context(),
    )


def poller_context():
    return telegram_poller.PollerContext(
        load_db=load_db,
        save_db_sections=save_db_sections,
        load_client_db=load_client_db,
        load_traffic_db=load_traffic_db,
        display_timezone=display_timezone,
        format_access_until=format_access_until,
        run_capture=run_capture,
        send_chat_message=send_chat_message,
        answer_callback_query=answer_callback_query,
        curl_json=curl_json,
        bot_name=bot_name,
        server_name_fragment=server_name_fragment,
        utc_stamp=utc_stamp,
        admin_context=admin_context(),
        xray_client=XRAY_CLIENT,
    )


def answer_callback_query(db, callback_id, text="", show_alert=False):
    return telegram_api.answer_callback_query(db, callback_id, text=text, show_alert=show_alert)


def configure_owner(send_test=True):
    telegram_setup.configure_owner(send_test=send_test)


def setup():
    telegram_setup.setup()


def set_enabled(value):
    telegram_setup.set_enabled(value)


def test_message():
    telegram_setup.test_message()


def configure_bot_commands():
    telegram_setup.configure_bot_commands()


def format_access_until(value):
    if not value:
        return "бессрочно"
    moment = parse_time(value)
    if not moment:
        return value
    tzinfo, label = display_timezone()
    return f"{moment.astimezone(tzinfo).strftime('%Y-%m-%d %H:%M')} {label}"


def payment_amount_label(db, client_db=None):
    return telegram_payments.payment_amount_label(db, client_db or load_client_db())


def print_payment_summary(db, client_db=None):
    summary = telegram_payments.payment_summary(db, client_db or load_client_db())
    print(f"Total rent amount: {summary['total']}")
    print(f"Paid clients: {summary['paidCount']}")
    print(f"Rounding: {summary['rounding']}")
    print(f"Amount per paid client: {summary['share']}")
    print(f"Payment details: {summary['transfer']}")
    if summary.get("warning"):
        print(f"WARN: {summary['warning']}")


def set_payment_amount(value):
    db = load_db()
    amount, _currency = telegram_payments.apply_payment_amount(db, value)
    save_db(db)
    if amount:
        print_payment_summary(db)
    else:
        print("Payment amount cleared.")


def set_payment_rounding(mode_value, step_value=None):
    db = load_db()
    telegram_payments.apply_payment_rounding(db, mode_value, step_value)
    save_db(db)
    print_payment_summary(db)


def show_payment_amount():
    db = load_db_readonly()
    print_payment_summary(db)


def set_payment_details(method, value="", bank=""):
    db = load_db()
    telegram_payments.apply_payment_transfer(db, method, value, bank)
    save_db(db)
    print_payment_summary(db)


def build_daily_summary_message(target_day=None):
    return telegram_notifications.build_daily_summary_message(notification_context(), target_day)


def notify_daily_summary(quiet=False, dry_run=False):
    return telegram_notifications.notify_daily_summary(notification_context(), quiet=quiet, dry_run=dry_run)


def poll_user_subscriptions(quiet=False, telegram_timeout=telegram_poller.USER_POLL_SHORT_TIMEOUT):
    return telegram_poller.poll_user_subscriptions(poller_context(), quiet=quiet, telegram_timeout=telegram_timeout)


def run_user_poller():
    telegram_poller.run_user_poller(poller_context())


def notify_expiry(quiet=False):
    return telegram_notifications.notify_expiry(notification_context(), quiet=quiet)


def notify_access_updated(name, quiet=False):
    return telegram_notifications.notify_access_updated(notification_context(), name, quiet=quiet)


def print_maintenance_notice_templates(db):
    telegram_notifications.print_maintenance_notice_templates(notification_context(), db)


def send_maintenance_notice(template_id="start", dry_run=False, yes=False):
    return telegram_notifications.send_maintenance_notice(
        notification_context(),
        template_id,
        dry_run=dry_run,
        yes=yes,
    )


def list_client_subscribers():
    db = load_db_readonly()
    client_db = load_client_db()
    clients = telegram_subscriptions.client_db_clients(client_db)
    subscriptions = db.get("clientSubscriptions", {})
    if not subscriptions:
        print("No client Telegram subscriptions.")
        return
    rows = []
    for chat_id, subscription in sorted(subscriptions.items(), key=lambda item: item[1].get("client", "")):
        name = subscription.get("client", "-")
        entry = clients.get(name, {})
        valid = "yes" if telegram_subscriptions.subscription_is_current(subscription, entry) else "no"
        rows.append(
            [
                name,
                subscription.get("chatLabel", "-"),
                chat_id,
                format_access_until(entry.get("expiresAt", "") if isinstance(entry, dict) else ""),
                valid,
                subscription.get("subscribedAt", "-"),
            ]
        )
    headers = ["CLIENT", "CHAT", "CHAT_ID", "ACCESS_UNTIL", "VALID", "SUBSCRIBED_AT"]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    print(border)
    print("| " + " | ".join(headers[index].ljust(widths[index]) for index in range(len(headers))) + " |")
    print(border)
    for row in rows:
        print("| " + " | ".join(str(row[index]).ljust(widths[index]) for index in range(len(row))) + " |")
    print(border)


def notify_geoip(quiet=False):
    return telegram_notifications.notify_geoip(notification_context(), quiet=quiet)


def status():
    db = load_db_readonly()
    subscriptions = db.get("clientSubscriptions", {})
    subscription_state = db.get("clientSubscriptionState", {})
    rows = [
        ("Enabled", "yes" if db.get("enabled") else "no"),
        ("Token", mask_token(db.get("token", ""))),
        ("Bot name", bot_name(db)),
        ("Chat", f"{db.get('chatLabel') or '-'} ({db.get('chatId') or '-'})"),
        ("Route mode", db.get("routeMode", "direct")),
        ("Payment amount", payment_amount_label(db)),
        ("Payment rounding", telegram_payments.payment_rounding_label(db)),
        ("Payment details", telegram_payments.payment_transfer_label(db)),
        ("Client subscriptions", str(len(subscriptions))),
        ("Manager DB", str(MANAGER_DB_PATH)),
        ("Last GeoIP notification", db.get("geoipState", {}).get("lastGeoipNotification") or db.get("lastGeoipNotification") or "never"),
        ("Last user poll", subscription_state.get("lastUserPoll") or "never"),
        ("Last expiry reminder", subscription_state.get("lastExpiryReminder") or "never"),
    ]
    width = max(len(key) for key, _value in rows)
    for key, value in rows:
        print(f"{key.ljust(width)} : {value}")


def usage():
    print(
        """Usage:
  xray-telegram status
  xray-telegram setup
  xray-telegram owner
  xray-telegram enable
  xray-telegram disable
  xray-telegram mode direct|cascade
  xray-telegram bot-name [NAME]
  xray-telegram test
  xray-telegram commands
  xray-telegram notify-geoip [--quiet]
  xray-telegram poll-users [--quiet]
  xray-telegram run-poller
  xray-telegram daily-summary [--dry-run]
  xray-telegram notify-daily-summary [--quiet|--dry-run]
  xray-telegram notify-expiry [--quiet]
  xray-telegram notify-access NAME [--quiet]
  xray-telegram maintenance-notice [start|done] [--dry-run|--yes]
  xray-telegram subscribers
  xray-telegram payment-amount [VALUE]
  xray-telegram payment-rounding [none|step VALUE]
  xray-telegram payment-details [none|phone PHONE BANK|card CARD_NUMBER|bank-account ACCOUNT]
"""
    )


def main():
    require_root()
    args = sys.argv[1:]
    command = args[0] if args else "status"
    try:
        if command == "status" and len(args) in (0, 1):
            status()
        elif command == "setup" and len(args) == 1:
            setup()
        elif command in ("owner", "chat", "finish-setup") and len(args) == 1:
            configure_owner(send_test=True)
        elif command == "enable" and len(args) == 1:
            set_enabled(True)
        elif command == "disable" and len(args) == 1:
            set_enabled(False)
        elif command == "mode" and len(args) == 2:
            set_route_mode(args[1])
        elif command == "bot-name" and len(args) in (1, 2):
            if len(args) == 1:
                print(f"Bot name: {bot_name(load_db_readonly())}")
            else:
                set_bot_name(args[1])
        elif command == "test" and len(args) == 1:
            test_message()
        elif command in ("commands", "set-commands") and len(args) == 1:
            configure_bot_commands()
        elif command == "notify-geoip" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--quiet":
                usage()
                sys.exit(1)
            sys.exit(notify_geoip(quiet=len(args) == 2))
        elif command == "poll-users" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--quiet":
                usage()
                sys.exit(1)
            sys.exit(poll_user_subscriptions(quiet=len(args) == 2))
        elif command in ("run-poller", "poll-daemon") and len(args) == 1:
            run_user_poller()
        elif command == "daily-summary" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--dry-run":
                usage()
                sys.exit(1)
            print(build_daily_summary_message())
        elif command == "notify-daily-summary" and len(args) in (1, 2):
            if len(args) == 2 and args[1] not in ("--quiet", "--dry-run"):
                usage()
                sys.exit(1)
            sys.exit(notify_daily_summary(quiet=len(args) == 2 and args[1] == "--quiet", dry_run=len(args) == 2 and args[1] == "--dry-run"))
        elif command == "notify-expiry" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--quiet":
                usage()
                sys.exit(1)
            sys.exit(notify_expiry(quiet=len(args) == 2))
        elif command == "notify-access" and len(args) in (2, 3):
            if len(args) == 3 and args[2] != "--quiet":
                usage()
                sys.exit(1)
            sys.exit(notify_access_updated(args[1], quiet=len(args) == 3))
        elif command == "maintenance-notice" and len(args) in (1, 2, 3):
            if len(args) == 1 or (len(args) == 2 and args[1] in ("list", "templates")):
                print_maintenance_notice_templates(load_db_readonly())
            else:
                template_id = args[1]
                flag = args[2] if len(args) == 3 else ""
                if flag and flag not in ("--dry-run", "--yes"):
                    usage()
                    sys.exit(1)
                sys.exit(send_maintenance_notice(template_id, dry_run=flag == "--dry-run", yes=flag == "--yes"))
        elif command in ("subscribers", "subscriptions") and len(args) == 1:
            list_client_subscribers()
        elif command == "payment-amount" and len(args) in (1, 2):
            if len(args) == 1:
                show_payment_amount()
            else:
                set_payment_amount(args[1])
        elif command == "payment-rounding" and len(args) in (1, 2, 3):
            if len(args) == 1:
                show_payment_amount()
            elif args[1] == "step":
                if len(args) != 3:
                    raise ValueError("Для режима step нужно указать шаг округления.")
                set_payment_rounding(args[1], args[2])
            elif len(args) == 2:
                set_payment_rounding(args[1])
            else:
                usage()
                sys.exit(1)
        elif command == "payment-details" and len(args) >= 1:
            if len(args) == 1:
                show_payment_amount()
            elif args[1] in ("none", "clear", "off") and len(args) == 2:
                set_payment_details(args[1])
            elif args[1] == "phone" and len(args) >= 4:
                set_payment_details(args[1], args[2], " ".join(args[3:]))
            elif args[1] in ("card", "bank-account") and len(args) >= 3:
                set_payment_details(args[1], " ".join(args[2:]))
            else:
                usage()
                sys.exit(1)
        else:
            usage()
            sys.exit(1)
    except Exception as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
