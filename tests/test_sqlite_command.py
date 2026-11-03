from contextlib import closing
import json
import os
import sqlite3
from pathlib import Path
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
            self.assertIn("recover manager.db from a verified backup", result.stdout)
            self.assertNotIn("run install.sh", result.stdout.lower())

    def test_status_reports_all_table_counts_and_live_wal_file_sizes(self) -> None:
        with Sandbox() as box, closing(database.connect()) as connection:
            settings.set_metadata(connection, "test", "fixture")
            names = schema.table_names(connection)
            self.assertEqual(len(names), 28)
            expected_counts = [
                f"{name}: {connection.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]}"
                for name in sorted(names)
            ]
            db_path = box.paths.manager_db
            expected_sizes = [
                f"Size {path.name}: {path.stat().st_size} bytes"
                for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"))
            ]
            result = box.run_cli("sqlite", "status")
            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertEqual(result.stdout.splitlines(), [
                f"Database: {db_path}", f"Schema: {schema.CURRENT_SCHEMA_VERSION}", "Quick check: ok",
                *expected_sizes, *expected_counts,
            ])

    def create_non_scannable_table(self, connection):
        try:
            connection.execute("CREATE VIRTUAL TABLE local_search USING fts5(body, content='', columnsize=0)")
        except sqlite3.OperationalError as exc:
            if str(exc) == "no such module: fts5":
                self.skipTest("SQLite собран без FTS5")
            raise
        connection.execute("INSERT INTO local_search(rowid, body) VALUES(1, 'sample')")

    def test_status_keeps_report_when_only_an_extra_table_cannot_be_counted(self) -> None:
        with Sandbox() as box, closing(database.connect()) as connection:
            counts = {
                name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                for name in schema.table_names(connection)
            }
            self.create_non_scannable_table(connection)
            result = box.run_cli("sqlite", "status")
            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            lines = result.stdout.splitlines()
            self.assertIn("local_search: unavailable", lines)
            for name, count in counts.items():
                self.assertIn(f"{name}: {count}", lines)
            for path in (box.paths.manager_db, *database.sidecar_paths(box.paths.manager_db)):
                self.assertIn(f"Size {path.name}: {path.stat().st_size} bytes", lines)

            connection.execute("DROP TABLE activity_blocklist_hits")
            connection.execute("ALTER TABLE local_search RENAME TO activity_blocklist_hits")
            failed = box.run_cli("sqlite", "status")
            self.assertEqual(failed.exit_code, 1)
            self.assertIn("activity_blocklist_hits", failed.stderr)

    def test_status_preserves_failed_quick_check_even_if_a_count_fails(self) -> None:
        with Sandbox() as box, closing(database.connect()) as connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute("UPDATE clients SET enabled=123")
            connection.execute("PRAGMA ignore_check_constraints=OFF")
            result = box.run_cli("sqlite", "status")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("Quick check: CHECK constraint failed in clients", result.stdout)
            self.create_non_scannable_table(connection)
            connection.execute('ALTER TABLE local_search RENAME TO "bad\nclients: 999\x1b[2J"')
            failed = box.run_cli("sqlite", "status")
            self.assertEqual(failed.exit_code, 1)
            self.assertEqual(failed.stderr.splitlines(), [
                r"ERROR: SQLite manager database: bad\nclients: 999\x1b[2J: table does not support scanning",
                "Quick check: CHECK constraint failed in clients",
            ])

    def test_status_rejects_incomplete_schema_even_with_extra_tables(self) -> None:
        with Sandbox() as box, closing(database.connect()) as writer:
            writer.execute("DROP TABLE activity_blocklist_hits")
            writer.execute("CREATE TABLE extra (id INTEGER)")
            result = box.run_cli("sqlite", "status")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("missing tables: activity_blocklist_hits", result.stderr)
            self.assertNotIn("activity_blocklist_hits", schema.table_names(writer))

    def test_status_counts_user_tables_with_sqlite_prefix_and_quoted_names(self) -> None:
        with Sandbox() as box, closing(database.connect()) as writer:
            writer.execute('CREATE TABLE "sqlite3_metrics" (id INTEGER PRIMARY KEY AUTOINCREMENT)')
            writer.execute("INSERT INTO sqlite3_metrics VALUES (1), (2)")
            writer.execute('CREATE TABLE "has""quote" (id INTEGER)')
            self.assertTrue(writer.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone())
            result = box.run_cli("sqlite", "status")
            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertIn("sqlite3_metrics: 2", result.stdout.splitlines())
            self.assertIn('has"quote: 0', result.stdout.splitlines())
            self.assertNotIn("sqlite_sequence:", result.stdout)

    def test_status_escapes_control_characters_without_duplicating_count_lines(self) -> None:
        with Sandbox(db=None) as box, closing(database.connect()) as writer:
            writer.execute('CREATE TABLE "user_metrics\nclients" (v)')
            writer.execute('INSERT INTO "user_metrics\nclients" VALUES (1), (2)')
            writer.execute(r'CREATE TABLE "user_metrics\nclients" (v)')
            result = box.run_cli("sqlite", "status")
            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            lines = result.stdout.splitlines()
            self.assertEqual([line for line in lines if line.startswith("clients:")], ["clients: 0"])
            self.assertIn(r"user_metrics\nclients: 2", lines)
            self.assertIn(r"user_metrics\\nclients: 0", lines)

    def test_status_rejects_unsupported_versions_regardless_of_tables(self) -> None:
        with Sandbox() as box:
            for layout in ("manager", "unrelated"):
                if layout == "unrelated":
                    box.paths.manager_db.unlink()
                with closing(sqlite3.connect(box.paths.manager_db, autocommit=True)) as writer:
                    if layout == "unrelated":
                        writer.execute("CREATE TABLE other_app (id INTEGER)")
                    for version in (-1, schema.CURRENT_SCHEMA_VERSION + 1):
                        with self.subTest(layout=layout, version=version):
                            writer.execute(f"PRAGMA user_version = {version}")
                            result = box.run_cli("sqlite", "status")
                            self.assertEqual(result.exit_code, 1)
                            self.assertIn(f"Unsupported SQLite schema version {version}", result.stderr)
                            self.assertIn("Schema downgrades are not supported", result.stderr)
                            self.assertIn("verified pre-upgrade backup", result.stderr)
                            self.assertIn("returns data to the time it was taken", result.stderr)
                            self.assertNotIn("Quick check:", result.stdout)
                            self.assertEqual(schema.schema_version(writer), version)

    def test_status_accepts_older_schema_without_migrating_it(self) -> None:
        with Sandbox() as box:
            box.paths.manager_db.unlink()
            with closing(sqlite3.connect(box.paths.manager_db, autocommit=True)) as writer:
                for migration in schema.MIGRATIONS:
                    schema.apply_migration(writer, migration)
                    with self.subTest(version=migration.version):
                        result = box.run_cli("sqlite", "status")
                        self.assertEqual((result.exit_code, result.stderr), (0, ""))
                        self.assertIn(f"Schema: {migration.version}", result.stdout.splitlines())
                        self.assertEqual(schema.schema_version(writer), migration.version)

    def test_status_observes_rollback_journal_and_counts_committed_rows(self) -> None:
        with Sandbox() as box, closing(database.connect()) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
            with database.transaction(writer):
                settings.set_metadata(writer, "uncommitted", "fixture")
                journal = Path(f"{box.paths.manager_db}-journal")
                result = box.run_cli("sqlite", "status")
                self.assertEqual((result.exit_code, result.stderr), (0, ""))
                self.assertIn(f"Size {journal.name}: {journal.stat().st_size} bytes", result.stdout.splitlines())
                self.assertIn("manager_metadata: 0", result.stdout.splitlines())
                self.assertNotIn("manager.db-wal", result.stdout)
                self.assertNotIn("manager.db-shm", result.stdout)
            self.assertEqual(database.journal_mode(writer), "delete")

    def test_migrate_requires_existing_db_and_init_is_repeatable(self):
        with Sandbox() as box:
            before = box.db_rows("clients", "client_credentials")
            self.assertEqual(box.run_cli("sqlite", "init").exit_code, 0)
            self.assertEqual(box.db_rows("clients", "client_credentials"), before)
            box.paths.manager_db.unlink()
            result = box.run_cli("sqlite", "migrate")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("ERROR:", result.stderr)
            self.assertIn("recover manager.db from a verified backup", result.stderr)
            self.assertNotIn("xray-backup restore", result.stderr)
            self.assertIn("To create a new empty database, run xray-vps-manager sqlite init", result.stderr)
            self.assertFalse(box.paths.manager_db.exists())
            self.assertEqual(box.run_cli("sqlite", "init").exit_code, 0)
            self.assertIn("Schema: 8", box.run_cli("sqlite", "status").stdout)
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
