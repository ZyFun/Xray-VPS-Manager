"""Telegram inline keyboards."""

from __future__ import annotations


def is_owner_chat(db, chat_id):
    return str(chat_id or "") == str(db.get("chatId") or "")


def is_subscribed_chat(db, chat_id):
    subscriptions = db.get("clientSubscriptions", {})
    return isinstance(subscriptions, dict) and isinstance(subscriptions.get(str(chat_id)), dict)


def subscribed_client_menu_keyboard():
    return [
        [{"text": "Статус подписки", "callback_data": "client:status"}],
        [{"text": "Получить VLESS-ссылку", "callback_data": "client:link"}],
        [{"text": "Статистика трафика", "callback_data": "client:traffic"}],
        [{"text": "Помощь", "callback_data": "client:help"}],
        [{"text": "Отписаться от бота", "callback_data": "client:unsubscribe"}],
    ]


def unsubscribed_client_menu_keyboard():
    return [
        [{"text": "Подключить уведомления", "callback_data": "client:subscribe"}],
        [{"text": "Помощь", "callback_data": "client:help"}],
    ]


def client_menu_keyboard(subscribed=False):
    if subscribed:
        return subscribed_client_menu_keyboard()
    return unsubscribed_client_menu_keyboard()


def client_traffic_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "За сутки", "callback_data": "client:traffic:day"}],
            [{"text": "За сутки по часам", "callback_data": "client:traffic:day-hours"}],
            [{"text": "За неделю по дням", "callback_data": "client:traffic:week-days"}],
            [{"text": "Назад", "callback_data": "client:menu"}],
        ]
    }


def client_keyboard_for_chat(db, chat_id):
    rows = list(client_menu_keyboard(subscribed=is_subscribed_chat(db, chat_id)))
    if is_owner_chat(db, chat_id):
        rows.append([{"text": "Админ-панель", "callback_data": "admin:menu"}])
    return {"inline_keyboard": rows}


def admin_menu_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "Статус бота", "callback_data": "admin:status"},
                {"text": "Подписки клиентов", "callback_data": "admin:subscribers"},
            ],
            [{"text": "Сводка сервера", "callback_data": "admin:daily-summary"}],
            [{"text": "Клиенты", "callback_data": "admin:clients"}],
            [
                {"text": "Проверить GeoIP", "callback_data": "admin:geoip"},
                {"text": "Проверить напоминания", "callback_data": "admin:expiry"},
            ],
            [
                {"text": "Проверка сервера", "callback_data": "admin:test"},
                {"text": "Создать backup", "callback_data": "admin:backup"},
            ],
            [{"text": "Уведомления", "callback_data": "admin:notices"}],
            [{"text": "Клиентское меню", "callback_data": "client:menu"}],
        ]
    }


def admin_clients_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "Продлить подписку", "callback_data": "admin:client-extend"}],
            [{"text": "Назад", "callback_data": "admin:menu"}],
        ]
    }


def admin_client_extend_keyboard(client_names):
    rows = [
        [{"text": str(name), "callback_data": f"admin:client-extend:{index}"}]
        for index, name in enumerate(client_names)
    ]
    rows.append([{"text": "Назад", "callback_data": "admin:clients"}])
    return {"inline_keyboard": rows}


def admin_client_extend_cancel_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "Отмена", "callback_data": "admin:client-extend-cancel"}],
            [{"text": "Назад", "callback_data": "admin:clients"}],
        ]
    }


def admin_notices_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "Плановые работы", "callback_data": "admin:notice:start"}],
            [{"text": "Работы завершены", "callback_data": "admin:notice:done"}],
            [{"text": "Своё сообщение", "callback_data": "admin:notice:custom"}],
            [{"text": "Назад", "callback_data": "admin:menu"}],
        ]
    }


def admin_notice_confirm_keyboard(kind):
    return {
        "inline_keyboard": [
            [
                {"text": "Отправить", "callback_data": f"admin:notice-send:{kind}"},
                {"text": "Отмена", "callback_data": "admin:notice-cancel"},
            ],
            [{"text": "Назад", "callback_data": "admin:notices"}],
        ]
    }
