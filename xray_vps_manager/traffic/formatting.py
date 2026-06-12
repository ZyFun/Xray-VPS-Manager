"""Human-readable traffic formatting."""

from __future__ import annotations


def format_traffic(value: int | None, none_label: str = "n/a") -> str:
    if value is None:
        return none_label
    value = int(value or 0)
    if value < 1024:
        return "0.00KB"
    units = [
        ("KB", 1024),
        ("MB", 1024**2),
        ("GB", 1024**3),
    ]
    for suffix, size in units:
        next_size = size * 1024
        if value < next_size or suffix == "GB":
            return f"{value / size:.2f}{suffix}"
    return "0.00KB"

