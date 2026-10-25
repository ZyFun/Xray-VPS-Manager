"""103-R01: совместимые записи activity не ждут занятого manager.lock."""
from contextlib import contextmanager
import fcntl
import unittest
from unittest import mock

from xray_vps_manager.activity import cleanup
from xray_vps_manager.core import locks
from support.sandbox import Sandbox


@contextmanager
def occupied_manager_lock(box):
    with box.paths.manager_lock.open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquire = locks.acquire_flock
        # Если сценарий ошибочно запросит manager.lock, тест сразу получит
        # настоящий LockTimeout вместо ожидания штатных 60 секунд.
        with mock.patch.object(locks, 'acquire_flock', side_effect=lambda fd, timeout: acquire(fd, 0)):
            yield


class ActivitySettingsLockingTests(unittest.TestCase):
    def test_settings_cli_succeeds_while_manager_lock_is_occupied(self):
        cases = [
            (('raw-log-rotate-time', '04:00'), 'XRAY_RAW_LOG_ROTATE_TIME', '04:00'),
            (('retention', '30'), 'ACTIVITY_RETENTION_DAYS', '30'),
        ]
        for args, key, value in cases:
            with self.subTest(args=args), Sandbox() as box, occupied_manager_lock(box):
                box.processes.script({
                    ('systemctl', 'daemon-reload'): box.processes.ok(),
                    ('systemctl', 'enable', '--now', 'xray-raw-log-rotate.timer'): box.processes.ok(),
                    ('systemctl', 'restart', 'xray-raw-log-rotate.timer'): box.processes.ok(),
                })
                result = box.run_cli('activity', *args)
                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(box.env()[key], value)
                self.assertNotIn('Traceback', result.stderr)

    def test_cleanup_retention_path_does_not_acquire_manager_lock(self):
        with Sandbox() as box, occupied_manager_lock(box):
            days, removed = cleanup._apply_retention(30)
            self.assertEqual(days, 30)
            self.assertEqual(removed, 0)
            self.assertEqual(box.env()['ACTIVITY_RETENTION_DAYS'], '30')
