"""Telegram Bot API transport helpers."""

from __future__ import annotations

import json

from xray_vps_manager.core.process import run_capture

TELEGRAM_SOCKS_HOST = "127.0.0.1"
TELEGRAM_SOCKS_PORT = 10810


def curl_json(db, method, payload=None, timeout=30):
    token = db.get("token", "")
    if not token:
        raise ValueError("Telegram bot token is not configured.")
    url = f"https://api.telegram.org/bot{token}/{method}"
    command = ["curl", "-fsS", "--connect-timeout", "10", "--max-time", str(timeout)]
    if db.get("routeMode") == "cascade":
        command.extend(["--proxy", f"socks5h://{TELEGRAM_SOCKS_HOST}:{TELEGRAM_SOCKS_PORT}"])
    if payload is not None:
        command.extend(["-H", "Content-Type: application/json", "-d", json.dumps(payload, ensure_ascii=False)])
    command.append(url)
    result = run_capture(command, timeout=timeout + 5)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"curl exited with {result.returncode}").strip()
        raise RuntimeError(detail)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Telegram returned invalid JSON: {exc}") from exc
    if not data.get("ok"):
        raise RuntimeError(json.dumps(data, ensure_ascii=False))
    return data


def send_chat_message(db, chat_id, text, reply_markup=None, parse_mode=None):
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        raise ValueError("Telegram chat is not configured.")
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return curl_json(db, "sendMessage", payload, timeout=30)


def send_message(db, text, parse_mode=None):
    return send_chat_message(db, db.get("chatId"), text, parse_mode=parse_mode)


def answer_callback_query(db, callback_id, text="", show_alert=False):
    callback_id = str(callback_id or "").strip()
    if not callback_id:
        return None
    payload = {"callback_query_id": callback_id, "show_alert": bool(show_alert)}
    if text:
        payload["text"] = text
    try:
        return curl_json(db, "answerCallbackQuery", payload, timeout=15)
    except Exception:
        return None
