import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xray_vps_manager.commands import test as test_command
from xray_vps_manager.xray import caddy


class XrayTestCommandTests(unittest.TestCase):
    def valid_cert(self, domain: str = "vpn.example.com") -> dict:
        return {
            "notAfter": "Jun 23 12:00:00 2030 GMT",
            "subjectAltName": (("DNS", domain),),
        }

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

        self.assertEqual(test_command.check_client_db_alignment(diag), "SQLite matches active managed connections")

    def test_client_db_alignment_accepts_trojan_connections(self) -> None:
        diag = SimpleNamespace(
            context={
                "client_db_source": "SQLite",
                "client_db": {
                    "clients": {
                        "alice": {"connection": "trojan-tls", "enabled": True},
                    }
                },
                "config": {
                    "inbounds": [
                        {
                            "tag": "vless-reality",
                            "protocol": "vless",
                            "settings": {"clients": []},
                            "streamSettings": {"security": "reality"},
                        },
                        {
                            "tag": "trojan-tls",
                            "protocol": "trojan",
                            "settings": {"clients": [{"email": "alice|created=2026-06-20T00:00:00Z"}]},
                            "streamSettings": {"security": "tls"},
                        },
                    ]
                },
            }
        )

        self.assertEqual(test_command.check_client_db_alignment(diag), "SQLite matches active managed connections")

    def test_tls_diagnostics_accepts_direct_tls_cert_key_and_sni(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cert = root / "fullchain.pem"
            key = root / "privkey.pem"
            cert.write_text("cert", encoding="utf-8")
            key.write_text("key", encoding="utf-8")
            cert.chmod(0o644)
            key.chmod(0o600)
            diag = SimpleNamespace(
                context={
                    "client_db": {
                        "connections": {
                            "trojan-tls": {
                                "sni": "vpn.example.com",
                            }
                        }
                    },
                    "config": {
                        "inbounds": [
                            {
                                "tag": "trojan-tls",
                                "protocol": "trojan",
                                "streamSettings": {
                                    "security": "tls",
                                    "tlsSettings": {
                                        "certificates": [
                                            {
                                                "certificateFile": str(cert),
                                                "keyFile": str(key),
                                            }
                                        ]
                                    },
                                },
                            }
                        ]
                    },
                }
            )

            with mock.patch.object(test_command, "decode_certificate_file", return_value=self.valid_cert()), \
                mock.patch.object(test_command.caddy_config, "list_site_configs", return_value=[]):
                result = test_command.check_tls_diagnostics(
                    diag,
                    now=datetime(2026, 6, 23, tzinfo=timezone.utc),
                )

        self.assertEqual(result, "TLS certificate diagnostics OK: direct=1, caddy=0")

    def test_tls_diagnostics_rejects_world_readable_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            key = Path(tmp_dir) / "privkey.pem"
            key.write_text("key", encoding="utf-8")
            key.chmod(0o604)

            with self.assertRaisesRegex(RuntimeError, "world-readable"):
                test_command.check_tls_file_path(key, "keyFile", private=True)

    def test_tls_diagnostics_accepts_managed_caddy_trojan_site(self) -> None:
        diag = SimpleNamespace(
            context={
                "client_db": {
                    "connections": {
                        "trojan-tls": {
                            "protocol": "trojan",
                            "security": "tls",
                            "transport": "ws",
                            "caddy": True,
                            "publicHost": "vpn.example.com",
                            "localPort": 10100,
                            "wsPath": "/trojan",
                        }
                    }
                },
                "config": {"inbounds": []},
            }
        )
        site = caddy.SiteConfig(
            path=Path("/etc/caddy/conf.d/vpn.example.com.caddy"),
            domain="vpn.example.com",
            local_port=10100,
            tls_min_version="tls1.2",
            tls_max_version="tls1.3",
            upstream_transport="http",
            match_path="/trojan",
        )

        with mock.patch.object(test_command.caddy_config, "list_site_configs", return_value=[site]), \
            mock.patch.object(test_command, "fetch_remote_tls_certificate", return_value=self.valid_cert()):
            result = test_command.check_tls_diagnostics(
                diag,
                now=datetime(2026, 6, 23, tzinfo=timezone.utc),
            )

        self.assertEqual(result, "TLS certificate diagnostics OK: direct=0, caddy=1")

    def test_tls_diagnostics_reports_caddy_route_mismatch(self) -> None:
        diag = SimpleNamespace(
            context={
                "client_db": {
                    "connections": {
                        "trojan-tls": {
                            "protocol": "trojan",
                            "security": "tls",
                            "transport": "ws",
                            "caddy": True,
                            "publicHost": "vpn.example.com",
                            "localPort": 10100,
                            "wsPath": "/trojan",
                        }
                    }
                },
                "config": {"inbounds": []},
            }
        )
        site = caddy.SiteConfig(
            path=Path("/etc/caddy/conf.d/vpn.example.com.caddy"),
            domain="vpn.example.com",
            local_port=10100,
            tls_min_version="tls1.2",
            tls_max_version="tls1.3",
            upstream_transport="http",
            match_path="/other",
        )

        with mock.patch.object(test_command.caddy_config, "list_site_configs", return_value=[site]), \
            mock.patch.object(test_command, "fetch_remote_tls_certificate", return_value=self.valid_cert()):
            with self.assertRaisesRegex(RuntimeError, "route path mismatch"):
                test_command.check_tls_diagnostics(
                    diag,
                    now=datetime(2026, 6, 23, tzinfo=timezone.utc),
                )

    def test_client_db_alignment_warns_for_legacy_trojan_users_section(self) -> None:
        diag = SimpleNamespace(
            context={
                "client_db": {
                    "clients": {
                        "alice": {"connection": "trojan-tls", "enabled": True},
                    }
                },
                "config": {
                    "inbounds": [
                        {
                            "tag": "trojan-tls",
                            "protocol": "trojan",
                            "settings": {"users": [{"email": "alice|created=2026-06-20T00:00:00Z"}]},
                            "streamSettings": {"security": "tls"},
                        },
                    ]
                },
            }
        )

        with self.assertRaisesRegex(RuntimeError, "alice: enabled in SQLite but absent from config"):
            test_command.check_client_db_alignment(diag)

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
