"""Cascade submenu wiring for the interactive Xray menu."""


def handlers(call):
    return {
        "1": ("Добавить/заменить каскад", lambda: call(["xray-set-cascade"])),
        "2": ("Проверить каскад", lambda: call(["xray-set-cascade", "--test"])),
        "3": ("Отключить каскад", lambda: call(["xray-set-cascade", "--disable"])),
    }
