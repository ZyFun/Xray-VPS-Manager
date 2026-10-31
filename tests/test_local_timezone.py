"""B1.1 / XR-REV-065: реальные TZif, переходы DST и обратимые подмены."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
from zoneinfo import TZPATH, ZoneInfo

from tests.support.sandbox import Sandbox
from xray_vps_manager.clients.runtime import format_time
from xray_vps_manager.core import clock
from xray_vps_manager.core.paths import paths, use


@contextmanager
def preserved_system_timezone():
    previous = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)
        time.tzset()


class ServerLocalTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.localtime = Path(self.tmp.name) / 'localtime'
        path_context = use(replace(paths(), localtime=self.localtime))
        path_context.__enter__()
        self.addCleanup(path_context.__exit__, None, None, None)
        self.enterContext(preserved_system_timezone())

    def berlin_file(self):
        for root in TZPATH:
            source = Path(root) / 'Europe/Berlin'
            if source.is_file():
                self.localtime.write_bytes(source.read_bytes())
                return
        self.fail('Для регрессии нужен системный TZif Europe/Berlin')

    def assert_berlin(self, zone):
        winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
        self.assertEqual(winter.astimezone(zone).utcoffset(), timedelta(hours=1))
        self.assertEqual(summer.astimezone(zone).utcoffset(), timedelta(hours=2))
        self.assertEqual(format_time(winter.isoformat(), zone), '2026-01-15 13:00 CET')
        self.assertEqual(format_time(summer.isoformat(), zone), '2026-07-15 14:00 CEST')

    def test_localtime_has_priority_over_tz_and_preserves_dst(self):
        self.berlin_file()
        os.environ['TZ'] = 'Asia/Tokyo'
        time.tzset()
        with clock.frozen(datetime(2026, 7, 15, tzinfo=timezone.utc)):
            self.assert_berlin(clock.server_local_timezone())

    def test_unchanged_localtime_is_parsed_once_and_keeps_dst(self):
        self.berlin_file()
        original_open = Path.open
        with mock.patch.object(Path, 'open', autospec=True, side_effect=original_open) as opened:
            first = clock.server_local_timezone()
            second = clock.server_local_timezone()
        self.assertIs(first, second)
        self.assertEqual(opened.call_count, 1)
        self.assert_berlin(second)

    def test_replaced_localtime_invalidates_cached_zone(self):
        self.berlin_file()
        first = clock.server_local_timezone()
        old_stat = self.localtime.stat()
        source = next(Path(root) / 'Asia/Tokyo' for root in TZPATH
                      if (Path(root) / 'Asia/Tokyo').is_file())
        replacement = self.localtime.with_name('replacement')
        replacement.write_bytes(source.read_bytes())
        os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        replacement.replace(self.localtime)
        second = clock.server_local_timezone()
        self.assertIsNot(first, second)
        self.assertEqual(datetime(2026, 1, 15, tzinfo=second).utcoffset(), timedelta(hours=9))
        self.assertIs(second, clock.server_local_timezone())

    def test_removed_localtime_does_not_return_cached_zone(self):
        self.berlin_file()
        self.assert_berlin(clock.server_local_timezone())
        self.localtime.unlink()
        os.environ['TZ'] = 'Asia/Tokyo'
        time.tzset()
        self.assertEqual(clock.server_local_timezone(), ZoneInfo('Asia/Tokyo'))

    def test_missing_localtime_uses_tz(self):
        os.environ['TZ'] = 'Europe/Berlin'
        time.tzset()
        self.assert_berlin(clock.server_local_timezone())

    def test_unreadable_localtime_uses_tz(self):
        self.berlin_file()
        os.environ['TZ'] = 'Asia/Tokyo'
        time.tzset()
        with mock.patch.object(Path, 'open', side_effect=PermissionError('test')) as opened:
            self.assertEqual(clock.server_local_timezone(), ZoneInfo('Asia/Tokyo'))
        opened.assert_called_once_with('rb')

    def test_corrupt_localtime_uses_tz(self):
        os.environ['TZ'] = ':Europe/Berlin'
        time.tzset()
        for data in (b'', b'not tzif', b'TZif2', b'TZif2' + bytes(39)):
            with self.subTest(data=data):
                self.localtime.write_bytes(data)
                self.assert_berlin(clock.server_local_timezone())

    def test_truncated_localtime_without_posix_footer_uses_tz(self):
        self.berlin_file()
        data, rule, end = self.localtime.read_bytes().rsplit(b'\n', 2)
        self.assertTrue(rule)
        self.assertEqual(end, b'')
        self.localtime.write_bytes(data)
        os.environ['TZ'] = 'UTC'
        time.tzset()
        self.assertEqual(clock.manager_timezone({'MANAGER_TIMEZONE': ''}).tz, ZoneInfo('UTC'))

    def test_invalid_or_missing_tz_uses_last_fixed_offset(self):
        for name in ('Mars/Base', '/invalid/zone', ''):
            with self.subTest(name=name), clock.frozen(datetime(2026, 7, 15, tzinfo=timezone.utc)):
                os.environ['TZ'] = name
                time.tzset()
                expected = clock.now().astimezone().utcoffset()
                zone = clock.server_local_timezone()
                self.assertIsInstance(zone, timezone)
                self.assertEqual(zone.utcoffset(None), expected)

    def test_os_error_loading_tz_uses_last_fixed_offset(self):
        os.environ['TZ'] = 'Europe/Berlin'
        time.tzset()
        for error in (IsADirectoryError, PermissionError):
            with self.subTest(error=error.__name__), mock.patch.object(clock, 'timezone_named', side_effect=error('test')):
                zone = clock.server_local_timezone()
                self.assertIsInstance(zone, timezone)
                self.assertEqual(zone.utcoffset(None), clock.now().astimezone().utcoffset())

    def test_posix_tz_uses_fixed_offset_when_no_iana_zone_exists(self):
        os.environ['TZ'] = 'CET-1CEST,M3.5.0,M10.5.0/3'
        time.tzset()
        with clock.frozen(datetime(2026, 7, 15, tzinfo=timezone.utc)):
            zone = clock.server_local_timezone()
            self.assertIsInstance(zone, timezone)
            self.assertEqual(zone.utcoffset(None), timedelta(hours=2))

    def test_nested_sandbox_restores_zone_clock_and_environment(self):
        before = dict(os.environ)
        with clock.frozen(datetime(2026, 1, 1, tzinfo=timezone.utc), local_timezone=ZoneInfo('UTC')):
            with Sandbox(tz='Europe/Berlin', now='2026-07-15T12:00:00Z'):
                outer = clock.now()
                self.assert_berlin(clock.server_local_timezone())
                with self.assertRaisesRegex(RuntimeError, 'restore'):
                    with Sandbox(tz='Asia/Tokyo', now='2026-01-15T12:00:00Z'):
                        self.assertEqual(clock.server_local_timezone(), ZoneInfo('Asia/Tokyo'))
                        raise RuntimeError('restore')
                self.assertEqual(clock.now(), outer)
                self.assertEqual(os.environ['TZ'], 'Europe/Berlin')
                self.assert_berlin(clock.server_local_timezone())
            self.assertEqual(clock.now(), datetime(2026, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(clock.server_local_timezone(), ZoneInfo('UTC'))
        self.assertEqual(dict(os.environ), before)

    def test_invalid_zone_paths_also_return_marked_local_time(self):
        with Sandbox(tz='Europe/Berlin'):
            for name in ('Mars/Base', '/etc/passwd', '../Europe/Berlin', 'Europe/Moscow # comment'):
                with self.subTest(name=name):
                    result = clock.manager_timezone({'MANAGER_TIMEZONE': name})
                    self.assertFalse(result.valid)
                    self.assertEqual(result.name, name)
                    self.assertEqual(result.label, f'MANAGER_TIMEZONE invalid: {name} (server local time)')
                    self.assert_berlin(result.tz)
                    with self.assertRaises(ValueError):
                        clock.normalize_timezone_name(name)


class TimezoneReadSafetyTests(unittest.TestCase):
    def test_invalid_configuration_reads_leave_state_and_locks_unchanged(self):
        from xray_vps_manager.activity.client_reports import activity_display_timezone
        from xray_vps_manager.clients import settings
        from xray_vps_manager.commands import menu_status, telegram
        from xray_vps_manager.core.time import manager_timezone
        with Sandbox(tz='Europe/Berlin') as box:
            box.paths.server_env.write_text('MANAGER_TIMEZONE=Mars/Base\n')
            before = box.db_rows('clients', 'reality_connections')
            env = box.paths.server_env.read_bytes()
            config = box.paths.config.read_bytes()
            self.assertFalse(box.paths.manager_lock.exists())
            for read in (activity_display_timezone, settings.manager_timezone,
                         settings.manager_timezone_label, menu_status.manager_timezone,
                         menu_status.manager_timezone_label, telegram.display_timezone,
                         manager_timezone):
                read()
            result = box.run_cli('client', 'timezone')
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertIn('MANAGER_TIMEZONE invalid: Mars/Base (server local time)', result.stdout)
            self.assertEqual(box.db_rows('clients', 'reality_connections'), before)
            self.assertEqual(box.paths.server_env.read_bytes(), env)
            self.assertEqual(box.paths.config.read_bytes(), config)
            self.assertFalse(box.paths.manager_lock.exists())
            self.assertEqual(box.processes.calls, [])

    def test_named_zone_os_errors_keep_read_and_strict_cli_contracts(self):
        with Sandbox(tz='Europe/Berlin') as box:
            box.paths.server_env.write_text('MANAGER_TIMEZONE=Europe\n')
            before = box.db_rows('clients', 'reality_connections')
            env = box.paths.server_env.read_bytes()
            for error in (IsADirectoryError, PermissionError):
                with self.subTest(error=error.__name__), mock.patch.object(clock, 'timezone_named', side_effect=error('test')):
                    result = clock.manager_timezone({'MANAGER_TIMEZONE': 'Europe'})
                    self.assertFalse(result.valid)
                    self.assertEqual(result.label, 'MANAGER_TIMEZONE invalid: Europe (server local time)')
                    self.assertEqual(result.tz, box.tz)
                    with self.assertRaises(ValueError) as caught:
                        clock.normalize_timezone_name('Europe')
                    self.assertIsInstance(caught.exception.__cause__, error)
                    read = box.run_cli('client', 'timezone')
                    self.assertEqual(read.exit_code, 0, read.stderr)
                    self.assertIn(result.label, read.stdout)
                    write = box.run_cli('client', 'set-timezone', 'Europe')
                    self.assertEqual(write.exit_code, 1)
                    self.assertEqual(write.stdout, '')
                    self.assertEqual(write.stderr, 'ERROR: MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time.\n')
                    self.assertEqual(box.paths.server_env.read_bytes(), env)
                    self.assertEqual(box.db_rows('clients', 'reality_connections'), before)
                    self.assertFalse(box.paths.manager_lock.exists())
                    self.assertEqual(box.processes.calls, [])

    def test_set_timezone_rejects_invalid_new_value_before_lock_or_writes(self):
        with Sandbox() as box:
            before = box.db_rows('clients', 'reality_connections')
            env = box.paths.server_env.read_bytes()
            for name in ('Mars/Base', '/etc/passwd', '../Europe/Berlin'):
                with self.subTest(name=name):
                    result = box.run_cli('client', 'set-timezone', name)
                    self.assertEqual(result.exit_code, 1)
                    self.assertIn('ERROR: MANAGER_TIMEZONE must be an IANA timezone', result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(box.paths.server_env.read_bytes(), env)
                    self.assertEqual(box.db_rows('clients', 'reality_connections'), before)
                    self.assertFalse(box.paths.manager_lock.exists())
                    self.assertEqual(box.processes.calls, [])
