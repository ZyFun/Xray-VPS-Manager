"""Новые швы R1.2 и совместимые политики времени."""
from datetime import date, datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.core import clock
from xray_vps_manager.core import time as legacy_time
from support.sandbox import Sandbox


class ClockTests(unittest.TestCase):
    def test_frozen_nested_exception_restores_clock(self):
        outer = datetime(2026, 10, 24, 22, 30, 0, 123456, tzinfo=timezone.utc)
        with clock.frozen(outer):
            self.assertEqual(clock.now(), outer)
            self.assertEqual(legacy_time.utc_now(), outer)
            self.assertEqual(clock.utc_stamp(), '2026-10-24T22:30:00Z')
            self.assertEqual(clock.today(ZoneInfo('Europe/Moscow')), date(2026, 10, 25))
            with self.assertRaisesRegex(RuntimeError, 'nested'):
                with clock.frozen(datetime(2027, 1, 1, tzinfo=timezone.utc)):
                    self.assertEqual(clock.now().year, 2027)
                    raise RuntimeError('nested')
            self.assertEqual(clock.now(), outer)
        self.assertNotEqual(clock.now(), outer)

    def test_frozen_rejects_naive_time(self):
        with self.assertRaises(ValueError):
            with clock.frozen(datetime(2026, 1, 1)):
                self.fail('naive time accepted')

    def test_local_midnight_uses_supplied_zone(self):
        berlin = ZoneInfo('Europe/Berlin')
        self.assertEqual(clock.local_midnight(date(2026, 3, 29), berlin).utcoffset(), timedelta(hours=1))
        self.assertEqual(clock.local_midnight(date(2026, 3, 30), berlin).utcoffset(), timedelta(hours=2))

    def test_timezone_result_and_legacy_policies(self):
        with Sandbox(tz='Asia/Tokyo'):
            valid = clock.manager_timezone({'MANAGER_TIMEZONE': ' Europe/Moscow '})
            self.assertEqual((valid.name, valid.label, valid.valid), ('Europe/Moscow', 'Europe/Moscow', True))
            self.assertEqual(valid.tz, ZoneInfo('Europe/Moscow'))
            local = clock.manager_timezone({'MANAGER_TIMEZONE': ''})
            self.assertEqual((local.name, local.label, local.valid), ('', 'server local time', True))
            self.assertEqual(local.tz.utcoffset(None), timedelta(hours=9))
            invalid = clock.manager_timezone({'MANAGER_TIMEZONE': 'Mars/Base'})
            self.assertFalse(invalid.valid)
            self.assertEqual(invalid.label, 'server local time')
            utc = clock.manager_timezone({'MANAGER_TIMEZONE': 'Mars/Base'}, invalid='utc')
            self.assertEqual((utc.tz, utc.label), (timezone.utc, 'UTC (invalid MANAGER_TIMEZONE: Mars/Base)'))
            with self.assertRaises(ZoneInfoNotFoundError):
                clock.manager_timezone({'MANAGER_TIMEZONE': 'Mars/Base'}, invalid='raise')

    def test_sandbox_nested_clock_sqlite_and_local_snapshot(self):
        import sqlite3
        from contextlib import closing
        with Sandbox(now='2026-01-15T12:00:00Z', tz='Europe/Berlin') as box:
            outer = clock.now()
            self.assertEqual(clock.server_local_timezone().utcoffset(None), timedelta(hours=1))
            with Sandbox(now='2026-07-15T12:00:00Z', tz='Asia/Tokyo'):
                self.assertEqual(clock.now().month, 7)
                self.assertEqual(clock.server_local_timezone().utcoffset(None), timedelta(hours=9))
            self.assertEqual(clock.now(), outer)
            self.assertEqual(legacy_time.utc_now(), outer)
            with closing(sqlite3.connect(box.paths.manager_db)) as conn:
                self.assertEqual(conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now')").fetchone()[0], clock.utc_stamp())
            self.assertIsInstance(clock.server_local_timezone(), timezone)
