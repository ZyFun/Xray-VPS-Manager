"""Канон server.env: динамические пути, удаление ключей и совместимые адаптеры."""
import os
import unittest
from unittest import mock

from xray_vps_manager.core import settings, paths, server_env
from xray_vps_manager.core.errors import ValidationError
from tests.support.sandbox import Sandbox


class SettingsTests(unittest.TestCase):
    def test_defaults_preserve_existing_values_and_precede_remove_and_changes(self):
        with Sandbox() as box:
            box.paths.server_env.write_text('EMPTY=\nKEEP=old\nCUSTOM=keep\n')
            defaults = {'EMPTY': 'fallback', 'KEEP': 'fallback', 'NEW': 'default', 'DROP': 'default'}
            result = settings.update_env({'KEEP': 'new'}, defaults=defaults, remove=('DROP',))
            self.assertEqual(result, {'EMPTY': '', 'KEEP': 'new', 'CUSTOM': 'keep', 'NEW': 'default'})
            self.assertEqual(box.paths.server_env.read_text(), 'CUSTOM=keep\nEMPTY=\nKEEP=new\nNEW=default\n')
            self.assertEqual(defaults['KEEP'], 'fallback')

    def test_paths_use_resolved_at_call_time_without_sandbox_defaults(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            custom = paths.Paths.under(Path(root))
            custom.config_dir.mkdir(parents=True)
            custom.server_env.write_text('SERVER_NAME=custom\n')
            with paths.use(custom):
                self.assertEqual(settings.read_env(), {'SERVER_NAME': 'custom'})
                self.assertEqual(server_env.read_server_env(), {'SERVER_NAME': 'custom'})
                from xray_vps_manager.clients import settings as clients
                from xray_vps_manager.traffic import settings as traffic
                from xray_vps_manager.traffic import controls
                self.assertEqual(clients.server_env_values(), {'SERVER_NAME': 'custom'})
                self.assertEqual(traffic.server_env_values(), {'SERVER_NAME': 'custom'})
                controls.set_total_multiplier('1.5')
                self.assertEqual(settings.read_env()['TRAFFIC_TOTAL_MULTIPLIER'], '1.5')

    def test_update_remove_unknown_keys_order_and_permissions(self):
        with Sandbox() as box:
            box.paths.server_env.write_text('REMOVE=x\nSERVER_NAME=before\nCUSTOM=keep\nPORT=443\n')
            result = settings.update_env({'SERVER_NAME': 'after'}, remove=('REMOVE', 'ABSENT'), ordered_keys=['PORT'])
            self.assertEqual(result, {'SERVER_NAME': 'after', 'CUSTOM': 'keep', 'PORT': '443'})
            self.assertEqual(box.paths.server_env.read_text(), 'PORT=443\nCUSTOM=keep\nSERVER_NAME=after\n')
            self.assertEqual(box.paths.server_env.stat().st_mode & 0o777, 0o640)

    def test_explicit_path_and_remove_then_set(self):
        with Sandbox() as box:
            other = box.root / 'other.env'
            other.write_text('REPLACE=old\nKEEP=yes\n')
            before = box.paths.server_env.read_bytes()
            result = settings.update_env({'REPLACE': 'new'}, remove=('REPLACE',), path=other)
            self.assertEqual(result, {'KEEP': 'yes', 'REPLACE': 'new'})
            self.assertEqual(box.paths.server_env.read_bytes(), before)

    def test_env_value_keeps_empty_value_environment_fallback(self):
        with Sandbox() as box, mock.patch.dict(os.environ, {'CUSTOM': 'process'}):
            box.paths.server_env.write_text('CUSTOM=\n')
            self.assertEqual(settings.env_value('CUSTOM', 'default'), 'process')
            box.paths.server_env.write_text('CUSTOM=file\n')
            self.assertEqual(settings.env_value('CUSTOM', 'default'), 'file')

    def test_read_normalizes_but_write_remains_full_replacement(self):
        """B1.2 / XR-REV-069: нормализация и ValidationError согласованы; запись остаётся полной."""
        with Sandbox() as box:
            box.paths.server_env.write_text('export KEY="value" # comment\nBROKEN\n')
            self.assertEqual(settings.read_env(), {'KEY': 'value'})
            with self.assertRaises(ValidationError):
                settings.read_env(strict=True)
            settings.write_env({'SERVER_NAME': 'new'})
            self.assertEqual(box.paths.server_env.read_text(), 'SERVER_NAME=new\n')
