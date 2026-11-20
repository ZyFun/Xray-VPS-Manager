import os
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.support.sandbox import Sandbox


class BackupListTests(unittest.TestCase):
    second_ns = int(datetime(2026, 11, 12, 17, 57, 20, tzinfo=timezone.utc).timestamp()) * 10**9

    def setUp(self) -> None:
        self.box = self.enterContext(Sandbox())
        self.backup_dir = self.box.paths.backup_dir
        self.backup_dir.mkdir(parents=True)

    def create_archive(self, name: str, mtime_ns: int = second_ns) -> Path:
        archive = self.backup_dir / name
        archive.write_bytes(b'x' * 1024)
        os.utime(archive, ns=(mtime_ns, mtime_ns))
        return archive

    def test_cli_orders_archives_with_identical_mtime_by_utc_name(self) -> None:
        archives = [
            self.create_archive('xray-backup-20261112-175721Z.tar.gz'),
            self.create_archive('xray-backup-20261112-175720500804Z.tar.gz'),
            self.create_archive('xray-backup-20261112-175720000001Z.tar.gz'),
            self.create_archive('xray-backup-20261112-175720Z.tar.gz'),
        ]
        expected = [[str(archive), '2026-11-12 17:57 UTC', '1.00KB'] for archive in archives]

        plain = self.box.run_cli('backup', 'list', '--plain')
        self.assertEqual((plain.exit_code, plain.stderr), (0, ''))
        self.assertEqual([line.split('\t') for line in plain.stdout.splitlines()], expected)

        table = self.box.run_cli('backup', 'list')
        self.assertEqual((table.exit_code, table.stderr), (0, ''))
        rows = [[cell.strip() for cell in line.split('|')[1:-1]]
                for line in table.stdout.splitlines() if line.startswith('|')]
        self.assertEqual(rows, [['PATH', 'CREATED', 'SIZE'], *expected])

    def test_fallback_orders_by_mtime_and_preserves_archive_paths(self) -> None:
        newest = self.create_archive('xray-backup-20261312-175720Z.tar.gz', self.second_ns + 10**9)
        fractional = self.create_archive('xray-backup-20261112-175720500804Z.tar.gz', self.second_ns - 10**9)
        legacy = self.create_archive('xray-backup.tar.gz')
        renamed = self.create_archive('copy-of-xray-backup-20991231-235959Z.tar.gz')
        previous = self.create_archive('xray-backup-20261112-175719Z.tar.gz', self.second_ns + 2 * 10**9)
        (self.backup_dir / 'missing.tar.gz').symlink_to(self.backup_dir / 'missing')
        archives = [newest, fractional, legacy, renamed, previous]

        result = self.box.run_cli('backup', 'list', '--plain')
        self.assertEqual((result.exit_code, result.stderr), (0, ''))
        self.assertEqual([line.split('\t')[0] for line in result.stdout.splitlines()],
                         [str(archive) for archive in archives])
        for archive in archives:
            for value in (archive.name, str(archive)):
                with self.subTest(value=value):
                    command = self.box.run_cli('backup', 'download-command', value, 'root@server.example')
                    self.assertEqual((command.exit_code, command.stderr), (0, ''))
                    self.assertIn(f'scp root@server.example:{archive} ', command.stdout)

    def test_menu_selects_the_newest_archive_as_number_one(self) -> None:
        older = self.create_archive('xray-backup-20261112-175720Z.tar.gz')
        newer = self.create_archive('xray-backup-20261112-175720500804Z.tar.gz')
        plain = self.box.run_cli('backup', 'list', '--plain')
        self.assertEqual((plain.exit_code, plain.stderr), (0, ''))
        self.box.processes.script({
            ('/usr/local/bin/xray', 'version'): self.box.processes.ok('Xray 26.1.1\n'),
            ('xray-backup', 'list', '--plain'): self.box.processes.ok(plain.stdout),
        })

        result = self.box.run_menu(['7', '4', '1', 'n', '0', '0'])

        self.assertEqual((result.exit_code, result.stderr), (0, ''))
        self.assertRegex(result.stdout, rf'(?m)^\| 1\s+\| {re.escape(newer.name)}\s+\|')
        self.assertRegex(result.stdout, rf'(?m)^\| 2\s+\| {re.escape(older.name)}\s+\|')
        self.assertIn(f'Будет восстановлен бэкап: {newer}', result.stdout)
        self.assertIn('Восстановление отменено.', result.stdout)
