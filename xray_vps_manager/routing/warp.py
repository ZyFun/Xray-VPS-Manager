"""WARP helpers for the routing layer: managed ``/etc/hosts`` pin of the Cloudflare API host.

Pure functions over ``/etc/hosts`` lines; reading, backup and writing the file stay in the caller.
Only a line ending with the managed comment belongs to the manager; other lines are never removed.
"""

from __future__ import annotations


def hosts_line_contains_name(line: str, name: str) -> bool:
    """Host names are case-insensitive, as in the system resolver."""
    parts = line.split("#", 1)[0].split()
    return name.lower() in (part.lower() for part in parts[1:])


def is_managed_hosts_line(line: str, comment: str) -> bool:
    return line.rstrip().endswith(comment)


def foreign_hosts_lines(lines: list[str], name: str, comment: str) -> list[str]:
    """Lines resolving ``name`` that the manager did not write."""
    return [line for line in lines if hosts_line_contains_name(line, name) and not is_managed_hosts_line(line, comment)]


def managed_hosts_line(ip: str, name: str, comment: str) -> str:
    return f"{ip} {name} {comment}"


def unpin_hosts_lines(lines: list[str], comment: str) -> list[str]:
    return [line for line in lines if not is_managed_hosts_line(line, comment)]


def pin_hosts_lines(lines: list[str], ip: str, name: str, comment: str) -> list[str]:
    """Replace the managed pin with ``ip``; all other lines stay in place."""
    return [*unpin_hosts_lines(lines, comment), managed_hosts_line(ip, name, comment)]
