from datetime import datetime
from zoneinfo import ZoneInfo
import unittest

from xray_vps_manager.telegram import traffic


class TelegramTrafficReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = {
            "clientSubscriptions": {
                "222": {
                    "client": "alice",
                    "clientId": "00000000-0000-0000-0000-000000000001",
                }
            }
        }
        self.client_db = {
            "clients": {
                "alice": {"id": "00000000-0000-0000-0000-000000000001"},
            }
        }
        self.traffic_db = {
            "clients": {
                "alice": {
                    "incoming": 3072,
                    "outgoing": 4096,
                    "history": {
                        "2026-06-07": {"00": {"incoming": 1024, "outgoing": 0}},
                        "2026-06-13": {
                            "00": {"incoming": 1024, "outgoing": 2048},
                            "01": {"incoming": 2048, "outgoing": 2048},
                        },
                    },
                }
            }
        }

    def display_timezone(self):
        return ZoneInfo("Europe/Moscow"), "Europe/Moscow"

    def report(self, kind: str):
        return traffic.traffic_report_for_chat(
            self.db,
            "222",
            self.client_db,
            self.traffic_db,
            self.display_timezone,
            kind,
            now=datetime(2026, 6, 13, 12, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        )

    def test_day_report_summarizes_current_day_without_client_name(self) -> None:
        text, parse_mode = self.report("day")

        self.assertEqual(parse_mode, "HTML")
        self.assertIn("Статистика трафика за сутки", text)
        self.assertIn("2026-06-13 Europe/Moscow", text)
        self.assertIn("3.00KB", text)
        self.assertIn("4.00KB", text)
        self.assertIn("7.00KB", text)
        self.assertNotIn("alice", text)

    def test_day_hours_report_contains_hourly_rows(self) -> None:
        text, parse_mode = self.report("day-hours")

        self.assertEqual(parse_mode, "HTML")
        self.assertIn("Статистика трафика за сутки по часам", text)
        self.assertIn("00:00", text)
        self.assertIn("01:00", text)
        self.assertIn("TOTAL", text)

    def test_week_report_contains_seven_day_period(self) -> None:
        text, parse_mode = self.report("week-days")

        self.assertEqual(parse_mode, "HTML")
        self.assertIn("Период: 2026-06-07 - 2026-06-13 Europe/Moscow", text)
        self.assertIn("2026-06-07", text)
        self.assertIn("2026-06-13", text)
        self.assertIn("8.00KB", text)

    def test_report_requires_subscription(self) -> None:
        text, parse_mode = traffic.traffic_report_for_chat(
            {"clientSubscriptions": {}},
            "222",
            self.client_db,
            self.traffic_db,
            self.display_timezone,
            "day",
        )

        self.assertIsNone(parse_mode)
        self.assertIn("Ты пока не подписан", text)


if __name__ == "__main__":
    unittest.main()
