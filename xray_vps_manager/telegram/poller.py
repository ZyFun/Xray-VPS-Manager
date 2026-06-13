"""Telegram user long-polling and client-facing actions."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xray_vps_manager.telegram import admin, bot_commands, keyboards, messages, subscriptions, traffic

USER_POLL_SHORT_TIMEOUT = 2
USER_POLL_LONG_TIMEOUT = 45
USER_POLLER_SLEEP_UNCONFIGURED = 30
USER_POLLER_SLEEP_ERROR = 5


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


def send_client_menu(ctx: PollerContext, db, chat_id, text=None, parse_mode=None):
    ctx.send_chat_message(
        db,
        chat_id,
        text or subscription_intro_text(ctx),
        reply_markup=keyboards.client_keyboard_for_chat(db, chat_id),
        parse_mode=parse_mode,
    )


def send_client_traffic_menu(ctx: PollerContext, db, chat_id, text=None, parse_mode=None):
    ctx.send_chat_message(
        db,
        chat_id,
        text or traffic.traffic_menu_text(),
        reply_markup=keyboards.client_traffic_keyboard(),
        parse_mode=parse_mode,
    )


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
        return "Уведомления уже подключены.\n\n" + subscriptions.client_access_summary(entry, ctx.format_access_until)
    if subscriptions.chat_has_subscription(db, chat_id) and error:
        return error + "\n\n" + messages.subscribe_prompt_text()
    return messages.subscribe_prompt_text()


def current_vless_link_code_for_chat(ctx: PollerContext, db, chat_id):
    return subscriptions.current_vless_link_code_for_chat(
        db,
        chat_id,
        ctx.load_client_db(),
        ctx.xray_client,
        ctx.run_capture,
        ctx.server_name_fragment(),
    )


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
        text, parse_mode = current_vless_link_code_for_chat(ctx, db, chat_id)
        send_client_menu(ctx, db, chat_id, text, parse_mode=parse_mode)
        return True
    if command == "/traffic":
        send_client_traffic_menu(ctx, db, chat_id)
        return True
    if command in ("/unsubscribe", "/stop"):
        text = subscriptions.unsubscribe_chat(db, chat_id)
        try_delete_chat_commands(ctx, db, chat_id)
        send_client_menu(ctx, db, chat_id, text)
        return True
    if subscriptions.find_vless_link(text):
        _name, entry = subscribe_chat_to_client(ctx, db, chat, text)
        try_set_subscribed_commands(ctx, db, chat_id)
        send_client_menu(
            ctx,
            db,
            chat_id,
            "Подписка подключена.\n\n"
            + subscriptions.client_access_summary(entry, ctx.format_access_until)
            + "\n\nНапоминания придут в 08:00 за 5 дней и за 1 день до окончания доступа.",
        )
        return True
    if subscriptions.chat_has_subscription(db, chat_id):
        send_client_menu(ctx, db, chat_id, client_home_text(ctx, db, chat_id))
    else:
        send_client_menu(ctx, db, chat_id, "Я не нашёл VLESS-ссылку. Отправь свою ссылку целиком или нажми кнопку ниже.")
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
        ctx.answer_callback_query(db, callback_id)
        return admin.handle_callback(ctx.admin_context, db, chat_id, data)

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
        text, parse_mode = current_vless_link_code_for_chat(ctx, db, chat_id)
        send_client_menu(ctx, db, chat_id, text, parse_mode=parse_mode)
        return True
    if data == "client:traffic":
        ctx.answer_callback_query(db, callback_id)
        send_client_traffic_menu(ctx, db, chat_id)
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
