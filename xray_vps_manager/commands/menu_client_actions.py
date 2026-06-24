"""Client actions used by the interactive menu."""

from __future__ import annotations

import os
import re
from collections.abc import Callable

from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients.listing import client_rows as build_client_rows
from xray_vps_manager.clients.repository import db_clients, load_db_sql
from xray_vps_manager.commands import menu_reality_actions
from xray_vps_manager.core.terminal import green, print_table, table_border, table_row, visible_len, yellow
from xray_vps_manager.core.time import manager_timezone, parse_time
from xray_vps_manager.xray import cascade as cascade_config
from xray_vps_manager.xray import client_routes
from xray_vps_manager.xray.config import connection_name_from_tag, load_config as load_xray_config

CLIENT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
CommandRunner = Callable[[list[str]], None]


def show_clients(call: CommandRunner) -> None:
    call(["xray-client", "list"])


def expire_due(call: CommandRunner) -> None:
    call(["xray-client", "expire-due"])


def show_traffic_limits(call: CommandRunner) -> None:
    call(["xray-client", "limit-list"])


def enforce_traffic_limits(call: CommandRunner) -> None:
    call(["xray-client", "enforce-limits", "--sync"])


def die(message: str) -> None:
    raise SystemExit(message)


def validate_client_name(value: str) -> str:
    if not CLIENT_RE.fullmatch(value or ""):
        die("Client name must be 1-64 chars: A-Z a-z 0-9 _ . @ -")
    return value


def load_config() -> dict:
    try:
        return load_xray_config()
    except FileNotFoundError as exc:
        die(str(exc))


def color_payment_status(value: object) -> str:
    text = str(value or "")
    if os.environ.get("NO_COLOR"):
        return text
    if text == "free":
        return green(text)
    if text == "paid":
        return yellow(text)
    return text


def format_access_until(value: str | None) -> str:
    parsed = parse_time(value)
    if parsed is None:
        return "бессрочно"
    timezone, _ = manager_timezone()
    return parsed.astimezone(timezone).strftime("%Y-%m-%d %H:%M")


def client_rows_for_selection(mode: str = "all") -> list[dict]:
    config = load_config()
    db = load_db_sql()
    rows = []
    connection_names = {row["tag"]: row["name"] for row in menu_reality_actions.connection_rows()}

    for row in build_client_rows(config, db):
        tag = row.get("connection") or menu_reality_actions.INBOUND_TAG
        row = dict(row)
        row["connectionName"] = connection_names.get(tag, connection_name_from_tag(tag))
        rows.append(row)

    if mode == "enabled":
        return [row for row in rows if row["status"] == "enabled"]
    if mode == "disabled":
        return [row for row in rows if row["status"] != "enabled"]
    return rows


def print_client_selection_table(rows: list[dict]) -> None:
    headers = ("№", "CONNECTION", "NAME", "STATUS", "PAYMENT", "ACCESS UNTIL", "CREATED")
    values = [
        [
            str(index),
            row["connectionName"],
            row["name"],
            row["status"],
            color_payment_status(row.get("paymentType", "free")),
            format_access_until(row.get("expiresAt", "")),
            row["created"],
        ]
        for index, row in enumerate(rows, start=1)
    ]
    values.append(["0", "Назад", "", "", "", "", ""])
    widths = [
        max(visible_len(headers[column]), *(visible_len(row[column]) for row in values))
        for column in range(len(headers))
    ]
    border = table_border(widths)
    print(border)
    print(table_row(headers, widths))
    print(border)
    for row in values:
        print(table_row(row, widths))
    print(border)


def choose_client_row(action: str, mode: str = "all") -> dict | None:
    rows = client_rows_for_selection(mode)
    if not rows:
        print(f"Нет клиентов для действия: {action}.")
        return None

    print(f"Выбери клиента для действия: {action}.")
    print_client_selection_table(rows)
    while True:
        choice = input("Клиент: ").strip()
        if choice == "0":
            return None
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1]
        print("Неизвестный клиент. Выбери номер из списка или 0 для возврата.")


def choose_client(action: str, mode: str = "all") -> str:
    row = choose_client_row(action, mode)
    return str(row["name"]) if row else ""


def client_exists_for_menu(name: str) -> bool:
    return any(row["name"] == name for row in client_rows_for_selection("all"))


def client_credential_connection_tags(name: str, selected_row: dict | None = None) -> set[str]:
    tags: set[str] = set()
    try:
        entry = db_clients(load_db_sql()).get(name)
    except Exception:
        entry = None
    if isinstance(entry, dict):
        tags.update(client_credentials.normalize_entry_credentials(entry))
    if selected_row and selected_row.get("connection"):
        tags.add(str(selected_row["connection"]))
    return tags


def choose_available_connection_for_client(name: str, selected_row: dict | None = None) -> str:
    used_connections = client_credential_connection_tags(name, selected_row)
    rows = [
        row
        for row in menu_reality_actions.connection_rows()
        if str(row["tag"]) not in used_connections
    ]
    if not rows:
        print("У клиента уже есть credentials во всех доступных подключениях.")
        return ""
    if len(rows) == 1:
        row = rows[0]
        print(f"Будет добавлено подключение: {row['name']} ({row['tag']}).")
        return str(row["tag"])
    print(f"Выбери новое подключение для клиента: {name}.")
    menu_reality_actions.print_connection_selection_table(rows)
    while True:
        choice = input("Подключение: ").strip()
        if choice == "0":
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return str(rows[index - 1]["tag"])
        print("Неизвестное подключение. Выбери номер из списка или 0 для возврата.")


def ask_payment_type() -> str:
    print("Статус оплаты клиента.")
    print("1) Бесплатный: не участвует в расчёте общей аренды и не получает напоминания об оплате.")
    print("2) Платный: участвует в расчёте суммы на клиента и получает напоминания.")
    choice = input("Статус оплаты [1-бесплатный]: ").strip() or "1"
    if choice == "1":
        return "free"
    if choice == "2":
        return "paid"
    print("Действие отменено: неизвестный статус оплаты.")
    return ""


def ask_new_client_command() -> list[str]:
    print("Введите имя клиента. Можно сразу указать срок через пробел.")
    print("Примеры: data_test2 или data_test2 30. Пустой срок или 0 означает бессрочно.")
    raw = input("Имя клиента [и дни]: ").strip()
    if not raw:
        die("Client name is required.")
    parts = raw.split(maxsplit=1)
    name = validate_client_name(parts[0])
    if client_exists_for_menu(name):
        print("Такой клиент уже существует.")
        print("Для добавления VLESS/Trojan credential используй пункт: Добавить подключение к клиенту.")
        return []
    tag = menu_reality_actions.choose_connection("добавления клиента")
    if not tag:
        return []
    payment_type = ask_payment_type()
    if not payment_type:
        return []
    command = ["xray-client", "add", name]
    if len(parts) == 1:
        return command + ["--connection", tag, "--payment", payment_type]
    return command + [parts[1].strip(), "--connection", tag, "--payment", payment_type]


def add_client_from_menu(call: CommandRunner) -> None:
    command = ask_new_client_command()
    if not command:
        print("Действие отменено.")
        return
    call(command)


def ask_existing_client_connection_command() -> list[str]:
    row = choose_client_row("добавления подключения к клиенту", "all")
    if not row:
        return []
    name = str(row["name"])
    tag = choose_available_connection_for_client(name, row)
    if not tag:
        return []
    return ["xray-client", "add", name, "--connection", tag]


def add_connection_to_client_from_menu(call: CommandRunner) -> None:
    command = ask_existing_client_connection_command()
    if not command:
        print("Действие отменено.")
        return
    call(command)


def ask_access_days() -> str:
    print("Количество календарных дней доступа.")
    print("Введите 0 или нажмите Enter, чтобы сделать доступ бессрочным.")
    value = input("ACCESS_DAYS [бессрочно]: ").strip()
    return value or "0"


def ask_extend_days() -> str:
    print("Количество дней, которое нужно добавить к текущей дате окончания доступа.")
    print("Если срок уже истёк или не был установлен, продление пойдёт от сегодняшней даты.")
    value = input("EXTEND_DAYS: ").strip()
    if not value:
        die("Extend days is required.")
    return value


def choose_limit_period() -> str:
    print("Период лимита трафика.")
    print("1) День: лимит сбрасывается каждый календарный день в 00:00 по времени сервера.")
    print("2) Месяц: лимит сбрасывается в начале следующего календарного месяца.")
    while True:
        value = input("Период [1-день, 2-месяц]: ").strip()
        if value == "1":
            return "daily"
        if value == "2":
            return "monthly"
        print("Выбери 1 для дневного лимита или 2 для месячного лимита.")


def update_selected_client_limit(call: CommandRunner) -> None:
    name = choose_client("установки лимита трафика", "all")
    if not name:
        print("Действие отменено.")
        return
    period = choose_limit_period()
    print("LIMIT_GB: лимит общего трафика IN+OUT в гигабайтах.")
    print("Пример: 15. Пустой ввод отменяет изменение, 0 убирает лимит.")
    value = input("LIMIT_GB: ").strip()
    if not value:
        print("Действие отменено.")
        return
    call(["xray-client", "set-limit", name, period, value])


def clear_selected_client_limit(call: CommandRunner) -> None:
    name = choose_client("снятия лимита трафика", "all")
    if not name:
        print("Действие отменено.")
        return
    call(["xray-client", "clear-limit", name])


def call_client_command(call: CommandRunner, command: str, action: str, mode: str = "all") -> None:
    name = choose_client(action, mode)
    if not name:
        print("Действие отменено.")
        return
    call(["xray-client", command, name])


def client_route_rows_for_selection(name: str) -> list[dict]:
    config = load_config()
    db = load_db_sql()
    client_routes.sync_routes_from_config(config, db)
    entry = db_clients(db).get(name, {})
    current_tag = client_routes.selected_route_tag(entry)
    active_tag = cascade_config.active_cascade_tag(config)
    rows = []
    for index, item in enumerate(client_routes.route_options(db, config), start=1):
        tag = str(item["tag"])
        rows.append(
            {
                "index": index,
                "display": item.get("display") or "-",
                "tag": tag,
                "current": "yes" if tag == current_tag else "-",
                "active": "yes" if tag == active_tag else "-",
            }
        )
    return rows


def print_client_route_selection_table(rows: list[dict]) -> None:
    values = [
        [str(row["index"]), row["display"], row["tag"], row["current"], row["active"]]
        for row in rows
    ]
    values.append(["0", "Назад", "", "", ""])
    print_table(["№", "COUNTRY", "TAG", "CURRENT", "GLOBAL ACTIVE"], values, empty_message=None)


def choose_client_route(name: str) -> str:
    rows = client_route_rows_for_selection(name)
    if not rows:
        print("Страны подключения не настроены. Добавь cascade через меню Маршрутизация -> Каскад.")
        return ""

    print(f"Выбери страну подключения для клиента: {name}.")
    print_client_route_selection_table(rows)
    while True:
        choice = input("Страна: ").strip()
        if choice == "0":
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return str(rows[index - 1]["tag"])
        print("Неизвестная страна. Выбери номер из списка или 0 для возврата.")


def update_selected_client_route(call: CommandRunner) -> None:
    name = choose_client("изменения страны подключения", "all")
    if not name:
        print("Действие отменено.")
        return
    tag = choose_client_route(name)
    if not tag:
        print("Действие отменено.")
        return
    call(["xray-client", "route", name, tag])
    print("После смены страны переподключи VPN, чтобы новые соединения пошли через выбранный маршрут.")


def update_selected_client_days(call: CommandRunner) -> None:
    name = choose_client("изменения срока", "all")
    if not name:
        print("Действие отменено.")
        return
    print("Что сделать со сроком доступа?")
    print("1) Продлить на N дней: прибавить дни к текущей дате окончания.")
    print("2) Установить срок N дней от сегодняшней даты.")
    print("3) Сделать доступ бессрочным.")
    choice = input("Действие [1-продлить]: ").strip() or "1"
    if choice == "1":
        call(["xray-client", "extend-days", name, ask_extend_days()])
    elif choice == "2":
        call(["xray-client", "set-days", name, ask_access_days()])
    elif choice == "3":
        call(["xray-client", "set-days", name, "0"])
    else:
        print("Действие отменено: неизвестный выбор.")


def update_selected_client_payment(call: CommandRunner) -> None:
    name = choose_client("изменения статуса оплаты", "all")
    if not name:
        print("Действие отменено.")
        return
    payment_type = ask_payment_type()
    if not payment_type:
        return
    call(["xray-client", "set-payment", name, payment_type])


def move_selected_client(call: CommandRunner) -> None:
    name = choose_client("переноса в другое подключение", "all")
    if not name:
        print("Действие отменено.")
        return
    tag = menu_reality_actions.choose_connection("переноса клиента", auto_single=False)
    if not tag:
        print("Действие отменено.")
        return
    call(["xray-client", "move-connection", name, tag])
