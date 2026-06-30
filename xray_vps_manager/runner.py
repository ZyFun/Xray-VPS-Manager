"""Helpers for launching packaged command modules."""

from __future__ import annotations

from importlib import import_module
from typing import Callable


COMMAND_MODULES = {
    "activity": "xray_vps_manager.commands.activity",
    "backup": "xray_vps_manager.commands.backup",
    "bypass": "xray_vps_manager.commands.set_bypass",
    "cascade": "xray_vps_manager.commands.set_cascade",
    "caddy": "xray_vps_manager.commands.caddy",
    "client": "xray_vps_manager.commands.client",
    "manager-update": "xray_vps_manager.commands.manager_update",
    "menu": "xray_vps_manager.commands.menu",
    "sqlite": "xray_vps_manager.commands.sqlite",
    "telegram": "xray_vps_manager.commands.telegram",
    "test": "xray_vps_manager.commands.test",
    "traffic-sync": "xray_vps_manager.commands.traffic_sync",
    "update": "xray_vps_manager.commands.update",
    "warp": "xray_vps_manager.commands.warp",
}


def command_main(command: str) -> Callable[[], object]:
    module_name = COMMAND_MODULES[command]
    module = import_module(module_name)
    if command == "menu":
        return module.menu
    return module.main


def run_command(command: str) -> object:
    return command_main(command)()
