"""Контракты парсеров до переноса R1.2; ожидания одинаковы на 3.9 и 3.12."""
from datetime import datetime, timedelta, timezone
import unittest

from xray_vps_manager.core import time as core_time
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.commands import telegram
from xray_vps_manager.clients import runtime
from support.sandbox import Sandbox


class TimeContractTests(unittest.TestCase):
    def test_common_parsers_keep_utc_and_whitespace_contract(self):
        for parser in (core_time.parse_time, activity_time.parse_time, telegram.parse_time):
            for raw in (None, "", "   ", "never", "unknown", "broken"):
                with self.subTest(parser=parser.__module__, raw=raw):
                    self.assertIsNone(parser(raw))
            expected = datetime(2026, 6, 12, 8, 0, 0, 123456, tzinfo=timezone.utc)
            for raw in (" 2026-06-12T11:00:00.123456+03:00 ", "2026-06-12T08:00:00.123456Z", "2026-06-12T08:00:00.123456"):
                parsed = parser(raw)
                self.assertEqual(parsed, expected)
                self.assertIs(parsed.tzinfo, timezone.utc)

    def test_runtime_parser_preserves_offset_and_rejects_outer_spaces(self):
        for raw in (None, "", "never", "unknown", " 2026-06-12T08:00:00Z "):
            self.assertIsNone(runtime.parse_time(raw))
        parsed = runtime.parse_time("2026-06-12T11:00:00+03:00")
        self.assertEqual(parsed.hour, 11)
        self.assertEqual(parsed.utcoffset(), timedelta(hours=3))
        self.assertEqual(runtime.parse_time("2026-06-12T08:00:00Z").tzinfo, timezone.utc)
        self.assertEqual(runtime.parse_time("2026-06-12T08:00:00").tzinfo, timezone.utc)
        self.assertIsNone(runtime.parse_time("2026-06-12Z08:00:00Z"))

    def test_legacy_now_precision_and_stamps(self):
        with Sandbox(now="2026-06-12T08:00:00.123456Z"):
            self.assertEqual(core_time.utc_now().microsecond, 123456)
            self.assertEqual(activity_time.utc_now().microsecond, 0)
            self.assertEqual(telegram.utc_now().microsecond, 0)
            for stamp in (core_time.utc_stamp, activity_time.utc_stamp, telegram.utc_stamp):
                self.assertEqual(stamp(), "2026-06-12T08:00:00Z")
