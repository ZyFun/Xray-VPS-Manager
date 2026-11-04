"""B2.1: старый JSON API удалён, совместимый chown_xray сохраняет поведение."""
import os
import pwd
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from xray_vps_manager.core import fs, json_store


class JsonStoreContractTests(unittest.TestCase):
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
        self.assertFalse(hasattr(json_store, "load_json"))
        self.assertFalse(hasattr(json_store, "save_json"))
        self.assertIs(json_store.chown_xray, fs.chown_xray)
