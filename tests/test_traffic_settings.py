from decimal import Decimal
import unittest

from xray_vps_manager.traffic import settings


class TrafficSettingsTests(unittest.TestCase):
    def test_total_multiplier_defaults_to_disabled_x2(self) -> None:
        env = settings.with_total_multiplier_defaults({})

        self.assertFalse(settings.total_multiplier_enabled(env))
        self.assertEqual(settings.total_multiplier(env), Decimal("2"))
        self.assertEqual(settings.total_multiplier_label(settings.total_multiplier(env)), "x2")

    def test_total_multiplier_accepts_decimal_comma_and_formats_label(self) -> None:
        multiplier = settings.parse_total_multiplier("1,50")

        self.assertEqual(multiplier, Decimal("1.50"))
        self.assertEqual(settings.format_total_multiplier(multiplier), "1.5")
        self.assertEqual(settings.total_multiplier_label(multiplier), "x1.5")

    def test_total_multiplier_rejects_invalid_values(self) -> None:
        for value in ("", "0", "-1", "101", "abc", "NaN"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "MULTIPLIER"):
                    settings.parse_total_multiplier(value)

    def test_multiplied_total_bytes_rounds_to_bytes(self) -> None:
        self.assertEqual(settings.multiplied_total_bytes(3, Decimal("1.5")), 5)


if __name__ == "__main__":
    unittest.main()
