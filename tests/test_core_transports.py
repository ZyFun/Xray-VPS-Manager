"""R1.3: канонические процессы, systemd, HTTP и вложенные песочницы."""
from contextlib import redirect_stdout, redirect_stderr
import io
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox, UnexpectedCommand
from xray_vps_manager.commands import manager_update, update, warp
from xray_vps_manager.core import process, systemd, http
from xray_vps_manager.core.errors import ExternalCommandError


class ProcessTests(unittest.TestCase):
    def test_fake_nested_context_restores_outer_on_error_and_records_unknown(self):
        def answer(command):
            return subprocess.CompletedProcess(command, 0, 'outer', '')
        with process.fake(answer) as outer:
            self.assertEqual(process.run(['one']).stdout, 'outer')
            with self.assertRaises(process.UnexpectedProcessError):
                with process.fake(lambda command: None) as inner:
                    process.run(['unknown'])
            self.assertEqual(inner, [['unknown']])
            self.assertEqual(process.run_capture(['two']).stdout, 'outer')
        self.assertEqual(outer, [['one'], ['two']])
        with mock.patch.object(subprocess, 'run', return_value='restored') as run:
            self.assertEqual(process.run(['real-seam']), 'restored')
        run.assert_called_once()

    def test_check_is_external_error_and_legacy_called_process_error(self):
        with Sandbox() as box:
            box.processes.script({('tool',): box.processes.result(7, 'stdout', 'stderr')})
            with self.assertRaises(ExternalCommandError) as caught:
                process.run(['tool'], check=True)
            error = caught.exception
            self.assertIsInstance(error, subprocess.CalledProcessError)
            self.assertEqual((error.command, error.cmd, error.returncode, error.stdout, error.stderr),
                             (['tool'], ['tool'], 7, 'stdout', 'stderr'))
            self.assertEqual(error.detail, 'stderr')
            self.assertEqual(error.message, 'stderr')

    def test_stream_outputs_and_timeout_without_popen(self):
        with Sandbox() as box:
            timeout = subprocess.TimeoutExpired(['stream'], 2, output='partial')
            box.processes.script({('stream',): [box.processes.result(9, 'out', 'err'), timeout]})
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(process.run_stream(['stream'], timeout=2), 9)
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('out', 'err'))
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                process.run_stream(['stream'], timeout=2)
            self.assertIs(caught.exception, timeout)
            self.assertEqual(box.processes.requests[0][1], {'text': True, 'timeout': 2})
            self.assertEqual(box.processes.unexpected, [])

    def test_callable_fake_streams_text_and_preserves_capture(self):
        with process.fake(lambda argv: subprocess.CompletedProcess(argv, 4, 'out', 'err')):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(process.run_stream(['tool']), 4)
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('out', 'err'))
            result = process.run_capture(['tool'])
            self.assertEqual((result.stdout, result.stderr), ('out', 'err'))
            with self.assertRaises(subprocess.CalledProcessError):
                process.run_capture(['tool'], check=True)

    def test_nested_sandbox_processes_restore_outer_and_detect_unknown(self):
        with Sandbox() as outer:
            outer.processes.script({('tool',): outer.processes.ok('outer')})
            self.assertEqual(process.run(['tool']).stdout, 'outer')
            with self.assertRaisesRegex(AssertionError, 'Незапланированные'):
                with Sandbox() as inner:
                    inner.processes.script({('tool',): inner.processes.ok('inner')})
                    self.assertEqual(process.run(['tool']).stdout, 'inner')
                    with self.assertRaises(UnexpectedCommand):
                        process.run_stream(['unknown'])
            self.assertEqual(process.run(['tool']).stdout, 'outer')
            self.assertEqual(outer.processes.calls, [('tool',), ('tool',)])


class SystemdTests(unittest.TestCase):
    def test_lists_and_legacy_restart_are_same_objects(self):
        self.assertIs(process.restart_systemd_unit, systemd.restart_unit)
        self.assertIs(process.SystemdUnitStartError, systemd.SystemdUnitStartError)
        self.assertIs(process.WRITER_STOP_UNITS, systemd.MANAGER_UNITS)
        self.assertIs(manager_update.MANAGER_SYSTEMD_UNITS, systemd.MANAGER_UNITS)
        self.assertEqual(systemd.DEFAULT_VERIFY_TIMEOUT, 3.0)

    def test_pause_restores_only_active_in_order_on_exception_and_failed_stop(self):
        for stop_code in (0, 1):
            with self.subTest(stop_code=stop_code), Sandbox() as box:
                units = ('first.service', 'inactive.service', 'last.timer')
                active = (units[0], units[2])
                box.processes.script({('systemctl', 'is-active', '--quiet', unit):
                                      box.processes.result(0 if unit in active else 3) for unit in units})
                box.processes.script({('systemctl', 'stop', *units): box.processes.result(stop_code),
                                      ('systemctl', 'start', *active): box.processes.ok()})
                with self.assertRaises(ExternalCommandError if stop_code else ValueError) as caught:
                    with systemd.writers_paused(units) as selected:
                        self.assertEqual(selected, active)
                        raise ValueError('operation failed')
                if stop_code:
                    self.assertEqual(caught.exception.command, ['systemctl', 'stop', *units])
                    self.assertEqual(caught.exception.returncode, stop_code)
                self.assertEqual(box.processes.calls, [
                    *(('systemctl', 'is-active', '--quiet', unit) for unit in units),
                    ('systemctl', 'stop', *units), ('systemctl', 'start', *active)])

    def test_pause_stops_inactive_units_without_starting_them(self):
        with Sandbox() as box:
            box.processes.script({('systemctl', 'is-active', '--quiet', 'off'): box.processes.result(3),
                                  ('systemctl', 'stop', 'off'): box.processes.ok()})
            with systemd.writers_paused(['off']) as active:
                self.assertEqual(active, ())
            self.assertEqual(box.processes.calls, [('systemctl', 'is-active', '--quiet', 'off'),
                                                   ('systemctl', 'stop', 'off')])

    def test_pause_stops_activating_oneshot_before_body_without_starting_it_again(self):
        with Sandbox() as box:
            timer = 'xray-traffic-sync.timer'
            oneshot = 'xray-traffic-sync.service'
            running = True

            def stop(command, **kwargs):
                nonlocal running
                self.assertEqual(command, ['systemctl', 'stop', *systemd.MANAGER_UNITS])
                self.assertNotIn('--no-block', command)
                running = False
                return box.processes.ok()

            box.processes.script({('systemctl', 'is-active', '--quiet', unit):
                                  box.processes.result(0 if unit == timer else 3)
                                  for unit in systemd.MANAGER_UNITS})
            box.processes.script({('systemctl', 'stop', *systemd.MANAGER_UNITS): stop,
                                  ('systemctl', 'start', timer): box.processes.ok()})
            # Выполняющийся Type=oneshot без RemainAfterExit находится в activating;
            # is-active возвращает 3, но service должен быть остановлен до yield.
            with systemd.writers_paused() as active:
                self.assertFalse(running)
                self.assertEqual(active, (timer,))
            self.assertEqual(box.processes.calls[-1], ('systemctl', 'start', timer))
            self.assertNotIn(('systemctl', 'start', oneshot), box.processes.calls)

    def test_pause_accepts_generator_and_empty_units(self):
        with Sandbox() as box:
            box.processes.script({('systemctl', 'is-active', '--quiet', 'off'): box.processes.result(3),
                                  ('systemctl', 'stop', 'off'): box.processes.ok()})
            with systemd.writers_paused(unit for unit in ['off']) as active:
                self.assertEqual(active, ())
            with systemd.writers_paused(()) as active:
                self.assertEqual(active, ())
            self.assertEqual(box.processes.calls, [('systemctl', 'is-active', '--quiet', 'off'),
                                                   ('systemctl', 'stop', 'off')])

    def test_resume_error_is_visible_and_keeps_operation_error_context(self):
        with Sandbox() as box:
            box.processes.script({('systemctl', 'is-active', '--quiet', 'on'): box.processes.ok(),
                                  ('systemctl', 'stop', 'on'): box.processes.ok(),
                                  ('systemctl', 'start', 'on'):
                                  box.processes.result(1, stdout='partial', stderr='resume failed')})
            with self.assertRaises(ExternalCommandError) as caught:
                with systemd.writers_paused(['on']):
                    raise ValueError('operation failed')
            self.assertEqual(caught.exception.message, 'resume failed')
            self.assertEqual(caught.exception.command, ['systemctl', 'start', 'on'])
            self.assertEqual(caught.exception.returncode, 1)
            self.assertEqual(caught.exception.output, 'partial')
            self.assertIsInstance(caught.exception.__context__, ValueError)

    def test_legacy_stop_and_start_keep_runtime_errors(self):
        with Sandbox() as box:
            box.processes.script({
                ('systemctl', 'stop', *systemd.MANAGER_UNITS):
                box.processes.result(2, stdout='out', stderr='err'),
                ('systemctl', 'start', 'on'): box.processes.result(7, stderr='legacy start failed'),
            })
            with self.assertRaisesRegex(RuntimeError, 'outerr') as caught:
                process.stop_writer_units()
            self.assertNotIsInstance(caught.exception, ExternalCommandError)
            with self.assertRaisesRegex(RuntimeError, 'legacy start failed'):
                process.restart_units(['on'])

    def test_reload_try_restart_and_errors(self):
        with Sandbox() as box:
            box.processes.script({('systemctl', 'daemon-reload'): box.processes.ok(),
                                  ('systemctl', 'try-restart', 'sample'): box.processes.result(1, stderr='failed')})
            self.assertIsNone(systemd.daemon_reload())
            with self.assertRaises(ExternalCommandError):
                systemd.try_restart('sample')
            self.assertEqual(box.processes.calls, [('systemctl', 'daemon-reload'),
                                                   ('systemctl', 'try-restart', 'sample')])


class HTTPTests(unittest.TestCase):
    def install_wgcf_in(self, box, payload):
        url = 'https://example.invalid/wgcf-linux-amd64'
        binary = box.root / 'usr/local/bin/wgcf'
        binary.parent.mkdir(parents=True, exist_ok=True)
        box.http.script({
            warp.GITHUB_API_LATEST: box.http.json({'assets': [{
                'name': 'wgcf_2.2.22_linux_amd64', 'browser_download_url': url}]}),
            url: payload,
        })
        with mock.patch('platform.machine', return_value='x86_64'), \
                mock.patch.object(tempfile, 'tempdir', str(box.root)), \
                redirect_stdout(io.StringIO()):
            warp.install_wgcf()
        self.assertEqual(binary.read_bytes(), payload)
        self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
        self.assertEqual(box.http.calls[-2:], [
            (warp.GITHUB_API_LATEST, {'User-agent': 'xray-vps-manager'}, 30),
            (url, {'User-agent': 'xray-vps-manager'}, 120),
        ])
        return binary

    def test_wgcf_json_and_binary_download_use_current_nested_sandbox(self):
        with Sandbox() as outer:
            with Sandbox() as inner:
                self.install_wgcf_in(inner, b'inner wgcf binary')
            self.assertEqual(outer.http.calls, [])
            binary = self.install_wgcf_in(outer, b'outer wgcf binary')
            calls = outer.http.calls.copy()
            with redirect_stdout(io.StringIO()):
                warp.install_wgcf()
            self.assertEqual(outer.http.calls, calls)
            self.assertEqual(binary.read_bytes(), b'outer wgcf binary')

    def test_nested_sandbox_http_restores_outer_after_error(self):
        url = 'https://example.invalid/release'
        with Sandbox() as outer:
            outer.http.script({url: outer.http.json({'name': 'outer'})})
            for helper in (manager_update.http_json, update.http_json, warp.http_json):
                self.assertEqual(helper(url), {'name': 'outer'})
                with self.assertRaisesRegex(ValueError, 'inner'):
                    with Sandbox() as inner:
                        inner.http.script({url: inner.http.json({'name': 'inner'})})
                        self.assertEqual(helper(url), {'name': 'inner'})
                        raise ValueError('inner')
                self.assertEqual(helper(url), {'name': 'outer'})
            self.assertEqual(len(outer.http.calls), 6)

    def test_unknown_http_is_detected_even_if_caller_swallows_it(self):
        with self.assertRaisesRegex(AssertionError, 'Незапланированные'):
            with Sandbox():
                try:
                    http.http_json('https://example.invalid/unknown')
                except Exception:
                    pass

    def test_fake_opener_closes_response_and_restores_on_decode_error(self):
        responses = []
        def open_outer(request, timeout):
            response = io.BytesIO(b'{"name": "outer"}')
            responses.append(response)
            return response
        with http.fake(open_outer):
            with self.assertRaises(ValueError):
                with http.fake(lambda request, timeout: io.BytesIO(b'{')):
                    http.http_json('https://example.invalid')
            self.assertEqual(http.http_json('https://example.invalid'), {'name': 'outer'})
        self.assertTrue(responses[0].closed)
        with mock.patch('urllib.request.urlopen', return_value=io.BytesIO(b'{}')) as opener:
            self.assertEqual(http.http_json('https://example.invalid'), {})
            opener.assert_called_once()
