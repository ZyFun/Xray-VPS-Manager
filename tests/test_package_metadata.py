"""Версия пакета, меню и метаданные установки согласованы."""

from pathlib import Path
import tomllib
import unittest

from xray_vps_manager import __version__
from xray_vps_manager.commands.menu import MENU_VERSION
from xray_vps_manager.commands.manager_update import current_manager_version


class PackageMetadataTests(unittest.TestCase):
    def test_version_has_one_runtime_source(self):
        project = tomllib.loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text())['project']
        self.assertEqual(project['version'], __version__)
        self.assertEqual(MENU_VERSION, 'v' + __version__)
        self.assertEqual(current_manager_version(), 'v' + __version__)
        self.assertEqual(project['requires-python'], '>=3.12')
