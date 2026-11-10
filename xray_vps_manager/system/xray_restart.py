"""Ручной перезапуск установленной конфигурации из меню."""

from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.xray.apply import restart_installed_config


def restart():
    with manager_lock(purpose="restart Xray"):
        return restart_installed_config(reason="ручной перезапуск")
