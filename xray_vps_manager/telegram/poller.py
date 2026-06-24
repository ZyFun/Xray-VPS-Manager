"""Telegram user long-polling and client-facing actions."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xray_vps_manager.telegram import admin, bot_commands, keyboards, messages, subscriptions, traffic
from xray_vps_manager.xray import client_routes

USER_POLL_SHORT_TIMEOUT = 2
USER_POLL_LONG_TIMEOUT = 45
USER_POLLER_SLEEP_UNCONFIGURED = 30
USER_POLLER_SLEEP_ERROR = 5
CLIENT_CALLBACK_GUARDS_KEY = "callbackGuards"
CLIENT_CONSUMED_CALLBACK_LIMIT = 40
CLIENT_CALLBACK_STALE_TEXT = "Эта кнопка уже устарела. Используй последнее сообщение бота."
CLIENT_CALLBACK_CROSS_MENU_ACTIONS = ("client:menu",)
CLIENT_CALLBACK_CROSS_MENU_PREFIXES = ("client:activity-exception:",)


@dataclass(frozen=True)
class PollerContext:
    load_db: Callable[[], dict]
    save_db_sections: Callable[[dict, tuple[str, ...]], None]
    load_client_db: Callable[[], dict]
    load_traffic_db: Callable[[], dict]
    display_timezone: Callable[[], tuple[Any, str]]
    format_access_until: Callable[[str], str]
    run_capture: Callable[..., Any]
    send_chat_message: Callable[..., Any]
    answer_callback_query: Callable[..., Any]
    curl_json: Callable[..., dict]
    bot_name: Callable[[dict | None], str]
    server_name_fragment: Callable[[], str]
    utc_stamp: Callable[[], str]
    admin_context: admin.AdminContext
    xray_client: Path


def chat_label(chat):
    parts = []
    if chat.get("username"):
        parts.append("@" + str(chat["username"]))
    full_name = " ".join(str(chat.get(key, "")).strip() for key in ("first_name", "last_name")).strip()
    if full_name:
        parts.append(full_name)
    if not parts:
        parts.append(str(chat.get("id", "")))
    return " / ".join(parts)


def subscription_intro_text(ctx: PollerContext):
    db = ctx.load_db()
    return messages.subscription_intro_text(db, ctx.bot_name)


def client_callback_state(db):
    state = db.setdefault("clientSubscriptionState", {})
    if not isinstance(state, dict):
        state = {}
        db["clientSubscriptionState"] = state
    return state


def client_callback_guard(db, chat_id):
    state = client_callback_state(db)
    guards = state.setdefault(CLIENT_CALLBACK_GUARDS_KEY, {})
    if not isinstance(guards, dict):
        guards = {}
        state[CLIENT_CALLBACK_GUARDS_KEY] = guards
    chat_key = str(chat_id)
    guard = guards.setdefault(chat_key, {})
    if not isinstance(guard, dict):
        guard = {}
        guards[chat_key] = guard
    return guard


def client_consumed_message_ids(guard):
    consumed = []
    for value in guard.get("consumedMessageIds", []):
        message_id = admin.normalize_message_id(value)
        if message_id is not None:
            consumed.append(message_id)
    return consumed[-CLIENT_CONSUMED_CALLBACK_LIMIT:]


def register_client_message(ctx: PollerContext, db, chat_id, response):
    message_id = admin.response_message_id(response)
    if message_id is None:
        return
    guard = client_callback_guard(db, chat_id)
    guard["activeMessageId"] = message_id
    guard["consumedMessageIds"] = client_consumed_message_ids(guard)
    ctx.save_db_sections(db, ("clientSubscriptionState",))


def accept_client_callback(ctx: PollerContext, db, chat_id, data, message_id):
    message_id = admin.normalize_message_id(message_id)
    if message_id is None:
        return True

    guard = client_callback_guard(db, chat_id)
    active_message_id = admin.normalize_message_id(guard.get("activeMessageId"))
    consumed = client_consumed_message_ids(guard)
    if not is_client_cross_menu_action(data) and active_message_id is not None and message_id != active_message_id:
        return False
    if message_id in consumed:
        return False

    consumed.append(message_id)
    guard["consumedMessageIds"] = consumed[-CLIENT_CONSUMED_CALLBACK_LIMIT:]
    guard["activeMessageId"] = ""
    ctx.save_db_sections(db, ("clientSubscriptionState",))
    return True


def is_client_cross_menu_action(data):
    if data in CLIENT_CALLBACK_CROSS_MENU_ACTIONS:
        return True
    return any(str(data or "").startswith(prefix) for prefix in CLIENT_CALLBACK_CROSS_MENU_PREFIXES)


def send_client_menu(ctx: PollerContext, db, chat_id, text=None, parse_mode=None):
    response = ctx.send_chat_message(
        db,
        chat_id,
        text or subscription_intro_text(ctx),
        reply_markup=keyboards.client_keyboard_for_chat(db, chat_id),
        parse_mode=parse_mode,
    )
    register_client_message(ctx, db, chat_id, response)


def send_client_traffic_menu(ctx: PollerContext, db, chat_id, text=None, parse_mode=None):
    response = ctx.send_chat_message(
        db,
        chat_id,
        text or traffic.traffic_menu_text(),
        reply_markup=keyboards.client_traffic_keyboard(),
        parse_mode=parse_mode,
    )
    register_client_message(ctx, db, chat_id, response)


def send_client_activity_menu(ctx: PollerContext, db, chat_id, text=None):
    subscription = db.get("clientSubscriptions", {}).get(str(chat_id))
    enabled = subscriptions.activity_notifications_enabled(subscription)
    response = ctx.send_chat_message(
        db,
        chat_id,
        text
        or subscriptions.activity_notification_status_for_chat(
            db,
            chat_id,
            ctx.load_client_db(),
            owner_chat=keyboards.is_owner_chat(db, chat_id),
        ),
        reply_markup=keyboards.client_activity_keyboard(enabled),
    )
    register_client_message(ctx, db, chat_id, response)


def send_client_activity_exception_list(ctx: PollerContext, db, chat_id):
    items = subscriptions.activity_exception_candidates(db, chat_id)
    if not items:
        response = ctx.send_chat_message(
            db,
            chat_id,
            "Список целей для исключения пуст или устарел.\n\n"
            "Дождись следующего уведомления активности или вернись в меню активности.",
            reply_markup=keyboards.client_activity_exception_keyboard([]),
        )
        register_client_message(ctx, db, chat_id, response)
        return
    text = "\n".join(
        [
            "Что добавить в исключения?",
            "",
            "Выбери адрес, по которому больше не нужно присылать личные предупреждения.",
            "Это исключение действует только для твоего Telegram-чата и не меняет маршрут VPN.",
            "",
            "Если сервис должен идти мимо VPN, добавь этот домен или IP в правила split tunneling своего VPN-клиента: Direct, Bypass, Routing или Rules.",
        ]
    )
    response = ctx.send_chat_message(
        db,
        chat_id,
        text,
        reply_markup=keyboards.client_activity_exception_keyboard(items),
    )
    register_client_message(ctx, db, chat_id, response)


def send_client_activity_exceptions_menu(ctx: PollerContext, db, chat_id, text=None):
    items = subscriptions.activity_exceptions_for_chat(db, chat_id)
    if not items:
        message = text or (
            "Личных исключений активности пока нет.\n\n"
            "Их можно добавить из следующего небольшого предупреждения активности кнопкой "
            "`Добавить в исключения`."
        )
    else:
        labels = [f"{index}. {subscriptions.activity_target_label(item)}" for index, item in enumerate(items, start=1)]
        message = text or "\n".join(
            [
                "Личные исключения активности",
                "",
                "Выбери запись, которую нужно удалить.",
                "После удаления предупреждения по этой цели снова будут приходить, если событие повторится.",
                "",
                *labels,
            ]
        )
    response = ctx.send_chat_message(
        db,
        chat_id,
        message,
        reply_markup=keyboards.client_activity_exceptions_manage_keyboard(items),
        parse_mode=None,
    )
    register_client_message(ctx, db, chat_id, response)


def send_client_country_menu(ctx: PollerContext, db, chat_id, text=None):
    client_db = ctx.load_client_db()
    _name, entry, error = subscriptions.subscription_entry_for_chat(db, chat_id, client_db)
    if error:
        send_client_menu(ctx, db, chat_id, error)
        return
    options = client_routes.route_options(client_db)
    if not options:
        send_client_menu(ctx, db, chat_id, "Страны подключения ещё не настроены. Обратись к администратору.")
        return
    current_tag = client_routes.selected_route_tag(entry)
    current_label = client_routes.selected_route_label(client_db, entry)
    response = ctx.send_chat_message(
        db,
        chat_id,
        text or f"Текущая страна: {current_label}\n\nВыбери страну подключения.",
        reply_markup=keyboards.client_country_keyboard(options, current_tag),
    )
    register_client_message(ctx, db, chat_id, response)


def subscription_status_for_chat(ctx: PollerContext, db, chat_id):
    return subscriptions.subscription_status_for_chat(db, chat_id, ctx.load_client_db(), ctx.format_access_until)


def client_home_text(ctx: PollerContext, db, chat_id):
    if subscriptions.chat_has_subscription(db, chat_id):
        return subscription_status_for_chat(ctx, db, chat_id)
    return subscription_intro_text(ctx)


def client_help_text(ctx: PollerContext, db):
    return messages.client_help_text(db, ctx.bot_name)


def subscribe_prompt_for_chat(ctx: PollerContext, db, chat_id):
    client_db = ctx.load_client_db()
    _name, entry, error = subscriptions.subscription_entry_for_chat(db, chat_id, client_db)
    if entry and not error:
        return "Уведомления уже подключены.\n\n" + subscriptions.client_access_summary(entry, ctx.format_access_until, client_db)
    if subscriptions.chat_has_subscription(db, chat_id) and error:
        return error + "\n\n" + messages.subscribe_prompt_text()
    return messages.subscribe_prompt_text()


def current_vless_link_code_for_chat(ctx: PollerContext, db, chat_id):
    return subscriptions.current_link_code_for_chat(
        db,
        chat_id,
        ctx.load_client_db(),
        ctx.xray_client,
        ctx.run_capture,
        ctx.server_name_fragment(),
    )


def client_link_selection_state(db):
    state = client_callback_state(db)
    selections = state.setdefault("linkCredentialSelections", {})
    if not isinstance(selections, dict):
        selections = {}
        state["linkCredentialSelections"] = selections
    return selections


def set_client_link_selection(ctx: PollerContext, db, chat_id, name, options):
    selections = client_link_selection_state(db)
    selections[str(chat_id)] = {
        "client": str(name or ""),
        "options": list(options),
        "updatedAt": ctx.utc_stamp(),
    }
    ctx.save_db_sections(db, ("clientSubscriptionState",))


def clear_client_link_selection(ctx: PollerContext, db, chat_id):
    selections = client_link_selection_state(db)
    if selections.pop(str(chat_id), None) is not None:
        ctx.save_db_sections(db, ("clientSubscriptionState",))


def selected_client_link_option(db, chat_id, index_value):
    try:
        index = int(index_value)
    except (TypeError, ValueError):
        return {}
    entry = client_link_selection_state(db).get(str(chat_id), {})
    options = entry.get("options", []) if isinstance(entry, dict) else []
    if index < 0 or index >= len(options):
        return {}
    item = options[index]
    return item if isinstance(item, dict) else {}


def send_current_link_for_option(ctx: PollerContext, db, chat_id, option):
    connection_tag = str(option.get("connection") or "")
    text, parse_mode = subscriptions.current_link_code_for_chat(
        db,
        chat_id,
        ctx.load_client_db(),
        ctx.xray_client,
        ctx.run_capture,
        ctx.server_name_fragment(),
        connection_tag=connection_tag,
    )
    clear_client_link_selection(ctx, db, chat_id)
    send_client_menu(ctx, db, chat_id, text, parse_mode=parse_mode)


def send_client_link_for_chat(ctx: PollerContext, db, chat_id):
    client_db = ctx.load_client_db()
    name, entry, error = subscriptions.subscription_entry_for_chat(db, chat_id, client_db)
    if error:
        send_client_menu(ctx, db, chat_id, error)
        return True
    options = subscriptions.credential_options_for_entry(client_db, entry)
    if not options:
        send_client_menu(ctx, db, chat_id, "Для этого клиента нет активных подключений. Обратись к администратору.")
        return True
    if len(options) == 1:
        send_current_link_for_option(ctx, db, chat_id, options[0])
        return True
    set_client_link_selection(ctx, db, chat_id, name, options)
    response = ctx.send_chat_message(
        db,
        chat_id,
        "Выбери подключение, для которого нужна VPN-ссылка.",
        reply_markup=keyboards.client_link_credential_keyboard(options),
    )
    register_client_message(ctx, db, chat_id, response)
    return True


def traffic_report_for_chat(ctx: PollerContext, db, chat_id, kind):
    return traffic.traffic_report_for_chat(
        db,
        chat_id,
        ctx.load_client_db(),
        ctx.load_traffic_db(),
        ctx.display_timezone,
        kind,
    )


def subscribe_chat_to_client(ctx: PollerContext, db, chat, text):
    return subscriptions.subscribe_chat_to_client(
        db,
        chat,
        text,
        ctx.load_client_db(),
        chat_label(chat),
        ctx.utc_stamp(),
    )


def try_set_subscribed_commands(ctx: PollerContext, db, chat_id):
    try:
        bot_commands.set_subscribed_chat_commands(db, chat_id, curl_json=ctx.curl_json)
    except Exception as exc:
        print(f"WARN: failed to update Telegram command menu for {chat_id}: {exc}", file=sys.stderr)


def try_delete_chat_commands(ctx: PollerContext, db, chat_id):
    try:
        bot_commands.delete_chat_commands(db, chat_id, curl_json=ctx.curl_json)
    except Exception as exc:
        print(f"WARN: failed to reset Telegram command menu for {chat_id}: {exc}", file=sys.stderr)


def handle_user_message(ctx: PollerContext, db, update):
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or "id" not in chat:
        return False
    chat_id = str(chat["id"])
    text = str(message.get("text") or "").strip()
    if not text:
        send_client_menu(ctx, db, chat_id, client_home_text(ctx, db, chat_id))
        return True
    if keyboards.is_owner_chat(db, chat_id) and admin.handle_pending_text(ctx.admin_context, db, chat_id, text):
        return True
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command == "/admin":
        if keyboards.is_owner_chat(db, chat_id):
            admin.send_admin_menu(ctx.admin_context, db, chat_id)
        else:
            send_client_menu(ctx, db, chat_id)
        return True
    if command == "/start":
        send_client_menu(ctx, db, chat_id, client_home_text(ctx, db, chat_id))
        return True
    if command == "/help":
        send_client_menu(ctx, db, chat_id, client_help_text(ctx, db))
        return True
    if command == "/status":
        send_client_menu(ctx, db, chat_id, subscription_status_for_chat(ctx, db, chat_id))
        return True
    if command == "/link":
        return send_client_link_for_chat(ctx, db, chat_id)
    if command == "/traffic":
        send_client_traffic_menu(ctx, db, chat_id)
        return True
    if command in ("/unsubscribe", "/stop"):
        text = subscriptions.unsubscribe_chat(db, chat_id)
        try_delete_chat_commands(ctx, db, chat_id)
        send_client_menu(ctx, db, chat_id, text)
        return True
    if subscriptions.find_client_key(text):
        try:
            _name, entry = subscribe_chat_to_client(ctx, db, chat, text)
        except ValueError as exc:
            send_client_menu(ctx, db, chat_id, str(exc))
            return True
        try_set_subscribed_commands(ctx, db, chat_id)
        send_client_menu(
            ctx,
            db,
            chat_id,
            "Подписка подключена.\n\n"
            + subscriptions.client_access_summary(entry, ctx.format_access_until, ctx.load_client_db())
            + "\n\nНапоминания придут в 08:00 за 5 дней и за 1 день до окончания доступа.",
        )
        return True
    if subscriptions.find_protocol_link(text):
        send_client_menu(
            ctx,
            db,
            chat_id,
            "Протокольные ссылки больше не используются для привязки бота.\n\n"
            f"Отправь ключ доступа формата {subscriptions.ACCESS_KEY_PLACEHOLDER}.",
        )
        return True
    if subscriptions.chat_has_subscription(db, chat_id):
        send_client_menu(ctx, db, chat_id, client_home_text(ctx, db, chat_id))
    else:
        send_client_menu(
            ctx,
            db,
            chat_id,
            f"Я не нашёл ключ доступа. Отправь ключ формата {subscriptions.ACCESS_KEY_PLACEHOLDER} или нажми кнопку ниже.",
        )
    return True


def handle_callback_query(ctx: PollerContext, db, update):
    callback = update.get("callback_query") or {}
    callback_id = callback.get("id", "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or "id" not in chat:
        ctx.answer_callback_query(db, callback_id)
        return False
    chat_id = str(chat["id"])
    data = str(callback.get("data") or "")

    if data.startswith("admin:"):
        if not keyboards.is_owner_chat(db, chat_id):
            ctx.answer_callback_query(db, callback_id, "Админ-панель доступна только владельцу.", show_alert=True)
            send_client_menu(ctx, db, chat_id)
            return True
        message_id = admin.callback_message_id(message)
        if not admin.accept_admin_callback(ctx.admin_context, db, chat_id, data, message_id):
            ctx.answer_callback_query(db, callback_id, CLIENT_CALLBACK_STALE_TEXT)
            return True
        ctx.answer_callback_query(db, callback_id)
        return admin.handle_callback(ctx.admin_context, db, chat_id, data)

    if data.startswith("client:"):
        message_id = admin.callback_message_id(message)
        if not accept_client_callback(ctx, db, chat_id, data, message_id):
            ctx.answer_callback_query(db, callback_id, CLIENT_CALLBACK_STALE_TEXT)
            return True

    if data == "client:subscribe":
        ctx.answer_callback_query(db, callback_id)
        send_client_menu(ctx, db, chat_id, subscribe_prompt_for_chat(ctx, db, chat_id))
        return True
    if data == "client:status":
        ctx.answer_callback_query(db, callback_id)
        send_client_menu(ctx, db, chat_id, subscription_status_for_chat(ctx, db, chat_id))
        return True
    if data == "client:link":
        ctx.answer_callback_query(db, callback_id)
        return send_client_link_for_chat(ctx, db, chat_id)
    if data.startswith("client:link-credential:"):
        option = selected_client_link_option(db, chat_id, data.rsplit(":", 1)[1])
        if not option:
            ctx.answer_callback_query(db, callback_id, "Список устарел")
            send_client_menu(ctx, db, chat_id, "Список подключений устарел. Открой получение VPN-ссылки заново.")
            return True
        ctx.answer_callback_query(db, callback_id)
        send_current_link_for_option(ctx, db, chat_id, option)
        return True
    if data == "client:traffic":
        ctx.answer_callback_query(db, callback_id)
        send_client_traffic_menu(ctx, db, chat_id)
        return True
    if data == "client:activity":
        ctx.answer_callback_query(db, callback_id)
        send_client_activity_menu(ctx, db, chat_id)
        return True
    if data in ("client:activity:on", "client:activity:off"):
        enabled = data.endswith(":on")
        if not subscriptions.set_activity_notifications(db, chat_id, enabled, ctx.utc_stamp()):
            ctx.answer_callback_query(db, callback_id, "Подписка не найдена", show_alert=True)
            send_client_menu(ctx, db, chat_id, subscribe_prompt_for_chat(ctx, db, chat_id))
            return True
        ctx.save_db_sections(db, ("clientSubscriptions",))
        ctx.answer_callback_query(db, callback_id, "Готово")
        status = subscriptions.activity_notification_status_for_chat(
            db,
            chat_id,
            ctx.load_client_db(),
            owner_chat=keyboards.is_owner_chat(db, chat_id),
        )
        send_client_activity_menu(ctx, db, chat_id, status)
        return True
    if data == "client:activity-exception:list":
        ctx.answer_callback_query(db, callback_id)
        send_client_activity_exception_list(ctx, db, chat_id)
        return True
    if data == "client:activity-exceptions":
        ctx.answer_callback_query(db, callback_id)
        send_client_activity_exceptions_menu(ctx, db, chat_id)
        return True
    if data.startswith("client:activity-exception:delete:"):
        raw_index = data.rsplit(":", 1)[-1]
        try:
            index = int(raw_index)
        except ValueError:
            index = -1
        removed = subscriptions.remove_activity_exception_for_chat(db, chat_id, index)
        if not removed:
            ctx.answer_callback_query(db, callback_id, "Список устарел", show_alert=True)
            send_client_activity_exceptions_menu(ctx, db, chat_id)
            return True
        ctx.save_db_sections(db, ("clientSubscriptionState",))
        ctx.answer_callback_query(db, callback_id, "Удалено")
        label = subscriptions.activity_target_label(removed)
        send_client_activity_exceptions_menu(
            ctx,
            db,
            chat_id,
            f"Исключение удалено: {label}.\n\n"
            "Если такое предупреждение повторится, я снова покажу его в личной рассылке.",
        )
        return True
    if data.startswith("client:activity-exception:add:"):
        raw_index = data.rsplit(":", 1)[-1]
        try:
            index = int(raw_index)
        except ValueError:
            index = -1
        items = subscriptions.activity_exception_candidates(db, chat_id)
        if index < 0 or index >= len(items):
            ctx.answer_callback_query(db, callback_id, "Список устарел", show_alert=True)
            send_client_activity_exception_list(ctx, db, chat_id)
            return True
        client_db = ctx.load_client_db()
        _name, entry, error = subscriptions.subscription_entry_for_chat(db, chat_id, client_db)
        if error:
            ctx.answer_callback_query(db, callback_id, "Подписка недоступна", show_alert=True)
            send_client_menu(ctx, db, chat_id, error)
            return True
        item = dict(items[index])
        item["clientId"] = subscriptions.client_entry_id(entry)
        added = subscriptions.add_activity_exception_for_chat(db, chat_id, item, ctx.utc_stamp())
        ctx.save_db_sections(db, ("clientSubscriptionState",))
        ctx.answer_callback_query(db, callback_id, "Добавлено")
        label = subscriptions.activity_target_label(added or item)
        response = ctx.send_chat_message(
            db,
            chat_id,
            "Готово.\n\n"
            f"Больше не буду присылать личные предупреждения по {label}.\n\n"
            "Если этот сервис должен идти мимо VPN, добавь его домен или IP в split tunneling своего VPN-клиента.",
            reply_markup=keyboards.client_activity_exception_keyboard([]),
        )
        register_client_message(ctx, db, chat_id, response)
        return True
    if data == "client:country":
        ctx.answer_callback_query(db, callback_id)
        send_client_country_menu(ctx, db, chat_id)
        return True
    if data.startswith("client:country:"):
        client_db = ctx.load_client_db()
        name, entry, error = subscriptions.subscription_entry_for_chat(db, chat_id, client_db)
        if error:
            ctx.answer_callback_query(db, callback_id)
            send_client_menu(ctx, db, chat_id, error)
            return True
        tag = data[len("client:country:"):]
        options = {item["tag"]: item for item in client_routes.route_options(client_db)}
        if tag not in options:
            ctx.answer_callback_query(db, callback_id, "Страна недоступна")
            send_client_country_menu(ctx, db, chat_id, "Эта страна сейчас недоступна. Выбери другую.")
            return True
        if client_routes.selected_route_tag(entry) == tag:
            label = client_routes.route_label(options[tag], tag)
            ctx.answer_callback_query(db, callback_id, "Уже выбрана")
            send_client_country_menu(ctx, db, chat_id, f"Эта страна уже выбрана: {label}.")
            return True
        result = ctx.run_capture([str(ctx.xray_client), "route", name, tag], timeout=20)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            ctx.answer_callback_query(db, callback_id, "Не удалось переключить")
            send_client_country_menu(ctx, db, chat_id, "Не удалось переключить страну: " + detail)
            return True
        label = client_routes.route_label(options[tag], tag)
        ctx.answer_callback_query(db, callback_id, "Готово")
        updated_db = ctx.load_client_db()
        updated_entry = subscriptions.client_db_clients(updated_db).get(name, entry)
        send_client_country_menu(
            ctx,
            db,
            chat_id,
            f"Страна подключения изменена: {label}.\n\n"
            f"Текущая страна: {client_routes.selected_route_label(updated_db, updated_entry)}\n\n"
            "Переподключи VPN, чтобы новые соединения пошли через выбранный маршрут.",
        )
        return True
    if data.startswith("client:traffic:"):
        ctx.answer_callback_query(db, callback_id)
        kind = data.rsplit(":", 1)[-1]
        text, parse_mode = traffic_report_for_chat(ctx, db, chat_id, kind)
        send_client_traffic_menu(ctx, db, chat_id, text, parse_mode=parse_mode)
        return True
    if data == "client:unsubscribe":
        ctx.answer_callback_query(db, callback_id, "Готово")
        text = subscriptions.unsubscribe_chat(db, chat_id)
        try_delete_chat_commands(ctx, db, chat_id)
        send_client_menu(ctx, db, chat_id, text)
        return True
    if data == "client:menu":
        ctx.answer_callback_query(db, callback_id)
        send_client_menu(ctx, db, chat_id, client_home_text(ctx, db, chat_id))
        return True
    if data == "client:help":
        ctx.answer_callback_query(db, callback_id)
        send_client_menu(ctx, db, chat_id, client_help_text(ctx, db))
        return True

    ctx.answer_callback_query(db, callback_id, "Неизвестная кнопка")
    send_client_menu(ctx, db, chat_id, client_home_text(ctx, db, chat_id))
    return True


def handle_telegram_update(ctx: PollerContext, db, update):
    if "callback_query" in update:
        return handle_callback_query(ctx, db, update)
    return handle_user_message(ctx, db, update)


def update_private_chat_id(update):
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") == "private" and chat.get("id"):
        return str(chat["id"])
    callback = update.get("callback_query") or {}
    callback_message = callback.get("message") or {}
    callback_chat = callback_message.get("chat") or {}
    if callback_chat.get("type") == "private" and callback_chat.get("id"):
        return str(callback_chat["id"])
    from_user = callback.get("from") or {}
    if from_user.get("id"):
        return str(from_user["id"])
    return ""


def configured(db):
    return bool(db.get("enabled") and db.get("token") and db.get("chatId"))


def update_offset_from_updates(state, updates):
    current = int(state.get("userUpdateOffset", 0) or 0)
    for update in updates:
        try:
            update_id = int(update.get("update_id", 0))
        except (TypeError, ValueError):
            continue
        next_offset = update_id + 1
        if next_offset > current:
            current = next_offset
    state["userUpdateOffset"] = current


def poll_user_subscriptions(ctx: PollerContext, quiet=False, telegram_timeout=USER_POLL_SHORT_TIMEOUT):
    db = ctx.load_db()
    if not db.get("enabled") or not db.get("token"):
        if not quiet:
            print("Telegram bot notifications are not configured or disabled.")
        return 0
    if not db.get("chatId"):
        if not quiet:
            print("Owner chat is not configured; user subscription polling skipped.")
        return 0
    state = db.setdefault("clientSubscriptionState", {})
    offset = int(state.get("userUpdateOffset", 0) or 0)
    payload = {"allowed_updates": ["message", "callback_query"], "timeout": max(0, int(telegram_timeout))}
    if offset:
        payload["offset"] = offset
    try:
        data = ctx.curl_json(db, "getUpdates", payload, timeout=max(15, int(telegram_timeout) + 10))
    except Exception as exc:
        if not quiet:
            print(f"ERROR: Telegram user polling failed: {exc}", file=sys.stderr)
            return 1
        return 0

    updates = data.get("result", [])
    update_offset_from_updates(state, updates)
    state["lastUserPoll"] = ctx.utc_stamp()
    ctx.save_db_sections(db, ("clientSubscriptionState",))

    processed = 0
    for update in updates:
        try:
            if handle_telegram_update(ctx, db, update):
                processed += 1
        except Exception as exc:
            chat_id = update_private_chat_id(update)
            if chat_id:
                try:
                    send_client_menu(ctx, db, chat_id, "Не удалось обработать действие: " + str(exc))
                except Exception:
                    pass
            if not quiet:
                print(f"ERROR: failed to process Telegram user update: {exc}", file=sys.stderr)
    state["lastUserPoll"] = ctx.utc_stamp()
    ctx.save_db_sections(db, ("clientSubscriptions", "clientSubscriptionState"))
    if not quiet:
        print(f"Processed Telegram user messages: {processed}")
    return 0


def run_user_poller(ctx: PollerContext):
    print("Telegram user poller started.", flush=True)
    while True:
        try:
            db = ctx.load_db()
            if not configured(db):
                print("Telegram user poller waits for enabled bot, token, and owner chat.", flush=True)
                time.sleep(USER_POLLER_SLEEP_UNCONFIGURED)
                continue
            rc = poll_user_subscriptions(ctx, quiet=True, telegram_timeout=USER_POLL_LONG_TIMEOUT)
            if rc != 0:
                time.sleep(USER_POLLER_SLEEP_ERROR)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"ERROR: Telegram user poller failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(USER_POLLER_SLEEP_ERROR)
