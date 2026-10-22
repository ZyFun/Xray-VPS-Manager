import inspect
import io
import os
import tempfile
import sqlite3
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xray_vps_manager.commands import test as test_command
from xray_vps_manager.db import schema
from xray_vps_manager.xray import caddy
from xray_vps_manager.xray import bypass as bypass_config
from xray_vps_manager.xray import outbound_links


VLESS_LINK = "vless://11111111-1111-1111-1111-111111111111@example.com:443?encryption=none&security=none&type=tcp#Example"


class XrayTestCommandTests(unittest.TestCase):
    def bypass_config(self) -> dict:
        outbound, _label = outbound_links.parse_vless_outbound(VLESS_LINK, "bypass-ru")
        config = {
            "outbounds": [
                {"tag": "direct", "protocol": "freedom"},
                {"tag": "blocked", "protocol": "blackhole"},
            ],
            "routing": {"domainStrategy": "IPIfNonMatch", "rules": []},
        }
        bypass_config.upsert_bypass_outbound(config, outbound)
        bypass_config.apply_bypass_route(config, "bypass-ru", "RU")
        return config

    def bypass_db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        schema.ensure_schema(connection)
        test_command.sqlite_bypass.upsert_route(
            connection,
            "bypass-ru",
            {
                "tag": "bypass-ru",
                "name": "ru",
                "regionCode": "RU",
                "regionLabel": "Россия",
                "label": "Example",
                "enabled": True,
            },
        )
        return connection

    def valid_cert(self, domain: str = "vpn.example.com") -> dict:
        return {
            "notAfter": "Jun 23 12:00:00 2030 GMT",
            "subjectAltName": (("DNS", domain),),
        }

    def test_geoip_warning_routing_accepts_bypass_marker_without_regular_warning_env(self) -> None:
        diag = SimpleNamespace(context={"config": self.bypass_config(), "server_env": {}})

        result = test_command.check_geoip_warning_routing(diag)

        self.assertIn("bypass marker", result)
        self.assertIn("RU", result)

    def test_bypass_diagnostics_accept_valid_enabled_route(self) -> None:
        diag = SimpleNamespace(context={"config": self.bypass_config()})
        connection = self.bypass_db()

        with mock.patch.object(test_command, "sqlite_ready_connection", return_value=connection):
            result = test_command.check_bypass_config(diag)

        self.assertIn("active regions=RU", result)

    def test_bypass_diagnostics_reject_config_route_without_enabled_sqlite_row(self) -> None:
        diag = SimpleNamespace(context={"config": self.bypass_config()})
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        schema.ensure_schema(connection)

        with mock.patch.object(test_command, "sqlite_ready_connection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "no enabled SQLite bypass_routes row"):
                test_command.check_bypass_config(diag)

    def test_bypass_diagnostics_reject_direct_geoip_to_bypass_rule(self) -> None:
        config = self.bypass_config()
        config["routing"]["rules"].insert(
            0,
            {"type": "field", "ip": ["geoip:ru"], "outboundTag": "bypass-ru"},
        )
        diag = SimpleNamespace(context={"config": config})
        connection = self.bypass_db()

        with mock.patch.object(test_command, "sqlite_ready_connection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "direct geoip:ru"):
                test_command.check_bypass_config(diag)

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


def write_logging_python(path: Path, version: str, log: Path, missing: str = "") -> Path:
    """Подмена интерпретатора: на пробу `-c` печатает версию и недостающие модули, остальные вызовы
    (`-m py_compile …`) пишет в журнал."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "-c" ]; then\n'
        f'    echo "{version} {missing}"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f'echo "{path.name} $*" >> "{log}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o755)
    return path


class HelperInterpreterTests(unittest.TestCase):
    """VIREI-81: xray-test проверяет shebang обёрток (WARN) и компилирует их и пакет тем же интерпретатором."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.bin = self.root / "bin"
        self.log = self.root / "calls.log"
        self.sbin = self.root / "sbin"
        self.sbin.mkdir()
        self.helpers = [str(self.sbin / "xray-menu"), str(self.sbin / "xray-test")]
        self.package = self.root / "lib" / "xray_vps_manager"
        self.package.mkdir(parents=True)
        (self.package / "__init__.py").write_text("", encoding="utf-8")
        # `#!/usr/bin/env python3` находит python3 через PATH — здесь это подмена с журналом.
        write_logging_python(self.bin / "python3", "3.12.3", self.log)
        patches = (
            mock.patch.object(test_command, "HELPER_SCRIPTS", self.helpers),
            mock.patch.object(test_command, "MANAGER_PACKAGE_DIR", self.package),
            mock.patch.dict(os.environ, {"PATH": str(self.bin)}),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def write_helpers(self, *shebangs: str) -> None:
        for helper, shebang in zip(self.helpers, shebangs):
            Path(helper).write_text(f"{shebang}\nimport sys\n", encoding="utf-8")
            os.chmod(helper, 0o755)

    def compile_calls(self) -> list:
        test_command.check_helper_syntax()
        test_command.check_manager_package()
        return self.log.read_text(encoding="utf-8").splitlines()

    def run_interpreter_check(self):
        diag = test_command.Diagnostics()
        with redirect_stdout(io.StringIO()) as output:
            diag.check("Helper interpreter", test_command.check_helper_interpreter, fatal=False)
        return diag, output.getvalue()

    def test_env_shebang_warns_and_compiles_with_python3_from_path(self) -> None:
        self.write_helpers("#!/usr/bin/env python3", "#!/usr/bin/env python3")

        diag, output = self.run_interpreter_check()

        self.assertEqual(diag.failures, [])
        self.assertEqual(len(diag.warnings), 1)
        self.assertIn("'#!/usr/bin/env python3' (Python 3.12.3)", diag.warnings[0])
        self.assertIn("WARN: Helper interpreter:", output)
        self.assertEqual(
            self.compile_calls(),
            [
                f"python3 -m py_compile {' '.join(self.helpers)}",
                f"python3 -m py_compile {self.package / '__init__.py'}",
            ],
        )

    def test_absolute_shebang_to_old_python_warns_and_compiles_with_it(self) -> None:
        old = write_logging_python(self.root / "opt" / "python3.9", "3.9.18", self.log)
        self.write_helpers(f"#!{old}", f"#!{old}")

        diag, _output = self.run_interpreter_check()

        self.assertEqual(diag.failures, [])
        self.assertEqual(len(diag.warnings), 1)
        self.assertIn(f"{old} is Python 3.9.18", diag.warnings[0])
        self.assertIn(">= 3.12", diag.warnings[0])
        self.assertEqual(
            self.compile_calls(),
            [
                f"python3.9 -m py_compile {' '.join(self.helpers)}",
                f"python3.9 -m py_compile {self.package / '__init__.py'}",
            ],
        )

    def test_absolute_shebang_to_supported_python_passes(self) -> None:
        python312 = write_logging_python(self.root / "opt" / "python3.12", "3.12.7", self.log)
        self.write_helpers(f"#!{python312}", f"#!{python312}")

        self.assertEqual(test_command.check_helper_interpreter(), f"helper scripts use {python312} (Python 3.12.7)")
        self.assertEqual(
            self.compile_calls(),
            [
                f"python3.12 -m py_compile {' '.join(self.helpers)}",
                f"python3.12 -m py_compile {self.package / '__init__.py'}",
            ],
        )

    def test_absolute_shebang_to_python_without_required_modules_warns(self) -> None:
        # 81-R01: версия подходит, но без sqlite3 менеджер на этом интерпретаторе не запускается.
        broken = write_logging_python(self.root / "opt" / "python3.12", "3.12.7", self.log, "sqlite3 lzma")
        self.write_helpers(f"#!{broken}", f"#!{broken}")

        diag, _output = self.run_interpreter_check()

        self.assertEqual(diag.failures, [])
        self.assertEqual(len(diag.warnings), 1)
        self.assertIn(f"{broken} (Python 3.12.7) lacks modules: sqlite3, lzma", diag.warnings[0])

    def test_missing_interpreter_and_mixed_shebangs_warn(self) -> None:
        python312 = write_logging_python(self.root / "opt" / "python3.12", "3.12.7", self.log)

        self.write_helpers(f"#!{self.root / 'removed' / 'python3.12'}", f"#!{self.root / 'removed' / 'python3.12'}")
        with self.assertRaisesRegex(RuntimeError, "does not run"):
            test_command.check_helper_interpreter()

        self.write_helpers(f"#!{python312}", "#!/usr/bin/env python3")
        with self.assertRaisesRegex(RuntimeError, "different interpreters"):
            test_command.check_helper_interpreter()

    def test_helper_interpreter_check_is_a_warning_in_diagnostics(self) -> None:
        source = inspect.getsource(test_command.run_diagnostics)

        self.assertIn('diag.check("Helper interpreter", check_helper_interpreter, fatal=False)', source)
        self.assertLess(source.index('"Helper interpreter"'), source.index('"Helper script syntax"'))


if __name__ == "__main__":
    unittest.main()
