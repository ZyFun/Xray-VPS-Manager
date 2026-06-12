"""Telegram bot actions used by the interactive menu."""

from __future__ import annotations

import re
from collections.abc import Callable

CommandRunner = Callable[[list[str]], None]
ConfirmCallback = Callable[[str], bool]


def die(message: str) -> None:
    raise SystemExit(message)


def update_route_mode(call: CommandRunner) -> None:
    print("Как Telegram-боту выходить в интернет?")
    print("1) direct: напрямую с этого сервера")
    print("2) cascade: через исходящий сервер, настроенный в каскаде")
    print("Cascade-режим добавит локальный SOCKS inbound 127.0.0.1:10810 только для Telegram Bot API.")
    choice = input("Маршрут [1-direct, 2-cascade]: ").strip() or "1"
    if choice == "1":
        call(["xray-telegram", "mode", "direct"])
    elif choice == "2":
        call(["xray-telegram", "mode", "cascade"])
    else:
        print("Действие отменено: неизвестный маршрут.")


def update_payment_rounding(call: CommandRunner) -> None:
    print("Округление суммы на платного клиента.")
    print("1) Оставить текущую настройку.")
    print("2) Без округления.")
    print("3) Округлять вверх до выбранного шага. Пример: шаг 10 превратит 223.10 в 230.")
    choice = input("Округление [1-оставить]: ").strip() or "1"
    if choice == "1":
        print("Округление не изменено.")
        return
    if choice == "2":
        call(["xray-telegram", "payment-rounding", "none"])
        return
    if choice != "3":
        print("Действие отменено: неизвестный выбор.")
        return
    print("Введите шаг округления. Сумма на клиента будет округляться вверх до кратного шага.")
    print("Примеры: 10, 50, 100.")
    step = input("Шаг округления [10]: ").strip().replace(",", ".") or "10"
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", step):
        die("Payment rounding step must be a positive number.")
    if float(step) <= 0:
        die("Payment rounding step must be greater than zero.")
    call(["xray-telegram", "payment-rounding", "step", step])


def update_payment_amount(call: CommandRunner) -> None:
    call(["xray-telegram", "payment-amount"])
    print("Введите общую сумму оплаты для всех клиентов.")
    print("Введите только число. Пример: 500. Введите 0, чтобы очистить значение.")
    print("Нажмите Enter, чтобы оставить текущую сумму.")
    amount = input("Сумма оплаты: ").strip().replace(",", ".")
    if not amount:
        print("Сумма оплаты не изменена.")
    elif amount == "0":
        call(["xray-telegram", "payment-amount", "0"])
    elif not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", amount):
        die("Payment amount must be a number.")
    else:
        print("Выберите валюту:")
        print("1) Рубли: ₽")
        print("2) Доллары: $")
        print("3) Евро: €")
        choice = input("Валюта [1-рубли]: ").strip() or "1"
        currencies = {
            "1": "₽",
            "2": "$",
            "3": "€",
        }
        symbol = currencies.get(choice)
        if not symbol:
            print("Действие отменено: неизвестная валюта.")
            return
        call(["xray-telegram", "payment-amount", f"{amount} {symbol}"])
    update_payment_rounding(call)


def update_bot_name(call: CommandRunner) -> None:
    print("Имя бота используется в сообщениях пользователям.")
    print("Например: Vireika. Оставь пустым, чтобы не менять.")
    value = input("Имя бота: ").strip()
    if not value:
        print("Имя бота не изменено.")
        return
    call(["xray-telegram", "bot-name", value])


def send_maintenance_notice(call: CommandRunner, confirm: ConfirmCallback) -> None:
    print("Выбери уведомление для подписанных клиентов.")
    call(["xray-telegram", "maintenance-notice", "templates"])
    print("3) Своё сообщение через Telegram админ-панель")
    choice = input("Уведомление [1]: ").strip() or "1"
    if choice == "3":
        print("Для своего сообщения открой Telegram-бота владельцем: /admin -> Уведомления -> Своё сообщение.")
        return
    notices = {
        "1": "start",
        "2": "done",
        "start": "start",
        "done": "done",
    }
    notice = notices.get(choice.lower())
    if not notice:
        print("Действие отменено: неизвестное уведомление.")
        return
    print()
    print("Предпросмотр:")
    call(["xray-telegram", "maintenance-notice", notice, "--dry-run"])
    if not confirm("Отправить это уведомление всем подписанным клиентам?"):
        print("Рассылка отменена.")
        return
    call(["xray-telegram", "maintenance-notice", notice, "--yes"])
