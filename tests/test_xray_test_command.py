import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xray_vps_manager.commands import test as test_command


class XrayTestCommandTests(unittest.TestCase):
    def test_client_db_alignment_accepts_tls_vless_connections(self) -> None:
        diag = SimpleNamespace(
            context={
                "client_db_source": "SQLite",
                "client_db": {
                    "clients": {
                        "alice": {"connection": "vless-reality", "enabled": True},
                        "bob": {"connection": "vless-tls", "enabled": True},
                    }
                },
                "config": {
                    "inbounds": [
                        {
                            "tag": "vless-reality",
                            "protocol": "vless",
                            "settings": {"clients": [{"email": "alice|created=2026-06-20T00:00:00Z"}]},
                            "streamSettings": {"security": "reality"},
                        },
                        {
                            "tag": "vless-tls",
                            "protocol": "vless",
                            "settings": {"clients": [{"email": "bob|created=2026-06-20T00:00:00Z"}]},
                            "streamSettings": {"security": "tls"},
                        },
                    ]
                },
            }
        )

        self.assertEqual(test_command.check_client_db_alignment(diag), "SQLite matches active VLESS connections")

    def test_client_db_alignment_still_warns_for_enabled_client_absent_from_config(self) -> None:
        diag = SimpleNamespace(
            context={
                "client_db": {"clients": {"alice": {"connection": "vless-reality", "enabled": True}}},
                "config": {
                    "inbounds": [
                        {
                            "tag": "vless-reality",
                            "protocol": "vless",
                            "settings": {"clients": []},
                            "streamSettings": {"security": "reality"},
                        }
                    ]
                },
            }
        )

        with self.assertRaisesRegex(RuntimeError, "alice: enabled in SQLite but absent from config"):
            test_command.check_client_db_alignment(diag)

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
