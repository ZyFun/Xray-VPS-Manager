#!/usr/bin/env python3
"""Caddy command helpers for Xray VPS Manager."""

from __future__ import annotations

import os
import subprocess
import sys

from xray_vps_manager.xray import caddy


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def require_root() -> None:
    if os.geteuid() != 0:
        die("Run this script as root.")


def usage() -> None:
    print("Usage:")
    print("  xray-vps-manager caddy random-tls-run [--domain DOMAIN] [--quiet]")


def cmd_random_tls_run(quiet: bool = False, domain: str | None = None) -> None:
    require_root()
    try:
        result = caddy.apply_random_tls_switch(domain=domain)
    except (FileNotFoundError, ValueError, OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        die(str(exc))
    if quiet:
        return
    previous = caddy.tls_version_label(result.previous_tls_min_version, result.previous_tls_max_version)
    current = caddy.tls_version_label(result.tls_min_version, result.tls_max_version)
    print(f"Caddy TLS randomized for {result.domain}: {previous} -> {current}")
    print(f"Site config: {result.path}")
    if result.backup:
        print(f"Backup: {result.backup}")


def main() -> None:
    args = list(sys.argv[1:])
    if not args or args[0] in ("-h", "--help", "help"):
        usage()
        return
    command = args.pop(0)
    quiet = "--quiet" in args
    args = [item for item in args if item != "--quiet"]
    domain = None
    while args:
        arg = args.pop(0)
        if arg == "--domain":
            if not args:
                die("--domain requires a value.")
            domain = args.pop(0)
            continue
        die(f"Unknown argument: {arg}")
    if command == "random-tls-run":
        cmd_random_tls_run(quiet=quiet, domain=domain)
        return
    die(f"Unknown caddy command: {command}")


if __name__ == "__main__":
    main()
