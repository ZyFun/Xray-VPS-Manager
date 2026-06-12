import unittest

from xray_vps_manager.core import terminal


class TerminalTests(unittest.TestCase):
    def test_visible_len_ignores_ansi_color_codes(self) -> None:
        self.assertEqual(terminal.visible_len(terminal.green("enabled")), len("enabled"))

    def test_visible_ljust_pads_by_visible_length(self) -> None:
        padded = terminal.visible_ljust(terminal.red("off"), 5)

        self.assertEqual(terminal.visible_len(padded), 5)
        self.assertTrue(padded.endswith("  "))

    def test_table_lines_align_plain_rows(self) -> None:
        lines = terminal.table_lines(
            ["NAME", "STATUS"],
            [
                ["alice", "enabled"],
                ["bob", "disabled"],
            ],
        )

        self.assertEqual(
            lines,
            [
                "+-------+----------+",
                "| NAME  | STATUS   |",
                "+-------+----------+",
                "| alice | enabled  |",
                "| bob   | disabled |",
                "+-------+----------+",
            ],
        )

    def test_table_lines_keep_alignment_with_colored_cells(self) -> None:
        def color_status(raw: str, padded: str) -> str:
            return terminal.green(padded) if raw == "free" else terminal.yellow(padded)

        lines = terminal.table_lines(
            ["CLIENT", "PAYMENT"],
            [["alice", "free"], ["bob", "paid"]],
            color_columns={1},
            colorizer=color_status,
        )

        self.assertEqual(terminal.ANSI_RE.sub("", lines[3]), "| alice  | free    |")
        self.assertEqual(terminal.ANSI_RE.sub("", lines[4]), "| bob    | paid    |")
        self.assertIn("\033[32m", lines[3])
        self.assertIn("\033[33m", lines[4])

    def test_table_lines_stripe_every_second_data_row_when_ansi_is_enabled(self) -> None:
        lines = terminal.table_lines(
            ["NAME", "STATUS"],
            [
                ["alice", "enabled"],
                ["bob", "disabled"],
                ["carol", "enabled"],
            ],
            enable_ansi=True,
        )

        self.assertNotIn(terminal.ZEBRA_BG, lines[3])
        self.assertIn(terminal.ZEBRA_BG, lines[4])
        self.assertNotIn(terminal.ZEBRA_BG, lines[5])
        self.assertEqual(terminal.ANSI_RE.sub("", lines[4]), "| bob   | disabled |")

    def test_striped_rows_keep_background_after_colored_cell_reset(self) -> None:
        def color_status(raw: str, padded: str) -> str:
            return terminal.yellow(padded) if raw == "paid" else padded

        lines = terminal.table_lines(
            ["CLIENT", "PAYMENT"],
            [["alice", "free"], ["bob", "paid"]],
            color_columns={1},
            colorizer=color_status,
            enable_ansi=True,
        )

        self.assertTrue(lines[4].startswith(terminal.ZEBRA_BG))
        self.assertIn(terminal.yellow("paid   "), lines[4])
        self.assertIn("\033[0m" + terminal.ZEBRA_BG, lines[4])
        self.assertEqual(terminal.ANSI_RE.sub("", lines[4]), "| bob    | paid    |")


if __name__ == "__main__":
    unittest.main()
