"""VIREI-106: согласованный CLI-контракт, ошибки и порядок проверок."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

from xray_vps_manager import runner
from xray_vps_manager.core.errors import ManagerError, Cancelled
from xray_vps_manager.core import process
from tests.support.sandbox import Sandbox


class RunnerContractTests(unittest.TestCase):
    def capture(self, action):
        out, err = StringIO(), StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                action()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def failing_command(self, error):
        def main():
            raise error
        module = types.ModuleType('runner_contract_command')
        module.main = main
        self.enterContext(patch.dict(sys.modules, {module.__name__: module}))
        self.enterContext(patch.dict(runner.COMMAND_MODULES, {'contract': module.__name__}))

    def test_manager_error_preserves_hint_detail_and_code(self):
        error = ManagerError('Не удалось применить', hint='Повторите позже', detail='Откат выполнен')
        error.exit_code = 7
        self.failing_command(error)
        with patch.dict(os.environ, {'XRAY_MANAGER_DEBUG': ''}):
            self.assertEqual(self.capture(lambda: runner.run_command('contract')),
                             (7, '', 'ERROR: Не удалось применить\nПовторите позже\nОткат выполнен\n'))

    def test_debug_adds_traceback_without_changing_exit_code(self):
        self.failing_command(ManagerError('Ошибка'))
        with patch.dict(os.environ, {'XRAY_MANAGER_DEBUG': '1'}):
            code, out, err = self.capture(lambda: runner.run_command('contract'))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('ERROR: Ошибка\n', err)
        self.assertIn('Traceback (most recent call last)', err)

    def test_interrupt_is_130_without_traceback(self):
        self.failing_command(KeyboardInterrupt())
        self.assertEqual(self.capture(lambda: runner.run_command('contract')), (130, '', ''))

    def test_cancelled_preserves_130(self):
        self.failing_command(Cancelled('Отменено'))
        self.assertEqual(self.capture(lambda: runner.run_command('contract')), (130, '', 'ERROR: Отменено\n'))

    def test_legacy_process_catch_and_finally_run_before_boundary(self):
        events = []
        module = types.ModuleType('rollback_contract_command')
        error = process.ProcessCommandError(subprocess.CompletedProcess(['tool'], 1, '', 'failed'))
        def main():
            try:
                raise error
            except subprocess.CalledProcessError:
                events.append('rollback')
                raise
            finally:
                events.append('released')
        module.main = main
        with patch.dict(sys.modules, {module.__name__: module}), patch.dict(runner.COMMAND_MODULES, {'rollback-contract': module.__name__}):
            code, out, err = self.capture(lambda: runner.run_command('rollback-contract'))
        self.assertEqual(events, ['rollback', 'released'])
        self.assertEqual((code, out, err), (1, '', 'ERROR: failed\n'))

    def test_numeric_system_exit_and_unexpected_errors_pass_through(self):
        self.failing_command(SystemExit(9))
        self.assertEqual(self.capture(lambda: runner.run_command('contract')), (9, '', ''))
        self.failing_command(RuntimeError('дефект'))
        with self.assertRaisesRegex(RuntimeError, 'дефект'):
            runner.run_command('contract')

    def test_help_for_all_commands_needs_neither_root_nor_config(self):
        for command in runner.COMMAND_MODULES:
            for flag in ('--help', '-h', 'help'):
                with self.subTest(command=command, flag=flag), Sandbox() as box:
                    box.paths.config.unlink()
                    box.paths.manager_db.unlink()
                    with patch.object(os, 'geteuid', return_value=1000):
                        result = box.run_cli(command, flag)
                    self.assertEqual((result.exit_code, result.stderr), (0, ''))
                    self.assertIn('Usage:', result.stdout)
                    self.assertFalse(box.processes.calls or box.http.calls)
                    self.assertFalse(box.paths.activity_lock.exists())

    def test_invalid_arguments_precede_root_and_side_effects(self):
        for command in runner.COMMAND_MODULES:
            with self.subTest(command=command), Sandbox() as box:
                with patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli(command, '--unknown-contract-option')
                self.assertEqual((result.exit_code, result.stdout), (2, ''))
                self.assertTrue(result.stderr.startswith('ERROR: '))
                self.assertIn('Usage:', result.stderr)
                self.assertFalse(box.processes.calls or box.http.calls)
                self.assertFalse(box.paths.activity_lock.exists())

    def test_root_policies_are_preserved(self):
        actions = {'activity': ['status'], 'backup': ['list'], 'bypass': ['status'],
                   'cascade': ['status'], 'client': ['list'], 'menu': [], 'telegram': ['status'],
                   'traffic-sync': [], 'update': ['--check'], 'warp': ['status'],
                   'caddy': ['random-tls-run'], 'manager-update': ['--rollback']}
        for command, args in actions.items():
            with self.subTest(command=command), Sandbox() as box:
                with patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli(command, *args)
                self.assertEqual((result.exit_code, result.stdout, result.stderr),
                                 (1, '', 'ERROR: Run this script as root.\n'))
                self.assertFalse(box.processes.calls or box.http.calls)

    def test_no_argument_help_and_valid_legacy_forms(self):
        for command in ('backup', 'caddy', 'client', 'manager-update', 'sqlite', 'units', 'update'):
            with self.subTest(command=command), Sandbox() as box:
                with patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli(command)
                self.assertEqual((result.exit_code, result.stderr), (0, ''))
                self.assertIn('Usage:', result.stdout)
                self.assertFalse(box.processes.calls or box.http.calls)
        cases = [
            ('manager-update', ['--update']), ('manager-update', ['--update=v99.0.0']),
            ('manager-update', ['--rollback=backup']),
            ('client', ['trojan-connection-update', 'main', '--tls-domain', 'example.com']),
            ('client', ['add', 'alice', '--connection', 'main', '30']),
            ('client', ['on', 'alice', '--connection', 'main']),
            ('bypass', ['enable']), ('bypass', ['region']), ('bypass', ['target']),
            ('bypass', ['target', 'ru', '--target', 'https://example.com']),
            ('bypass', ['disable']), ('bypass', ['remove']), ('bypass', ['test']),
            ('cascade', ['use']), ('cascade', ['--test']),
        ]
        for command, args in cases:
            with self.subTest(command=command, args=args), Sandbox() as box:
                with patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli(command, *args)
                self.assertEqual((result.exit_code, result.stdout, result.stderr),
                                 (1, '', 'ERROR: Run this script as root.\n'))
                self.assertFalse(box.processes.calls or box.http.calls)

    def test_cascade_country_accepts_multiple_words_before_root(self):
        for action in ('country', 'set-country'):
            for country in (['United', 'States'], ['United States'], ['US']):
                with self.subTest(action=action, country=country), Sandbox() as box:
                    with patch.object(os, 'geteuid', return_value=1000):
                        result = box.run_cli('cascade', action, 'main', *country)
                    self.assertEqual((result.exit_code, result.stdout, result.stderr),
                                     (1, '', 'ERROR: Run this script as root.\n'))
                    self.assertFalse(box.processes.calls or box.http.calls)
            with self.subTest(action=action, missing_country=True), Sandbox() as box:
                with patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli('cascade', action, 'main')
                self.assertEqual((result.exit_code, result.stdout), (2, ''))
                self.assertIn('Invalid number of arguments.', result.stderr)
                self.assertFalse(box.processes.calls or box.http.calls)

    def test_trojan_transport_case_and_whitespace_preserve_arity(self):
        for transport, positional in (
            ('tcp', ['main', '443', 'example.com', 'example.crt', 'example.key']),
            ('TCP', ['main', '443', 'example.com', 'example.crt', 'example.key']),
            (' Tcp ', ['main', '443', 'example.com', 'example.crt', 'example.key', 'chrome']),
            ('WS', ['main', '443', 'example.com']),
        ):
            with self.subTest(transport=transport), Sandbox() as box:
                with patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli('client', 'add-trojan-connection', *positional, '--transport', transport)
                self.assertEqual((result.exit_code, result.stdout, result.stderr),
                                 (1, '', 'ERROR: Run this script as root.\n'))
                self.assertFalse(box.processes.calls or box.http.calls)
        with Sandbox() as box:
            with patch.object(os, 'geteuid', return_value=1000):
                result = box.run_cli('client', 'add-trojan-connection', 'main', '443', 'example.com', '--transport', 'TCP')
            self.assertEqual((result.exit_code, result.stdout), (2, ''))
            self.assertIn('Invalid number of Trojan connection arguments.', result.stderr)
            self.assertFalse(box.processes.calls or box.http.calls)

    def test_backfill_modes_preserve_dry_run_with_confirmation(self):
        cases = [
            (['--dry-run'], True),
            (['--dry-run', '--yes'], True),
            (['--apply', '--yes'], True),
            ([], False),
            (['--yes'], False),
            (['--apply'], False),
            (['--dry-run', '--apply'], False),
            (['--dry-run', '--apply', '--yes'], False),
        ]
        for flags, valid in cases:
            with self.subTest(flags=flags), Sandbox() as box:
                before = box.paths.manager_db.read_bytes()
                with patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli('activity', 'backfill', 'all', '2026-10-01', '2026-10-02', *flags)
                self.assertEqual((result.exit_code, result.stdout), (1 if valid else 2, ''))
                if valid:
                    self.assertEqual(result.stderr, 'ERROR: Run this script as root.\n')
                else:
                    self.assertIn('Usage:', result.stderr)
                self.assertFalse(box.processes.calls or box.http.calls)
                self.assertEqual(box.paths.manager_db.read_bytes(), before)
                self.assertFalse(box.paths.activity_lock.exists())

    def test_detail_clients_empty_set_and_clear_remove_saved_selection(self):
        from xray_vps_manager.activity import repository
        for action, message in (('set', 'Selected clients updated: -\n'),
                                ('clear', 'Selected clients cleared.\n')):
            with self.subTest(action=action), Sandbox() as box:
                repository.set_detail_clients_for_write(['alice'])
                config_before = box.paths.config.read_bytes()
                self.assertEqual(repository.detail_capture_status_for_read()['selectedClients'], ['alice'])
                result = box.run_cli('activity', 'detail-clients', action)
                self.assertEqual((result.exit_code, result.stdout, result.stderr), (0, message, ''))
                self.assertEqual(repository.detail_capture_status_for_read()['selectedClients'], [])
                self.assertEqual(box.paths.config.read_bytes(), config_before)
                self.assertFalse(box.processes.calls or box.http.calls)

    def test_single_dash_text_is_positional_but_unknown_long_options_fail(self):
        cases = [
            ('activity', ['block-add', 'example.com', '', '7']),
            ('telegram', ['payment-details', 'card']),
            ('cascade', ['country', 'main']),
        ]
        for command, args in cases:
            for text in ('-ddos source', '- 1234', '-', '-3', '-h', '--unknown'):
                with self.subTest(command=command, text=text), Sandbox() as box:
                    before = box.paths.manager_db.read_bytes()
                    with patch.object(os, 'geteuid', return_value=1000):
                        result = box.run_cli(command, *args, text)
                    self.assertEqual(result.stdout, '')
                    if text in ('-h', '--unknown'):
                        self.assertEqual(result.exit_code, 2)
                        self.assertIn(f'Unknown option: {text}', result.stderr)
                    else:
                        self.assertEqual((result.exit_code, result.stderr),
                                         (1, 'ERROR: Run this script as root.\n'))
                    self.assertEqual(box.paths.manager_db.read_bytes(), before)
                    self.assertFalse(box.processes.calls or box.http.calls)

    def test_implicit_add_accepts_known_leading_options_only(self):
        cases = [
            ('cascade', ['--country', 'RU']),
            ('cascade', ['--country', 'RU', 'main']),
            ('bypass', ['--region', 'RU', 'example']),
            ('bypass', ['--test-target', 'https://example.com', 'example']),
            ('bypass', ['--target', 'https://example.com', 'example']),
            ('bypass', ['--replace', 'example', '--region', 'RU']),
        ]
        for command, args in cases:
            with self.subTest(command=command, args=args), Sandbox() as box:
                with patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli(command, *args)
                self.assertEqual((result.exit_code, result.stdout, result.stderr),
                                 (1, '', 'ERROR: Run this script as root.\n'))
                self.assertFalse(box.processes.calls or box.http.calls)
        for command, args in [('cascade', ['--country']), ('bypass', ['--region']),
                              ('cascade', ['--unknown']), ('bypass', ['--unknown'])]:
            with self.subTest(command=command, args=args), Sandbox() as box:
                with patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli(command, *args)
                self.assertEqual((result.exit_code, result.stdout), (2, ''))
                self.assertIn('Usage:', result.stderr)
                self.assertFalse(box.processes.calls or box.http.calls)

    def test_xray_update_backup_listing_is_a_report(self):
        from xray_vps_manager.commands import update
        for action in ('--backups', 'backups', '--list-backups'):
            for state in ('missing', 'empty', 'populated'):
                with self.subTest(action=action, state=state), Sandbox() as box:
                    self.assertTrue(update.BACKUP_DIR.is_relative_to(box.root))
                    if state != 'missing':
                        update.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                    if state == 'populated':
                        (update.BACKUP_DIR / 'example-backup').mkdir()
                    before = box.paths.manager_db.read_bytes()
                    result = box.run_cli('update', action)
                    expected = ((0, 'example-backup  version=unknown  created=example-backup\n', '')
                                if state == 'populated' else
                                (1, 'FAIL: Нет сохранённых предыдущих версий Xray.\n', ''))
                    self.assertEqual((result.exit_code, result.stdout, result.stderr), expected)
                    self.assertEqual(box.paths.manager_db.read_bytes(), before)
                    self.assertFalse(box.processes.calls or box.http.calls)

    def test_read_only_commands_remain_available_without_root(self):
        for command, args in [('sqlite', ['status']), ('units', ['sync', '--check']),
                              ('manager-update', ['--backups'])]:
            with self.subTest(command=command), Sandbox() as box:
                with patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli(command, *args)
                self.assertIn(result.exit_code, (0, 1))
                self.assertEqual(result.stderr, '')
                self.assertNotIn('root', result.stdout)

    def test_extra_arguments_and_conflicting_options_are_usage_errors(self):
        cases = [('client', ['list', 'extra']), ('traffic-sync', ['--quiet', 'extra']),
                 ('backup', ['list', '--unexpected']), ('caddy', ['random-tls-run', '--domain']),
                 ('warp', ['enable', '--pin-hosts', '--no-pin-hosts']),
                 ('manager-update', ['--check', '--rollback']),
                 ('client', ['add', 'alice', '--connection', 'one', '--protocol', 'vless']),
                 ('activity', ['cleanup', '7']), ('menu', ['--help', '--unknown']),
                 ('client', ['add', '--connection', 'main', 'alice']),
                 ('client', ['link', '--connection', 'main', 'alice']),
                 ('client', ['connection-transport', 'main', '--xhttp-path', '/', 'xhttp'])]
        for command, args in cases:
            with self.subTest(command=command, args=args), Sandbox() as box:
                with patch.object(os, 'geteuid', return_value=1000):
                    result = box.run_cli(command, *args)
                self.assertEqual((result.exit_code, result.stdout), (2, ''))
                self.assertFalse(box.processes.calls or box.http.calls)
