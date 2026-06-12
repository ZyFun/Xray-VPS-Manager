"""Telegram message templates."""

from __future__ import annotations

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


def admin_intro_text():
    return (
        "Xray VPS Manager: админ-панель\n\n"
        "Кнопки ниже выполняют проверку, сводку, создание резервной копии и ручные уведомления клиентам."
    )


def admin_notices_intro_text():
    return (
        "Xray VPS Manager: уведомления клиентам\n\n"
        "Выбери готовое сообщение или составь своё. Перед отправкой бот покажет предпросмотр."
    )


def truncate_telegram_text(text, limit=TELEGRAM_MESSAGE_LIMIT):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + "\n\n...вывод сокращён..."


def build_expiry_reminder_message(db, entry, days_before, expiry_local, timezone_label, bot_name, payment_amount_label):
    day_word = "день" if days_before == 1 else "дней"
    return "\n".join(
        [
            f"{bot_name(db)}: напоминание об оплате",
            "",
            f"Через {days_before} {day_word} заканчивается текущий период.",
            f"Доступ до: {expiry_local.strftime('%Y-%m-%d %H:%M')} {timezone_label}",
            f"Сумма оплаты: {payment_amount_label(db)}",
            "",
            "Когда будет удобно, переведи оплату за совместную аренду сервера.",
        ]
    )


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
