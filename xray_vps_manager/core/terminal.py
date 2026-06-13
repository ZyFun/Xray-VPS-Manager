"""Terminal formatting helpers."""

from __future__ import annotations

import re

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def color(text: object, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def green(text: object) -> str:
    return color(text, "32")


def red(text: object) -> str:
    return color(text, "31")


def yellow(text: object) -> str:
    return color(text, "33")


def visible_len(value: object) -> int:
    return len(ANSI_RE.sub("", str(value)))


def visible_ljust(value: object, width: int) -> str:
    text = str(value)
    return text + " " * max(0, width - visible_len(text))


def table_border(widths: list[int]) -> str:
    return "+" + "+".join("-" * (width + 2) for width in widths) + "+"


def table_row(
    values: list[object],
    widths: list[int],
    color_columns: set[int] | None = None,
    colorizer=None,
    enable_ansi: bool = True,
) -> str:
    color_columns = color_columns or set()
    cells = []
    for index, (value, width) in enumerate(zip(values, widths)):
        raw = str(value)
        text = visible_ljust(raw if enable_ansi else ANSI_RE.sub("", raw), width)
        if enable_ansi and index in color_columns and colorizer:
            text = colorizer(str(value), text)
        cells.append(text)
    return "| " + " | ".join(cells) + " |"


def table_lines(
    headers: list[object],
    rows: list[list[object]],
    color_columns: set[int] | None = None,
    colorizer=None,
    enable_ansi: bool = True,
) -> list[str]:
    all_rows = [headers, *rows]
    widths = [max(visible_len(row[index]) for row in all_rows) for index in range(len(headers))]
    border = table_border(widths)
    lines = [border, table_row(headers, widths, enable_ansi=enable_ansi), border]
    lines.extend(
        table_row(row, widths, color_columns=color_columns, colorizer=colorizer, enable_ansi=enable_ansi)
        for row in rows
    )
    lines.append(border)
    return lines


def print_table(
    headers: list[object],
    rows: list[list[object]],
    empty_message: str | None = "No rows.",
    color_columns: set[int] | None = None,
    colorizer=None,
    enable_ansi: bool = True,
) -> None:
    if not rows and empty_message is not None:
        print(empty_message)
        return
    for line in table_lines(headers, rows, color_columns=color_columns, colorizer=colorizer, enable_ansi=enable_ansi):
        print(line)
