import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xray_vps_manager.commands import test as test_command


class XrayTestCommandTests(unittest.TestCase):
    def test_manager_package_python_files_ignores_appledouble_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            package = root / "xray_vps_manager"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "client.py").write_text("value = 1\n", encoding="utf-8")
            (package / "._client.py").write_bytes(b"\0appledouble")
            (package / ".___init__.py").write_bytes(b"\0appledouble")

            with mock.patch.object(test_command, "MANAGER_PACKAGE_DIR", package):
                files = test_command.manager_package_python_files()

            self.assertEqual(
                files,
                [
                    str(package / "__init__.py"),
                    str(package / "client.py"),
                ],
            )


if __name__ == "__main__":
    unittest.main()
