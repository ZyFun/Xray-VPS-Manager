"""R1.1: существующий контракт записи JSON до переноса в fs."""
import json
import os
import pwd
import shutil
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from xray_vps_manager.core import json_store


class JsonStoreContractTests(unittest.TestCase):
    def test_json_format_mode_owner_and_move_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nested' / 'state.json'
            path.parent.mkdir()
            path.write_text('old')
            events = []
            real_move = shutil.move
            def owner(candidate, uid, gid):
                self.assertEqual(path.read_text(), 'old')
                self.assertEqual(candidate, path.with_suffix('.json.tmp'))
                self.assertEqual(candidate.stat().st_mode & 0o777, 0o640)
                events.append((uid, gid))
            def move(source, target):
                self.assertEqual(events, [(0, 42)])
                return real_move(source, target)
            with mock.patch.object(pwd, 'getpwnam', return_value=SimpleNamespace(pw_gid=42)), mock.patch.object(os, 'chown', side_effect=owner), mock.patch.object(shutil, 'move', side_effect=move):
                json_store.save_json(path, {'имя': 'тест'})
            self.assertEqual(path.read_text(), json.dumps({'имя': 'тест'}, indent=2, ensure_ascii=False)+'\n')
            self.assertFalse(path.with_suffix('.json.tmp').exists())

    def test_permission_failure_keeps_old_file_and_existing_temporary_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('old')
            with mock.patch.object(os, 'chmod', side_effect=OSError('mode')):
                with self.assertRaisesRegex(OSError, 'mode'):
                    json_store.save_json(path, {'new': 1}, group_xray=False)
            self.assertEqual(path.read_text(), 'old')
            self.assertTrue(path.with_suffix('.json.tmp').exists())

    def test_owner_missing_or_permission_denied_is_ignored_other_errors_propagate(self):
        with mock.patch.object(pwd, 'getpwnam', side_effect=KeyError):
            json_store.chown_xray(Path('unused'))
        with mock.patch.object(pwd, 'getpwnam', return_value=SimpleNamespace(pw_gid=42)):
            with mock.patch.object(os, 'chown', side_effect=PermissionError):
                json_store.chown_xray(Path('unused'))
            with mock.patch.object(os, 'chown', side_effect=OSError('owner')):
                with self.assertRaisesRegex(OSError, 'owner'):
                    json_store.chown_xray(Path('unused'))

    def test_legacy_entrypoints_are_canonical_aliases(self):
        from xray_vps_manager.core import fs
        self.assertIs(json_store.save_json, fs.save_json)
        self.assertIs(json_store.chown_xray, fs.chown_xray)
