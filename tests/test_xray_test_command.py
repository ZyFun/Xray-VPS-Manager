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

    def test_certificate_domain_match_fallback_without_ssl_match_hostname(self) -> None:
        with mock.patch.object(test_command.ssl, "match_hostname", new=None, create=True):
            test_command.certificate_domain_matches(self.valid_cert("*.example.com"), "vpn.example.com")
            with self.assertRaisesRegex(RuntimeError, "certificate does not match"):
                test_command.certificate_domain_matches(self.valid_cert("*.example.com"), "vpn.other.com")

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

    def test_tls_diagnostics_deep_checks_caddy_endpoint_and_public_port(self) -> None:
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
                            "publicPort": 8443,
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

        with mock.patch.object(test_command.caddy_config, "list_site_configs", return_value=[site]), mock.patch.object(
            test_command, "fetch_remote_tls_certificate", return_value=self.valid_cert()
        ) as fetch_cert, mock.patch.object(
            test_command, "probe_caddy_endpoint", return_value="vpn.example.com:8443/trojan WebSocket endpoint responded 101"
        ) as probe_endpoint:
            result = test_command.check_tls_diagnostics(
                diag,
                now=datetime(2026, 6, 23, tzinfo=timezone.utc),
                deep=True,
            )

        self.assertEqual(result, "TLS certificate diagnostics OK: direct=0, caddy=1, endpoint=1")
        fetch_cert.assert_called_once_with("vpn.example.com", port=8443)
        probe_endpoint.assert_called_once()
        self.assertEqual(probe_endpoint.call_args.args[0]["publicPort"], 8443)
        self.assertEqual(probe_endpoint.call_args.args[0]["protocol"], "trojan")

    def test_caddy_endpoint_probe_accepts_empty_xhttp_404_from_xray(self) -> None:
        item = {
            "protocol": "vless",
            "transport": "xhttp",
            "domain": "api.example.com",
            "publicPort": 443,
            "path": "/api/v1/sync",
        }

        result = test_command.describe_caddy_endpoint_response(
            item,
            404,
            "HTTP/1.1 404 Not Found",
            {"server": "Caddy", "content-length": "0"},
            "",
        )

        self.assertIn("route reached Xray", result)

    def test_caddy_endpoint_probe_rejects_json_or_html_fallback(self) -> None:
        item = {
            "protocol": "vless",
            "transport": "xhttp",
            "domain": "api.example.com",
            "publicPort": 443,
            "path": "/api/v1/sync",
        }

        with self.assertRaisesRegex(RuntimeError, "fallback-looking 404"):
            test_command.describe_caddy_endpoint_response(
                item,
                404,
                "HTTP/1.1 404 Not Found",
                {"content-type": "application/json"},
                '{"error":"not_found"}',
            )

        with self.assertRaisesRegex(RuntimeError, "HTML fallback"):
            test_command.describe_caddy_endpoint_response(
                item,
                200,
                "HTTP/1.1 200 OK",
                {"content-type": "text/html; charset=utf-8"},
                "<!doctype html><html>",
            )

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

    def test_deprecated_trojan_websocket_warning_is_clear(self) -> None:
        diag = SimpleNamespace(
            context={
                "config": {
                    "inbounds": [
                        {
                            "tag": "trojan-tls",
                            "protocol": "trojan",
                            "streamSettings": {"network": "ws", "security": "none"},
                        }
                    ]
                }
            }
        )

        with self.assertRaises(RuntimeError) as raised:
            test_command.check_deprecated_trojan_websocket_usage(diag)

        message = str(raised.exception)
        self.assertIn("Trojan (with no Flow, etc.) is deprecated", message)
        self.assertIn("compatibility/DPI-bypass mode", message)
        self.assertIn("WebSocket transport (with ALPN http/1.1, etc.) is deprecated", message)

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

    def test_reality_inbounds_allow_same_client_on_different_connections(self) -> None:
        client_id = "00000000-0000-0000-0000-000000000001"
        diag = SimpleNamespace(
            context={
                "config": {
                    "inbounds": [
                        {
                            "tag": "vless-reality",
                            "port": 443,
                            "protocol": "vless",
                            "settings": {
                                "clients": [
                                    {
                                        "id": client_id,
                                        "email": "alice|created=2026-06-20T00:00:00Z|connection=vless-reality",
                                        "flow": "xtls-rprx-vision",
                                    }
                                ]
                            },
                            "streamSettings": {
                                "security": "reality",
                                "realitySettings": {
                                    "serverNames": ["example.com"],
                                    "dest": "example.com:443",
                                    "privateKey": "private",
                                    "shortIds": ["abcd"],
                                },
                            },
                        },
                        {
                            "tag": "vless-reality-2",
                            "port": 8443,
                            "protocol": "vless",
                            "settings": {
                                "clients": [
                                    {
                                        "id": client_id,
                                        "email": "alice|created=2026-06-20T00:00:00Z|connection=vless-reality-2",
                                        "flow": "xtls-rprx-vision",
                                    }
                                ]
                            },
                            "streamSettings": {
                                "security": "reality",
                                "realitySettings": {
                                    "serverNames": ["mirror.example.com"],
                                    "dest": "mirror.example.com:443",
                                    "privateKey": "private",
                                    "shortIds": ["ef01"],
                                },
                            },
                        },
                    ]
                },
            }
        )

        result = test_command.check_reality_inbounds(diag)

        self.assertIn("vless-reality:443", result)
        self.assertIn("vless-reality-2:8443", result)

    def test_duplicate_active_client_names_accepts_vless_and_trojan_credentials(self) -> None:
        vless_email = "alice|created=2026-06-20T00:00:00Z|connection=vless-reality"
        trojan_email = "alice|created=2026-06-21T00:00:00Z|connection=trojan-tls"
        diag = SimpleNamespace(
            context={
                "client_db": {
                    "clients": {
                        "alice": {
                            "id": "00000000-0000-0000-0000-000000000001",
                            "created": "2026-06-20T00:00:00Z",
                            "connection": "vless-reality",
                            "client": {"id": "00000000-0000-0000-0000-000000000001", "email": vless_email},
                            "credentials": {
                                "vless-reality": {
                                    "connection": "vless-reality",
                                    "protocol": "vless",
                                    "client": {"id": "00000000-0000-0000-0000-000000000001", "email": vless_email},
                                },
                                "trojan-tls": {
                                    "connection": "trojan-tls",
                                    "protocol": "trojan",
                                    "client": {"password": "secret", "email": trojan_email},
                                },
                            },
                        }
                    }
                },
                "config": {
                    "inbounds": [
                        {
                            "tag": "vless-reality",
                            "protocol": "vless",
                            "settings": {"clients": [{"email": vless_email}]},
                            "streamSettings": {"security": "reality"},
                        },
                        {
                            "tag": "trojan-tls",
                            "protocol": "trojan",
                            "settings": {"clients": [{"email": trojan_email}]},
                            "streamSettings": {"security": "tls"},
                        },
                    ]
                },
            }
        )

        result = test_command.check_duplicate_active_client_names(diag)

        self.assertIn("alice", result)
        self.assertIn("vless-reality:vless", result)
        self.assertIn("trojan-tls:trojan", result)

    def test_duplicate_active_client_names_reports_missing_sqlite_credential(self) -> None:
        vless_email = "alice|created=2026-06-20T00:00:00Z|connection=vless-reality"
        trojan_email = "alice|created=2026-06-21T00:00:00Z|connection=trojan-tls"
        diag = SimpleNamespace(
            context={
                "client_db": {
                    "clients": {
                        "alice": {
                            "id": "00000000-0000-0000-0000-000000000001",
                            "created": "2026-06-20T00:00:00Z",
                            "connection": "vless-reality",
                            "client": {"id": "00000000-0000-0000-0000-000000000001", "email": vless_email},
                            "credentials": {
                                "vless-reality": {
                                    "connection": "vless-reality",
                                    "protocol": "vless",
                                    "client": {"id": "00000000-0000-0000-0000-000000000001", "email": vless_email},
                                },
                            },
                        }
                    }
                },
                "config": {
                    "inbounds": [
                        {
                            "tag": "vless-reality",
                            "protocol": "vless",
                            "settings": {"clients": [{"email": vless_email}]},
                            "streamSettings": {"security": "reality"},
                        },
                        {
                            "tag": "trojan-tls",
                            "protocol": "trojan",
                            "settings": {"clients": [{"email": trojan_email}]},
                            "streamSettings": {"security": "tls"},
                        },
                    ]
                },
            }
        )

        with self.assertRaisesRegex(RuntimeError, "trojan-tls"):
            test_command.check_duplicate_active_client_names(diag)

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
