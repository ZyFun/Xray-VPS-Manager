"""Traffic submenu wiring for the interactive Xray menu."""


def handlers(call, open_traffic_menu, update_selected_client_limit, clear_selected_client_limit):
    return {
        "1": ("Просмотр трафика", open_traffic_menu),
        "2": ("Показать лимиты трафика", lambda: call(["xray-client", "limit-list"])),
        "3": ("Установить лимит трафика", update_selected_client_limit),
        "4": ("Убрать лимит трафика", clear_selected_client_limit),
        "5": ("Проверить лимиты трафика", lambda: call(["xray-client", "enforce-limits", "--sync"])),
    }


def report_handlers(name, show_traffic_day, show_traffic_week, show_traffic_month, show_traffic_period):
    return {
        "1": ("Трафик за день по часам", lambda: show_traffic_day(name)),
        "2": ("Трафик за неделю по дням", lambda: show_traffic_week(name)),
        "3": ("Трафик за месяц по дням", lambda: show_traffic_month(name)),
        "4": ("Трафик за период по дням", lambda: show_traffic_period(name)),
    }
