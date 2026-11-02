"""Проверка TAR перед распаковкой бэкапов и релизов. Очистка при неудачном восстановлении."""

import io
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from xray_vps_manager.commands import backup, manager_update
from xray_vps_manager.xray import caddy


class BackupExtractionTests(unittest.TestCase):
    extractors = (
        (backup.extract_archive, "usr/local/etc/xray/manager.db", SystemExit),
        (caddy.extract_config_backup, "etc/caddy/conf.d/site.caddy", ValueError),
        (caddy.extract_site_backup, "site/index.html", ValueError),
    )

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.archive = self.root / "backup.tar.gz"
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.enterContext(patch.object(tempfile, "tempdir", str(self.scratch)))

    def write_archive(self, first_name, second):
        with tarfile.open(self.archive, "w:gz") as tar:
            first = tarfile.TarInfo(first_name)
            data = b"backup content"
            first.size = len(data)
            tar.addfile(first, io.BytesIO(data))
            tar.addfile(second)

    def test_unknown_type_after_regular_file_raises_validation_error(self):
        for extract, name, error in self.extractors:
            with self.subTest(extractor=extract.__name__):
                bad = tarfile.TarInfo(name + ".bad")
                bad.type = b"V"
                self.write_archive(name, bad)
                with redirect_stderr(io.StringIO()) as stderr, self.assertRaises(error) as caught:
                    extract(self.archive)
                self.assertIn(f"Unsupported archive member type: {bad.name}", stderr.getvalue() + str(caught.exception))
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_extraction_error_removes_partially_written_files(self):
        for extract, name, _ in self.extractors:
            with self.subTest(extractor=extract.__name__):
                # Обе записи разрешены, но обычный файл не может быть родителем второй.
                self.write_archive(name, tarfile.TarInfo(name + "/child"))
                with self.assertRaises(OSError):
                    extract(self.archive)
                self.assertEqual(list(self.scratch.iterdir()), [])

    def extract_release(self, archive):
        return manager_update.safe_extract_archive(archive, self.scratch)

    def test_rejected_members_are_reported_before_extraction_starts(self):
        release = (self.extract_release, "release/xray-menu", manager_update.ManagerUpdateError)
        for extract, name, error in (*self.extractors, release):
            path_error = "Unsafe archive member"
            type_error = "Unsupported archive member type"
            if error is manager_update.ManagerUpdateError:
                path_error = "Небезопасный путь в архиве"
                type_error = "Неподдерживаемый тип файла в архиве"
            cases = [
                ("../outside", tarfile.REGTYPE, path_error),
                (str(self.root / "outside"), tarfile.REGTYPE, path_error),
            ]
            for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, b"D"):
                cases.append((name + ".bad", kind, type_error))
            if extract in (caddy.extract_config_backup, caddy.extract_site_backup):
                cases.append(("unexpected.txt", tarfile.REGTYPE, "Unexpected archive member"))

            for bad_name, kind, message in cases:
                with self.subTest(extractor=extract.__name__, name=bad_name, kind=kind):
                    bad = tarfile.TarInfo(bad_name)
                    bad.type = kind
                    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                        bad.linkname = name
                    self.write_archive(name, bad)
                    with patch.object(tarfile.TarFile, "extractall") as extractall:
                        with redirect_stderr(io.StringIO()) as stderr, self.assertRaises(error) as caught:
                            extract(self.archive)
                        extractall.assert_not_called()
                    self.assertIn(f"{message}: {bad_name}", stderr.getvalue() + str(caught.exception))
                    self.assertEqual(list(self.scratch.iterdir()), [])
