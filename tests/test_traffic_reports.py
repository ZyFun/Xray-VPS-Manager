from decimal import Decimal
import unittest

from xray_vps_manager.traffic import reports


class TrafficReportsTests(unittest.TestCase):
    def test_total_summary_hides_multiplied_total_when_disabled(self) -> None:
        rows = reports.total_summary_rows(3 * 1024**3, multiplier_enabled=False, multiplier=Decimal("2"))

        self.assertEqual(
            rows,
            [
                ["TOTAL", "3.00GB"],
                ["Множитель x2", "Выкл"],
            ],
        )

    def test_total_summary_shows_multiplied_total_when_enabled(self) -> None:
        rows = reports.total_summary_rows(3 * 1024**3, multiplier_enabled=True, multiplier=Decimal("2"))

        self.assertEqual(
            rows,
            [
                ["TOTAL", "3.00GB"],
                ["TOTAL x2", "6.00GB"],
                ["Множитель x2", "Вкл"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
