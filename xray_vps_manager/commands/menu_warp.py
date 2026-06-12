"""WARP submenu wiring for the interactive Xray menu."""


def handlers(call, recreate_warp_profile):
    return {
        "1": ("Статус WARP", lambda: call(["xray-warp", "status"])),
        "2": ("Создать WARP outbound", lambda: call(["xray-warp", "create"])),
        "3": ("Пересоздать WARP профиль", recreate_warp_profile),
        "4": ("Включить WARP для Xray", lambda: call(["xray-warp", "enable"])),
        "5": ("Отключить WARP", lambda: call(["xray-warp", "disable"])),
        "6": ("Проверить WARP", lambda: call(["xray-warp", "test"])),
        "7": ("Удалить WARP из config.json", lambda: call(["xray-warp", "remove"])),
        "8": ("Проверить, что WARP отключен", lambda: call(["xray-warp", "verify-disabled"])),
    }
