"""Helpers for launching packaged command modules."""

from __future__ import annotations

from importlib import import_module
from functools import wraps
import os
import sys
import traceback
from typing import Callable

from xray_vps_manager.core.errors import ManagerError
from xray_vps_manager.commands.arguments import UsageError, validate


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
    "units": "xray_vps_manager.commands.units",
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
    return _boundary(lambda: command_main(command)())


def error_exit(error: ManagerError) -> None:
    """Единое представление фатальной ошибки на границе CLI."""
    print(f"ERROR: {error.message}", file=sys.stderr)
    if error.hint:
        print(error.hint, file=sys.stderr)
    if error.detail and error.detail != error.message:
        print(error.detail, file=sys.stderr)
    if os.environ.get("XRAY_MANAGER_DEBUG") == "1" and error.__traceback__ is not None:
        traceback.print_exception(error, file=sys.stderr)
    raise SystemExit(error.exit_code)


def fail(message, code=1) -> None:
    """Совместимый переходный адаптер старых die(message, code)."""
    error = ManagerError(str(message))
    error.exit_code = code
    error_exit(error)


def require_root(*, error_type=None, message="Run this script as root.") -> None:
    """Одна проверка UID; прямые legacy-вызовы сохраняют тип своей ошибки."""
    if os.geteuid() != 0:
        if error_type is not None:
            raise error_type(message)
        fail(message)


def _boundary(action):
    try:
        return action()
    except ManagerError as exc:
        error_exit(exc)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except SystemExit as exc:
        # Числовой статус отчёта/legacy-адаптера не является новой ошибкой.
        if exc.code is not None and not isinstance(exc.code, int):
            fail(exc.code)
        raise


_ROOT_COMMANDS = frozenset({"activity", "backup", "bypass", "cascade", "client",
                            "menu", "telegram", "traffic-sync", "update", "warp"})
_HELP_WITHOUT_ARGUMENTS = frozenset({"backup", "caddy", "client", "manager-update", "sqlite", "units", "update"})


def command_entry(command):
    """Одинаковая граница для обёртки, Sandbox и прямого вызова main/menu."""
    def decorate(main):
        @wraps(main)
        def entry():
            return _boundary(lambda: _execute(command, main))
        return entry
    return decorate


def _show_help(command):
    module = import_module(COMMAND_MODULES[command])
    show = module.print_usage if command in ("bypass", "cascade") else module.usage
    result = show()
    if result is not None:
        print(result)


def _execute(command, main):
    args = sys.argv[1:]
    if args in (["--help"], ["-h"], ["help"]) or not args and command in _HELP_WITHOUT_ARGUMENTS:
        return _show_help(command)
    try:
        validate(command, args)
    except UsageError as exc:
        exc.hint = f"Usage: xray-vps-manager {command} [ARGS...] (use --help)"
        raise
    if (command in _ROOT_COMMANDS or command == "caddy"
            or command == "manager-update" and any(arg.split('=', 1)[0] in ("--update", "--rollback") for arg in args)):
        require_root()
    return main()
