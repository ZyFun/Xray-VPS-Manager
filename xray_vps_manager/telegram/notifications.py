"""Telegram notification services."""

from __future__ import annotations

import hashlib
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.core.paths import MANAGER_DB_PATH
from xray_vps_manager.telegram import messages, payments, subscriptions
from xray_vps_manager.traffic import formatting as traffic_formatting
from xray_vps_manager.traffic import history as traffic_history

XRAY_GEOIP_OUTBOUND_PREFIX = "geoip-warning-"
EXPIRY_REMINDER_DAYS = (5, 1)
EXPIRY_REMINDER_HOUR = 8
DAILY_SUMMARY_HOUR = 8
ONLINE_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class NotificationContext:
    load_db: Callable[[], dict]
    save_db_sections: Callable[[dict, tuple[str, ...]], None]
    load_client_db: Callable[[], dict]
    load_traffic_db: Callable[[], dict]
    display_timezone: Callable[[], tuple[Any, str]]
    format_event_time: Callable[[str], str]
    format_access_until: Callable[[str], str]
    parse_time: Callable[[str], datetime | None]
    utc_now: Callable[[], datetime]
    utc_stamp: Callable[[], str]
    run_capture: Callable[..., Any]
    send_chat_message: Callable[..., Any]
    send_message: Callable[..., Any]
    bot_name: Callable[[dict | None], str]
    manager_db_path: Path | None = None


def format_traffic(value):
    return traffic_formatting.format_traffic(value, none_label="0.00KB")


def traffic_day_total(entry, day_key):
    return traffic_history.traffic_day_total(entry, day_key)


def systemd_state(ctx: NotificationContext, unit):
    result = ctx.run_capture(["systemctl", "is-active", unit], timeout=5)
    if result.returncode == 0:
        return result.stdout.strip() or "active"
    return (result.stdout or result.stderr or "unknown").strip() or "unknown"


def disk_usage_label(path="/"):
    usage = shutil.disk_usage(path)
    used = usage.total - usage.free
    percent = int((used / usage.total) * 100) if usage.total else 0
    return f"{percent}% занято, свободно {format_traffic(usage.free)}"


def database_usage_label(path: Path | None = None):
    db_path = Path(path or MANAGER_DB_PATH)
    related = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
    existing = [item for item in related if item.exists()]
    if not existing:
        return f"{db_path.name}: missing"
    total = sum(item.stat().st_size for item in existing if item.is_file())
    details = ", ".join(f"{item.name} {format_traffic(item.stat().st_size)}" for item in existing if item.is_file())
    return f"{format_traffic(total)} ({details})"


def client_enabled(entry):
    return entry.get("enabled") is not False


def online_clients_count(ctx: NotificationContext, client_db, traffic_db):
    now = ctx.utc_now()
    count = 0
    clients = subscriptions.client_db_clients(client_db)
    for name, entry in clients.items():
        if not client_enabled(entry):
            continue
        traffic_entry = traffic_db.get("clients", {}).get(name, {})
        parsed = ctx.parse_time(traffic_entry.get("lastOnline", ""))
        if parsed and (now - parsed).total_seconds() <= ONLINE_WINDOW_SECONDS:
            count += 1
    return count


def daily_traffic_rows(client_db, traffic_db, day_key):
    names = set(subscriptions.client_db_clients(client_db)) | set(traffic_db.get("clients", {}))
    rows = []
    total_in = 0
    total_out = 0
    for name in sorted(names):
        entry = traffic_db.get("clients", {}).get(name, {})
        incoming, outgoing = traffic_day_total(entry, day_key)
        total = incoming + outgoing
        total_in += incoming
        total_out += outgoing
        if total > 0:
            rows.append({"name": name, "incoming": incoming, "outgoing": outgoing, "total": total})
    rows.sort(key=lambda item: item["total"], reverse=True)
    return rows, total_in, total_out


def build_daily_summary_message(ctx: NotificationContext, target_day=None):
    tzinfo, timezone_label = ctx.display_timezone()
    now_local = ctx.utc_now().astimezone(tzinfo)
    day = target_day or (now_local.date() - timedelta(days=1))
    day_key = day.isoformat()
    db = ctx.load_db()
    client_db = ctx.load_client_db()
    traffic_db = ctx.load_traffic_db()
    clients = subscriptions.client_db_clients(client_db)
    enabled_count = sum(1 for entry in clients.values() if client_enabled(entry))
    rows, total_in, total_out = daily_traffic_rows(client_db, traffic_db, day_key)
    total = total_in + total_out
    total_rent = payments.format_payment_amount(
        str(db.get("paymentTotalAmount") or "").strip(),
        db.get("paymentCurrency") or "₽",
    )

    lines = [
        "Xray VPS Manager: ежедневная сводка",
        "",
        f"Период трафика: {day_key} ({timezone_label})",
        f"Xray: {systemd_state(ctx, 'xray.service')}",
        f"Telegram poller: {systemd_state(ctx, 'xray-telegram-poller.service')}",
        f"Клиенты: включено {enabled_count} из {len(clients)}, online сейчас: {online_clients_count(ctx, client_db, traffic_db)}",
        f"Общая аренда сервера: {total_rent}",
        f"База данных: {database_usage_label(ctx.manager_db_path)}",
        f"Диск /: {disk_usage_label('/')}",
        "",
        "Трафик за предыдущий день:",
        f"IN: {format_traffic(total_in)}",
        f"OUT: {format_traffic(total_out)}",
        f"TOTAL: {format_traffic(total)}",
    ]
    if rows:
        lines.extend(["", "Топ клиентов:"])
        for index, row in enumerate(rows[:5], start=1):
            lines.append(
                f"{index}. {row['name']}: {format_traffic(row['total'])} "
                f"(IN {format_traffic(row['incoming'])} / OUT {format_traffic(row['outgoing'])})"
            )
        if len(rows) > 5:
            lines.append(f"...и ещё клиентов с трафиком: {len(rows) - 5}")
    else:
        lines.extend(["", "Трафика за этот день не было."])
    return "\n".join(lines)


def notify_daily_summary(ctx: NotificationContext, quiet=False, dry_run=False):
    db = ctx.load_db()
    if not db.get("enabled") or not db.get("token") or not db.get("chatId"):
        if not quiet:
            print("Telegram bot notifications are not configured or disabled.")
        return 0

    tzinfo, _timezone_label = ctx.display_timezone()
    now_local = ctx.utc_now().astimezone(tzinfo)
    target_day = now_local.date() - timedelta(days=1)
    target_key = target_day.isoformat()
    state = db.setdefault("dailySummaryState", {})

    if dry_run:
        print(build_daily_summary_message(ctx, target_day))
        return 0

    if now_local.hour < DAILY_SUMMARY_HOUR:
        if not quiet:
            print(f"Daily summary is not due yet: local time {now_local.strftime('%H:%M')}.")
        return 0

    if state.get("lastSentDate") == target_key:
        if not quiet:
            print(f"Daily summary already sent for {target_key}.")
        return 0

    message = build_daily_summary_message(ctx, target_day)
    try:
        ctx.send_message(db, message)
    except Exception as exc:
        if not quiet:
            print(f"ERROR: daily summary notification failed: {exc}", file=sys.stderr)
        return 1

    state["lastSentDate"] = target_key
    state["lastSentAt"] = ctx.utc_stamp()
    ctx.save_db_sections(db, ("dailySummaryState",))
    if not quiet:
        print(f"Daily summary sent for {target_key}.")
    return 0


def expiry_reminder_schedule(ctx: NotificationContext, entry, days_before):
    expires_at = ctx.parse_time(entry.get("expiresAt", ""))
    if not expires_at:
        return None
    tzinfo, label = ctx.display_timezone()
    expiry_local = expires_at.astimezone(tzinfo)
    reminder_date = expiry_local.date() - timedelta(days=days_before)
    reminder_local = datetime(
        reminder_date.year,
        reminder_date.month,
        reminder_date.day,
        EXPIRY_REMINDER_HOUR,
        0,
        0,
        tzinfo=tzinfo,
    )
    return reminder_local, expiry_local, label


def expiry_reminder_due(ctx: NotificationContext, entry, days_before, now=None):
    schedule = expiry_reminder_schedule(ctx, entry, days_before)
    if not schedule:
        return None
    reminder_local, expiry_local, label = schedule
    now_local = (now or ctx.utc_now()).astimezone(reminder_local.tzinfo)
    if now_local >= reminder_local and now_local < expiry_local:
        return reminder_local, expiry_local, label
    return None


def expiry_reminder_key(chat_id, name, entry, days_before):
    return "|".join(
        [
            str(chat_id),
            str(name),
            subscriptions.client_entry_id(entry),
            str(entry.get("expiresAt", "")),
            f"{days_before}d",
        ]
    )


def build_expiry_reminder_message(ctx: NotificationContext, db, client_db, entry, days_before, expiry_local, timezone_label):
    return messages.build_expiry_reminder_message(
        db,
        entry,
        days_before,
        expiry_local,
        timezone_label,
        ctx.bot_name,
        lambda current_db: payments.payment_amount_label(current_db, client_db),
    )


def notify_expiry(ctx: NotificationContext, quiet=False):
    db = ctx.load_db()
    if not db.get("enabled") or not db.get("token"):
        if not quiet:
            print("Telegram bot notifications are not configured or disabled.")
        return 0
    client_db = ctx.load_client_db()
    clients = subscriptions.client_db_clients(client_db)
    user_subscriptions = db.setdefault("clientSubscriptions", {})
    state = db.setdefault("clientSubscriptionState", {})
    sent = state.setdefault("expiryReminders", {})
    now = ctx.utc_now()
    sent_count = 0

    for chat_id, subscription in list(user_subscriptions.items()):
        if not isinstance(subscription, dict) or subscription.get("enabled") is False:
            continue
        name = subscription.get("client", "")
        entry = clients.get(name)
        if not isinstance(entry, dict):
            continue
        if entry.get("paymentType") != "paid":
            continue
        if not subscriptions.subscription_matches_entry(subscription, entry):
            continue
        for days_before in EXPIRY_REMINDER_DAYS:
            due = expiry_reminder_due(ctx, entry, days_before, now)
            if not due:
                continue
            key = expiry_reminder_key(chat_id, name, entry, days_before)
            if key in sent:
                continue
            _reminder_local, expiry_local, timezone_label = due
            message = build_expiry_reminder_message(ctx, db, client_db, entry, days_before, expiry_local, timezone_label)
            try:
                ctx.send_chat_message(db, chat_id, message)
            except Exception as exc:
                if not quiet:
                    print(f"ERROR: expiry reminder failed for {name}/{chat_id}: {exc}", file=sys.stderr)
                continue
            sent[key] = ctx.utc_stamp()
            sent_count += 1

    if len(sent) > 2000:
        sent_items = sorted(sent.items(), key=lambda item: item[1])[-2000:]
        state["expiryReminders"] = dict(sent_items)
    state["lastExpiryReminderCheck"] = ctx.utc_stamp()
    if sent_count:
        state["lastExpiryReminder"] = ctx.utc_stamp()
    ctx.save_db_sections(db, ("clientSubscriptionState",))
    if not quiet:
        print(f"Expiry reminders sent: {sent_count}")
    return 0


def build_access_updated_message(ctx: NotificationContext, db, entry):
    return messages.build_access_updated_message(db, entry, ctx.bot_name, ctx.format_access_until)


def notify_access_updated(ctx: NotificationContext, name, quiet=False):
    db = ctx.load_db()
    if not db.get("enabled") or not db.get("token"):
        if not quiet:
            print("Telegram bot notifications are not configured or disabled.")
        return 0
    client_db = ctx.load_client_db()
    entry = subscriptions.client_db_clients(client_db).get(name)
    if not isinstance(entry, dict):
        if not quiet:
            print(f"Client not found: {name}")
        return 1
    if entry.get("paymentType") != "paid":
        if not quiet:
            print(f"Client is free, access payment notification skipped: {name}")
        return 0
    message = build_access_updated_message(ctx, db, entry)
    sent_count = 0
    for chat_id, subscription in db.get("clientSubscriptions", {}).items():
        if not isinstance(subscription, dict) or subscription.get("enabled") is False:
            continue
        if subscription.get("client") != name:
            continue
        if not subscriptions.subscription_matches_entry(subscription, entry):
            continue
        try:
            ctx.send_chat_message(db, chat_id, message)
            sent_count += 1
        except Exception as exc:
            if not quiet:
                print(f"ERROR: access update notification failed for {name}/{chat_id}: {exc}", file=sys.stderr)
    if not quiet:
        print(f"Access update notifications sent: {sent_count}")
    return 0


def maintenance_notice_message(ctx: NotificationContext, db, template_id):
    return messages.maintenance_notice_message(db, template_id, ctx.bot_name)


def normalize_maintenance_template_id(value):
    return messages.normalize_maintenance_template_id(value)


def maintenance_notice_recipients(db):
    recipients = []
    seen = set()
    for chat_id, subscription in db.get("clientSubscriptions", {}).items():
        if not isinstance(subscription, dict) or subscription.get("enabled") is False:
            continue
        chat_id = str(chat_id or "").strip()
        if not chat_id or chat_id in seen:
            continue
        recipients.append((chat_id, subscription))
        seen.add(chat_id)
    return recipients


def print_maintenance_notice_templates(ctx: NotificationContext, db):
    print("Доступные уведомления о технических работах:")
    print()
    for index, (key, template) in enumerate(messages.MAINTENANCE_NOTICE_TEMPLATES.items(), start=1):
        print(f"{index}) {template['title']} ({key})")
        print(maintenance_notice_message(ctx, db, key))
        print()


def send_notice_message(ctx: NotificationContext, db, message, dry_run=False, yes=False, label="message"):
    recipients = maintenance_notice_recipients(db)
    if dry_run or not yes:
        print(f"Notice: {label}")
        print(f"Recipients: {len(recipients)}")
        print()
        print(message)
        if not dry_run:
            print()
            print("To send, repeat command with --yes.")
        return 0
    sent_count = 0
    failed_count = 0
    for chat_id, _subscription in recipients:
        try:
            ctx.send_chat_message(db, chat_id, message)
            sent_count += 1
        except Exception as exc:
            failed_count += 1
            print(f"ERROR: maintenance notice failed for {chat_id}: {exc}", file=sys.stderr)
    print(f"Maintenance notice sent: {sent_count}")
    if failed_count:
        print(f"Maintenance notice failed: {failed_count}")
        return 1
    return 0


def send_maintenance_notice(ctx: NotificationContext, template_id="start", dry_run=False, yes=False):
    db = ctx.load_db()
    if not db.get("enabled") or not db.get("token"):
        print("Telegram bot notifications are not configured or disabled.")
        return 0
    template_id = normalize_maintenance_template_id(template_id)
    message = maintenance_notice_message(ctx, db, template_id)
    template = messages.MAINTENANCE_NOTICE_TEMPLATES[str(template_id)]
    return send_notice_message(ctx, db, message, dry_run=dry_run, yes=yes, label=template["title"])


def geoip_regions(event):
    risks = set(event.get("risks") or [])
    outbound = str(event.get("outbound") or "").lower()
    if outbound.startswith(XRAY_GEOIP_OUTBOUND_PREFIX):
        code = outbound[len(XRAY_GEOIP_OUTBOUND_PREFIX):].upper()
        if code:
            risks.add(f"xray-geoip:{code}")
    regions = []
    for risk in sorted(risks):
        if str(risk).startswith("xray-geoip:"):
            regions.append(str(risk).split(":", 1)[1].upper())
    return regions


def event_id(event):
    payload = "|".join(
        str(event.get(key, ""))
        for key in ("time", "client", "host", "port", "outbound", "source", "target")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def iter_new_sqlite_events(ctx: NotificationContext, db, state):
    after_id = int(state.get("sqliteLastEventId", 0) or 0)
    after_time = None
    if after_id <= 0:
        after_time = state.get("lastGeoipNotification") or db.get("lastGeoipNotification")
    result = activity_repository.geoip_events_after_for_read(
        after_id=after_id,
        after_time=after_time,
        db_path=ctx.manager_db_path,
    )
    events, last_id = result
    state["sqliteLastEventId"] = int(last_id or after_id or 0)
    state["updated"] = ctx.utc_stamp()
    return events


def iter_new_events(ctx: NotificationContext, db):
    state = db.setdefault("geoipState", {})
    return iter_new_sqlite_events(ctx, db, state)


def build_geoip_message(ctx: NotificationContext, events):
    grouped = {}
    for event in events:
        regions = ",".join(geoip_regions(event)) or "-"
        host = event.get("host") or "-"
        port = str(event.get("port") or "-")
        key = (event.get("client") or "-", regions, host, port)
        row = grouped.setdefault(
            key,
            {
                "client": event.get("client") or "-",
                "regions": regions,
                "host": host,
                "port": port,
                "count": 0,
                "last": event.get("time") or "",
                "outbound": event.get("outbound") or "-",
            },
        )
        row["count"] += 1
        if event.get("time", "") >= row["last"]:
            row["last"] = event.get("time") or row["last"]
            row["outbound"] = event.get("outbound") or row["outbound"]

    rows = sorted(grouped.values(), key=lambda item: (item["count"], item["last"]), reverse=True)
    lines = [
        "Xray VPS Manager: обнаружено подключение по GeoIP",
        f"Новых событий: {len(events)}",
        "",
    ]
    for row in rows[:10]:
        lines.extend(
            [
                f"Клиент: {row['client']}",
                f"Регион: {row['regions']}",
                f"Цель: {row['host']}:{row['port']}",
                f"Событий: {row['count']}",
                f"Последнее: {ctx.format_event_time(row['last'])}",
                f"Outbound: {row['outbound']}",
                "",
            ]
        )
    if len(rows) > 10:
        lines.append(f"И ещё целей: {len(rows) - 10}")
    return "\n".join(lines).strip()


def notify_geoip(ctx: NotificationContext, quiet=False):
    db = ctx.load_db()
    if not db.get("enabled") or not db.get("token") or not db.get("chatId"):
        if not quiet:
            print("Telegram bot notifications are not configured or disabled.")
        return 0

    state = db.setdefault("geoipState", {})
    sent_ids = list(state.get("sentIds", []))[-500:]
    sent_set = set(sent_ids)
    exceptions = activity_exceptions.exception_items(db_path=ctx.manager_db_path)
    candidates = []
    for event in iter_new_events(ctx, db):
        if not geoip_regions(event):
            continue
        if activity_exceptions.event_exception(event, exceptions):
            continue
        item_id = event_id(event)
        if item_id in sent_set:
            continue
        candidates.append(event)
        sent_ids.append(item_id)
        sent_set.add(item_id)

    if not candidates:
        state["sentIds"] = sent_ids[-500:]
        ctx.save_db_sections(db, ("geoipState",))
        if not quiet:
            print("No new GeoIP events for Telegram notification.")
        return 0

    message = build_geoip_message(ctx, candidates)
    try:
        ctx.send_message(db, message)
    except Exception as exc:
        if not quiet:
            print(f"ERROR: Telegram notification failed: {exc}", file=sys.stderr)
            return 1
        return 0
    state["sentIds"] = sent_ids[-500:]
    state["lastGeoipNotification"] = ctx.utc_stamp()
    ctx.save_db_sections(db, ("geoipState",))
    if not quiet:
        print(f"Telegram GeoIP notification sent: {len(candidates)} events.")
    return 0
