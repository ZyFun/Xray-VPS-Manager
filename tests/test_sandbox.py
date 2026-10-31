"""R1.1: публичные швы Sandbox; старые имена модулей знает только фасад."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
from xray_vps_manager.core import paths as path_module, process, time as clock
from xray_vps_manager.core.server_env import read_server_env
from tests.support.sandbox import Sandbox, FakeProcesses, UnexpectedCommand


class FakeProcessesTests(unittest.TestCase):
    def test_script_records_output_failure_and_exact_order(self):
        fake = FakeProcesses()
        fake.script({('tool', 'check'): [fake.ok('first\n'), fake.result(7, stderr='failed\n')]})
        self.assertEqual(fake.run(['tool','check'], text=True, capture_output=True).stdout, 'first\n')
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            fake.run(['tool','check'], text=True, capture_output=True, check=True)
        self.assertEqual(caught.exception.returncode, 7)
        self.assertEqual(caught.exception.stderr, 'failed\n')
        with self.assertRaises(UnexpectedCommand):
            fake.run(['tool','check'])
        self.assertEqual(fake.calls, [('tool','check')]*3)

    def test_unknown_command_is_not_a_success_and_shell_is_forbidden(self):
        fake = FakeProcesses()
        with self.assertRaises(UnexpectedCommand):
            fake.run(['unconfigured'])
        with self.assertRaises(UnexpectedCommand):
            fake.run('echo unsafe', shell=True)

    def test_stderr_joins_captured_or_inherited_stdout(self):
        from contextlib import redirect_stdout
        import io
        fake = FakeProcesses()
        fake.script({('tool',): fake.result(3, stdout='out', stderr='err')})
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            fake.run(['tool'], text=True, stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, check=True)
        self.assertEqual(caught.exception.stdout, 'outerr')
        self.assertIsNone(caught.exception.stderr)
        stream = io.StringIO()
        with redirect_stdout(stream):
            result = fake.run(['tool'], text=True, stderr=subprocess.STDOUT)
        self.assertEqual(stream.getvalue(), 'outerr')
        self.assertIsNone(result.stdout)
        self.assertIsNone(result.stderr)


class SandboxTests(unittest.TestCase):
    def test_geoip_cache_is_isolated_between_sequential_and_nested_sandboxes(self):
        from xray_vps_manager.activity import parser

        def write_geoip(box, code):
            box.paths.asset_dir.mkdir(parents=True, exist_ok=True)
            (box.paths.asset_dir/'geoip.dat').write_bytes(b'\x0a\x04\x0a\x02'+code.encode())

        original = ['original-cache']
        with mock.patch.object(parser, '_GEOIP_CODES_CACHE', original):
            for code in (None, 'DE', 'US'):
                with self.subTest(code=code), Sandbox() as box:
                    if code:
                        write_geoip(box, code)
                    self.assertEqual(parser.available_geoip_codes(), [code] if code else [])
                self.assertIs(parser._GEOIP_CODES_CACHE, original)
            with Sandbox() as outer:
                write_geoip(outer, 'DE')
                outer_codes = parser.available_geoip_codes()
                with Sandbox() as inner:
                    write_geoip(inner, 'US')
                    self.assertEqual(parser.available_geoip_codes(), ['US'])
                self.assertIs(parser.available_geoip_codes(), outer_codes)
                self.assertEqual(outer_codes, ['DE'])
            self.assertIs(parser._GEOIP_CODES_CACHE, original)

    def test_sqlite_defaults_and_updates_use_frozen_time(self):
        from xray_vps_manager.db import database
        from xray_vps_manager.db.repositories import telegram
        with Sandbox(now='2026-10-06T15:00:00+03:00') as box:
            with closing(database.open_database(box.paths.manager_db)) as connection:
                telegram.set_setting(connection, 'enabled', '0')
                self.assertEqual(connection.execute(
                    "SELECT updated_at FROM telegram_settings WHERE key='enabled'"
                ).fetchone()[0], '2026-10-06T12:00:00Z')
                connection.execute("UPDATE telegram_settings SET updated_at='old'")
                connection.commit()
                telegram.set_setting(connection, 'enabled', '1')
                self.assertEqual(connection.execute(
                    "SELECT updated_at FROM telegram_settings WHERE key='enabled'"
                ).fetchone()[0], '2026-10-06T12:00:00Z')
            self.assertIn('2026-10-06T12:00:00Z', box.db_rows('telegram_settings'))

    def test_sqlite_clock_is_scoped_and_preserves_native_formatting(self):
        original_connect = sqlite3.connect
        query = "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
        with Sandbox(now='2026-10-06T12:00:00Z'):
            with closing(sqlite3.connect(':memory:')) as outer_connection:
                self.assertEqual(outer_connection.execute(query).fetchone()[0], '2026-10-06T12:00:00Z')
                with Sandbox(now='2026-10-07T12:00:00Z'):
                    with closing(sqlite3.connect(':memory:')) as inner_connection:
                        self.assertEqual(inner_connection.execute(query).fetchone()[0], '2026-10-07T12:00:00Z')
                        self.assertEqual(inner_connection.execute(
                            "SELECT strftime('%Y-%m-%d %H:%M:%f', '2001-02-03T04:05:06.789Z')"
                        ).fetchone()[0], '2001-02-03 04:05:06.789')
                        self.assertIsNone(inner_connection.execute("SELECT strftime('%Y', 'invalid')").fetchone()[0])
                    self.assertEqual(outer_connection.execute(query).fetchone()[0], '2026-10-06T12:00:00Z')
                with closing(sqlite3.connect(':memory:')) as after_inner:
                    self.assertEqual(after_inner.execute(query).fetchone()[0], '2026-10-06T12:00:00Z')
        self.assertIs(sqlite3.connect, original_connect)

    def test_dns_and_port_probe_never_reach_host_even_if_error_is_swallowed(self):
        import socket
        original_dns, original_socket = socket.getaddrinfo, socket.socket
        attempts = (lambda: socket.getaddrinfo('example.com', 443),
                    lambda: process.tcp_port_in_use(8443))
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                with mock.patch.object(socket, 'getaddrinfo', side_effect=RuntimeError('host DNS')) as dns:
                    with mock.patch.object(socket, 'socket', side_effect=RuntimeError('host socket')) as constructor:
                        with self.assertRaisesRegex(AssertionError, 'Незапланированные внешние операции'):
                            with Sandbox() as box:
                                try:
                                    attempt()
                                except Exception:
                                    pass
                        dns.assert_not_called()
                        constructor.assert_not_called()
                        self.assertEqual(len(box.processes.unexpected), 1)
        self.assertIs(socket.getaddrinfo, original_dns)
        self.assertIs(socket.socket, original_socket)

    def test_executable_lookup_is_explicit_including_defaults_and_nested_contexts(self):
        import shutil
        from xray_vps_manager.core import interpreter
        original_which = shutil.which
        original_defaults = interpreter.candidate_paths.__kwdefaults__
        with mock.patch.object(shutil, 'which', side_effect=AssertionError('host lookup')), Sandbox():
            self.assertIsNone(shutil.which('systemctl'))
        with Sandbox() as outer:
            self.assertIsNone(shutil.which('systemctl'))
            self.assertEqual(interpreter.candidate_paths(), [])
            outer.processes.executables['systemctl'] = '/fixture/bin/systemctl'
            outer.processes.executables['python3.12'] = '/fixture/bin/python3.12'
            self.assertEqual(shutil.which('systemctl'), '/fixture/bin/systemctl')
            self.assertIn('/fixture/bin/python3.12', interpreter.candidate_paths())
            with Sandbox():
                self.assertIsNone(shutil.which('systemctl'))
                self.assertNotIn('/fixture/bin/python3.12', interpreter.candidate_paths())
            self.assertEqual(shutil.which('systemctl'), '/fixture/bin/systemctl')
        self.assertIs(shutil.which, original_which)
        self.assertIs(interpreter.candidate_paths.__kwdefaults__, original_defaults)

    def test_symlinked_temp_directory_has_canonical_root_and_portable_golden(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            physical = base/'physical'
            physical.mkdir()
            alias = base/'alias'
            alias.symlink_to(physical, target_is_directory=True)
            golden = base/'golden'
            (golden/'example').mkdir(parents=True)
            (golden/'example/stdout.txt').write_text('<ROOT>/usr/local/etc/xray/config.json')
            with mock.patch.object(tempfile, 'tempdir', str(alias)), Sandbox() as box:
                self.assertEqual(box.root, box.root.resolve())
                self.assertTrue(box.paths.config.resolve().is_relative_to(box.root))
                with mock.patch.dict(os.environ, {'UPDATE_GOLDEN': '0'}):
                    box.assert_golden('example', {'stdout.txt': str(box.paths.config.resolve())}, root=golden)
            self.assertFalse(box.root.exists())

    def test_frozen_timezone_preserves_zone_name_and_dst_arithmetic(self):
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        from xray_vps_manager.clients import access
        with Sandbox() as box:
            current = access.local_now()
            self.assertIs(current.tzinfo, ZoneInfo('Europe/Moscow'))
            self.assertEqual(current.tzname(), 'MSK')
            result = box.run_cli('client', 'timezone')
            self.assertEqual(result.exit_code, 0)
            self.assertIn('Current time: 2026-10-06 15:00:00 MSK\n', result.stdout)
        with Sandbox(now='2026-10-24T12:00:00Z', tz='Europe/Berlin'):
            current = access.local_now()
            self.assertEqual(current.utcoffset(), timedelta(hours=2))
            self.assertEqual((current+timedelta(days=1)).utcoffset(), timedelta(hours=1))

    def test_frozen_datetime_preserves_fold_and_naive_utc_contract(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        with Sandbox(now='2026-10-25T01:30:00.123456Z', tz='Europe/Berlin'):
            aware = clock.datetime.now(ZoneInfo('Europe/Berlin'))
            naive = clock.datetime.now()
            self.assertEqual((aware.fold, naive.fold), (1, 1))
            self.assertEqual((aware.hour, naive.hour), (2, 2))
            self.assertIsNone(naive.tzinfo)
            self.assertEqual(clock.datetime.utcnow(), datetime(2026, 10, 25, 1, 30, 0, 123456))
            self.assertEqual(aware.microsecond, 123456)

    def test_dangerous_site_roots_remain_protected_and_restore_in_nested_contexts(self):
        from xray_vps_manager.xray import caddy
        original = caddy.DANGEROUS_SITE_ROOTS
        with Sandbox() as outer:
            outer_guards = caddy.DANGEROUS_SITE_ROOTS
            for path in (Path('/usr'), Path('/etc'), outer.root/'etc'):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'dangerous site root'):
                    caddy.validate_site_root(path, must_exist=False)
            with Sandbox() as inner:
                inner.assert_legacy_paths()
                for path in (Path('/usr'), Path('/etc'), inner.root/'etc'):
                    with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'dangerous site root'):
                        caddy.validate_site_root(path, must_exist=False)
                site = inner.root/'srv/test-site'
                site.mkdir(parents=True)
                self.assertEqual(caddy.validate_site_root(site), site.resolve())
            self.assertIs(caddy.DANGEROUS_SITE_ROOTS, outer_guards)
            outer.assert_legacy_paths()
        self.assertIs(caddy.DANGEROUS_SITE_ROOTS, original)

    def test_swallowed_external_attempt_fails_on_exit_and_restores_dependencies(self):
        from xray_vps_manager.commands.test import Diagnostics, is_active
        from contextlib import redirect_stdout
        import io
        original_paths, original_runner = path_module.paths(), subprocess.run
        for attempt, message in (
            (lambda: Diagnostics().check('xray.service', lambda: is_active('xray')),
             'systemctl is-active xray'),
            (lambda: os.system('never'), 'never'),
            (lambda: subprocess.run('never', shell=True), 'never'),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(AssertionError, message):
                    with Sandbox() as box, redirect_stdout(io.StringIO()):
                        try:
                            attempt()
                        except Exception:
                            pass
                self.assertIs(path_module.paths(), original_paths)
                self.assertIs(subprocess.run, original_runner)
                self.assertFalse(box.root.exists())

    def test_unexpected_attempt_does_not_mask_an_existing_exception(self):
        with self.assertRaisesRegex(RuntimeError, 'original error'):
            with Sandbox():
                try:
                    subprocess.run(['never'])
                except Exception:
                    pass
                raise RuntimeError('original error')

    def test_cli_reads_real_schema_fixture_and_reports_missing_database(self):
        with Sandbox() as box:
            before = box.db_rows('clients', 'client_credentials')
            config_before = box.paths.config.read_bytes()
            env_before = box.paths.server_env.read_bytes()
            result = box.run_cli('sqlite', 'status')
            self.assertEqual((result.exit_code, result.stderr), (0, ''))
            self.assertIn('Schema: 8\nQuick check: ok\nSQLite ready: yes\n', result.stdout)
            self.assertIn('clients: 1\n', result.stdout)
            self.assertEqual(box.db_rows('clients', 'client_credentials'), before)
            self.assertEqual(box.processes.calls, [])
            self.assertEqual(box.paths.config.read_bytes(), config_before)
            self.assertEqual(box.paths.server_env.read_bytes(), env_before)
            box.assert_golden('sqlite/status', {
                'stdout.txt': result.stdout, 'stderr.txt': result.stderr,
                'exit_code.txt': str(result.exit_code)+'\n', 'db.txt': before,
                'config.json': box.paths.config.read_text(),
                'server.env': box.paths.server_env.read_text(),
                'calls.txt': repr(box.processes.calls)+'\n',
            })
            box.paths.manager_db.unlink()
            result = box.run_cli('sqlite', 'status')
            self.assertEqual(result.exit_code, 1)
            self.assertIn('Status: missing', result.stdout)

    def test_legacy_inventory_defaults_nested_context_and_exception_restore(self):
        original = path_module.paths()
        with Sandbox(tz='Europe/Moscow') as outer:
            outer.assert_legacy_paths()
            self.assertEqual(read_server_env()['MANAGER_TIMEZONE'], 'Europe/Moscow')
            self.assertEqual(clock.utc_stamp(), '2026-10-06T12:00:00Z')
            with self.assertRaisesRegex(RuntimeError, 'inner'):
                with Sandbox(now='2026-10-07T12:00:00Z', tz='UTC') as inner:
                    inner.assert_legacy_paths()
                    self.assertEqual(read_server_env()['MANAGER_TIMEZONE'], 'UTC')
                    self.assertEqual(clock.utc_stamp(), '2026-10-07T12:00:00Z')
                    raise RuntimeError('inner')
            outer.assert_legacy_paths()
            self.assertEqual(read_server_env()['MANAGER_TIMEZONE'], 'Europe/Moscow')
            self.assertEqual(clock.utc_stamp(), '2026-10-06T12:00:00Z')
        self.assertIs(path_module.paths(), original)

    def test_external_commands_require_script_even_with_captured_default_runner(self):
        with self.assertRaisesRegex(AssertionError, 'Незапланированные внешние операции'), Sandbox() as box:
            for call in (lambda: subprocess.run(['never']), lambda: process.active_units(['never.service']), lambda: subprocess.Popen(['never']), lambda: os.system('never')):
                with self.subTest(call=call), self.assertRaises(UnexpectedCommand):
                    call()
            box.processes.script({('systemctl', 'is-active', '--quiet', 'sample.service'): box.processes.ok('active\n')})
            self.assertEqual(process.active_units(['sample.service']), ('sample.service',))
            box.processes.script({('tool','bytes'): box.processes.ok('data\n')})
            self.assertEqual(subprocess.check_output(['tool','bytes']), b'data\n')
            self.assertEqual(len(box.processes.unexpected), 4)

    def test_cli_error_and_menu_have_captured_exit_and_output(self):
        with Sandbox() as box:
            result = box.run_cli('client', 'not-a-command')
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn('Usage', result.stdout+result.stderr)
            box.processes.script({('/usr/local/bin/xray', 'version'): box.processes.ok('Xray 26.1.1\n')})
            result = box.run_menu(['0'])
            self.assertEqual(result.exit_code, 0)
            self.assertIn('Xray VPS Manager', result.stdout)

    def test_db_rows_orders_primary_key_and_validates_table(self):
        with Sandbox() as box:
            with closing(sqlite3.connect(box.paths.manager_db)) as conn:
                conn.execute("INSERT INTO manager_metadata(key,value) VALUES('z','2'),('a','1')")
                conn.commit()
            dump = box.db_rows('manager_metadata')
            self.assertLess(dump.index('"a"'), dump.index('"z"'))
            with self.assertRaises(ValueError):
                box.db_rows('clients; DROP TABLE clients')

    def test_golden_requires_explicit_update_and_normalizes_root(self):
        with tempfile.TemporaryDirectory() as directory, Sandbox() as box:
            root = Path(directory)
            with mock.patch.dict(os.environ, {'UPDATE_GOLDEN':'0'}):
                with self.assertRaises(AssertionError):
                    box.assert_golden('example', {'stdout.txt': str(box.paths.config)}, root=root)
                self.assertFalse((root/'example').exists())
            with mock.patch.dict(os.environ, {'UPDATE_GOLDEN':'1'}):
                box.assert_golden('example', {'stdout.txt': str(box.paths.config)}, root=root)
            self.assertEqual((root/'example/stdout.txt').read_text(), '<ROOT>/usr/local/etc/xray/config.json')
            with mock.patch.dict(os.environ, {'UPDATE_GOLDEN':'0'}):
                box.assert_golden('example', {'stdout.txt': str(box.paths.config)}, root=root)

    def test_failed_enter_restores_paths_and_processes(self):
        original = path_module.paths()
        runner = subprocess.run
        with self.assertRaises(FileNotFoundError):
            with Sandbox(config='fixtures/config/missing.json'):
                pass
        self.assertIs(path_module.paths(), original)
        self.assertIs(subprocess.run, runner)

    def test_all_legacy_copies_and_function_defaults_are_restored(self):
        from tests.support.sandbox import LEGACY_PATH_ATTRS, _legacy_owner
        originals = {(module, name): getattr(*_legacy_owner(module, name))
                     for module, name in LEGACY_PATH_ATTRS}
        defaults = read_server_env.__defaults__
        with Sandbox():
            self.assertIsNot(read_server_env.__defaults__, defaults)
        for (module, name), value in originals.items():
            self.assertIs(getattr(*_legacy_owner(module, name)), value)
        self.assertIs(read_server_env.__defaults__, defaults)

    def test_wrapped_context_manager_default_is_redirected(self):
        from xray_vps_manager.xray.caddy import random_tls_lock
        original = random_tls_lock.__wrapped__.__defaults__
        with Sandbox() as box:
            with random_tls_lock():
                self.assertTrue((box.root/'tmp/xray-caddy-random-tls.lock').exists())
        self.assertIs(random_tls_lock.__wrapped__.__defaults__, original)
