"""Telegram message templates."""

from __future__ import annotations

from xray_vps_manager.telegram import payments

TELEGRAM_MESSAGE_LIMIT = 3900

MAINTENANCE_NOTICE_TEMPLATES = {
    "start": {
        "title": "Плановые работы",
        "lines": [
            "{bot}: плановые работы",
            "",
            "Сейчас я обновляю настройки сервера.",
            "Во время работ VPN может кратковременно переподключаться.",
            "",
            "Ничего делать не нужно. Если связь пропадёт, подключение можно повторить через пару минут.",
        ],
    },
    "done": {
        "title": "Работы завершены",
        "lines": [
            "{bot}: работы завершены",
            "",
            "Обновление настроек сервера завершено.",
            "VPN должен работать в обычном режиме.",
            "",
            "Если связь не восстановилась автоматически, можно переподключиться вручную.",
        ],
    },
}


def subscription_intro_text(db, bot_name):
    return (
        f"Привет. Я бот {bot_name(db)}.\n\n"
        "Я помогу не забыть о переводе за совместную аренду сервера.\n\n"
        "Чтобы подключить напоминания, нажми кнопку «Подключить уведомления» "
        "или просто отправь сюда свою VLESS Reality-ссылку. "
        "По ней я определю твой ключ и включу уведомления."
    )


def subscribe_prompt_text():
    return (
        "Отправь сюда свою VLESS Reality-ссылку целиком.\n\n"
        "Я определю ключ пользователя по параметрам ссылки и включу уведомления."
    )


def client_help_text(db, bot_name):
    return (
        f"{bot_name(db)}: помощь\n\n"
        "Я рядом, чтобы было проще следить за доступом к VPN.\n\n"
        "Здесь можно:\n"
        "• проверить статус подписки;\n"
        "• получить актуальную VLESS-ссылку;\n"
        "• посмотреть статистику трафика;\n"
        "• отписаться от бота, если уведомления больше не нужны.\n\n"
        "Обычно я напоминаю об оплате в 08:00 за 5 дней и за 1 день до окончания доступа.\n\n"
        "Иногда я также присылаю технические уведомления: например, если на сервере проводятся работы "
        "или возможны краткие перебои связи."
    )


def admin_intro_text():
    return (
        "Xray VPS Manager: админ-панель\n\n"
        "Выбери раздел ниже."
    )


def admin_status_intro_text():
    return (
        "Xray VPS Manager: статус\n\n"
        "Здесь собраны проверки сервера, сводка и ручной запуск напоминаний."
    )


def admin_clients_intro_text():
    return (
        "Xray VPS Manager: клиенты\n\n"
        "Здесь можно посмотреть Telegram-подписки клиентов и продлить доступ."
    )


def admin_payments_intro_text():
    return (
        "Xray VPS Manager: платежи\n\n"
        "Здесь показаны сохранённая сумма аренды, расчёт на клиента и округление."
    )


def admin_notices_intro_text():
    return (
        "Xray VPS Manager: уведомления клиентам\n\n"
        "Выбери готовое сообщение или составь своё. Перед отправкой бот покажет предпросмотр."
    )


def admin_backups_intro_text():
    return (
        "Xray VPS Manager: бэкапы\n\n"
        "Здесь можно создать резервную копию на сервере."
    )


def admin_activity_intro_text():
    return (
        "Xray VPS Manager: активность\n\n"
        "Здесь можно вручную проверить новые GeoIP-предупреждения."
    )


def admin_settings_intro_text():
    return (
        "Xray VPS Manager: настройки бота\n\n"
        "Здесь собрана служебная информация о Telegram-боте."
    )


def truncate_telegram_text(text, limit=TELEGRAM_MESSAGE_LIMIT):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + "\n\n...вывод сокращён..."


def build_expiry_reminder_message(db, entry, days_before, expiry_local, timezone_label, bot_name, payment_amount_label):
    day_word = "день" if days_before == 1 else "дней"
    lines = [
        f"{bot_name(db)}: напоминание об оплате",
        "",
        f"Через {days_before} {day_word} заканчивается текущий период.",
        f"Доступ до: {expiry_local.strftime('%Y-%m-%d %H:%M')} {timezone_label}",
        f"Сумма оплаты: {payment_amount_label(db)}",
    ]
    lines.extend(payments.payment_transfer_message_lines(db))
    lines.extend(
        [
            "",
            "Когда будет удобно, переведи оплату за совместную аренду сервера.",
        ]
    )
    return "\n".join(lines)


def build_access_updated_message(db, entry, bot_name, format_access_until):
    return "\n".join(
        [
            f"{bot_name(db)}: всё готово",
            "",
            "Спасибо! Оплата за совместную аренду сервера получена.",
            "",
            f"Доступ продлён до: {format_access_until(entry.get('expiresAt', ''))}",
        ]
    )


def normalize_maintenance_template_id(value):
    raw = str(value or "").strip().lower()
    aliases = {
        "": "start",
        "1": "start",
        "start": "start",
        "begin": "start",
        "maintenance": "start",
        "2": "done",
        "done": "done",
        "finish": "done",
        "complete": "done",
    }
    return aliases.get(raw, raw)


def maintenance_notice_message(db, template_id, bot_name):
    template_id = normalize_maintenance_template_id(template_id)
    template = MAINTENANCE_NOTICE_TEMPLATES.get(template_id)
    if not template:
        raise ValueError("Неизвестный шаблон уведомления о работах.")
    return "\n".join(line.format(bot=bot_name(db)) for line in template["lines"])
