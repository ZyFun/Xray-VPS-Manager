"""R1.1: набор путей и обратимость тестового контекста."""
from dataclasses import fields, FrozenInstanceError
from pathlib import Path
import tempfile
import unittest
from xray_vps_manager.core import paths as path_module


class PathsTests(unittest.TestCase):
    def test_default_keeps_existing_paths_and_under_relocates_every_field(self):
        original = {name: value for name, value in vars(path_module).items() if name.isupper() and isinstance(value, Path)}
        default = path_module.Paths.default()
        self.assertEqual(default.config, Path('/usr/local/etc/xray/config.json'))
        self.assertEqual(default.manager_update_lock, Path('/usr/local/etc/xray/manager-update.lock'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            custom = path_module.Paths.under(root)
            for field in fields(default):
                self.assertEqual(getattr(custom, field.name), root / getattr(default, field.name).relative_to('/'))
            with path_module.use(custom) as current:
                self.assertIs(current, custom)
                self.assertIs(path_module.paths(), custom)
                for name, value in original.items():
                    self.assertEqual(getattr(path_module, name), root / value.relative_to('/'))
            for name, value in original.items():
                self.assertEqual(getattr(path_module, name), value)
        with self.assertRaises(FrozenInstanceError):
            default.config = Path('other')

    def test_nested_exception_restores_identity_and_preexisting_alias_override(self):
        original = path_module.paths()
        saved = path_module.CONFIG_PATH
        path_module.CONFIG_PATH = Path('/test/custom-config')
        try:
            with path_module.use(path_module.Paths.under(Path('/outer'))) as outer:
                with self.assertRaisesRegex(RuntimeError, 'inner'):
                    with path_module.use(path_module.Paths.under(Path('/inner'))):
                        raise RuntimeError('inner')
                self.assertIs(path_module.paths(), outer)
                self.assertEqual(path_module.CONFIG_PATH, outer.config)
            self.assertIs(path_module.paths(), original)
            self.assertEqual(path_module.CONFIG_PATH, Path('/test/custom-config'))
        finally:
            path_module.CONFIG_PATH = saved
