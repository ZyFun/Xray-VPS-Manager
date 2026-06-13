#!/usr/bin/env python3
import os
import subprocess
import sys

from xray_vps_manager.commands import (
    menu_activity_actions,
    menu_activity_exception_actions,
    menu_activity_export_actions,
    menu_backup_actions,
    menu_client_actions,
    menu_reality_actions,
    menu_security_actions,
    menu_status,
    menu_telegram_actions,
    menu_timezone_actions,
    menu_traffic_actions,
    menu_xray_actions,
)
from xray_vps_manager.core.terminal import red, table_border, table_row

MENU_VERSION = "v1.0.0"
MENU_UPDATED = "2026-06-13 20:13 UTC"


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def print_menu_header():
    rows = [
        ("Xray Version", menu_status.current_xray_version()),
        ("Manager Version", MENU_VERSION),
        ("Manager Updated", menu_status.manager_updated_header_value(MENU_UPDATED)),
        ("Geo Assets", menu_status.geo_assets_header_value()),
        ("Security Audit", menu_status.security_audit_header_value()),
    ]
    label_width = max(len(row[0]) for row in rows)
    value_width = max(len(row[1]) for row in rows)
    total_width = label_width + value_width + 3
    title_border = "+" + "-" * (total_width + 2) + "+"
    row_border = table_border([label_width, value_width])

    print(title_border)
    print(f"| {'Xray VPS Manager'.ljust(total_width)} |")
    print(row_border)
    for row in rows:
        print(table_row(row, [label_width, value_width]))
    print(row_border)
    warning = menu_status.security_audit_header_warning()
    if warning:
        print(red(f"! {warning}"))


def main_menu_actions():
    return [
        ("1", "Клиенты"),
        ("2", "Настройки Xray"),
        ("3", "Безопасность"),
        ("4", "Резервные копии"),
        ("5", "Telegram бот"),
        ("0", "Выход"),
    ]


def telegram_menu_actions():
    return [
        ("1", "Статус бота"),
        ("2", "Первичная настройка"),
        ("3", "Донастроить владельца/чат"),
        ("4", "Включить уведомления"),
        ("5", "Отключить уведомления"),
        ("6", "Изменить маршрут"),
        ("7", "Отправить тестовое сообщение"),
        ("8", "Проверить GeoIP-уведомления сейчас"),
        ("9", "Показать подписки клиентов"),
        ("10", "Обработать сообщения пользователей"),
        ("11", "Проверить напоминания об оплате"),
        ("12", "Настроить оплату и округление"),
        ("13", "Настроить реквизиты оплаты"),
        ("14", "Изменить имя бота"),
        ("15", "Уведомить о работах на сервере"),
        ("16", "Обновить меню команд Telegram"),
        ("0", "Назад"),
    ]


def client_menu_actions():
    return [
        ("1", "Показать клиентов"),
        ("2", "Добавить клиента"),
        ("3", "Изменить срок доступа"),
        ("4", "Изменить статус оплаты"),
        ("5", "Отключить клиента"),
        ("6", "Включить клиента"),
        ("7", "Удалить клиента"),
        ("8", "Вывести ссылку клиента"),
        ("9", "Проверить просроченных клиентов"),
        ("10", "Трафик"),
        ("11", "Журнал активности"),
        ("0", "Назад"),
    ]


def reality_menu_actions():
    return [
        ("1", "Показать подключения"),
        ("2", "Создать подключение"),
        ("3", "Обновить PORT"),
        ("4", "Обновить REALITY_SNI и REALITY_DEST"),
        ("5", "Обновить PORT, REALITY_SNI и REALITY_DEST"),
        ("6", "Обновить FINGERPRINT"),
        ("7", "Удалить подключение"),
        ("0", "Назад"),
    ]


def cascade_menu_actions():
    return [
        ("1", "Добавить/заменить каскад"),
        ("2", "Проверить каскад"),
        ("3", "Отключить каскад"),
        ("0", "Назад"),
    ]


def xray_settings_menu_actions():
    return [
        ("1", "Статус Xray"),
        ("2", "Перезапустить Xray"),
        ("3", "Проверить config.json"),
        ("4", "Проверить timers"),
        ("5", "Прогнать все тесты сервера"),
        ("6", "Настройки Reality"),
        ("7", "Каскад"),
        ("8", "Обновление Xray"),
        ("9", "Стартовая ссылка"),
        ("10", "WARP"),
        ("11", "Показать доступ к торрентам"),
        ("12", "Запретить торренты"),
        ("13", "Разрешить торренты"),
        ("14", "Показать часовой пояс"),
        ("15", "Изменить часовой пояс"),
        ("0", "Назад"),
    ]


def security_menu_actions():
    return [
        ("1", "Проверить безопасность сервера"),
        ("2", "Показать SSH-доступ"),
        ("3", "Отключить вход по паролю SSH"),
        ("4", "Включить вход по паролю SSH"),
        ("0", "Назад"),
    ]


def update_menu_actions():
    return [
        ("1", "Проверить доступность обновления"),
        ("2", "Проверить latest с текущим config.json"),
        ("3", "Обновить Xray"),
        ("4", "Показать бэкапы Xray"),
        ("5", "Откатить Xray к предыдущей версии"),
        ("6", "Обновить geoip/geosite из Xray release"),
        ("7", "Обновить geoip/geosite из Loyalsoldier"),
        ("8", "Обновить geoip.dat из v2fly"),
        ("9", "SQLite: статус базы"),
        ("0", "Назад"),
    ]


def warp_menu_actions():
    return [
        ("1", "Статус WARP"),
        ("2", "Создать WARP outbound"),
        ("3", "Пересоздать WARP профиль"),
        ("4", "Включить WARP для Xray"),
        ("5", "Отключить WARP"),
        ("6", "Проверить WARP"),
        ("7", "Удалить WARP из config.json"),
        ("8", "Проверить, что WARP отключен"),
        ("0", "Назад"),
    ]


def traffic_menu_actions():
    return [
        ("1", "Просмотр трафика"),
        ("2", "Показать лимиты трафика"),
        ("3", "Установить лимит трафика"),
        ("4", "Убрать лимит трафика"),
        ("5", "Проверить лимиты трафика"),
        ("0", "Назад"),
    ]


def traffic_report_actions():
    return [
        ("1", "За день по часам"),
        ("2", "За неделю по дням"),
        ("3", "За месяц по дням"),
        ("4", "За период по дням"),
        ("0", "Назад"),
    ]


def suspicious_menu_actions():
    return [
        ("1", "Сводка suspicious"),
        ("2", "GeoIP-риски подробно"),
        ("3", "Настройки исключений"),
        ("0", "Назад"),
    ]


def activity_exception_menu_actions():
    return [
        ("1", "Показать исключения"),
        ("2", "Добавить из suspicious"),
        ("3", "Добавить вручную"),
        ("4", "Удалить исключение"),
        ("5", "Удалить все исключения"),
        ("0", "Назад"),
    ]


def backup_menu_actions():
    return [
        ("1", "Создать бэкап на сервере"),
        ("2", "Создать бэкап и показать команду скачивания"),
        ("3", "Показать бэкапы на сервере"),
        ("4", "Восстановить из бэкапа на сервере"),
        ("5", "Показать команду загрузки бэкапа на сервер"),
        ("6", "Удалить бэкап"),
        ("0", "Назад"),
    ]


def activity_menu_actions():
    return [
        ("1", "Статус журнала активности"),
        ("2", "Включить парсинг activity log"),
        ("3", "Отключить парсинг activity log"),
        ("4", "Синхронизировать сейчас"),
        ("5", "Отчёт по клиенту"),
        ("6", "Подозрительная активность"),
        ("7", "Экспорт отчёта по клиенту"),
        ("8", "Показать архивы экспорта"),
        ("9", "Удалить архив экспорта"),
        ("10", "Удалить все архивы экспорта"),
        ("11", "Изменить срок хранения журнала"),
        ("12", "Настроить лимиты suspicious"),
        ("13", "GeoIP routing: выбрать регион"),
        ("14", "GeoIP routing: отключить"),
        ("0", "Назад"),
    ]


def print_menu_table(rows):
    headers = ("№", "Действие")
    widths = [
        max(len(headers[0]), *(len(row[0]) for row in rows)),
        max(len(headers[1]), *(len(row[1]) for row in rows)),
    ]
    border = table_border(widths)

    print(border)
    print(table_row(headers, widths))
    print(border)
    for row in rows:
        print(table_row(row, widths))
    print(border)


def action_separator(title):
    line_width = max(60, len(title) + 10)
    side = max(2, (line_width - len(title) - 2) // 2)
    line = "=" * side + f" {title} "
    line += "=" * max(2, line_width - len(line))
    return line


def begin_action(title):
    print()
    print(action_separator(title))
    print()
    sys.stdout.flush()


def end_action(title):
    print()
    print("=" * len(action_separator(title)))
    sys.stdout.flush()


def call(command):
    subprocess.run(command, check=False)


def confirm(message):
    answer = input(f"{message} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def execute_action(title, func):
    begin_action(title)
    try:
        func()
    finally:
        end_action(title)


def client_menu_handlers():
    return {
        "1": ("Показать клиентов", lambda: menu_client_actions.show_clients(call)),
        "2": ("Добавить клиента", lambda: menu_client_actions.add_client_from_menu(call)),
        "3": ("Изменить срок доступа", lambda: menu_client_actions.update_selected_client_days(call)),
        "4": ("Изменить статус оплаты", lambda: menu_client_actions.update_selected_client_payment(call)),
        "5": (
            "Отключить клиента",
            lambda: menu_client_actions.call_client_command(call, "disable", "отключения", "enabled"),
        ),
        "6": (
            "Включить клиента",
            lambda: menu_client_actions.call_client_command(call, "enable", "включения", "disabled"),
        ),
        "7": (
            "Удалить клиента",
            lambda: menu_client_actions.call_client_command(call, "remove", "удаления", "all"),
        ),
        "8": (
            "Вывести ссылку клиента",
            lambda: menu_client_actions.call_client_command(call, "link", "вывода ссылки", "all"),
        ),
        "9": ("Проверить просроченных клиентов", lambda: menu_client_actions.expire_due(call)),
        "10": ("Трафик", open_traffic_tools_menu),
        "11": ("Журнал активности", open_activity_menu),
    }


def reality_menu_handlers():
    return {
        "1": ("Показать подключения", lambda: menu_reality_actions.show_settings(call)),
        "2": ("Создать подключение", lambda: menu_reality_actions.create_connection(call)),
        "3": ("Обновить PORT", menu_reality_actions.update_port),
        "4": ("Обновить REALITY_SNI и REALITY_DEST", menu_reality_actions.update_sni),
        "5": ("Обновить PORT, REALITY_SNI и REALITY_DEST", menu_reality_actions.update_port_and_sni),
        "6": ("Обновить FINGERPRINT", menu_reality_actions.update_fingerprint),
        "7": ("Удалить подключение", lambda: menu_reality_actions.delete_connection(call, confirm)),
    }


def cascade_menu_handlers():
    return {
        "1": ("Добавить/заменить каскад", lambda: menu_xray_actions.add_or_replace_cascade(call)),
        "2": ("Проверить каскад", lambda: menu_xray_actions.test_cascade(call)),
        "3": ("Отключить каскад", lambda: menu_xray_actions.disable_cascade(call)),
    }


def warp_menu_handlers():
    return {
        "1": ("Статус WARP", lambda: menu_xray_actions.warp_status(call)),
        "2": ("Создать WARP outbound", lambda: menu_xray_actions.create_warp_outbound(call)),
        "3": ("Пересоздать WARP профиль", lambda: menu_xray_actions.recreate_warp_profile(call, confirm)),
        "4": ("Включить WARP для Xray", lambda: menu_xray_actions.enable_warp(call)),
        "5": ("Отключить WARP", lambda: menu_xray_actions.disable_warp(call)),
        "6": ("Проверить WARP", lambda: menu_xray_actions.test_warp(call)),
        "7": ("Удалить WARP из config.json", lambda: menu_xray_actions.remove_warp(call)),
        "8": ("Проверить, что WARP отключен", lambda: menu_xray_actions.verify_warp_disabled(call)),
    }


def xray_settings_menu_handlers():
    return {
        "1": ("Статус Xray", lambda: menu_xray_actions.show_xray_status(call)),
        "2": ("Перезапустить Xray", lambda: menu_xray_actions.restart_xray(call)),
        "3": ("Проверить config.json", lambda: menu_xray_actions.check_config(call)),
        "4": ("Проверить timers", lambda: menu_xray_actions.check_timers(call)),
        "5": ("Прогнать все тесты сервера", lambda: menu_xray_actions.run_all_tests(call)),
        "6": ("Настройки Reality", open_reality_menu),
        "7": ("Каскад", open_cascade_menu),
        "8": ("Обновление Xray", open_update_menu),
        "9": ("Вывести стартовую ссылку", menu_xray_actions.print_initial_link),
        "10": ("WARP", open_warp_menu),
        "11": ("Показать доступ к торрентам", menu_xray_actions.print_torrent_status),
        "12": ("Запретить торренты", menu_xray_actions.block_torrents),
        "13": ("Разрешить торренты", menu_xray_actions.allow_torrents),
        "14": ("Показать часовой пояс", lambda: menu_timezone_actions.show_timezone(call)),
        "15": ("Изменить часовой пояс", lambda: menu_timezone_actions.update_timezone(call)),
    }


def security_menu_handlers():
    return {
        "1": (
            "Проверить безопасность сервера",
            lambda: menu_security_actions.run_security_audit(
                menu_status.record_security_audit_run,
                menu_status.format_manager_time,
            ),
        ),
        "2": ("Показать SSH-доступ", menu_security_actions.show_ssh_access),
        "3": ("Отключить вход по паролю SSH", lambda: menu_security_actions.disable_ssh_password_login(confirm)),
        "4": ("Включить вход по паролю SSH", lambda: menu_security_actions.enable_ssh_password_login(confirm)),
    }


def update_menu_handlers():
    return {
        "1": ("Проверить доступность обновления", lambda: menu_xray_actions.check_update(call)),
        "2": ("Проверить latest с текущим config.json", lambda: menu_xray_actions.test_latest(call)),
        "3": ("Обновить Xray", lambda: menu_xray_actions.update_xray(call)),
        "4": ("Показать бэкапы Xray", lambda: menu_xray_actions.show_update_backups(call)),
        "5": ("Откатить Xray к предыдущей версии", lambda: menu_xray_actions.rollback_xray(call, confirm)),
        "6": ("Обновить geoip/geosite из Xray release", lambda: menu_xray_actions.update_assets(call, "xray")),
        "7": ("Обновить geoip/geosite из Loyalsoldier", lambda: menu_xray_actions.update_assets(call, "loyalsoldier")),
        "8": ("Обновить geoip.dat из v2fly", lambda: menu_xray_actions.update_assets(call, "v2fly")),
        "9": ("SQLite: статус базы", lambda: menu_xray_actions.sqlite_status(call)),
    }


def traffic_menu_handlers():
    return {
        "1": ("Просмотр трафика", open_traffic_menu),
        "2": ("Показать лимиты трафика", lambda: menu_client_actions.show_traffic_limits(call)),
        "3": ("Установить лимит трафика", lambda: menu_client_actions.update_selected_client_limit(call)),
        "4": ("Убрать лимит трафика", lambda: menu_client_actions.clear_selected_client_limit(call)),
        "5": ("Проверить лимиты трафика", lambda: menu_client_actions.enforce_traffic_limits(call)),
    }


def traffic_report_handlers(name):
    return {
        "1": ("Трафик за день по часам", lambda: menu_traffic_actions.show_traffic_day(call, name)),
        "2": ("Трафик за неделю по дням", lambda: menu_traffic_actions.show_traffic_week(call, name)),
        "3": ("Трафик за месяц по дням", lambda: menu_traffic_actions.show_traffic_month(call, name)),
        "4": ("Трафик за период по дням", lambda: menu_traffic_actions.show_traffic_period(call, name)),
    }


def backup_menu_handlers():
    return {
        "1": ("Создать бэкап на сервере", lambda: menu_backup_actions.create_backup_server(call)),
        "2": (
            "Создать бэкап и показать команду скачивания",
            lambda: menu_backup_actions.create_backup_download_command(call),
        ),
        "3": ("Показать бэкапы на сервере", lambda: menu_backup_actions.list_backups(call)),
        "4": ("Восстановить из бэкапа на сервере", lambda: menu_backup_actions.restore_backup_from_menu(call, confirm)),
        "5": (
            "Показать команду загрузки бэкапа на сервер",
            lambda: menu_backup_actions.show_backup_upload_command(call),
        ),
        "6": ("Удалить бэкап", lambda: menu_backup_actions.delete_backup_from_menu(call, confirm)),
    }


def telegram_menu_handlers():
    return {
        "1": ("Статус бота", lambda: menu_telegram_actions.show_status(call)),
        "2": ("Первичная настройка", lambda: menu_telegram_actions.setup_bot(call)),
        "3": ("Донастроить владельца/чат", lambda: menu_telegram_actions.configure_owner(call)),
        "4": ("Включить уведомления", lambda: menu_telegram_actions.enable_notifications(call)),
        "5": ("Отключить уведомления", lambda: menu_telegram_actions.disable_notifications(call)),
        "6": ("Изменить маршрут", lambda: menu_telegram_actions.update_route_mode(call)),
        "7": ("Отправить тестовое сообщение", lambda: menu_telegram_actions.send_test_message(call)),
        "8": ("Проверить GeoIP-уведомления сейчас", lambda: menu_telegram_actions.notify_geoip_now(call)),
        "9": ("Показать подписки клиентов", lambda: menu_telegram_actions.show_subscribers(call)),
        "10": ("Обработать сообщения пользователей", lambda: menu_telegram_actions.poll_users(call)),
        "11": ("Проверить напоминания об оплате", lambda: menu_telegram_actions.notify_expiry(call)),
        "12": ("Настроить оплату и округление", lambda: menu_telegram_actions.update_payment_amount(call)),
        "13": ("Настроить реквизиты оплаты", lambda: menu_telegram_actions.update_payment_details(call)),
        "14": ("Изменить имя бота", lambda: menu_telegram_actions.update_bot_name(call)),
        "15": ("Уведомить о работах на сервере", lambda: menu_telegram_actions.send_maintenance_notice(call, confirm)),
        "16": ("Обновить меню команд Telegram", lambda: menu_telegram_actions.update_commands_menu(call)),
    }


def suspicious_menu_handlers():
    return {
        "1": ("Сводка suspicious", lambda: menu_activity_actions.activity_suspicious_report(call)),
        "2": ("GeoIP-риски подробно", lambda: menu_activity_actions.activity_geoip_risk_details(call)),
        "3": ("Настройки исключений", open_activity_exception_menu),
    }


def activity_exception_menu_handlers():
    return {
        "1": ("Показать исключения", lambda: menu_activity_exception_actions.show_activity_exceptions(call)),
        "2": (
            "Добавить из suspicious",
            lambda: menu_activity_exception_actions.activity_exception_add_from_suspicious(
                call,
                menu_activity_actions.ask_activity_days,
            ),
        ),
        "3": ("Добавить вручную", lambda: menu_activity_exception_actions.activity_exception_add_manual(call)),
        "4": (
            "Удалить исключение",
            lambda: menu_activity_exception_actions.activity_exception_delete_from_menu(call),
        ),
        "5": (
            "Удалить все исключения",
            lambda: menu_activity_exception_actions.activity_exception_delete_all_from_menu(call, confirm),
        ),
    }


def activity_menu_handlers():
    return {
        "1": ("Статус журнала активности", lambda: menu_activity_actions.show_activity_status(call)),
        "2": ("Включить парсинг activity log", lambda: menu_activity_actions.enable_activity_parser(call)),
        "3": ("Отключить парсинг activity log", lambda: menu_activity_actions.disable_activity_parser(call)),
        "4": ("Синхронизировать сейчас", lambda: menu_activity_actions.sync_activity_now(call)),
        "5": (
            "Отчёт по клиенту",
            lambda: menu_activity_actions.activity_client_report(menu_client_actions.choose_client, call),
        ),
        "6": ("Подозрительная активность", open_activity_suspicious_menu),
        "7": (
            "Экспорт отчёта по клиенту",
            lambda: menu_activity_export_actions.activity_export_report(menu_client_actions.choose_client, call),
        ),
        "8": ("Показать архивы экспорта", lambda: menu_activity_export_actions.list_activity_exports(call)),
        "9": (
            "Удалить архив экспорта",
            lambda: menu_activity_export_actions.delete_activity_export_from_menu(call, confirm),
        ),
        "10": (
            "Удалить все архивы экспорта",
            lambda: menu_activity_export_actions.delete_all_activity_exports_from_menu(call, confirm),
        ),
        "11": ("Изменить срок хранения журнала", lambda: menu_activity_actions.update_activity_retention(call)),
        "12": ("Настроить лимиты suspicious", lambda: menu_activity_actions.update_activity_risk_limits(call)),
        "13": ("GeoIP routing: выбрать регион", menu_activity_actions.set_xray_geoip_routing_region),
        "14": ("GeoIP routing: отключить", menu_activity_actions.disable_xray_geoip_routing_region),
    }


def print_section_title(title):
    print(f"Раздел: {title}")


def menu_loop(title, rows, handlers, back_label="Назад"):
    while True:
        print()
        print_menu_header()
        print()
        print_section_title(title)
        print()
        print_menu_table(rows)
        choice = input("Выбор: ").strip()
        if not sys.stdin.isatty():
            print()

        if choice == "0":
            return
        if choice in handlers:
            action_title, handler = handlers[choice]
            execute_action(action_title, handler)
        else:
            print(f"Неизвестный пункт меню. 0 - {back_label}.")


def open_clients_menu():
    menu_loop("Клиенты", client_menu_actions(), client_menu_handlers())


def open_reality_menu():
    menu_loop("Настройки Reality", reality_menu_actions(), reality_menu_handlers())


def open_cascade_menu():
    menu_loop("Каскад", cascade_menu_actions(), cascade_menu_handlers())


def open_warp_menu():
    menu_loop("WARP", warp_menu_actions(), warp_menu_handlers())


def open_xray_settings_menu():
    menu_loop("Настройки Xray", xray_settings_menu_actions(), xray_settings_menu_handlers())


def open_security_menu():
    menu_loop("Безопасность", security_menu_actions(), security_menu_handlers())


def open_update_menu():
    menu_loop("Обновление Xray", update_menu_actions(), update_menu_handlers())


def open_backup_menu():
    menu_loop("Резервные копии", backup_menu_actions(), backup_menu_handlers())


def open_telegram_menu():
    menu_loop("Telegram бот", telegram_menu_actions(), telegram_menu_handlers())


def open_activity_suspicious_menu():
    menu_loop("Подозрительная активность", suspicious_menu_actions(), suspicious_menu_handlers())


def open_activity_exception_menu():
    menu_loop("Настройки исключений", activity_exception_menu_actions(), activity_exception_menu_handlers())


def open_activity_menu():
    menu_loop("Журнал активности", activity_menu_actions(), activity_menu_handlers())


def open_client_traffic_menu(name):
    menu_loop(f"Просмотр трафика: {name}", traffic_report_actions(), traffic_report_handlers(name))


def open_traffic_tools_menu():
    menu_loop("Трафик", traffic_menu_actions(), traffic_menu_handlers())


def open_traffic_menu():
    while True:
        print()
        print_menu_header()
        print()
        print_section_title("Просмотр трафика")
        print()
        name = menu_traffic_actions.choose_traffic_client()
        if not name:
            return
        open_client_traffic_menu(name)


def main_menu_handlers():
    return {
        "1": ("Клиенты", open_clients_menu),
        "2": ("Настройки Xray", open_xray_settings_menu),
        "3": ("Безопасность", open_security_menu),
        "4": ("Резервные копии", open_backup_menu),
        "5": ("Telegram бот", open_telegram_menu),
    }


def menu():
    if os.geteuid() != 0:
        die("Run this script as root.")

    handlers = main_menu_handlers()
    while True:
        print()
        print_menu_header()
        print()
        print_section_title("Главное меню")
        print()
        print_menu_table(main_menu_actions())
        choice = input("Выбор: ").strip()
        if not sys.stdin.isatty():
            print()

        if choice == "0":
            return
        if choice in handlers:
            _title, handler = handlers[choice]
            handler()
        else:
            print("Неизвестный пункт меню.")


if __name__ == "__main__":
    menu()
