"""CLI синхронизации канонических systemd unit-файлов."""
import sys

from xray_vps_manager.core.errors import ManagerError
from xray_vps_manager.system.unit_sync import sync_units

from xray_vps_manager.runner import command_entry, error_exit


def usage():
    print("Usage: xray-vps-manager units sync [--no-systemctl] [--check]")


@command_entry('units')
def main():
    args = sys.argv[1:]
    if not args or args in (["--help"], ["-h"]):
        usage()
        return
    if args[0] != "sync" or any(arg not in ("--no-systemctl", "--check") for arg in args[1:]):
        usage()
        raise SystemExit(1)
    check = "--check" in args
    try:
        changed = sync_units(systemctl="--no-systemctl" not in args, check=check,
                             on_warning=lambda message: print(message, file=sys.stderr))
    except ManagerError as exc:
        error_exit(exc)
    if check:
        print("Отличающиеся systemd units: " + ", ".join(changed) if changed else "Systemd units совпадают с каноном.")
        if changed:
            raise SystemExit(1)
    else:
        print("Обновлены systemd units: " + ", ".join(changed) if changed else "Systemd units уже актуальны.")
