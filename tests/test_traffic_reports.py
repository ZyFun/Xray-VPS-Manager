from decimal import Decimal
from datetime import date
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

    def test_credential_period_rows_split_multi_credential_client_traffic(self) -> None:
        rows = reports.credential_period_rows(
            [
                {
                    "name": "alice",
                    "protocol": "vless",
                    "security": "reality",
                    "transport": "tcp",
                    "connection": "vless-reality",
                    "status": "enabled",
                },
                {
                    "name": "alice",
                    "protocol": "trojan",
                    "security": "tls/caddy",
                    "transport": "ws",
                    "connection": "trojan-tls",
                    "status": "enabled",
                },
            ],
            {
                "credentials": {
                    "alice": {
                        "vless-reality": {
                            "history": {"2026-06-12": {"08": {"incoming": 1024, "outgoing": 2048}}},
                        },
                        "trojan-tls": {
                            "history": {"2026-06-12": {"09": {"incoming": 4096, "outgoing": 8192}}},
                        },
                    }
                }
            },
            date(2026, 6, 12),
            date(2026, 6, 12),
        )

        self.assertEqual(
            rows,
            [
                ["vless", "reality", "tcp", "vless-reality", "enabled", "1.00KB", "2.00KB", "3.00KB"],
                ["trojan", "tls/caddy", "ws", "trojan-tls", "enabled", "4.00KB", "8.00KB", "12.00KB"],
                ["TOTAL", "-", "-", "-", "-", "5.00KB", "10.00KB", "15.00KB"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
