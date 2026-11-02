#!/usr/bin/env python3
"""Unified CLI entrypoint for Xray VPS Manager."""

from __future__ import annotations

import sys

from xray_vps_manager.runner import COMMAND_MODULES, error_exit, run_command
from xray_vps_manager.commands.arguments import UsageError


ALIASES = {
    "set-bypass": "bypass",
    "set-cascade": "cascade",
    "traffic": "traffic-sync",
    "sync": "traffic-sync",
}


def usage() -> None:
    commands = ", ".join(sorted(COMMAND_MODULES))
    print("Usage: xray-vps-manager COMMAND [ARGS...]")
    print()
    print(f"Commands: {commands}")
    print()
    print("Compatibility command names remain available: xray-menu, xray-client, xray-telegram, ...")


def main() -> None:
    args = sys.argv[1:]
    if not args or args in (["-h"], ["--help"], ["help"]):
        usage()
        return
    command = ALIASES.get(sys.argv[1], sys.argv[1])
    if command not in COMMAND_MODULES:
        error_exit(UsageError(f"Unknown command: {sys.argv[1]}",
                              hint="Usage: xray-vps-manager COMMAND [ARGS...] (use --help)"))
    sys.argv = [f"xray-{command}", *sys.argv[2:]]
    run_command(command)


if __name__ == "__main__":
    main()
