"""Telegram client subscription helpers."""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from xray_vps_manager.xray import client_routes

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ACTIVITY_EXCEPTION_LIMIT = 100


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
    transport = normalize_value(connection.get("transport") or "tcp")
    expected = {
        "port": str(connection.get("port", "")),
        "pbk": connection.get("publicKey", ""),
        "sni": connection.get("sni", ""),
        "sid": connection.get("shortId", ""),
        "fp": connection.get("fingerprint", ""),
        "security": "reality",
        "encryption": "none",
        "type": transport,
    }
    if transport == "tcp":
        expected["flow"] = (entry.get("client") or {}).get("flow", "xtls-rprx-vision")
    elif transport == "grpc":
        expected["serviceName"] = connection.get("grpcServiceName", "")
    elif transport == "xhttp":
        expected["path"] = connection.get("xhttpPath", "")
        expected["mode"] = connection.get("xhttpMode", "")
    return expected


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


def client_access_summary(entry, format_access_until, client_db=None):
    status = "отключён" if entry.get("enabled") is False else "включён"
    reason = entry.get("disabledReason", "")
    if reason == "expired":
        status = "отключён: срок истёк"
    elif reason == "traffic-limit":
        status = "отключён: лимит трафика"
    lines = [f"Статус: {status}"]
    if isinstance(client_db, dict):
        lines.append(f"Страна: {client_routes.selected_route_label(client_db, entry)}")
    lines.append(f"Доступ до: {format_access_until(entry.get('expiresAt', ''))}")
    return "\n".join(lines)


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
    return "Текущая подписка:\n" + client_access_summary(entry, format_access_until, client_db)


def activity_notifications_enabled(subscription):
    return isinstance(subscription, dict) and subscription.get("activityNotificationsEnabled") is True


def activity_notification_status_for_chat(db, chat_id, client_db, *, owner_chat=False):
    subscription = db.get("clientSubscriptions", {}).get(str(chat_id))
    if not subscription:
        return "Ты пока не подписан на бота. Сначала отправь свою VLESS-ссылку."
    _name, _entry, error = subscription_entry_for_chat(db, chat_id, client_db)
    if error:
        return error
    status = "включена" if activity_notifications_enabled(subscription) else "отключена"
    exception_count = len(activity_exceptions_for_chat(db, chat_id))
    lines = [
        "Уведомления активности",
        "",
        f"Клиентская рассылка: {status}.",
        f"Личных исключений: {exception_count}.",
        "",
        "Это личные GeoIP-предупреждения по твоему VPN-ключу.",
        "Они помогают заметить, что через VPN пошло подключение к региону, который администратор пометил для проверки split tunneling.",
        "",
        "В сообщении будут только метаданные подключения: регион правила, адрес или домен, порт и время.",
        "Бот не видит и не сохраняет содержимое сайтов, сообщений, файлов или запросов.",
    ]
    if owner_chat:
        lines.extend(
            [
                "",
                "Этот чат также настроен как владелец бота, поэтому админская рассылка активности приходит отдельно.",
                "Клиентская копия в этот же чат не дублируется.",
            ]
        )
    return "\n".join(lines)


def set_activity_notifications(db, chat_id, enabled, timestamp=""):
    subscription = db.get("clientSubscriptions", {}).get(str(chat_id))
    if not isinstance(subscription, dict):
        return False
    subscription["activityNotificationsEnabled"] = bool(enabled)
    if timestamp:
        subscription["activityNotificationsUpdatedAt"] = timestamp
        subscription["updatedAt"] = timestamp
    return True


def client_subscription_state(db):
    state = db.setdefault("clientSubscriptionState", {})
    if not isinstance(state, dict):
        state = {}
        db["clientSubscriptionState"] = state
    return state


def normalize_activity_target_item(item):
    if not isinstance(item, dict):
        return {}
    host = str(item.get("host") or "").strip().lower()
    port = str(item.get("port") or "").strip()
    regions = str(item.get("regions") or "").strip().upper()
    client_id = normalize_value(item.get("clientId") or item.get("client_id") or "")
    if not host or host == "-":
        return {}
    return {
        "host": host,
        "port": port,
        "regions": regions,
        "clientId": client_id,
    }


def activity_target_label(item):
    normalized = normalize_activity_target_item(item)
    if not normalized:
        return "-"
    port = normalized.get("port") or "-"
    regions = normalized.get("regions") or "-"
    return f"{normalized['host']}:{port} ({regions})"


def set_activity_exception_candidates(db, chat_id, items, timestamp=""):
    state = client_subscription_state(db)
    candidates = state.setdefault("activityExceptionCandidates", {})
    if not isinstance(candidates, dict):
        candidates = {}
        state["activityExceptionCandidates"] = candidates
    normalized = []
    seen = set()
    for item in items:
        target = normalize_activity_target_item(item)
        if not target:
            continue
        key = (target["clientId"], target["host"], target["port"], target["regions"])
        if key in seen:
            continue
        seen.add(key)
        normalized.append(target)
    candidates[str(chat_id)] = {"items": normalized, "updatedAt": timestamp}


def activity_exception_candidates(db, chat_id):
    state = client_subscription_state(db)
    candidates = state.get("activityExceptionCandidates", {})
    if not isinstance(candidates, dict):
        return []
    entry = candidates.get(str(chat_id), {})
    if not isinstance(entry, dict):
        return []
    items = entry.get("items", [])
    if not isinstance(items, list):
        return []
    return [item for item in (normalize_activity_target_item(item) for item in items) if item]


def activity_exceptions_for_chat(db, chat_id):
    state = client_subscription_state(db)
    exceptions = state.get("activityNotificationExceptions", {})
    if not isinstance(exceptions, dict):
        return []
    items = exceptions.get(str(chat_id), [])
    if not isinstance(items, list):
        return []
    return [item for item in (normalize_activity_target_item(item) for item in items) if item]


def add_activity_exception_for_chat(db, chat_id, item, timestamp=""):
    target = normalize_activity_target_item(item)
    if not target:
        return None
    state = client_subscription_state(db)
    exceptions = state.setdefault("activityNotificationExceptions", {})
    if not isinstance(exceptions, dict):
        exceptions = {}
        state["activityNotificationExceptions"] = exceptions
    chat_key = str(chat_id)
    items = activity_exceptions_for_chat(db, chat_id)
    key = (target["clientId"], target["host"], target["port"], target["regions"])
    for existing in items:
        existing_key = (existing["clientId"], existing["host"], existing["port"], existing["regions"])
        if existing_key == key:
            return existing
    target["addedAt"] = timestamp
    items.append(target)
    exceptions[chat_key] = items[-ACTIVITY_EXCEPTION_LIMIT:]
    return target


def remove_activity_exception_for_chat(db, chat_id, index):
    items = activity_exceptions_for_chat(db, chat_id)
    if index < 0 or index >= len(items):
        return None
    removed = items.pop(index)
    state = client_subscription_state(db)
    exceptions = state.setdefault("activityNotificationExceptions", {})
    if not isinstance(exceptions, dict):
        exceptions = {}
        state["activityNotificationExceptions"] = exceptions
    exceptions[str(chat_id)] = items
    return removed


def activity_exception_matches(db, chat_id, item):
    target = normalize_activity_target_item(item)
    if not target:
        return False
    for exception in activity_exceptions_for_chat(db, chat_id):
        if exception.get("clientId") and exception.get("clientId") != target.get("clientId"):
            continue
        if exception.get("host") != target.get("host"):
            continue
        if exception.get("port") and exception.get("port") != target.get("port"):
            continue
        if exception.get("regions") and exception.get("regions") != target.get("regions"):
            continue
        return True
    return False


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
        "activityNotificationsEnabled": False,
    }
    return name, entry


def subscription_is_current(subscription, entry):
    return bool(entry) and normalize_value(subscription.get("clientId")) == client_entry_id(entry)


def subscription_matches_entry(subscription, entry):
    client_id = normalize_value(subscription.get("clientId"))
    return bool(entry) and (not client_id or client_id == client_entry_id(entry))
