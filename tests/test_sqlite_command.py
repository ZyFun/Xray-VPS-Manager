from contextlib import closing
import json
import os
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox
from xray_vps_manager import runner
from xray_vps_manager.db import database, schema
from xray_vps_manager.db.repositories import clients, connections, settings


def install_environment(protocol, transport, client_name="first"):
    env = {
        "INITIAL_PROTOCOL": protocol, "REALITY_TRANSPORT": transport, "CLIENT_NAME": client_name,
        "CREATED": "2026-10-06T12:00:00Z", "PORT": "8443", "REALITY_SNI": "example.com",
        "REALITY_DEST": "example.com:443", "FINGERPRINT": "chrome", "PUBLIC_KEY": "fixture",
        "SHORT_ID": "1234", "GRPC_SERVICE_NAME": "service", "XHTTP_PATH": "/example",
        "XHTTP_MODE": "auto", "TROJAN_CONNECTION_NAME": "trojan", "TROJAN_LOCAL_PORT": "5050",
        "TROJAN_DOMAIN": "example.org", "TROJAN_WS_PATH": "/ws", "TROJAN_TLS_MIN_VERSION": "1.2",
        "TROJAN_TLS_MAX_VERSION": "1.3", "TROJAN_PASSWORD": "FixtureOnly-TestPassword0123456789-example",
        "UUID": "00000000-0000-4000-8000-000000000001",
        "TROJAN_UUID": "00000000-0000-4000-8000-000000000002",
    }
    return {"INSTALL_" + key: value for key, value in env.items()}


class SQLiteCommandTests(unittest.TestCase):
    def test_runner_exposes_sqlite_command(self) -> None:
        self.assertIn("sqlite", runner.COMMAND_MODULES)
        self.assertTrue(callable(runner.command_main("sqlite")))

    def test_status_reports_missing_database_without_creating_it(self) -> None:
        with Sandbox() as box:
            box.paths.manager_db.unlink()
            result = box.run_cli("sqlite", "status")
            self.assertEqual(result.exit_code, 1)
            self.assertFalse(box.paths.manager_db.exists())
            self.assertIn("Status: missing", result.stdout)
            self.assertIn("Run install.sh or restore a backup", result.stdout)

    def test_status_reports_ready_database(self) -> None:
        with Sandbox(db=None) as box:
            with database.write_session() as connection:
                settings.set_metadata(connection, "jsonImport.completed", "true")
            result = box.run_cli("sqlite", "status")
            self.assertEqual(result.exit_code, 0)
            self.assertIn(f"Schema: {schema.CURRENT_SCHEMA_VERSION}", result.stdout)
            self.assertIn("Quick check: ok", result.stdout)
            self.assertIn("SQLite ready: yes", result.stdout)
            self.assertIn("activity_blocklist: 0", result.stdout)
            self.assertIn("activity_blocklist_hits: 0", result.stdout)
            self.assertIn("clients: 0", result.stdout)

    def test_migrate_requires_existing_db_and_init_is_repeatable(self):
        with Sandbox() as box:
            before = box.db_rows("clients", "client_credentials")
            self.assertEqual(box.run_cli("sqlite", "init").exit_code, 0)
            self.assertEqual(box.db_rows("clients", "client_credentials"), before)
            box.paths.manager_db.unlink()
            result = box.run_cli("sqlite", "migrate")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("ERROR:", result.stderr)
            self.assertIn("restore a backup containing manager.db with xray-backup restore", result.stderr)
            self.assertIn("To create an empty database, run xray-vps-manager sqlite init", result.stderr)
            self.assertFalse(box.paths.manager_db.exists())
            self.assertEqual(box.run_cli("sqlite", "init").exit_code, 0)
            self.assertIn("SQLite ready: yes", box.run_cli("sqlite", "status").stdout)
            with closing(database.connect()) as conn:
                conn.execute(f"PRAGMA user_version={schema.CURRENT_SCHEMA_VERSION + 1}")
            result = box.run_cli("sqlite", "migrate")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("newer than supported", result.stderr)

    def test_main_rejects_removed_cutover_command(self) -> None:
        with Sandbox() as box:
            before = box.paths.manager_db.read_bytes()
            for argv in (["validate-cutover"], ["migrate", "--from-install"], ["init", "extra"]):
                result = box.run_cli("sqlite", *argv)
                self.assertEqual(result.exit_code, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("Usage: xray-vps-manager sqlite", result.stderr)
            self.assertEqual(box.paths.manager_db.read_bytes(), before)

    def test_install_rejects_populated_database_without_changing_state(self):
        with Sandbox() as box:
            config = box.paths.config.read_bytes()
            tables = ("clients", "reality_connections", "client_credentials", "manager_metadata")
            rows = box.db_rows(*tables)
            env = install_environment("vless", "tcp", client_name="another-client")
            with mock.patch.dict(os.environ, env):
                result = box.run_cli("sqlite", "init", "--from-install")
            self.assertEqual((result.exit_code, result.stdout, result.stderr),
                             (1, "", "ERROR: Installation requires an empty manager database.\n"))
            self.assertEqual(box.paths.config.read_bytes(), config)
            self.assertEqual(box.db_rows(*tables), rows)

    def test_install_seeds_config_and_database_using_the_same_credentials(self):
        for protocol, transport in (("vless", "grpc"), ("trojan", "tcp"), ("both", "xhttp")):
            with self.subTest(protocol=protocol), Sandbox(db=None) as box:
                box.paths.manager_db.unlink()
                config = box.config()
                vless = config["inbounds"].pop()
                vless["settings"]["clients"] = []
                vless["streamSettings"].update(network=transport, grpcSettings={"serviceName": "service"},
                                              xhttpSettings={"path": "/example", "mode": "auto"})
                if protocol != "trojan":
                    config["inbounds"].append(vless)
                if protocol != "vless":
                    config["inbounds"].append({"tag": "trojan-tls", "protocol": "trojan",
                                               "port": 5050, "listen": "127.0.0.1", "settings": {"clients": []},
                                               "streamSettings": {"network": "ws", "security": "none",
                                                                  "wsSettings": {"path": "/ws"}}})
                box.paths.config.write_text(json.dumps(config))
                env = install_environment(protocol, transport)
                with mock.patch.dict(os.environ, env):
                    if protocol == "both":
                        original_config = box.paths.config.read_bytes()
                        with mock.patch("shutil.chown", side_effect=PermissionError("fixture")):
                            failed = box.run_cli("sqlite", "init", "--from-install")
                        self.assertEqual(failed.exit_code, 1)
                        self.assertIn("Cannot save initial Xray configuration", failed.stderr)
                        self.assertEqual(box.paths.config.read_bytes(), original_config)
                        with database.session() as conn:
                            self.assertEqual(connections.list_connections(conn), {})
                            self.assertEqual(clients.list_clients(conn), {})
                            self.assertNotIn("jsonImport.completed", settings.list_metadata(conn))
                    result = box.run_cli("sqlite", "init", "--from-install")
                self.assertEqual((result.exit_code, result.stderr), (0, ""))
                with database.session() as conn:
                    stored = connections.list_connections(conn)
                    credentials = clients.list_client_credentials(conn, "first")
                    self.assertEqual(settings.get_metadata(conn, "jsonImport.completed"), "true")
                expected = {inbound["tag"] for inbound in config["inbounds"] if inbound["tag"] != "api"}
                self.assertEqual(set(stored), expected)
                self.assertEqual(set(credentials), expected)
                for inbound in box.config()["inbounds"]:
                    tag = inbound["tag"]
                    if tag not in expected:
                        continue
                    item, = inbound["settings"]["clients"]
                    self.assertEqual(item["email"], credentials[tag]["client"]["email"])
                    if tag == "trojan-tls":
                        self.assertTrue(stored[tag]["caddy"])
                        self.assertEqual(credentials[tag]["id"], env["INSTALL_TROJAN_UUID" if protocol == "both" else "INSTALL_UUID"])
                        self.assertEqual(item["password"], env["INSTALL_TROJAN_PASSWORD"])
                        self.assertEqual(credentials[tag]["client"]["password"], item["password"])
                    else:
                        self.assertEqual(item["id"], env["INSTALL_UUID"])
                        self.assertEqual(credentials[tag]["id"], item["id"])
                        if transport == "grpc":
                            self.assertEqual(stored[tag]["grpcServiceName"], "service")
                        else:
                            self.assertEqual(stored[tag]["xhttpPath"], "/example")
                self.assertEqual(box.paths.config.stat().st_mode & 0o777, 0o640)
                self.assertEqual(box.paths.manager_db.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
