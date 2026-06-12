"""Shared rendering helpers for the interactive Xray menu."""

from __future__ import annotations

import sys

from xray_vps_manager.core.terminal import table_border, table_row


def print_header(title: str, rows: list[tuple[str, str]], warning: str = "") -> None:
    label_width = max(len(row[0]) for row in rows)
    value_width = max(len(row[1]) for row in rows)
    total_width = label_width + value_width + 3
    title_border = "+" + "-" * (total_width + 2) + "+"
    row_border = table_border([label_width, value_width])

    print(title_border)
    print(f"| {title.ljust(total_width)} |")
    print(row_border)
    for row in rows:
        print(table_row(row, [label_width, value_width]))
    print(row_border)
    if warning:
        print(warning)


def print_menu_table(rows: list[tuple[str, str]]) -> None:
    headers = ("№", "Действие")
    widths = [
        max(len(headers[0]), *(len(row[0]) for row in rows)),
        max(len(headers[1]), *(len(row[1]) for row in rows)),
    ]
    border = table_border(widths)

    print(border)
    print(table_row(headers, widths))
    print(border)
    for row in rows:
        print(table_row(row, widths))
    print(border)


def action_separator(title: str) -> str:
    line_width = max(60, len(title) + 10)
    side = max(2, (line_width - len(title) - 2) // 2)
    line = "=" * side + f" {title} "
    line += "=" * max(2, line_width - len(line))
    return line


def begin_action(title: str) -> None:
    print()
    print(action_separator(title))
    print()
    sys.stdout.flush()


def end_action(title: str) -> None:
    print()
    print("=" * len(action_separator(title)))
    sys.stdout.flush()


def print_section_title(title: str) -> None:
    print(f"Раздел: {title}")


def menu_loop(title, rows, handlers, header_printer, action_executor, back_label="Назад") -> None:
    while True:
        print()
        header_printer()
        print()
        print_section_title(title)
        print()
        print_menu_table(rows)
        choice = input("Выбор: ").strip()
        if not sys.stdin.isatty():
            print()

        if choice == "0":
            return
        if choice in handlers:
            action_title, handler = handlers[choice]
            action_executor(action_title, handler)
        else:
            print(f"Неизвестный пункт меню. 0 - {back_label}.")
