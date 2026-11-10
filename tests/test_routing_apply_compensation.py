"""VIREI-125: временные пробы, точечная компенсация и порядок блокировок."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import fcntl
import json
from io import StringIO
from pathlib import Path
from unittest import TestCase, mock

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from xray_vps_manager.activity import blocklist as activity_blocks
from xray_vps_manager.clients import repository as clients
from xray_vps_manager.commands import menu, menu_activity_actions, menu_xray_actions
from xray_vps_manager.core import settings
from xray_vps_manager.core.errors import ApplyConfigError, ManagerError
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity_blocklist as blocks, bypass as bypass_db
from xray_vps_manager.routing import blocklist, bypass, cascade, geoip_warning
from xray_vps_manager.system import activity_sync, xray_restart
from xray_vps_manager.telegram import setup, settings as telegram
from xray_vps_manager.xray import apply


@contextmanager
def runtime(box):
    box.processes.script({
        (str(box.paths.xray_bin), 'run', '-test', '-format', 'json', '-config', str(box.paths.config.with_suffix('.json.tmp'))): box.processes.ok(),
        (str(box.paths.xray_bin), 'run', '-test', '-config', str(box.paths.config)): box.processes.ok(),
        'systemctl stop xray': box.processes.ok(),
        'systemctl reset-failed xray': box.processes.ok(),
        'systemctl restart xray': box.processes.ok(),
    })
    with running_xray(box):
        yield


def free_lock(path):
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


class TemporaryConfigTests(TestCase):
    def test_probe_accepts_legacy_order_and_restores_exact_bytes_without_backup(self):
        with Sandbox() as box, runtime(box):
            config = box.config()
            config['outbounds'].reverse()
            config.pop('api')  # Проба проверяется Xray, но не постоянными инвариантами.
            original = ('  ' + json.dumps(config) + '\n\n').encode()
            box.paths.config.write_bytes(original)
            with apply.temporary_config(config):
                self.assertEqual(box.config(), config)
            self.assertEqual(box.paths.config.read_bytes(), original)
            self.assertEqual(list(box.paths.config.parent.glob('config.json.bak.*')), [])
            box.assert_restarted(2)

    def test_identical_temporary_bytes_still_restart_twice(self):
        with Sandbox() as box, runtime(box):
            config = box.config()
            box.paths.config.write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n')
            with apply.temporary_config(config):
                pass
            box.assert_restarted(2)

    def test_candidate_rejection_or_cancel_precedes_working_file(self):
        for error in (1, KeyboardInterrupt()):
            with self.subTest(error=error), Sandbox() as box, runtime(box):
                original = box.paths.config.read_bytes()
                response = box.processes.result(error) if isinstance(error, int) else error
                box.processes.script({
                    (str(box.paths.xray_bin), 'run', '-test', '-format', 'json', '-config', str(box.paths.config.with_suffix('.json.tmp'))): response,
                })
                with self.assertRaises(ApplyConfigError) as caught:
                    with apply.temporary_config(box.config()):
                        self.fail('Проба не должна начаться')
                self.assertEqual(caught.exception.exit_code, 1 if isinstance(error, int) else 130)
                self.assertEqual(box.paths.config.read_bytes(), original)
                box.assert_restarted(0)
                self.assertFalse(box.paths.config.with_suffix('.json.tmp').exists())

    def test_body_cancellation_restores_once_and_rollback_failure_is_fatal(self):
        for rollback_failed in (False, True):
            with self.subTest(rollback_failed=rollback_failed), Sandbox() as box, runtime(box):
                original = box.paths.config.read_bytes()
                candidate = box.config()
                candidate['log']['loglevel'] = 'debug'
                if rollback_failed:
                    box.processes.script({'systemctl restart xray': [box.processes.ok(), box.processes.result(1), box.processes.result(1)]})
                with self.assertRaises(ApplyConfigError if rollback_failed else KeyboardInterrupt) as caught:
                    with apply.temporary_config(candidate):
                        raise KeyboardInterrupt('cancel probe')
                self.assertEqual(box.paths.config.read_bytes(), original)
                box.assert_restarted(3 if rollback_failed else 2)
                if rollback_failed:
                    self.assertEqual(caught.exception.stage, 'rollback')
                    self.assertIn('cancel probe', caught.exception.detail)
                    self.assertIn('Возврат:', caught.exception.detail)


def legacy_telegram_config(box):
    config = box.config()
    config['outbounds'].append({'tag': 'cascade-legacy', 'protocol': 'freedom'})
    setup.ensure_telegram_proxy_config(config)
    rules = config['routing']['rules']
    rules[0], rules[1] = rules[1], rules[0]  # Порядок, создававшийся до VIREI-125.
    box.paths.config.write_text('  ' + json.dumps(config) + '\n\n')
    return config


class LegacyTelegramCandidateTests(TestCase):
    def test_blocklist_accepts_legacy_telegram_order_and_preserves_its_route(self):
        with Sandbox() as box, runtime(box):
            config = legacy_telegram_config(box)
            original = box.paths.config.read_bytes()
            telegram_rule = config['routing']['rules'][0]
            item, backup = blocklist.add_block('blocked.example')
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(box.config()['routing']['rules'][0]['outboundTag'], 'api')
            self.assertIn(telegram_rule, box.config()['routing']['rules'])
            blocklist.delete_block(str(item['id']))
            with database.session() as connection:
                self.assertEqual(blocks.list_blocks(connection), [])
            self.assertIn(telegram_rule, box.config()['routing']['rules'])

    def test_route_candidates_accept_legacy_order_without_changing_other_rules(self):
        actions = {
            'cascade/warp': lambda config: cascade.apply_route_config(config),
            'bypass': lambda config: bypass.apply_route(config, {'tag': 'bypass-new', 'regionCode': 'RU'}, {}),
            'geoip': lambda config: geoip_warning.set_region(''),
            'telegram': lambda config: setup.apply_config(config),
        }
        for name, action in actions.items():
            with self.subTest(action=name), Sandbox() as box, runtime(box):
                config = legacy_telegram_config(box)
                others = [rule for rule in config['routing']['rules'] if rule['outboundTag'] != 'api']
                action(config)
                rules = box.config()['routing']['rules']
                self.assertEqual(rules[0]['outboundTag'], 'api')
                self.assertEqual(rules[1:], others)

    def test_legacy_order_is_restored_byte_for_byte_after_failed_apply(self):
        with Sandbox() as box, runtime(box):
            legacy_telegram_config(box)
            original = box.paths.config.read_bytes()
            box.processes.script({'systemctl restart xray': [box.processes.result(1), box.processes.result(1), box.processes.ok()]})
            with self.assertRaises(ApplyConfigError) as caught:
                blocklist.add_block('blocked.example')
            self.assertTrue(caught.exception.restored)
            self.assertEqual(box.paths.config.read_bytes(), original)
            with database.session() as connection:
                self.assertEqual(blocks.list_blocks(connection), [])


class ActivitySyncErrorTests(TestCase):
    def test_sync_preserves_cancellation_and_reports_failed_rollback_even_when_quiet(self):
        for quiet in (False, True):
            for rollback_failed in (False, True):
                with self.subTest(quiet=quiet, rollback_failed=rollback_failed), Sandbox() as box, runtime(box):
                    original = box.paths.config.read_bytes()
                    activity_blocks.add_block('blocked.example')
                    responses = [KeyboardInterrupt('apply cancelled')]
                    if rollback_failed:
                        responses += [OSError('rollback restart denied'), OSError('rollback restart denied')]
                    else:
                        responses += [box.processes.ok()]
                    box.processes.script({'systemctl restart xray': responses})
                    result = box.run_cli('activity', 'sync', *(['--quiet'] if quiet else []))
                    self.assertEqual(result.exit_code, 1 if rollback_failed else 130)
                    self.assertIn('ERROR:', result.stderr)
                    self.assertIn('apply cancelled', result.stderr)
                    if rollback_failed:
                        self.assertIn('rollback restart denied', result.stderr)
                    self.assertEqual(box.paths.config.read_bytes(), original)


class NewMenuErrorTests(TestCase):
    @staticmethod
    def actions():
        return (
            (geoip_warning, 'set_region', menu_activity_actions.set_xray_geoip_routing_region),
            (geoip_warning, 'set_region', menu_activity_actions.disable_xray_geoip_routing_region),
            (xray_restart, 'restart', lambda: menu_xray_actions.restart_xray(lambda command: None)),
        )

    def test_new_menu_actions_show_apply_rollback_details_and_manager_hints(self):
        for module, name, action in self.actions():
            for kind in ('restored', 'rollback_failed', 'manager'):
                with self.subTest(action=action, kind=kind):
                    if kind == 'manager':
                        error = ManagerError('Состояние не подтверждено', detail='database failed; env rollback failed', hint='Проверьте состояние')
                        expected = ('database failed', 'env rollback failed', 'Проверьте состояние')
                    else:
                        error = ApplyConfigError('Не удалось применить конфигурацию Xray', stage='restart',
                                                 detail='apply failed' if kind == 'restored' else 'apply failed; rollback failed',
                                                 backup=Path('/tmp/synthetic-config-backup'), restored=kind == 'restored')
                        expected = ('apply failed', 'Restored backup:' if kind == 'restored' else 'rollback failed')
                    stdout, stderr = StringIO(), StringIO()
                    with mock.patch.object(module, name, side_effect=error), mock.patch('builtins.input', return_value='1'), \
                            redirect_stdout(stdout), redirect_stderr(stderr):
                        menu.execute_action('Проверка ошибки', action)
                    for text in expected:
                        self.assertIn(text, stderr.getvalue())
                    self.assertNotIn('Отменено', stdout.getvalue())
                    if kind == 'rollback_failed':
                        self.assertNotIn('Restored backup:', stderr.getvalue())

    def test_new_menu_actions_propagate_cancellation_through_standard_menu_handler(self):
        for module, name, action in self.actions():
            with self.subTest(action=action):
                error = ApplyConfigError('Отмена после успешного отката', stage='restart', restored=True)
                error.exit_code = 130
                stdout, stderr = StringIO(), StringIO()
                with mock.patch.object(module, name, side_effect=error), mock.patch('builtins.input', return_value='1'), \
                        redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(KeyboardInterrupt):
                    menu.execute_action('Проверка отмены', action)
                self.assertEqual(stdout.getvalue().count('Отменено'), 1)
                self.assertEqual(stderr.getvalue(), '')


class CompensationTests(TestCase):
    def test_numeric_block_value_does_not_snapshot_another_record_by_id(self):
        """125-SR-001: add ищет прежнюю запись по value, даже если value совпадает с ID."""
        for existing in (False, True):
            with self.subTest(existing=existing), Sandbox() as box, runtime(box):
                other = activity_blocks.add_block('original.example')
                value = str(other['id'])
                if existing:
                    activity_blocks.add_block(value, comment='old comment')
                before = box.db_rows('activity_blocklist', 'activity_blocklist_hits')
                box.processes.script({
                    (str(box.paths.xray_bin), 'run', '-test', '-format', 'json', '-config', str(box.paths.config.with_suffix('.json.tmp'))): box.processes.result(1),
                })
                with self.assertRaises(ApplyConfigError):
                    blocklist.add_block(value, comment='new comment')
                self.assertEqual(box.db_rows('activity_blocklist', 'activity_blocklist_hits'), before)

    def test_route_metadata_failure_after_commit_compensates_only_touched_records(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), Sandbox() as box, runtime(box):
                config = box.config()
                original = box.paths.config.read_bytes()
                before = box.db_rows('clients', 'client_credentials', 'cascade_routes', 'traffic_totals')
                db = clients.load_db_sql()
                db['cascadeRoutes']['cascade-example'] = {'tag': 'cascade-example', 'country': 'DE', 'name': 'example'}
                config['log']['loglevel'] = 'debug'
                save = clients.save_db
                calls = []
                def fail_after_commit(*args, **kwargs):
                    save(*args, **kwargs)
                    calls.append(True)
                    if len(calls) == 1:
                        raise KeyboardInterrupt('cancel after commit') if cancel else OSError('late database error')
                with mock.patch.object(clients, 'save_db', side_effect=fail_after_commit), self.assertRaises(ManagerError) as caught:
                    cascade.apply_route_config(config, db)
                self.assertEqual(caught.exception.exit_code, 130 if cancel else 1)
                self.assertEqual(box.paths.config.read_bytes(), original)
                self.assertEqual(box.db_rows('clients', 'client_credentials', 'cascade_routes', 'traffic_totals'), before)
                box.assert_restarted(2)

    def test_bypass_env_failure_after_rename_restores_config_records_and_env(self):
        with Sandbox() as box, runtime(box):
            config = box.config()
            original = box.paths.config.read_bytes()
            original_env = box.paths.server_env.read_bytes()
            with database.write_session() as connection:
                bypass_db.upsert_route(connection, 'bypass-other', {'enabled': True, 'regionCode': 'RU'})
            before = box.db_rows('bypass_routes')
            config['log']['loglevel'] = 'debug'
            record = {'tag': 'bypass-new', 'enabled': True, 'regionCode': 'RU'}
            write = settings.write_env
            def fail_env(*args, **kwargs):
                write(*args, **kwargs)
                raise KeyboardInterrupt('env committed')
            with mock.patch.object(settings, 'write_env', side_effect=fail_env), self.assertRaises(ManagerError) as caught:
                bypass.apply_route(config, record, {'NEW': 'value'}, conflict={'tag': 'bypass-other'})
            self.assertEqual(caught.exception.exit_code, 130)
            self.assertEqual(box.paths.config.read_bytes(), original)
            self.assertEqual(box.paths.server_env.read_bytes(), original_env)
            self.assertEqual(box.db_rows('bypass_routes'), before)
            box.assert_restarted(2)

    def test_telegram_failed_apply_restores_mode_but_preserves_owner_and_token(self):
        with Sandbox() as box, runtime(box):
            config = box.config()
            config['outbounds'].append({'tag': 'cascade-main', 'protocol': 'freedom'})
            box.paths.config.write_text(json.dumps(config))
            telegram.save_db_sections({'routeMode': 'direct', 'token': 'synthetic-token', 'chatId': '42'}, ('routeMode', 'token', 'chatId'))
            before = telegram.load_db_sql()
            original = box.paths.config.read_bytes()
            box.processes.script({'systemctl restart xray': [box.processes.result(1), box.processes.result(1), box.processes.ok()]})
            with self.assertRaises(ApplyConfigError) as caught:
                setup.apply_route_mode('cascade')
            self.assertTrue(caught.exception.restored)
            self.assertEqual(telegram.load_db_sql(), before)
            self.assertEqual(box.paths.config.read_bytes(), original)
            box.assert_restarted(3)

    def test_telegram_success_keeps_api_first_and_uses_common_backup(self):
        with Sandbox() as box, runtime(box):
            config = box.config()
            config['outbounds'].append({'tag': 'cascade-main', 'protocol': 'freedom'})
            box.paths.config.write_text(json.dumps(config))
            setup.apply_route_mode('cascade')
            self.assertEqual(box.config()['routing']['rules'][0]['outboundTag'], 'api')
            self.assertEqual(telegram.load_db_sql()['routeMode'], 'cascade')
            backups = list(box.paths.config.parent.glob('config.json.bak.*'))
            self.assertEqual(len(backups), 1)
            self.assertNotIn('telegram', backups[0].name)

    def test_block_delete_restores_id_and_hits_on_apply_failure_without_activity_lock(self):
        with Sandbox() as box, runtime(box):
            item, _ = blocklist.add_block('blocked.example')
            with database.write_session() as connection:
                blocks.record_hit(connection, item['id'], 'alice', '2026-11-08T12:00:00Z')
            before = box.db_rows('activity_blocklist', 'activity_blocklist_hits')
            original = box.paths.config.read_bytes()
            attempts = []
            def restart(argv, **kwargs):
                self.assertFalse(free_lock(box.paths.manager_lock))
                self.assertTrue(free_lock(box.paths.activity_lock))
                attempts.append(True)
                return box.processes.result(1) if len(attempts) < 3 else box.processes.ok()
            box.processes.script({'systemctl restart xray': restart})
            with self.assertRaises(ApplyConfigError):
                blocklist.delete_block(str(item['id']))
            self.assertEqual(box.db_rows('activity_blocklist', 'activity_blocklist_hits'), before)
            self.assertEqual(box.paths.config.read_bytes(), original)
            self.assertEqual(len(attempts), 3)

    def test_geoip_late_env_failure_returns_previous_config(self):
        with Sandbox() as box, runtime(box):
            original = box.paths.config.read_bytes()
            env = box.paths.server_env.read_bytes()
            with mock.patch.object(settings, 'write_env', side_effect=OSError('env denied')), self.assertRaises(ManagerError):
                geoip_warning.set_region('RU')
            self.assertEqual(box.paths.config.read_bytes(), original)
            self.assertEqual(box.paths.server_env.read_bytes(), env)
            box.assert_restarted(2)

    def test_failed_compensation_reports_both_errors(self):
        with Sandbox() as box, runtime(box):
            db = clients.load_db_sql()
            db['cascadeRoutes']['cascade-new'] = {'country': 'DE'}
            config = box.config()
            config['log']['loglevel'] = 'debug'
            box.processes.script({'systemctl restart xray': [box.processes.ok(), box.processes.result(1), box.processes.result(1)]})
            with mock.patch.object(clients, 'save_db', side_effect=OSError('database denied')), self.assertRaises(ManagerError) as caught:
                cascade.apply_route_config(config, db)
            self.assertIn('состояние не подтверждено', str(caught.exception))
            self.assertIn('database denied', caught.exception.detail)
            self.assertIn('Конфигурация Xray', caught.exception.detail)

    def test_manual_restart_checks_config_under_manager_lock(self):
        with Sandbox() as box, runtime(box):
            original = box.paths.config.read_bytes()
            def check(argv, **kwargs):
                self.assertFalse(free_lock(box.paths.manager_lock))
                return box.processes.ok()
            box.processes.script({(str(box.paths.xray_bin), 'run', '-test', '-config', str(box.paths.config)): check})
            xray_restart.restart()
            box.assert_restarted(1)
            self.assertEqual(box.paths.config.read_bytes(), original)

    def test_activity_pipeline_reconciles_after_unlock_even_without_access_log(self):
        with Sandbox() as box, runtime(box):
            activity_blocks.add_block('blocked.example')
            def check(argv, **kwargs):
                self.assertFalse(free_lock(box.paths.manager_lock))
                self.assertTrue(free_lock(box.paths.activity_lock))
                return box.processes.ok()
            box.processes.script({'systemctl restart xray': check})
            box.paths.access_log.unlink(missing_ok=True)
            self.assertEqual(activity_sync.sync(lambda message: None), 0)
            box.assert_restarted(1)
