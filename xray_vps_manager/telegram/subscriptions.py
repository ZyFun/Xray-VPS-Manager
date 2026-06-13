"""Telegram client subscription helpers."""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def normalize_value(value):
    return str(value or "").strip().lower()


def find_vless_link(text):
    match = re.search(r"vless://[^\s<>()]+", str(text or ""), flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(0).rstrip(".,;")


def parse_vless_link(text):
    link = find_vless_link(text)
    if not link:
        raise ValueError("VLESS-ссылка не найдена.")
    parsed = urlsplit(link)
    if parsed.scheme.lower() != "vless":
        raise ValueError("Это не VLESS-ссылка.")
    client_id = unquote(parsed.username or "").strip().lower()
    if not UUID_RE.fullmatch(client_id):
        raise ValueError("В ссылке не найден корректный UUID клиента.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("В ссылке указан некорректный порт.") from exc
    if not port:
        raise ValueError("В ссылке не указан порт подключения.")
    params = {key.lower(): value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
    for key in ("security", "pbk", "sni", "sid"):
        if not params.get(key):
            raise ValueError(f"В ссылке нет обязательного Reality-параметра: {key}")
    if normalize_value(params.get("security")) != "reality":
        raise ValueError("Поддерживаются только VLESS Reality-ссылки.")
    return {
        "raw": link,
        "id": client_id,
        "host": parsed.hostname or "",
        "port": str(port or ""),
        "params": params,
        "hash": hashlib.sha256(link.encode("utf-8")).hexdigest(),
    }


def client_db_clients(client_db):
    clients = client_db.get("clients", {})
    return clients if isinstance(clients, dict) else {}


def client_db_connections(client_db):
    connections = client_db.get("connections", {})
    return connections if isinstance(connections, dict) else {}


def client_entry_id(entry):
    return normalize_value(entry.get("id") or (entry.get("client") or {}).get("id"))


def expected_connection_params(client_db, entry):
    connection_tag = entry.get("connection", "")
    connection = client_db_connections(client_db).get(connection_tag, {})
    return {
        "port": str(connection.get("port", "")),
        "pbk": connection.get("publicKey", ""),
        "sni": connection.get("sni", ""),
        "sid": connection.get("shortId", ""),
        "fp": connection.get("fingerprint", ""),
        "security": "reality",
        "encryption": "none",
        "type": "tcp",
        "flow": (entry.get("client") or {}).get("flow", "xtls-rprx-vision"),
    }


def match_vless_to_client(parsed_link, client_db):
    clients = client_db_clients(client_db)
    same_uuid = []
    for name, entry in clients.items():
        if client_entry_id(entry) == parsed_link["id"]:
            same_uuid.append((name, entry))
    if not same_uuid:
        return None, "По UUID из ссылки клиент в базе не найден."

    matches = []
    mismatch_notes = []
    for name, entry in same_uuid:
        expected = expected_connection_params(client_db, entry)
        mismatches = []
        for key, expected_value in expected.items():
            if not expected_value:
                continue
            actual_value = parsed_link["port"] if key == "port" else parsed_link["params"].get(key, "")
            if actual_value and normalize_value(actual_value) != normalize_value(expected_value):
                mismatches.append(key)
        if mismatches:
            mismatch_notes.append(f"{name}: " + ", ".join(mismatches))
            continue
        matches.append((name, entry))

    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, "По ссылке найдено несколько клиентов. Обратись к администратору."
    detail = "; ".join(mismatch_notes) if mismatch_notes else "параметры подключения не совпали"
    return None, "UUID найден, но Reality-параметры ссылки не совпадают с текущей базой: " + detail


def client_access_summary(entry, format_access_until):
    status = "отключён" if entry.get("enabled") is False else "включён"
    reason = entry.get("disabledReason", "")
    if reason == "expired":
        status = "отключён: срок истёк"
    elif reason == "traffic-limit":
        status = "отключён: лимит трафика"
    return "\n".join(
        [
            f"Статус: {status}",
            f"Доступ до: {format_access_until(entry.get('expiresAt', ''))}",
        ]
    )


def subscription_entry_for_chat(db, chat_id, client_db):
    subscription = db.get("clientSubscriptions", {}).get(str(chat_id))
    if not subscription:
        return None, None, "Ты пока не подписан на напоминания. Сначала отправь свою VLESS-ссылку."
    name = subscription.get("client", "")
    entry = client_db_clients(client_db).get(name)
    if not entry:
        return None, None, "Подписка найдена, но клиента уже нет в базе. Отправь актуальную VLESS-ссылку или обратись к администратору."
    if subscription.get("clientId") and normalize_value(subscription.get("clientId")) != client_entry_id(entry):
        return None, None, "Подписка устарела: клиент был пересоздан. Отправь актуальную VLESS-ссылку заново."
    return name, entry, ""


def chat_has_subscription(db, chat_id):
    subscription = db.get("clientSubscriptions", {}).get(str(chat_id))
    return isinstance(subscription, dict)


def subscription_status_for_chat(db, chat_id, client_db, format_access_until):
    subscription = db.get("clientSubscriptions", {}).get(str(chat_id))
    if not subscription:
        return "Ты пока не подписан на напоминания. Отправь свою VLESS-ссылку, чтобы подключить уведомления."
    _name, entry, error = subscription_entry_for_chat(db, chat_id, client_db)
    if error:
        return error
    return "Текущая подписка:\n" + client_access_summary(entry, format_access_until)


def neutral_vless_fragment(link, server_fragment):
    raw = str(link or "").strip()
    if not raw:
        return raw
    base = raw.split("#", 1)[0]
    return base + "#" + server_fragment


def telegram_html_escape(value):
    return html.escape(str(value or ""), quote=False)


def current_vless_link_value_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment):
    name, _entry, error = subscription_entry_for_chat(db, chat_id, client_db)
    if error:
        return "", error
    xray_client = Path(xray_client)
    if not xray_client.exists():
        return "", "xray-client не найден на сервере. Обратись к администратору."
    result = run_capture([str(xray_client), "link", name], timeout=20)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        return "", "Не удалось получить актуальную VLESS-ссылку: " + detail
    link = ""
    for line in result.stdout.splitlines():
        if line.strip().startswith("vless://"):
            link = line.strip()
            break
    if not link:
        return "", "Не удалось получить актуальную VLESS-ссылку: xray-client не вернул ссылку."
    return neutral_vless_fragment(link, server_fragment), ""


def current_vless_link_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment):
    link, error = current_vless_link_value_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment)
    if error:
        return error
    return "\n".join(
        [
            "Актуальная VLESS Reality-ссылка:",
            "",
            link,
            "",
            "Если настройки подключения менялись, импортируй эту ссылку заново.",
        ]
    )


def current_vless_link_code_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment):
    link, error = current_vless_link_value_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment)
    if error:
        return error, None
    return (
        "\n".join(
            [
                "Актуальная VLESS Reality-ссылка:",
                "",
                f"<pre><code>{telegram_html_escape(link)}</code></pre>",
                "",
                "Если настройки подключения менялись, импортируй эту ссылку заново.",
            ]
        ),
        "HTML",
    )


def unsubscribe_chat(db, chat_id):
    removed = db.setdefault("clientSubscriptions", {}).pop(str(chat_id), None)
    if removed:
        return "Подписка на бота отключена."
    return "Активной подписки нет."


def subscribe_chat_to_client(db, chat, text, client_db, chat_label, timestamp):
    parsed_link = parse_vless_link(text)
    match, reason = match_vless_to_client(parsed_link, client_db)
    if not match:
        raise ValueError(reason)
    name, entry = match
    chat_id = str(chat["id"])
    db.setdefault("clientSubscriptions", {})[chat_id] = {
        "client": name,
        "clientId": client_entry_id(entry),
        "connection": entry.get("connection", ""),
        "chatLabel": chat_label,
        "linkHash": parsed_link["hash"],
        "subscribedAt": timestamp,
        "enabled": True,
    }
    return name, entry


def subscription_is_current(subscription, entry):
    return bool(entry) and normalize_value(subscription.get("clientId")) == client_entry_id(entry)


def subscription_matches_entry(subscription, entry):
    client_id = normalize_value(subscription.get("clientId"))
    return bool(entry) and (not client_id or client_id == client_entry_id(entry))
