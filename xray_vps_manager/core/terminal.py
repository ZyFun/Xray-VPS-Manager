"""Terminal formatting helpers."""

from __future__ import annotations

import os
import re
import sys

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
RESET = "\033[0m"
ZEBRA_BG = "\033[48;5;236m"


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


def should_use_ansi(stream=None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    stream = stream or sys.stdout
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def apply_ansi_style(text: str, style: str) -> str:
    if not style:
        return text
    return style + text.replace(RESET, RESET + style) + RESET


def zebra_style(row_index: int | None, enable_ansi: bool | None = None) -> str:
    if row_index is None or row_index % 2 == 0:
        return ""
    if enable_ansi is None:
        enable_ansi = should_use_ansi()
    return ZEBRA_BG if enable_ansi else ""


def table_border(widths: list[int]) -> str:
    return "+" + "+".join("-" * (width + 2) for width in widths) + "+"


def table_row(
    values: list[object],
    widths: list[int],
    color_columns: set[int] | None = None,
    colorizer=None,
    row_index: int | None = None,
    enable_ansi: bool | None = None,
) -> str:
    color_columns = color_columns or set()
    cells = []
    for index, (value, width) in enumerate(zip(values, widths)):
        text = visible_ljust(value, width)
        if index in color_columns and colorizer:
            text = colorizer(str(value), text)
        cells.append(text)
    row = "| " + " | ".join(cells) + " |"
    return apply_ansi_style(row, zebra_style(row_index, enable_ansi))


def table_lines(
    headers: list[object],
    rows: list[list[object]],
    color_columns: set[int] | None = None,
    colorizer=None,
    enable_ansi: bool | None = None,
) -> list[str]:
    all_rows = [headers, *rows]
    widths = [max(visible_len(row[index]) for row in all_rows) for index in range(len(headers))]
    border = table_border(widths)
    lines = [border, table_row(headers, widths), border]
    lines.extend(
        table_row(
            row,
            widths,
            color_columns=color_columns,
            colorizer=colorizer,
            row_index=index,
            enable_ansi=enable_ansi,
        )
        for index, row in enumerate(rows)
    )
    lines.append(border)
    return lines


def print_table(
    headers: list[object],
    rows: list[list[object]],
    empty_message: str | None = "No rows.",
    color_columns: set[int] | None = None,
    colorizer=None,
    enable_ansi: bool | None = None,
) -> None:
    if not rows and empty_message is not None:
        print(empty_message)
        return
    for line in table_lines(headers, rows, color_columns=color_columns, colorizer=colorizer, enable_ansi=enable_ansi):
        print(line)
