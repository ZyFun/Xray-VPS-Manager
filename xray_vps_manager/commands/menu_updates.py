"""Xray update submenu wiring for the interactive Xray menu."""


def handlers(call, rollback_xray):
    return {
        "1": ("Проверить доступность обновления", lambda: call(["xray-update", "--check"])),
        "2": ("Проверить latest с текущим config.json", lambda: call(["xray-update", "--test-latest"])),
        "3": ("Обновить Xray", lambda: call(["xray-update", "--update"])),
        "4": ("Показать бэкапы Xray", lambda: call(["xray-update", "--backups"])),
        "5": ("Откатить Xray к предыдущей версии", rollback_xray),
        "6": ("Обновить geoip/geosite из Xray release", lambda: call(["xray-update", "--update-assets", "xray"])),
        "7": ("Обновить geoip/geosite из Loyalsoldier", lambda: call(["xray-update", "--update-assets", "loyalsoldier"])),
        "8": ("Обновить geoip.dat из v2fly", lambda: call(["xray-update", "--update-assets", "v2fly"])),
    }
