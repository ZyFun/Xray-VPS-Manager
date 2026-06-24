"""Telegram client subscription helpers."""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.xray import client_routes
from xray_vps_manager.xray.config import xhttp_extra_json

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
CLIENT_KEY_RE = re.compile(r"\b(?:vpn-key|client-key|xray-key):([0-9a-fA-F-]{36})\b", re.IGNORECASE)
PROTOCOL_LINK_RE = re.compile(r"(?:vless|trojan)://[^\s<>()]+", re.IGNORECASE)
ACTIVITY_EXCEPTION_LIMIT = 100
ACCESS_KEY_PLACEHOLDER = "vpn-key:00000000-0000-0000-0000-000000000000"


def normalize_value(value):
    return str(value or "").strip().lower()


def find_vless_link(text):
    match = re.search(r"vless://[^\s<>()]+", str(text or ""), flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(0).rstrip(".,;")


def find_protocol_link(text):
    match = PROTOCOL_LINK_RE.search(str(text or ""))
    if not match:
        return ""
    return match.group(0).rstrip(".,;")


def find_client_key(text):
    match = CLIENT_KEY_RE.search(str(text or ""))
    if not match:
        return ""
    value = match.group(1).strip().lower()
    return value if UUID_RE.fullmatch(value) else ""


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
    security = normalize_value(params.get("security"))
    if not security:
        raise ValueError("В ссылке нет обязательного параметра: security")
    if security == "reality":
        for key in ("pbk", "sni", "sid"):
            if not params.get(key):
                raise ValueError(f"В ссылке нет обязательного Reality-параметра: {key}")
    elif security == "tls":
        if not params.get("sni"):
            raise ValueError("В ссылке нет обязательного TLS-параметра: sni")
    else:
        raise ValueError("Поддерживаются только VLESS Reality и TLS-ссылки.")
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


def credential_entry_id(credential):
    if not isinstance(credential, dict):
        return ""
    client = credential.get("client") if isinstance(credential.get("client"), dict) else {}
    return normalize_value(credential.get("id") or client.get("id"))


def client_credentials_for_entry(entry):
    credentials = client_credentials.normalize_entry_credentials(entry)
    if credentials:
        return credentials
    connection_tag = str(entry.get("connection") or "").strip()
    client_id = client_entry_id(entry)
    if not connection_tag or not client_id:
        return {}
    return {
        connection_tag: {
            "id": client_id,
            "connection": connection_tag,
            "protocol": "vless",
            "client": {"id": client_id},
        }
    }


def expected_connection_params(client_db, entry):
    connection_tag = entry.get("connection", "")
    connection = client_db_connections(client_db).get(connection_tag, {})
    transport = normalize_value(connection.get("transport") or "tcp")
    security = normalize_value(connection.get("security") or "reality")
    expected = {"port": str(connection.get("port", "")), "security": security, "encryption": "none", "type": transport}
    if security == "tls":
        public_host = connection.get("publicHost") or connection.get("sni", "")
        expected.update(
            {
                "host": public_host,
                "sni": public_host,
                "path": connection.get("xhttpPath", ""),
                "mode": connection.get("xhttpMode", ""),
            }
        )
        extra = xhttp_extra_json(connection.get("xhttpExtra"))
        if extra:
            expected["extra"] = extra
        return expected

    expected.update(
        {
            "pbk": connection.get("publicKey", ""),
            "sni": connection.get("sni", ""),
            "sid": connection.get("shortId", ""),
            "fp": connection.get("fingerprint", ""),
        }
    )
    if transport == "tcp":
        expected["flow"] = (entry.get("client") or {}).get("flow", "xtls-rprx-vision")
    elif transport == "grpc":
        expected["serviceName"] = connection.get("grpcServiceName", "")
    elif transport == "xhttp":
        expected["path"] = connection.get("xhttpPath", "")
        expected["mode"] = connection.get("xhttpMode", "")
        extra = xhttp_extra_json(connection.get("xhttpExtra"))
        if extra:
            expected["extra"] = extra
    return expected


def expected_credential_params(client_db, entry, credential):
    connection_tag = credential.get("connection", "") or entry.get("connection", "")
    connection = client_db_connections(client_db).get(connection_tag, {})
    credential_client = credential.get("client") if isinstance(credential.get("client"), dict) else {}
    pseudo_entry = dict(entry)
    pseudo_entry["connection"] = connection_tag
    pseudo_entry["client"] = credential_client
    expected = expected_connection_params({"connections": client_db_connections(client_db)}, pseudo_entry)
    if normalize_value(connection.get("security") or "reality") == "tls":
        expected["port"] = str(connection.get("publicPort") or connection.get("port", ""))
    return expected


def match_vless_to_client(parsed_link, client_db):
    clients = client_db_clients(client_db)
    same_uuid = []
    for name, entry in clients.items():
        for credential in client_credentials_for_entry(entry).values():
            if credential_entry_id(credential) == parsed_link["id"]:
                same_uuid.append((name, entry, credential))
    if not same_uuid:
        return None, "По UUID из ссылки клиент в базе не найден."

    matches = []
    mismatch_notes = []
    for name, entry, credential in same_uuid:
        expected = expected_credential_params(client_db, entry, credential)
        mismatches = []
        for key, expected_value in expected.items():
            if not expected_value:
                continue
            if key == "port":
                actual_value = parsed_link["port"]
            elif key == "host":
                actual_value = parsed_link["host"]
            else:
                actual_value = parsed_link["params"].get(key, "")
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
    return None, "UUID найден, но параметры VLESS-ссылки не совпадают с текущей базой: " + detail


def match_client_key_to_client(client_uuid, client_db):
    matches = [
        (name, entry)
        for name, entry in client_db_clients(client_db).items()
        if client_entry_id(entry) == normalize_value(client_uuid)
    ]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, "По ключу найдено несколько клиентов. Обратись к администратору."
    return None, "Клиентский ключ в базе не найден."


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
        return None, None, f"Ты пока не подписан на напоминания. Сначала отправь ключ доступа формата {ACCESS_KEY_PLACEHOLDER}."
    name = subscription.get("client", "")
    entry = client_db_clients(client_db).get(name)
    if not entry:
        return None, None, f"Подписка найдена, но клиента уже нет в базе. Отправь актуальный ключ доступа формата {ACCESS_KEY_PLACEHOLDER} или обратись к администратору."
    if subscription.get("clientId") and normalize_value(subscription.get("clientId")) != client_entry_id(entry):
        return None, None, f"Подписка устарела: клиент был пересоздан. Отправь актуальный ключ доступа формата {ACCESS_KEY_PLACEHOLDER} заново."
    return name, entry, ""


def chat_has_subscription(db, chat_id):
    subscription = db.get("clientSubscriptions", {}).get(str(chat_id))
    return isinstance(subscription, dict)


def subscription_status_for_chat(db, chat_id, client_db, format_access_until):
    subscription = db.get("clientSubscriptions", {}).get(str(chat_id))
    if not subscription:
        return f"Ты пока не подписан на напоминания. Отправь ключ доступа формата {ACCESS_KEY_PLACEHOLDER}, чтобы подключить уведомления."
    _name, entry, error = subscription_entry_for_chat(db, chat_id, client_db)
    if error:
        return error
    return "Текущая подписка:\n" + client_access_summary(entry, format_access_until, client_db)


def activity_notifications_enabled(subscription):
    return isinstance(subscription, dict) and subscription.get("activityNotificationsEnabled") is True


def activity_notification_status_for_chat(db, chat_id, client_db, *, owner_chat=False):
    subscription = db.get("clientSubscriptions", {}).get(str(chat_id))
    if not subscription:
        return f"Ты пока не подписан на бота. Сначала отправь ключ доступа формата {ACCESS_KEY_PLACEHOLDER}."
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
    return neutral_link_fragment(link, server_fragment)


def neutral_link_fragment(link, server_fragment):
    raw = str(link or "").strip()
    if not raw:
        return raw
    base = raw.split("#", 1)[0]
    return base + "#" + server_fragment


def telegram_html_escape(value):
    return html.escape(str(value or ""), quote=False)


def connection_links_from_output(output):
    links = []
    for line in str(output or "").splitlines():
        value = line.strip()
        if value.lower().startswith(("vless://", "trojan://")):
            links.append(value)
    return links


def first_connection_link(output):
    links = connection_links_from_output(output)
    return links[0] if links else ""


def access_key_from_output(output):
    match = re.search(r"\b(?:Access key:\s*)?(vpn-key:[0-9a-fA-F-]{36})\b", str(output or ""), re.IGNORECASE)
    return match.group(1).lower() if match else ""


def credential_options_for_entry(client_db, entry):
    connections = client_db_connections(client_db)
    options = []
    for tag, credential in client_credentials_for_entry(entry).items():
        connection = connections.get(tag, {}) if isinstance(connections.get(tag, {}), dict) else {}
        protocol = normalize_value(credential.get("protocol") or connection.get("protocol") or "vless")
        name = str(connection.get("name") or tag)
        transport = normalize_value(connection.get("transport") or credential.get("transport") or "")
        security = normalize_value(connection.get("security") or credential.get("security") or "")
        parts = [protocol.upper(), name]
        details = [value for value in (security, transport) if value]
        if details:
            parts.append("/".join(details))
        options.append(
            {
                "connection": str(tag),
                "protocol": protocol,
                "name": name,
                "label": " · ".join(parts),
            }
        )
    return sorted(options, key=lambda item: (item["protocol"], item["name"], item["connection"]))


def credential_options_for_client(client_db, name):
    entry = client_db_clients(client_db).get(name)
    if not isinstance(entry, dict):
        return []
    return credential_options_for_entry(client_db, entry)


def current_link_value_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment, connection_tag=""):
    name, _entry, error = subscription_entry_for_chat(db, chat_id, client_db)
    if error:
        return "", error
    xray_client = Path(xray_client)
    if not xray_client.exists():
        return "", "xray-client не найден на сервере. Обратись к администратору."
    command = [str(xray_client), "link", name]
    if connection_tag:
        command.extend(["--connection", str(connection_tag)])
    result = run_capture(command, timeout=20)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        return "", "Не удалось получить актуальную ссылку подключения: " + detail
    link = first_connection_link(result.stdout)
    if not link:
        return "", "Не удалось получить актуальную ссылку подключения: xray-client не вернул ссылку."
    return neutral_link_fragment(link, server_fragment), ""


def current_vless_link_value_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment):
    return current_link_value_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment)


def current_vless_link_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment):
    link, error = current_link_value_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment)
    if error:
        return error
    return "\n".join(
        [
            "Актуальная ссылка подключения:",
            "",
            link,
            "",
            "Если настройки подключения менялись, импортируй эту ссылку заново.",
        ]
    )


def current_vless_link_code_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment):
    return current_link_code_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment)


def current_link_code_for_chat(db, chat_id, client_db, xray_client, run_capture, server_fragment, connection_tag=""):
    link, error = current_link_value_for_chat(
        db,
        chat_id,
        client_db,
        xray_client,
        run_capture,
        server_fragment,
        connection_tag=connection_tag,
    )
    if error:
        return error, None
    return (
        "\n".join(
            [
                "Актуальная ссылка подключения:",
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
    client_key = find_client_key(text)
    if not client_key:
        if find_protocol_link(text):
            raise ValueError(
                "Для подключения уведомлений отправь ключ доступа формата "
                f"{ACCESS_KEY_PLACEHOLDER}. Протокольные ссылки больше не используются для привязки бота."
            )
        raise ValueError(f"Ключ доступа не найден. Отправь ключ формата {ACCESS_KEY_PLACEHOLDER}.")
    match, reason = match_client_key_to_client(client_key, client_db)
    if not match:
        raise ValueError(reason)
    name, entry = match
    chat_id = str(chat["id"])
    db.setdefault("clientSubscriptions", {})[chat_id] = {
        "client": name,
        "clientId": client_entry_id(entry),
        "connection": entry.get("connection", ""),
        "chatLabel": chat_label,
        "linkHash": "",
        "clientKey": f"vpn-key:{client_entry_id(entry)}",
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
