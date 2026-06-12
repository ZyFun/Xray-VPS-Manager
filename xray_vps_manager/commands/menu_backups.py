"""Backup submenu wiring for the interactive Xray menu."""


def handlers(
    call,
    create_backup_server,
    create_backup_download_command,
    restore_backup_from_menu,
    show_backup_upload_command,
    delete_backup_from_menu,
):
    return {
        "1": ("Создать бэкап на сервере", create_backup_server),
        "2": ("Создать бэкап и показать команду скачивания", create_backup_download_command),
        "3": ("Показать бэкапы на сервере", lambda: call(["xray-backup", "list"])),
        "4": ("Восстановить из бэкапа на сервере", restore_backup_from_menu),
        "5": ("Показать команду загрузки бэкапа на сервер", show_backup_upload_command),
        "6": ("Удалить бэкап", delete_backup_from_menu),
    }
