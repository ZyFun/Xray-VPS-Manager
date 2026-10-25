"""R1.3: контракты до переноса, через единую песочницу."""
import json
import subprocess
import unittest
from urllib.error import HTTPError, URLError

from support.sandbox import Sandbox
from xray_vps_manager.commands import manager_update, update, warp
from xray_vps_manager.core import process


class TransportContracts(unittest.TestCase):
    def test_http_parameters_decode_and_errors_are_preserved(self):
        url = 'https://example.invalid/release'
        for module, agent, timeout in ((manager_update, 'xray-manager-update', 20),
                                       (update, 'xray-update', 20),
                                       (warp, 'xray-vps-manager', 30)):
            with self.subTest(module=module.__name__), Sandbox() as box:
                errors = [HTTPError(url, 404, 'missing', {}, None), URLError('offline')]
                box.http.script({url: [box.http.json({'name': 'Релиз'}), *errors, b'{', b'\xff']})
                self.assertEqual(module.http_json(url), {'name': 'Релиз'})
                for error in errors:
                    with self.assertRaises(type(error)) as caught:
                        module.http_json(url)
                    self.assertIs(caught.exception, error)
                with self.assertRaises(json.JSONDecodeError):
                    module.http_json(url)
                with self.assertRaises(UnicodeDecodeError):
                    module.http_json(url)
                self.assertEqual(box.http.calls, [(url, {'User-agent': agent}, timeout)] * 5)

    def test_capture_input_failure_and_timeout(self):
        with Sandbox() as box:
            expired = subprocess.TimeoutExpired(['tool'], 9, output='partial', stderr='late')
            box.processes.script({('tool',): [box.processes.result(7, 'out', 'err'), expired]})
            result = process.run_capture(['tool'], timeout=9, input_text='input')
            self.assertEqual((result.returncode, result.stdout, result.stderr), (7, 'out', 'err'))
            self.assertEqual(box.processes.requests[0][1]['input'], 'input')
            self.assertEqual(box.processes.requests[0][1]['timeout'], 9)
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                process.run(['tool'], timeout=9)
            self.assertIs(caught.exception, expired)

    def test_legacy_writers_stop_all_and_restore_selected_after_failure(self):
        with Sandbox() as box:
            selected = ('xray-traffic-sync.timer', 'xray-telegram-poller.service')
            box.processes.script({('systemctl', 'is-active', '--quiet', unit):
                                  box.processes.result(0 if unit in selected else 3)
                                  for unit in process.WRITER_RESTART_UNITS})
            box.processes.script({('systemctl', 'stop', *process.WRITER_STOP_UNITS): box.processes.ok(),
                                  ('systemctl', 'start', *selected): box.processes.ok()})
            active = process.active_units(process.WRITER_RESTART_UNITS)
            with self.assertRaisesRegex(ValueError, 'operation'):
                try:
                    process.stop_writer_units()
                    raise ValueError('operation')
                finally:
                    process.restart_units(active)
            self.assertEqual(active, selected)
            self.assertEqual(box.processes.calls[-2:], [
                ('systemctl', 'stop', *process.WRITER_STOP_UNITS), ('systemctl', 'start', *selected)])

    def test_retry_keeps_order_timeouts_outputs_and_exception_type(self):
        with Sandbox() as box:
            box.processes.script({('systemctl', 'restart', 'xray'): [
                box.processes.result(1, 'out1', 'err1'), box.processes.result(2, 'out2', 'err2')],
                ('systemctl', 'reset-failed', 'xray'): box.processes.ok()})
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                process.restart_systemd_unit('xray', timeout=45, retry_delay=0, verify_timeout=0)
            self.assertEqual((caught.exception.returncode, caught.exception.stdout, caught.exception.stderr),
                             (2, 'out1\nout2', 'err1\nerr2'))
            self.assertEqual([(cmd[1], kw['timeout']) for cmd, kw in box.processes.requests],
                             [('restart', 45), ('reset-failed', 10), ('restart', 45)])
