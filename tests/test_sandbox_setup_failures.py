"""R1.3 / 104-R04: ошибки подготовки не оставляют глобальные подмены."""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.request

from tests.support.restore_sandbox import RestoreSandbox
from tests import test_restart_verification as restart_tests
from xray_vps_manager.core import clock, http, paths, process
from xray_vps_manager.core.server_env import read_server_env


class SandboxSetupFailuresTests(unittest.TestCase):
    def dependencies(self):
        return (subprocess.run, subprocess.Popen, urllib.request.urlopen,
                socket.socket, socket.create_connection, socket.getaddrinfo,
                os.geteuid, paths.paths(), read_server_env.__defaults__)

    def assert_restored(self, original):
        for actual, expected in zip(self.dependencies(), original):
            self.assertIs(actual, expected)
        self.assertEqual(clock.utc_stamp(), '2001-02-03T04:05:06Z')
        response = subprocess.CompletedProcess(['restored'], 0, 'restored', '')
        with mock.patch('subprocess.run', return_value=response):
            self.assertIs(process.run(['restored']), response)
        with mock.patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"restored": true}')):
            self.assertEqual(http.http_json('https://example.invalid/restored'), {'restored': True})

    def test_rollback_helper_restores_context_after_setup_write_failure(self):
        original_write = Path.write_text
        original_config = json.dumps(restart_tests.config_with_port(443))

        def write(path, data, *args, **kwargs):
            if data == original_config:
                raise OSError('setup write failed')
            return original_write(path, data, *args, **kwargs)

        with clock.frozen(datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)):
            original = self.dependencies()
            case = restart_tests.ApplyConfigRollbackTests()
            with mock.patch.object(Path, 'write_text', write):
                with self.assertRaisesRegex(OSError, 'setup write failed'):
                    case.rollback_sandbox(None, restart_tests.FakeClock())
            self.assert_restored(original)

    def test_restore_helper_restores_context_after_setup_patch_failure(self):
        original_patch = mock.patch.object

        def patch(target, name, *args, **kwargs):
            if name == 'sync_traffic':
                raise AttributeError('setup patch failed')
            return original_patch(target, name, *args, **kwargs)

        with clock.frozen(datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)):
            original = self.dependencies()
            with tempfile.TemporaryDirectory() as directory:
                scenario = RestoreSandbox(directory)
                with mock.patch('unittest.mock.patch.object', side_effect=patch):
                    with self.assertRaisesRegex(AttributeError, 'setup patch failed'):
                        scenario.patches()
                self.assert_restored(original)
