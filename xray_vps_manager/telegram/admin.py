"""Telegram admin panel actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xray_vps_manager.clients import access as client_access
from xray_vps_manager.telegram import keyboards, messages, notifications, payments, subscriptions


@dataclass(frozen=True)
class AdminContext:
    load_client_db: Callable[[], dict]
    save_db_sections: Callable[[dict, tuple[str, ...]], None]
    format_access_until: Callable[[str], str]
    run_capture: Callable[..., Any]
    send_chat_message: Callable[..., Any]
    bot_name: Callable[[dict | None], str]
    notification_context: notifications.NotificationContext
    xray_client: Path = Path("/usr/local/sbin/xray-client")


def send_admin_menu(ctx: AdminContext, db, chat_id, text=None):
    ctx.send_chat_message(db, chat_id, text or messages.admin_intro_text(), reply_markup=keyboards.admin_menu_keyboard())


def send_admin_notices_menu(ctx: AdminContext, db, chat_id, text=None):
    ctx.send_chat_message(
        db,
        chat_id,
        text or messages.admin_notices_intro_text(),
        reply_markup=keyboards.admin_notices_keyboard(),
    )


def send_admin_clients_menu(ctx: AdminContext, db, chat_id, text=None):
    ctx.send_chat_message(
        db,
        chat_id,
        text or "Xray VPS Manager: клиенты",
        reply_markup=keyboards.admin_clients_keyboard(),
    )


def admin_client_names(ctx: AdminContext) -> list[str]:
    clients = subscriptions.client_db_clients(ctx.load_client_db())
    return sorted(str(name) for name, entry in clients.items() if isinstance(entry, dict))


def send_admin_client_extend_list(ctx: AdminContext, db, chat_id):
    names = admin_client_names(ctx)
    if not names:
        clear_client_extend_state_if_pending(ctx, db, chat_id)
        send_admin_clients_menu(ctx, db, chat_id, "Клиентов пока нет.")
        return
    set_client_extend_selection(ctx, db, chat_id, names)
    ctx.send_chat_message(
        db,
        chat_id,
        "Выбери клиента, которому нужно продлить подписку.",
        reply_markup=keyboards.admin_client_extend_keyboard(names),
    )


def status_text(ctx: AdminContext, db):
    user_subscriptions = db.get("clientSubscriptions", {})
    subscription_state = db.get("clientSubscriptionState", {})
    daily_summary_state = db.get("dailySummaryState", {})
    client_db = ctx.load_client_db()
    return "\n".join(
        [
            "Xray VPS Manager: статус бота",
            "",
            f"Уведомления: {'включены' if db.get('enabled') else 'отключены'}",
            f"Маршрут Telegram: {db.get('routeMode', 'direct')}",
            f"Оплата: {payments.payment_amount_label(db, client_db)}",
            f"Округление: {payments.payment_rounding_label(db)}",
            f"Подписки клиентов: {len(user_subscriptions)}",
            f"Последний GeoIP: {db.get('geoipState', {}).get('lastGeoipNotification') or db.get('lastGeoipNotification') or 'never'}",
            f"Последний poll: {subscription_state.get('lastUserPoll') or 'never'}",
            f"Последнее напоминание: {subscription_state.get('lastExpiryReminder') or 'never'}",
            f"Последняя сводка: {daily_summary_state.get('lastSentDate') or 'never'}",
        ]
    )


def subscribers_text(ctx: AdminContext, db):
    user_subscriptions = db.get("clientSubscriptions", {})
    if not user_subscriptions:
        return "Подписок клиентов пока нет."
    client_db = ctx.load_client_db()
    clients = subscriptions.client_db_clients(client_db)
    lines = ["Xray VPS Manager: подписки клиентов", ""]
    for chat_id, subscription in sorted(user_subscriptions.items(), key=lambda item: item[1].get("client", ""))[:25]:
        name = subscription.get("client", "-")
        entry = clients.get(name, {})
        access_until = ctx.format_access_until(entry.get("expiresAt", "") if isinstance(entry, dict) else "")
        valid = "актуальна" if subscriptions.subscription_is_current(subscription, entry) else "требует проверки"
        lines.append(f"- {name}: {valid}, до {access_until}, чат {subscription.get('chatLabel', chat_id)}")
    if len(user_subscriptions) > 25:
        lines.append(f"...и ещё подписок: {len(user_subscriptions) - 25}")
    return "\n".join(lines)


def admin_utc_stamp(ctx: AdminContext) -> str:
    notification_context = ctx.notification_context
    if notification_context is not None and hasattr(notification_context, "utc_stamp"):
        return notification_context.utc_stamp()
    return ""


def run_server_test_text(ctx: AdminContext):
    test_script = Path("/usr/local/sbin/xray-test")
    if not test_script.exists():
        return "xray-test не найден на сервере."
    result = ctx.run_capture([str(test_script)], timeout=90)
    output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    header = "Xray VPS Manager: проверка сервера"
    if result.returncode == 0:
        header += "\nСтатус: OK"
    else:
        header += f"\nСтатус: ошибка, exit {result.returncode}"
    return messages.truncate_telegram_text(header + "\n\n" + output.strip())


def create_backup_text(ctx: AdminContext):
    backup_script = Path("/usr/local/sbin/xray-backup")
    if not backup_script.exists():
        return "xray-backup не найден на сервере."
    result = ctx.run_capture([str(backup_script), "create", "--path-only"], timeout=120)
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return messages.truncate_telegram_text(f"Не удалось создать backup, exit {result.returncode}.\n\n{output}")
    return "Backup создан на сервере:\n" + output


def maintenance_notice_message(ctx: AdminContext, db, template_id):
    return notifications.maintenance_notice_message(ctx.notification_context, db, template_id)


def maintenance_notice_recipients(db):
    return notifications.maintenance_notice_recipients(db)


def send_notice_message(ctx: AdminContext, db, message, dry_run=False, yes=False, label="message"):
    return notifications.send_notice_message(
        ctx.notification_context,
        db,
        message,
        dry_run=dry_run,
        yes=yes,
        label=label,
    )


def preview_notice(ctx: AdminContext, db, chat_id, kind):
    if kind == "custom":
        message = str(db.get("adminState", {}).get("customNoticeText") or "").strip()
        if not message:
            send_admin_notices_menu(ctx, db, chat_id, "Черновик своего сообщения пуст. Нажми «Своё сообщение» и отправь текст заново.")
            return
        title = "своё сообщение"
    else:
        message = maintenance_notice_message(ctx, db, kind)
        title = messages.MAINTENANCE_NOTICE_TEMPLATES[kind]["title"]
    recipients = len(maintenance_notice_recipients(db))
    text = "\n".join(
        [
            f"Предпросмотр: {title}",
            f"Получателей: {recipients}",
            "",
            message,
        ]
    )
    ctx.send_chat_message(db, chat_id, text, reply_markup=keyboards.admin_notice_confirm_keyboard(kind))


def set_custom_notice_waiting(ctx: AdminContext, db, chat_id):
    db.setdefault("adminState", {})[str(chat_id)] = {"action": "custom-notice-text", "startedAt": admin_utc_stamp(ctx)}
    ctx.save_db_sections(db, ("adminState",))


def set_client_extend_waiting(ctx: AdminContext, db, chat_id, name):
    db.setdefault("adminState", {})[str(chat_id)] = {
        "action": "extend-subscription-days",
        "client": name,
        "startedAt": admin_utc_stamp(ctx),
    }
    ctx.save_db_sections(db, ("adminState",))


def set_client_extend_selection(ctx: AdminContext, db, chat_id, names):
    db.setdefault("adminState", {})[str(chat_id)] = {
        "action": "extend-subscription-select",
        "clients": list(names),
        "startedAt": admin_utc_stamp(ctx),
    }
    ctx.save_db_sections(db, ("adminState",))


def clear_admin_state(ctx: AdminContext, db, chat_id, clear_custom_notice=True):
    state = db.setdefault("adminState", {})
    state.pop(str(chat_id), None)
    if clear_custom_notice:
        state.pop("customNoticeText", None)
    ctx.save_db_sections(db, ("adminState",))


def clear_client_extend_state_if_pending(ctx: AdminContext, db, chat_id):
    pending = db.get("adminState", {}).get(str(chat_id), {})
    if pending.get("action") in ("extend-subscription-select", "extend-subscription-days"):
        clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)


def send_admin_notice(ctx: AdminContext, db, chat_id, kind):
    if kind == "custom":
        message = str(db.get("adminState", {}).get("customNoticeText") or "").strip()
        if not message:
            send_admin_notices_menu(ctx, db, chat_id, "Черновик своего сообщения пуст. Отправка отменена.")
            return
        label = "своё сообщение"
    else:
        message = maintenance_notice_message(ctx, db, kind)
        label = messages.MAINTENANCE_NOTICE_TEMPLATES[kind]["title"]
    rc = send_notice_message(ctx, db, message, yes=True, label=label)
    clear_admin_state(ctx, db, chat_id)
    if rc == 0:
        send_admin_notices_menu(ctx, db, chat_id, "Уведомление отправлено подписанным клиентам.")
    else:
        send_admin_notices_menu(ctx, db, chat_id, "Уведомление отправлено не всем. Проверь логи сервера.")


def handle_custom_notice_text(ctx: AdminContext, db, chat_id, text):
    pending = db.get("adminState", {}).get(str(chat_id), {})
    if pending.get("action") != "custom-notice-text":
        return False
    if text.lower() in ("/cancel", "cancel", "отмена"):
        clear_admin_state(ctx, db, chat_id)
        send_admin_notices_menu(ctx, db, chat_id, "Создание своего сообщения отменено.")
        return True
    db.setdefault("adminState", {})["customNoticeText"] = text
    db["adminState"].pop(str(chat_id), None)
    ctx.save_db_sections(db, ("adminState",))
    preview_notice(ctx, db, chat_id, "custom")
    return True


def selected_client_name(db, chat_id, index_value):
    try:
        index = int(index_value)
    except (TypeError, ValueError):
        return ""
    pending = db.get("adminState", {}).get(str(chat_id), {})
    names = pending.get("clients", []) if pending.get("action") == "extend-subscription-select" else []
    if index < 0 or index >= len(names):
        return ""
    return str(names[index])


def set_extend_subscription_waiting(ctx: AdminContext, db, chat_id, name):
    set_client_extend_waiting(ctx, db, chat_id, name)
    ctx.send_chat_message(
        db,
        chat_id,
        f"Отправь числом, на сколько дней продлить подписку для {name}.\n\n"
        "Например: 30\n"
        "Если передумаешь, отправь /cancel.",
        reply_markup=keyboards.admin_client_extend_cancel_keyboard(),
    )


def command_output(result):
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return (stdout + (("\n" + stderr) if stderr else "")).strip()


def extend_subscription_text(ctx: AdminContext, name, days, result):
    if getattr(result, "returncode", 1) != 0:
        output = command_output(result)
        details = f"\n\n{output}" if output else ""
        return messages.truncate_telegram_text(f"Не удалось продлить подписку для {name}, exit {getattr(result, 'returncode', 1)}.{details}")

    try:
        clients = subscriptions.client_db_clients(ctx.load_client_db())
    except Exception:
        clients = {}
    entry = clients.get(name, {})
    access_until = ctx.format_access_until(entry.get("expiresAt", "") if isinstance(entry, dict) else "")
    lines = [
        f"Подписка продлена для {name} на {days} дн.",
        f"Доступ до: {access_until}",
    ]
    output = command_output(result)
    if output:
        lines.extend(["", output])
    return messages.truncate_telegram_text("\n".join(lines))


def handle_extend_subscription_days(ctx: AdminContext, db, chat_id, text):
    pending = db.get("adminState", {}).get(str(chat_id), {})
    if pending.get("action") != "extend-subscription-days":
        return False
    if text.lower() in ("/cancel", "cancel", "отмена"):
        clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
        send_admin_clients_menu(ctx, db, chat_id, "Продление подписки отменено.")
        return True

    name = str(pending.get("client") or "")
    if name not in admin_client_names(ctx):
        clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
        send_admin_clients_menu(ctx, db, chat_id, "Клиент больше не найден. Выбери клиента заново.")
        return True

    try:
        days = client_access.parse_extend_days(text)
    except ValueError:
        ctx.send_chat_message(
            db,
            chat_id,
            "Нужно отправить положительное целое число дней. Например: 30\n\n"
            "Если передумаешь, отправь /cancel.",
            reply_markup=keyboards.admin_client_extend_cancel_keyboard(),
        )
        return True

    result = ctx.run_capture([str(ctx.xray_client), "extend-days", name, str(days)], timeout=120)
    clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
    send_admin_clients_menu(ctx, db, chat_id, extend_subscription_text(ctx, name, days, result))
    return True


def handle_pending_text(ctx: AdminContext, db, chat_id, text):
    pending = db.get("adminState", {}).get(str(chat_id), {})
    action = pending.get("action")
    if action == "custom-notice-text":
        return handle_custom_notice_text(ctx, db, chat_id, text)
    if action == "extend-subscription-days":
        return handle_extend_subscription_days(ctx, db, chat_id, text)
    return False


def handle_callback(ctx: AdminContext, db, chat_id, data):
    if data == "admin:menu":
        send_admin_menu(ctx, db, chat_id)
        return True
    if data == "admin:status":
        send_admin_menu(ctx, db, chat_id, status_text(ctx, db))
        return True
    if data == "admin:subscribers":
        send_admin_menu(ctx, db, chat_id, subscribers_text(ctx, db))
        return True
    if data == "admin:clients":
        clear_client_extend_state_if_pending(ctx, db, chat_id)
        send_admin_clients_menu(ctx, db, chat_id)
        return True
    if data == "admin:client-extend":
        send_admin_client_extend_list(ctx, db, chat_id)
        return True
    if data.startswith("admin:client-extend:"):
        name = selected_client_name(db, chat_id, data.rsplit(":", 1)[1])
        if not name:
            send_admin_clients_menu(ctx, db, chat_id, "Клиент не найден. Открой список заново.")
            return True
        if name not in admin_client_names(ctx):
            clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
            send_admin_clients_menu(ctx, db, chat_id, "Клиент больше не найден. Выбери клиента заново.")
            return True
        set_extend_subscription_waiting(ctx, db, chat_id, name)
        return True
    if data == "admin:client-extend-cancel":
        clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
        send_admin_clients_menu(ctx, db, chat_id, "Продление подписки отменено.")
        return True
    if data == "admin:daily-summary":
        send_admin_menu(ctx, db, chat_id, notifications.build_daily_summary_message(ctx.notification_context))
        return True
    if data == "admin:geoip":
        rc = notifications.notify_geoip(ctx.notification_context, quiet=True)
        text = "GeoIP-проверка выполнена. Если были новые события, бот отправил отдельное уведомление."
        if rc != 0:
            text = f"GeoIP-проверка завершилась с ошибкой, exit {rc}."
        send_admin_menu(ctx, db, chat_id, text)
        return True
    if data == "admin:expiry":
        rc = notifications.notify_expiry(ctx.notification_context, quiet=True)
        text = "Проверка напоминаний выполнена."
        if rc != 0:
            text = f"Проверка напоминаний завершилась с ошибкой, exit {rc}."
        send_admin_menu(ctx, db, chat_id, text)
        return True
    if data == "admin:test":
        send_admin_menu(ctx, db, chat_id, run_server_test_text(ctx))
        return True
    if data == "admin:backup":
        send_admin_menu(ctx, db, chat_id, create_backup_text(ctx))
        return True
    if data == "admin:notices":
        send_admin_notices_menu(ctx, db, chat_id)
        return True
    if data in ("admin:notice:start", "admin:notice:done"):
        preview_notice(ctx, db, chat_id, data.rsplit(":", 1)[1])
        return True
    if data == "admin:notice:custom":
        set_custom_notice_waiting(ctx, db, chat_id)
        ctx.send_chat_message(
            db,
            chat_id,
            "Отправь следующим сообщением текст, который нужно разослать подписанным клиентам.\n\n"
            "Если передумаешь, отправь /cancel.",
            reply_markup={"inline_keyboard": [[{"text": "Отмена", "callback_data": "admin:notice-cancel"}]]},
        )
        return True
    if data.startswith("admin:notice-send:"):
        kind = data.rsplit(":", 1)[1]
        if kind not in ("start", "done", "custom"):
            send_admin_notices_menu(ctx, db, chat_id, "Неизвестный тип уведомления.")
            return True
        send_admin_notice(ctx, db, chat_id, kind)
        return True
    if data == "admin:notice-cancel":
        clear_admin_state(ctx, db, chat_id)
        send_admin_notices_menu(ctx, db, chat_id, "Рассылка отменена.")
        return True
    send_admin_menu(ctx, db, chat_id, "Неизвестная админская кнопка.")
    return True
