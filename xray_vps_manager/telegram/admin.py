"""Telegram admin panel actions."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xray_vps_manager.clients import access as client_access
from xray_vps_manager.telegram import keyboards, messages, notifications, payments, server_settings, subscriptions
from xray_vps_manager.xray import caddy

ADMIN_CALLBACK_GUARDS_KEY = "callbackGuards"
ADMIN_CONSUMED_CALLBACK_LIMIT = 40
CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def default_server_name_fragment() -> str:
    return "Xray"


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
    server_name_fragment: Callable[[], str] = default_server_name_fragment
    list_tls_sites: Callable[[], list[dict[str, Any]]] = server_settings.tls_site_rows
    set_tls_site_version: Callable[..., Any] = server_settings.set_tls_site_version


def normalize_message_id(value):
    try:
        message_id = int(value)
    except (TypeError, ValueError):
        return None
    if message_id <= 0:
        return None
    return message_id


def response_message_id(response):
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        result = response
    return normalize_message_id(result.get("message_id"))


def callback_message_id(callback_message):
    if not isinstance(callback_message, dict):
        return None
    return normalize_message_id(callback_message.get("message_id"))


def _callback_guard(db, chat_id):
    admin_state = db.setdefault("adminState", {})
    guards = admin_state.setdefault(ADMIN_CALLBACK_GUARDS_KEY, {})
    if not isinstance(guards, dict):
        guards = {}
        admin_state[ADMIN_CALLBACK_GUARDS_KEY] = guards
    chat_key = str(chat_id)
    guard = guards.setdefault(chat_key, {})
    if not isinstance(guard, dict):
        guard = {}
        guards[chat_key] = guard
    return guard


def _consumed_message_ids(guard):
    consumed = []
    for value in guard.get("consumedMessageIds", []):
        message_id = normalize_message_id(value)
        if message_id is not None:
            consumed.append(message_id)
    return consumed[-ADMIN_CONSUMED_CALLBACK_LIMIT:]


def accept_admin_callback(ctx: AdminContext, db, chat_id, data, message_id):
    message_id = normalize_message_id(message_id)
    if message_id is None:
        return True

    guard = _callback_guard(db, chat_id)
    active_message_id = normalize_message_id(guard.get("activeMessageId"))
    consumed = _consumed_message_ids(guard)
    if data != "admin:menu" and active_message_id is not None and message_id != active_message_id:
        return False
    if message_id in consumed:
        return False

    consumed.append(message_id)
    guard["consumedMessageIds"] = consumed[-ADMIN_CONSUMED_CALLBACK_LIMIT:]
    guard["activeMessageId"] = ""
    ctx.save_db_sections(db, ("adminState",))
    return True


def register_admin_message(ctx: AdminContext, db, chat_id, response):
    message_id = response_message_id(response)
    if message_id is None:
        return
    guard = _callback_guard(db, chat_id)
    guard["activeMessageId"] = message_id
    guard["consumedMessageIds"] = _consumed_message_ids(guard)
    ctx.save_db_sections(db, ("adminState",))


def send_admin_response(ctx: AdminContext, db, chat_id, text, reply_markup, parse_mode=None):
    response = ctx.send_chat_message(db, chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    register_admin_message(ctx, db, chat_id, response)


def send_admin_menu(ctx: AdminContext, db, chat_id, text=None):
    send_admin_response(ctx, db, chat_id, text or messages.admin_intro_text(), keyboards.admin_menu_keyboard())


def send_admin_status_menu(ctx: AdminContext, db, chat_id, text=None):
    send_admin_response(
        ctx,
        db,
        chat_id,
        text or messages.admin_status_intro_text(),
        keyboards.admin_status_keyboard(),
    )


def send_admin_notices_menu(ctx: AdminContext, db, chat_id, text=None):
    send_admin_response(
        ctx,
        db,
        chat_id,
        text or messages.admin_notices_intro_text(),
        keyboards.admin_notices_keyboard(),
    )


def send_admin_clients_menu(ctx: AdminContext, db, chat_id, text=None, parse_mode=None):
    send_admin_response(
        ctx,
        db,
        chat_id,
        text or messages.admin_clients_intro_text(),
        keyboards.admin_clients_keyboard(),
        parse_mode=parse_mode,
    )


def send_admin_payments_menu(ctx: AdminContext, db, chat_id, text=None):
    send_admin_response(
        ctx,
        db,
        chat_id,
        text or messages.admin_payments_intro_text(),
        keyboards.admin_payments_keyboard(),
    )


def send_admin_backups_menu(ctx: AdminContext, db, chat_id, text=None):
    send_admin_response(
        ctx,
        db,
        chat_id,
        text or messages.admin_backups_intro_text(),
        keyboards.admin_backups_keyboard(),
    )


def send_admin_activity_menu(ctx: AdminContext, db, chat_id, text=None):
    send_admin_response(
        ctx,
        db,
        chat_id,
        text or messages.admin_activity_intro_text(),
        keyboards.admin_activity_keyboard(),
    )


def send_admin_settings_menu(ctx: AdminContext, db, chat_id, text=None):
    send_admin_response(
        ctx,
        db,
        chat_id,
        text or messages.admin_settings_intro_text(),
        keyboards.admin_settings_keyboard(),
    )


def send_admin_server_settings_menu(ctx: AdminContext, db, chat_id, text=None):
    send_admin_response(
        ctx,
        db,
        chat_id,
        text or "Xray VPS Manager: настройки сервера",
        keyboards.admin_server_settings_keyboard(),
    )


def server_settings_state(db):
    admin_state = db.setdefault("adminState", {})
    state = admin_state.setdefault("serverSettings", {})
    if not isinstance(state, dict):
        state = {}
        admin_state["serverSettings"] = state
    return state


def set_server_tls_sites(ctx: AdminContext, db, chat_id, sites):
    state = server_settings_state(db)
    state[str(chat_id)] = {"tlsSites": list(sites), "updatedAt": admin_utc_stamp(ctx)}
    ctx.save_db_sections(db, ("adminState",))


def selected_tls_site(db, chat_id, index_value) -> dict[str, Any]:
    try:
        index = int(index_value)
    except (TypeError, ValueError):
        return {}
    state = server_settings_state(db)
    entry = state.get(str(chat_id), {})
    sites = entry.get("tlsSites", []) if isinstance(entry, dict) else []
    if index < 0 or index >= len(sites):
        return {}
    item = sites[index]
    return item if isinstance(item, dict) else {}


def server_tls_summary_text(sites):
    lines = ["Xray VPS Manager: TLS", ""]
    if not sites:
        lines.append("TLS site configs не найдены.")
        lines.append("Создай TLS/XHTTP-подключение и Caddy site config через SSH-меню, затем вернись сюда.")
        return "\n".join(lines)
    lines.append("Текущее шифрование:")
    for item in sites:
        domain = str(item.get("domain") or "-")
        label = str(item.get("tlsLabel") or "-")
        modified_at = str(item.get("modifiedAt") or "-")
        lines.append(f"- {domain}: {label}")
        lines.append(f"  Изменено: {modified_at}")
    lines.extend(["", "Выбери site config, чтобы сменить TLS."])
    return "\n".join(lines)


def server_tls_site_text(site, prefix=""):
    domain = str(site.get("domain") or "-")
    label = str(site.get("tlsLabel") or "-")
    modified_at = str(site.get("modifiedAt") or "-")
    local_port = site.get("localPort") or "-"
    upstream = f"127.0.0.1:{local_port}" if local_port != "-" else "-"
    lines = []
    if prefix:
        lines.extend([prefix, ""])
    lines.extend(
        [
            f"TLS: {domain}",
            "",
            f"Текущее шифрование: {label}",
            f"Изменено: {modified_at}",
            f"Upstream: {upstream}",
            "",
            "Выбери новый TLS-профиль.",
        ]
    )
    return "\n".join(lines)


def send_admin_server_tls_menu(ctx: AdminContext, db, chat_id, text=None):
    sites = ctx.list_tls_sites()
    set_server_tls_sites(ctx, db, chat_id, sites)
    send_admin_response(
        ctx,
        db,
        chat_id,
        text or server_tls_summary_text(sites),
        keyboards.admin_server_tls_sites_keyboard(sites),
    )


def send_admin_server_tls_site_menu(ctx: AdminContext, db, chat_id, index_value, text=None):
    site = selected_tls_site(db, chat_id, index_value)
    if not site:
        send_admin_server_tls_menu(ctx, db, chat_id, "Список TLS site configs устарел. Открой TLS заново.")
        return True
    send_admin_response(
        ctx,
        db,
        chat_id,
        text or server_tls_site_text(site),
        keyboards.admin_server_tls_site_keyboard(index_value, site.get("tlsChoice", "")),
    )
    return True


def handle_server_tls_set(ctx: AdminContext, db, chat_id, index_value, choice_key):
    site = selected_tls_site(db, chat_id, index_value)
    if not site:
        send_admin_server_tls_menu(ctx, db, chat_id, "Список TLS site configs устарел. Открой TLS заново.")
        return True
    try:
        choice = caddy.tls_version_choice(choice_key)
    except ValueError:
        send_admin_server_tls_site_menu(ctx, db, chat_id, index_value, "Неизвестный TLS-профиль.")
        return True
    if site.get("tlsChoice") == choice.key:
        send_admin_server_tls_site_menu(ctx, db, chat_id, index_value, f"Этот TLS-профиль уже выбран: {choice.label}.")
        return True
    try:
        local_port = int(site.get("localPort") or 0)
    except (TypeError, ValueError):
        local_port = 0
    site_path = str(site.get("path") or "")
    if local_port <= 0 and not site_path:
        send_admin_server_tls_site_menu(ctx, db, chat_id, index_value, "Не удалось определить upstream local port для site config.")
        return True
    try:
        ctx.set_tls_site_version(str(site.get("domain") or ""), local_port, choice.key, site_path or None)
    except Exception as exc:
        text = messages.truncate_telegram_text(
            f"Не удалось изменить TLS для {site.get('domain') or '-'}.\n\n"
            f"Caddy config был откатан, если запись уже успела начаться.\n\n{exc}"
        )
        send_admin_server_tls_site_menu(ctx, db, chat_id, index_value, text)
        return True
    send_admin_server_tls_menu(
        ctx,
        db,
        chat_id,
        f"TLS обновлён для {site.get('domain')}: {choice.label}.\n\nCaddy config проверен и применён.",
    )
    return True


def admin_client_names(ctx: AdminContext) -> list[str]:
    clients = subscriptions.client_db_clients(ctx.load_client_db())
    return sorted(str(name) for name, entry in clients.items() if isinstance(entry, dict))


def admin_connection_options(ctx: AdminContext) -> list[dict[str, Any]]:
    connections = subscriptions.client_db_connections(ctx.load_client_db())
    rows = []
    for tag, entry in connections.items():
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "tag": str(tag),
                "name": str(entry.get("name") or tag),
                "port": entry.get("port", ""),
            }
        )
    return sorted(rows, key=lambda item: (item["name"], item["tag"]))


def validate_new_client_name(value: str) -> str:
    name = str(value or "").strip()
    if not CLIENT_NAME_RE.fullmatch(name):
        raise ValueError("Имя клиента должно быть 1-64 символа: A-Z a-z 0-9 _ . @ -")
    return name


def parse_new_client_input(text: str) -> tuple[str, int | None, str]:
    parts = str(text or "").strip().split(maxsplit=1)
    if not parts:
        raise ValueError("Отправь имя клиента. Например: ivan 30")
    name = validate_new_client_name(parts[0])
    days_raw = parts[1].strip() if len(parts) > 1 else "0"
    try:
        access_days = client_access.parse_access_days(days_raw)
    except ValueError as exc:
        raise ValueError("Срок доступа должен быть числом дней. 0 или пустой срок означает бессрочно.") from exc
    return name, access_days, str(access_days if access_days is not None else 0)


def send_admin_client_extend_list(ctx: AdminContext, db, chat_id):
    names = admin_client_names(ctx)
    if not names:
        clear_client_flow_state_if_pending(ctx, db, chat_id)
        send_admin_clients_menu(ctx, db, chat_id, "Клиентов пока нет.")
        return
    set_client_extend_selection(ctx, db, chat_id, names)
    send_admin_response(
        ctx,
        db,
        chat_id,
        "Выбери клиента, которому нужно продлить подписку.",
        keyboards.admin_client_extend_keyboard(names),
    )


def send_admin_client_link_list(ctx: AdminContext, db, chat_id):
    names = admin_client_names(ctx)
    if not names:
        clear_client_flow_state_if_pending(ctx, db, chat_id)
        send_admin_clients_menu(ctx, db, chat_id, "Клиентов пока нет.")
        return
    set_client_link_selection(ctx, db, chat_id, names)
    send_admin_response(
        ctx,
        db,
        chat_id,
        "Выбери клиента, для которого нужно получить актуальную VLESS-ссылку.",
        keyboards.admin_client_link_keyboard(names),
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
        activity = "активность вкл" if subscriptions.activity_notifications_enabled(subscription) else "активность выкл"
        lines.append(f"- {name}: {valid}, до {access_until}, {activity}, чат {subscription.get('chatLabel', chat_id)}")
    if len(user_subscriptions) > 25:
        lines.append(f"...и ещё подписок: {len(user_subscriptions) - 25}")
    return "\n".join(lines)


def payment_total_text(ctx: AdminContext, db):
    client_db = ctx.load_client_db()
    summary = payments.payment_summary(db, client_db)
    return "\n".join(
        [
            "Xray VPS Manager: текущая сумма",
            "",
            f"Месячная аренда сервера: {summary['serverMonthly']}",
            f"Годовая аренда домена: {summary['domainAnnual']}",
            f"Аренда домена в месяц: {summary['domainMonthly']}",
            f"Общая месячная аренда: {summary['total']}",
            f"Платных клиентов: {payments.paid_client_count(client_db)}",
            f"Реквизиты: {payments.payment_transfer_label(db)}",
        ]
    )


def payment_share_text(ctx: AdminContext, db):
    client_db = ctx.load_client_db()
    return "\n".join(
        [
            "Xray VPS Manager: сумма на клиента",
            "",
            f"Платных клиентов: {payments.paid_client_count(client_db)}",
            f"Сумма на клиента: {payments.payment_amount_label(db, client_db)}",
            f"Округление: {payments.payment_rounding_label(db)}",
        ]
    )


def payment_rounding_text(db):
    return "\n".join(
        [
            "Xray VPS Manager: округление оплаты",
            "",
            f"Округление: {payments.payment_rounding_label(db)}",
        ]
    )


def settings_status_text(ctx: AdminContext, db):
    chat_label = str(db.get("chatLabel") or "").strip()
    chat_id = str(db.get("chatId") or "").strip()
    owner = chat_label or chat_id or "не настроен"
    return "\n".join(
        [
            "Xray VPS Manager: настройки бота",
            "",
            f"Имя бота: {ctx.bot_name(db)}",
            f"Username бота: {'@' + db.get('botUsername', '') if db.get('botUsername') else 'не указан'}",
            f"Уведомления: {'включены' if db.get('enabled') else 'отключены'}",
            f"Owner chat: {owner}",
            f"Маршрут Telegram: {db.get('routeMode', 'direct')}",
        ]
    )


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


def news_notice_message(ctx: AdminContext, db, text):
    return messages.news_notice_message(db, text, ctx.bot_name)


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
    elif kind == "news":
        draft = str(db.get("adminState", {}).get("newsNoticeText") or "").strip()
        if not draft:
            send_admin_notices_menu(ctx, db, chat_id, "Черновик новости пуст. Нажми «Новости» и отправь текст заново.")
            return
        message = news_notice_message(ctx, db, draft)
        title = "Новости"
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
    send_admin_response(ctx, db, chat_id, text, keyboards.admin_notice_confirm_keyboard(kind))


def set_custom_notice_waiting(ctx: AdminContext, db, chat_id):
    db.setdefault("adminState", {})[str(chat_id)] = {"action": "custom-notice-text", "startedAt": admin_utc_stamp(ctx)}
    ctx.save_db_sections(db, ("adminState",))


def set_news_notice_waiting(ctx: AdminContext, db, chat_id):
    db.setdefault("adminState", {})[str(chat_id)] = {"action": "news-notice-text", "startedAt": admin_utc_stamp(ctx)}
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


def set_client_link_selection(ctx: AdminContext, db, chat_id, names):
    db.setdefault("adminState", {})[str(chat_id)] = {
        "action": "client-link-select",
        "clients": list(names),
        "startedAt": admin_utc_stamp(ctx),
    }
    ctx.save_db_sections(db, ("adminState",))


def set_client_add_input_waiting(ctx: AdminContext, db, chat_id):
    db.setdefault("adminState", {})[str(chat_id)] = {
        "action": "add-client-input",
        "startedAt": admin_utc_stamp(ctx),
    }
    ctx.save_db_sections(db, ("adminState",))


def set_client_add_payment_waiting(ctx: AdminContext, db, chat_id, name, access_days_arg):
    db.setdefault("adminState", {})[str(chat_id)] = {
        "action": "add-client-payment",
        "client": name,
        "accessDays": str(access_days_arg),
        "startedAt": admin_utc_stamp(ctx),
    }
    ctx.save_db_sections(db, ("adminState",))


def set_client_add_connection_selection(ctx: AdminContext, db, chat_id, pending, connections):
    db.setdefault("adminState", {})[str(chat_id)] = {
        "action": "add-client-connection",
        "client": pending.get("client", ""),
        "accessDays": str(pending.get("accessDays", "0")),
        "paymentType": pending.get("paymentType", "free"),
        "connections": list(connections),
        "startedAt": admin_utc_stamp(ctx),
    }
    ctx.save_db_sections(db, ("adminState",))


def clear_admin_state(ctx: AdminContext, db, chat_id, clear_custom_notice=True):
    state = db.setdefault("adminState", {})
    state.pop(str(chat_id), None)
    if clear_custom_notice:
        state.pop("customNoticeText", None)
        state.pop("newsNoticeText", None)
    ctx.save_db_sections(db, ("adminState",))


def clear_client_extend_state_if_pending(ctx: AdminContext, db, chat_id):
    clear_client_flow_state_if_pending(ctx, db, chat_id)


def clear_client_flow_state_if_pending(ctx: AdminContext, db, chat_id):
    pending = db.get("adminState", {}).get(str(chat_id), {})
    if pending.get("action") in (
        "client-link-select",
        "extend-subscription-select",
        "extend-subscription-days",
        "add-client-input",
        "add-client-payment",
        "add-client-connection",
    ):
        clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)


def send_admin_notice(ctx: AdminContext, db, chat_id, kind):
    if kind == "custom":
        message = str(db.get("adminState", {}).get("customNoticeText") or "").strip()
        if not message:
            send_admin_notices_menu(ctx, db, chat_id, "Черновик своего сообщения пуст. Отправка отменена.")
            return
        label = "своё сообщение"
    elif kind == "news":
        draft = str(db.get("adminState", {}).get("newsNoticeText") or "").strip()
        if not draft:
            send_admin_notices_menu(ctx, db, chat_id, "Черновик новости пуст. Отправка отменена.")
            return
        message = news_notice_message(ctx, db, draft)
        label = "Новости"
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


def handle_news_notice_text(ctx: AdminContext, db, chat_id, text):
    pending = db.get("adminState", {}).get(str(chat_id), {})
    if pending.get("action") != "news-notice-text":
        return False
    if text.lower() in ("/cancel", "cancel", "отмена"):
        clear_admin_state(ctx, db, chat_id)
        send_admin_notices_menu(ctx, db, chat_id, "Создание новости отменено.")
        return True
    db.setdefault("adminState", {})["newsNoticeText"] = text
    db["adminState"].pop(str(chat_id), None)
    ctx.save_db_sections(db, ("adminState",))
    preview_notice(ctx, db, chat_id, "news")
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


def selected_client_link_name(db, chat_id, index_value):
    try:
        index = int(index_value)
    except (TypeError, ValueError):
        return ""
    pending = db.get("adminState", {}).get(str(chat_id), {})
    names = pending.get("clients", []) if pending.get("action") == "client-link-select" else []
    if index < 0 or index >= len(names):
        return ""
    return str(names[index])


def selected_connection(db, chat_id, index_value) -> dict[str, Any]:
    try:
        index = int(index_value)
    except (TypeError, ValueError):
        return {}
    pending = db.get("adminState", {}).get(str(chat_id), {})
    connections = pending.get("connections", []) if pending.get("action") == "add-client-connection" else []
    if index < 0 or index >= len(connections):
        return {}
    item = connections[index]
    return item if isinstance(item, dict) else {}


def set_add_client_input_waiting(ctx: AdminContext, db, chat_id):
    set_client_add_input_waiting(ctx, db, chat_id)
    send_admin_response(
        ctx,
        db,
        chat_id,
        "Отправь имя клиента и срок доступа одним сообщением.\n\n"
        "Примеры:\n"
        "ivan 30\n"
        "ivan 0\n\n"
        "Если срок не указать или указать 0, доступ будет бессрочным.\n"
        "Если передумаешь, отправь /cancel.",
        keyboards.admin_client_add_cancel_keyboard(),
    )


def set_extend_subscription_waiting(ctx: AdminContext, db, chat_id, name):
    set_client_extend_waiting(ctx, db, chat_id, name)
    send_admin_response(
        ctx,
        db,
        chat_id,
        f"Отправь числом, на сколько дней продлить подписку для {name}.\n\n"
        "Например: 30\n"
        "Если передумаешь, отправь /cancel.",
        keyboards.admin_client_extend_cancel_keyboard(),
    )


def command_output(result):
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    return (stdout + (("\n" + stderr) if stderr else "")).strip()


def first_vless_link(output):
    for line in str(output or "").splitlines():
        value = line.strip()
        if value.startswith("vless://"):
            return value
    return ""


def add_client_command(ctx: AdminContext, pending, connection_tag=""):
    command = [
        str(ctx.xray_client),
        "add",
        str(pending.get("client") or ""),
        str(pending.get("accessDays") or "0"),
    ]
    if connection_tag:
        command.extend(["--connection", str(connection_tag)])
    command.extend(["--payment", str(pending.get("paymentType") or "free")])
    return command


def add_client_success_text(ctx: AdminContext, db, name, payment_type, result):
    link = first_vless_link(command_output(result))
    if not link:
        link_result = ctx.run_capture([str(ctx.xray_client), "link", name], timeout=20)
        if getattr(link_result, "returncode", 1) == 0:
            link = first_vless_link(command_output(link_result))
    if not link:
        return "Клиент добавлен, но xray-client не вернул VLESS-ссылку. Выведи её через SSH: xray-client link " + name

    try:
        client_db = ctx.load_client_db()
        clients = subscriptions.client_db_clients(client_db)
    except Exception:
        client_db = {"clients": {}}
        clients = {}
    entry = clients.get(name, {})
    access_until = ctx.format_access_until(entry.get("expiresAt", "") if isinstance(entry, dict) else "")
    amount_label = payments.payment_amount_label(db, client_db)
    return messages.build_client_added_message(db, link, access_until, payment_type, amount_label, ctx.bot_name)


def add_client_result_message(ctx: AdminContext, db, pending, result):
    name = str(pending.get("client") or "")
    if getattr(result, "returncode", 1) != 0:
        output = command_output(result)
        details = f"\n\n{output}" if output else ""
        return messages.truncate_telegram_text(f"Не удалось добавить клиента {name}, exit {getattr(result, 'returncode', 1)}.{details}"), None
    return add_client_success_text(ctx, db, name, pending.get("paymentType", "free"), result), "HTML"


def run_add_client_from_pending(ctx: AdminContext, db, chat_id, pending, connection_tag=""):
    result = ctx.run_capture(add_client_command(ctx, pending, connection_tag), timeout=120)
    clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
    text, parse_mode = add_client_result_message(ctx, db, pending, result)
    send_admin_clients_menu(ctx, db, chat_id, text, parse_mode=parse_mode)
    return True


def add_client_payment_text(name):
    return "\n".join(
        [
            "Выбери статус оплаты для нового клиента.",
            "",
            f"Клиент: {name}",
            "Платный клиент участвует в расчёте суммы и получает напоминания об оплате.",
        ]
    )


def handle_add_client_input(ctx: AdminContext, db, chat_id, text):
    pending = db.get("adminState", {}).get(str(chat_id), {})
    if pending.get("action") != "add-client-input":
        return False
    if text.lower() in ("/cancel", "cancel", "отмена"):
        clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
        send_admin_clients_menu(ctx, db, chat_id, "Добавление клиента отменено.")
        return True

    try:
        name, _access_days, access_days_arg = parse_new_client_input(text)
    except ValueError as exc:
        send_admin_response(
            ctx,
            db,
            chat_id,
            str(exc) + "\n\nНапример: ivan 30\nЕсли передумаешь, отправь /cancel.",
            keyboards.admin_client_add_cancel_keyboard(),
        )
        return True

    if name in admin_client_names(ctx):
        send_admin_response(
            ctx,
            db,
            chat_id,
            f"Клиент {name} уже существует. Отправь другое имя или /cancel.",
            keyboards.admin_client_add_cancel_keyboard(),
        )
        return True

    set_client_add_payment_waiting(ctx, db, chat_id, name, access_days_arg)
    send_admin_response(ctx, db, chat_id, add_client_payment_text(name), keyboards.admin_client_add_payment_keyboard())
    return True


def handle_add_client_payment(ctx: AdminContext, db, chat_id, payment_type):
    if payment_type not in ("paid", "free"):
        send_admin_clients_menu(ctx, db, chat_id, "Неизвестный статус оплаты.")
        return True
    pending = db.get("adminState", {}).get(str(chat_id), {})
    if pending.get("action") != "add-client-payment":
        send_admin_clients_menu(ctx, db, chat_id, "Добавление клиента не активно. Начни заново.")
        return True
    name = str(pending.get("client") or "")
    if not name:
        clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
        send_admin_clients_menu(ctx, db, chat_id, "Не удалось прочитать имя клиента. Начни заново.")
        return True
    if name in admin_client_names(ctx):
        clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
        send_admin_clients_menu(ctx, db, chat_id, "Клиент уже существует. Добавление отменено.")
        return True

    pending = {
        "client": name,
        "accessDays": str(pending.get("accessDays") or "0"),
        "paymentType": payment_type,
    }
    connections = admin_connection_options(ctx)
    if len(connections) > 1:
        set_client_add_connection_selection(ctx, db, chat_id, pending, connections)
        send_admin_response(
            ctx,
            db,
            chat_id,
            "Выбери VLESS-подключение для нового клиента.",
            keyboards.admin_client_add_connection_keyboard(connections),
        )
        return True
    connection_tag = connections[0]["tag"] if len(connections) == 1 else ""
    return run_add_client_from_pending(ctx, db, chat_id, pending, connection_tag)


def handle_add_client_connection(ctx: AdminContext, db, chat_id, index_value):
    pending = db.get("adminState", {}).get(str(chat_id), {})
    if pending.get("action") != "add-client-connection":
        send_admin_clients_menu(ctx, db, chat_id, "Выбор подключения не активен. Начни добавление заново.")
        return True
    item = selected_connection(db, chat_id, index_value)
    tag = str(item.get("tag") or "")
    if not tag:
        send_admin_clients_menu(ctx, db, chat_id, "Подключение не найдено. Начни добавление заново.")
        return True
    return run_add_client_from_pending(ctx, db, chat_id, pending, tag)


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


def client_link_text(ctx: AdminContext, name, result):
    if getattr(result, "returncode", 1) != 0:
        output = command_output(result)
        details = f"\n\n{output}" if output else ""
        return messages.truncate_telegram_text(f"Не удалось получить VLESS-ссылку для {name}, exit {getattr(result, 'returncode', 1)}.{details}"), None

    link = first_vless_link(command_output(result))
    if not link:
        return "Не удалось получить VLESS-ссылку: xray-client не вернул ссылку.", None

    link = subscriptions.neutral_vless_fragment(link, ctx.server_name_fragment())
    return (
        "\n".join(
            [
                "Можно переслать это сообщение пользователю:",
                "",
                "Актуальная VLESS-ссылка:",
                "",
                f"<pre><code>{subscriptions.telegram_html_escape(link)}</code></pre>",
                "",
                "Если настройки подключения менялись, импортируй эту ссылку заново.",
            ]
        ),
        "HTML",
    )


def handle_client_link_selection(ctx: AdminContext, db, chat_id, index_value):
    name = selected_client_link_name(db, chat_id, index_value)
    if not name:
        send_admin_clients_menu(ctx, db, chat_id, "Клиент не найден. Открой список заново.")
        return True
    if name not in admin_client_names(ctx):
        clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
        send_admin_clients_menu(ctx, db, chat_id, "Клиент больше не найден. Выбери клиента заново.")
        return True
    result = ctx.run_capture([str(ctx.xray_client), "link", name], timeout=20)
    clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
    text, parse_mode = client_link_text(ctx, name, result)
    send_admin_clients_menu(ctx, db, chat_id, text, parse_mode=parse_mode)
    return True


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
        send_admin_response(
            ctx,
            db,
            chat_id,
            "Нужно отправить положительное целое число дней. Например: 30\n\n"
            "Если передумаешь, отправь /cancel.",
            keyboards.admin_client_extend_cancel_keyboard(),
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
    if action == "news-notice-text":
        return handle_news_notice_text(ctx, db, chat_id, text)
    if action == "add-client-input":
        return handle_add_client_input(ctx, db, chat_id, text)
    if action == "extend-subscription-days":
        return handle_extend_subscription_days(ctx, db, chat_id, text)
    return False


def handle_callback(ctx: AdminContext, db, chat_id, data):
    if data == "admin:menu":
        send_admin_menu(ctx, db, chat_id)
        return True
    if data == "admin:status-menu":
        send_admin_status_menu(ctx, db, chat_id)
        return True
    if data == "admin:status":
        send_admin_status_menu(ctx, db, chat_id, status_text(ctx, db))
        return True
    if data == "admin:settings-status":
        send_admin_settings_menu(ctx, db, chat_id, settings_status_text(ctx, db))
        return True
    if data == "admin:subscribers":
        send_admin_clients_menu(ctx, db, chat_id, subscribers_text(ctx, db))
        return True
    if data == "admin:clients":
        clear_client_flow_state_if_pending(ctx, db, chat_id)
        send_admin_clients_menu(ctx, db, chat_id)
        return True
    if data == "admin:payments":
        send_admin_payments_menu(ctx, db, chat_id)
        return True
    if data == "admin:payment-total":
        send_admin_payments_menu(ctx, db, chat_id, payment_total_text(ctx, db))
        return True
    if data == "admin:payment-share":
        send_admin_payments_menu(ctx, db, chat_id, payment_share_text(ctx, db))
        return True
    if data == "admin:payment-rounding":
        send_admin_payments_menu(ctx, db, chat_id, payment_rounding_text(db))
        return True
    if data == "admin:backups":
        send_admin_backups_menu(ctx, db, chat_id)
        return True
    if data == "admin:activity":
        send_admin_activity_menu(ctx, db, chat_id)
        return True
    if data == "admin:settings":
        send_admin_settings_menu(ctx, db, chat_id)
        return True
    if data == "admin:server-settings":
        send_admin_server_settings_menu(ctx, db, chat_id)
        return True
    if data == "admin:server-tls":
        send_admin_server_tls_menu(ctx, db, chat_id)
        return True
    if data.startswith("admin:server-tls-site:"):
        return send_admin_server_tls_site_menu(ctx, db, chat_id, data.rsplit(":", 1)[1])
    if data.startswith("admin:server-tls-set:"):
        parts = data.split(":")
        if len(parts) != 4:
            send_admin_server_tls_menu(ctx, db, chat_id, "Не удалось прочитать выбранный TLS-профиль.")
            return True
        return handle_server_tls_set(ctx, db, chat_id, parts[2], parts[3])
    if data == "admin:client-add":
        set_add_client_input_waiting(ctx, db, chat_id)
        return True
    if data.startswith("admin:client-add-payment:"):
        return handle_add_client_payment(ctx, db, chat_id, data.rsplit(":", 1)[1])
    if data.startswith("admin:client-add-connection:"):
        return handle_add_client_connection(ctx, db, chat_id, data.rsplit(":", 1)[1])
    if data == "admin:client-add-cancel":
        clear_admin_state(ctx, db, chat_id, clear_custom_notice=False)
        send_admin_clients_menu(ctx, db, chat_id, "Добавление клиента отменено.")
        return True
    if data == "admin:client-link":
        send_admin_client_link_list(ctx, db, chat_id)
        return True
    if data.startswith("admin:client-link:"):
        return handle_client_link_selection(ctx, db, chat_id, data.rsplit(":", 1)[1])
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
        send_admin_status_menu(ctx, db, chat_id, notifications.build_daily_summary_message(ctx.notification_context))
        return True
    if data == "admin:geoip":
        rc = notifications.notify_geoip(ctx.notification_context, quiet=True)
        text = "GeoIP-проверка выполнена. Если были новые события, бот отправил отдельное уведомление."
        if rc != 0:
            text = f"GeoIP-проверка завершилась с ошибкой, exit {rc}."
        send_admin_activity_menu(ctx, db, chat_id, text)
        return True
    if data == "admin:expiry":
        rc = notifications.notify_expiry(ctx.notification_context, quiet=True)
        text = "Проверка напоминаний выполнена."
        if rc != 0:
            text = f"Проверка напоминаний завершилась с ошибкой, exit {rc}."
        send_admin_status_menu(ctx, db, chat_id, text)
        return True
    if data == "admin:test":
        send_admin_status_menu(ctx, db, chat_id, run_server_test_text(ctx))
        return True
    if data == "admin:backup":
        send_admin_backups_menu(ctx, db, chat_id, create_backup_text(ctx))
        return True
    if data == "admin:notices":
        send_admin_notices_menu(ctx, db, chat_id)
        return True
    if data in ("admin:notice:start", "admin:notice:done"):
        preview_notice(ctx, db, chat_id, data.rsplit(":", 1)[1])
        return True
    if data == "admin:notice:news":
        set_news_notice_waiting(ctx, db, chat_id)
        send_admin_response(
            ctx,
            db,
            chat_id,
            "Отправь следующим сообщением текст новости для подписанных клиентов.\n\n"
            "Бот добавит заголовок объявления и покажет предпросмотр перед отправкой.\n\n"
            "Если передумаешь, отправь /cancel.",
            {"inline_keyboard": [[{"text": "Отмена", "callback_data": "admin:notice-cancel"}]]},
        )
        return True
    if data == "admin:notice:custom":
        set_custom_notice_waiting(ctx, db, chat_id)
        send_admin_response(
            ctx,
            db,
            chat_id,
            "Отправь следующим сообщением текст, который нужно разослать подписанным клиентам.\n\n"
            "Если передумаешь, отправь /cancel.",
            {"inline_keyboard": [[{"text": "Отмена", "callback_data": "admin:notice-cancel"}]]},
        )
        return True
    if data.startswith("admin:notice-send:"):
        kind = data.rsplit(":", 1)[1]
        if kind not in ("start", "done", "news", "custom"):
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
