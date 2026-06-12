"""Security submenu wiring for the interactive Xray menu."""


def handlers(
    run_security_audit,
    show_ssh_access,
    disable_ssh_password_login,
    enable_ssh_password_login,
):
    return {
        "1": ("Проверить безопасность сервера", run_security_audit),
        "2": ("Показать SSH-доступ", show_ssh_access),
        "3": ("Отключить вход по паролю SSH", disable_ssh_password_login),
        "4": ("Включить вход по паролю SSH", enable_ssh_password_login),
    }
