"""Регрессия VIREI-134 для config-only клиентов и credentials."""

from contextlib import closing
from copy import deepcopy
from io import StringIO
import json
import sqlite3
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_client_characterization import script_processes
from xray_vps_manager.clients import listing, repository, service
from xray_vps_manager.commands.menu_client_actions import client_rows_for_selection
from xray_vps_manager.core.paths import Paths, use
from xray_vps_manager.db import database, schema
from xray_vps_manager.traffic import repository as traffic_repository


VLESS_TAG = "vless-reality"
TROJAN_TAG = "trojan-tls"
SECOND_TAG = "trojan-tls-2"
BOB_ID = "00000000-0000-4000-8000-000000000099"
PASSWORD = " fixture password +/ for a config-only client "
SECOND_PASSWORD = "fixture_other_trojan_password_00001"
STORED_PASSWORD = "fixture_stored_trojan_password_0001"


def add_trojan_inbound(config, tag, port, *users):
    config["inbounds"].append({
        "tag": tag,
        "protocol": "trojan",
        "port": port,
        "settings": {"users": list(users)},
        "streamSettings": {"security": "none", "network": "ws", "wsSettings": {"path": f"/{tag}"}},
    })


def trojan(name, password):
    return {"email": f"{name}|created=2026-10-01T00:00:00Z", "password": password, "level": 2}


def db_dump():
    with database.session() as conn:
        return tuple(conn.iterdump())


class ClientRepairTests(unittest.TestCase):
    def setUp(self):
        self.box = self.enterContext(Sandbox())
        self.config = self.box.config()
        self.vless_inbound = next(item for item in self.config["inbounds"] if item["tag"] == VLESS_TAG)
        add_trojan_inbound(self.config, TROJAN_TAG, 9443, trojan("bob", PASSWORD))
        add_trojan_inbound(self.config, SECOND_TAG, 9444, trojan("bob", SECOND_PASSWORD))
        with database.write_session() as conn:
            for tag, port in ((TROJAN_TAG, 1443), (SECOND_TAG, 2443)):
                repository.save_connection(conn, tag, {
                    "name": tag, "publicHost": f"{tag}.example", "publicPort": port, "port": port,
                    "protocol": "trojan", "security": "tls", "caddy": True,
                })
        self.save_config()

    def save_config(self):
        self.box.paths.config.write_text(json.dumps(self.config))

    def run_client(self, *args, stdin="", code=0):
        result = self.box.run_cli("client", *args, stdin=stdin)
        self.assertEqual(result.exit_code, code, result.stderr)
        return result

    def assert_unchanged(self, before, config, env):
        self.assertEqual(db_dump(), before)
        self.assertEqual(self.box.paths.config.read_bytes(), config)
        self.assertEqual(self.box.paths.server_env.read_bytes(), env)
        self.assertEqual(list(self.box.paths.config_dir.glob("config.json.bak.*")), [])

    def test_listing_menu_and_links_keep_config_only_credentials_visible(self):
        self.vless_inbound["settings"]["clients"].append({"id": BOB_ID, "email": "bob"})
        self.save_config()
        before, config, env = db_dump(), self.box.paths.config.read_bytes(), self.box.paths.server_env.read_bytes()
        db = repository.load_db()
        original = deepcopy(db)
        rows = listing.client_rows(self.box.config(), db, resolve_countries=True)
        bob = next(row for row in rows if row["name"] == "bob")
        self.assertEqual((bob["status"], bob["credentialsActive"], bob["credentialsTotal"]), ("enabled", 3, 3))
        self.assertEqual(db, original)
        self.assertIn("bob", [row["name"] for row in client_rows_for_selection("enabled")])
        links = self.run_client("link", "bob").stdout.splitlines()
        self.assertEqual(links[::2], [f"[trojan {TROJAN_TAG}]", f"[trojan {SECOND_TAG}]", f"[vless {VLESS_TAG}]"])
        selected = self.run_client("link", "bob", "--connection", TROJAN_TAG).stdout.strip()
        self.assertEqual(links[1], selected)
        link = urlsplit(selected)
        self.assertEqual((unquote(link.username), link.hostname, link.port), (PASSWORD, "trojan-tls.example", 1443))
        self.assertEqual(parse_qs(link.query)["path"], [f"/{TROJAN_TAG}"])
        self.assertTrue(links[-1].startswith(f"vless://{BOB_ID}@"))
        self.run_client("route", "bob")
        self.assert_unchanged(before, config, env)

    def test_mutations_reject_config_only_client_before_any_side_effect(self):
        sync = self.box.paths.sbin_dir / "xray-traffic-sync"
        sync.parent.mkdir(parents=True, exist_ok=True)
        sync.touch()
        before, config, env = db_dump(), self.box.paths.config.read_bytes(), self.box.paths.server_env.read_bytes()
        commands = (
            ("disable", "bob"), ("enable", "bob"), ("enable", "bob", "--connection", TROJAN_TAG),
            ("set-days", "bob", "3"), ("extend-days", "bob", "3"),
            ("set-payment", "bob", "paid"), ("set-limit", "bob", "daily", "1"),
            ("clear-limit", "bob"), ("add", "bob", "--connection", VLESS_TAG),
            ("move-connection", "bob", SECOND_TAG), ("route", "bob", "any"),
            ("rotate-trojan-password", "bob", "--connection", TROJAN_TAG),
        )
        for argv in commands:
            with self.subTest(argv=argv):
                result = self.run_client(*argv, code=1)
                self.assertIn("xray-client repair", result.stderr)
                self.assert_unchanged(before, config, env)
                self.assertEqual(self.box.processes.calls, [])

    def test_enable_after_explicit_repair_reports_the_active_client(self):
        self.run_client("enable", "bob", code=1)
        self.run_client("repair", stdin="yes\n")

        config = self.box.paths.config.read_bytes()
        env = self.box.paths.server_env.read_bytes()
        before = db_dump()
        result = self.run_client("enable", "bob", code=1)
        self.assertEqual(result.stderr.strip(), "ERROR: Client already enabled: bob")
        result = self.run_client("enable", "bob", "--connection", TROJAN_TAG, code=1)
        self.assertEqual(result.stderr.strip(), f"ERROR: Client credential already enabled: bob ({TROJAN_TAG})")
        result = self.run_client("enable", "absent", code=1)
        self.assertEqual(result.stderr.strip(), "ERROR: Client not found: absent")
        self.assert_unchanged(before, config, env)
        self.assertEqual(self.box.processes.calls, [])

    def test_enable_rejects_config_only_client_without_migrating_sqlite(self):
        legacy_db = self.box.root / "legacy.db"
        with closing(sqlite3.connect(legacy_db)) as conn, conn:
            for migration in schema.MIGRATIONS:
                if migration.version > 7:
                    break
                schema.apply_migration(conn, migration)
        legacy_db.replace(self.box.paths.manager_db)
        original = self.box.paths.manager_db.read_bytes()

        result = self.run_client("enable", "bob", code=1)

        self.assertIn("xray-client repair", result.stderr)
        self.assertEqual(self.box.paths.manager_db.read_bytes(), original)
        self.assertEqual(list(self.box.paths.config_dir.glob("config.json.bak.*")), [])
        self.assertEqual(self.box.processes.calls, [])

    def test_client_preflight_reads_the_current_manager_database(self):
        alternate = Paths.under(self.box.root / "alternate")
        with closing(database.initialize(alternate.manager_db)) as conn:
            self.assertIsNone(repository.get_client(conn, "alice"))

        self.assertTrue(repository.client_exists_in_sqlite("alice"))
        with use(alternate):
            self.assertFalse(repository.client_exists_in_sqlite("alice"))

    def test_client_preflight_rejects_a_newer_schema_before_recommending_repair(self):
        supported = schema.CURRENT_SCHEMA_VERSION
        newer = supported + 1
        with database.write_session() as conn:
            conn.execute(f"PRAGMA user_version = {newer}")
        db_path = self.box.paths.manager_db
        original = db_path.read_bytes()

        for argv in (("enable", "bob"), ("set-days", "bob", "3"), ("extend-days", "bob", "3")):
            with self.subTest(argv=argv):
                result = self.run_client(*argv, code=1)
                self.assertIn(f"SQLite schema version {newer} is newer than supported version {supported}", result.stderr)
                self.assertNotIn("xray-client repair", result.stderr)
                self.assertEqual(db_path.read_bytes(), original)
                self.assertEqual(self.box.processes.calls, [])

    def test_repair_adds_missing_credentials_and_preserves_existing_data(self):
        self.vless_inbound["settings"]["clients"].append({"id": BOB_ID, "email": "vless-user"})
        inbound = self.config["inbounds"][-1]
        inbound["settings"]["users"].extend([trojan("alice", PASSWORD), trojan("vless-user", STORED_PASSWORD)])
        self.save_config()
        with database.write_session() as conn:
            alice = repository.get_client(conn, "alice")
            alice["paymentType"] = "paid"
            alice["expiresAt"] = "2026-12-31T00:00:00Z"
            alice["extra"] = {"marker": "preserve"}
            repository.save_client(conn, "alice", alice)
            conn.execute("INSERT INTO traffic_totals(client_name, incoming_bytes) VALUES ('alice', 1234)")
            conn.execute("INSERT INTO client_traffic_limit_state(client_name) VALUES ('alice')")
        original = repository.load_db()
        config, env = self.box.paths.config.read_bytes(), self.box.paths.server_env.read_bytes()

        selected = self.run_client("link", "alice", "--connection", SECOND_TAG).stdout.strip()
        self.assertEqual(unquote(urlsplit(selected).username), PASSWORD)
        result = self.run_client("repair", stdin="yes\n")
        self.assertIn("Restored credentials in SQLite: 5", result.stdout)
        self.assertNotIn(PASSWORD, result.stdout + result.stderr)
        after = repository.load_db()
        bob = after["clients"]["bob"]
        self.assertEqual(str(UUID(bob["id"])), bob["id"])
        self.assertEqual((bob["connection"], bob["protocol"], bob["enabled"]), (TROJAN_TAG, "trojan", True))
        credential = bob["credentials"][TROJAN_TAG]
        self.assertEqual((credential["protocol"], credential["transport"], credential["created"]),
                         ("trojan", "ws", "2026-10-01T00:00:00Z"))
        self.assertEqual(credential["client"]["password"], PASSWORD)
        self.assertEqual(credential["client"]["level"], 2)
        selected = self.run_client("link", "bob", "--connection", TROJAN_TAG).stdout.strip()
        self.assertEqual(unquote(urlsplit(selected).username), PASSWORD)
        vless = after["clients"]["vless-user"]
        self.assertEqual(vless["id"], BOB_ID)
        self.assertEqual(vless["credentials"][VLESS_TAG]["client"]["id"], BOB_ID)
        restored_alice = after["clients"]["alice"]
        self.assertEqual(restored_alice["credentials"].pop(SECOND_TAG)["client"]["password"], PASSWORD)
        self.assertEqual(restored_alice, original["clients"]["alice"])
        self.assertEqual(after["connections"], original["connections"])
        with database.session() as conn:
            self.assertEqual(conn.execute("SELECT incoming_bytes FROM traffic_totals").fetchone()[0], 1234)
            self.assertEqual(conn.execute("SELECT client_name FROM client_traffic_limit_state").fetchone()[0], "alice")
            self.assertEqual((schema.schema_version(conn), database.journal_mode(conn)), (8, "wal"))

        before = db_dump()
        self.assertIn("No missing client credentials", self.run_client("repair").stdout)
        self.assert_unchanged(before, config, env)
        self.assertEqual(self.box.processes.calls, [])

    def test_disable_then_enable_restores_a_repaired_trojan_password_verbatim(self):
        with running_xray(self.box):
            script_processes(self.box)
            self.run_client("repair", stdin="y\n")
            self.run_client("disable", "bob", "--connection", TROJAN_TAG)
            self.run_client("enable", "bob", "--connection", TROJAN_TAG)
        inbound = next(inbound for inbound in self.box.config()["inbounds"] if inbound["tag"] == TROJAN_TAG)
        self.assertEqual(inbound["settings"]["clients"][0], trojan("bob", PASSWORD))

    def test_move_disabled_repaired_trojan_keeps_password_and_uuid(self):
        self.config["inbounds"][-1]["settings"]["users"].clear()
        self.save_config()
        with running_xray(self.box):
            script_processes(self.box)
            self.run_client("repair", stdin="y\n")
            client_id = repository.load_db()["clients"]["bob"]["id"]
            self.run_client("disable", "bob")
            self.run_client("move-connection", "bob", SECOND_TAG)
            self.run_client("enable", "bob", "--connection", SECOND_TAG)
        inbound = next(inbound for inbound in self.box.config()["inbounds"] if inbound["tag"] == SECOND_TAG)
        self.assertEqual(inbound["settings"]["clients"][0]["password"], PASSWORD)
        bob = repository.load_db()["clients"]["bob"]
        self.assertEqual(bob["id"], client_id)
        self.assertEqual(bob["credentials"][SECOND_TAG]["client"]["password"], PASSWORD)

    def test_trojan_link_prefers_existing_credential_for_its_connection(self):
        self.run_client("repair", stdin="y\n")
        self.config["inbounds"][-2]["settings"]["users"][0]["password"] = STORED_PASSWORD
        self.save_config()
        before = db_dump()
        link = self.run_client("link", "bob", "--connection", TROJAN_TAG).stdout.strip()
        self.assertEqual(unquote(urlsplit(link).username), PASSWORD)
        self.assertEqual(db_dump(), before)

    def test_repair_restores_physical_credential_when_legacy_fields_are_present(self):
        with database.write_session() as conn:
            original = repository.get_client(conn, "alice")
            conn.execute("DELETE FROM client_credentials WHERE client_name = 'alice'")
            self.assertIn(VLESS_TAG, repository.list_credentials(conn, "alice"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM client_credentials").fetchone()[0], 0)
        config, env = self.box.paths.config.read_bytes(), self.box.paths.server_env.read_bytes()
        restored = self.run_client("repair", stdin="y\n")
        self.assertIn(f"alice — {VLESS_TAG} (vless)", restored.stdout)
        with database.session() as conn:
            row = conn.execute(
                "SELECT credential_uuid FROM client_credentials WHERE client_name = ? AND connection_tag = ?",
                ("alice", VLESS_TAG),
            ).fetchone()
            self.assertEqual(row["credential_uuid"], original["credentials"][VLESS_TAG]["id"])
            self.assertEqual(repository.get_client(conn, "alice")["id"], original["id"])

        self.assertTrue(traffic_repository.write_traffic_db_to_sqlite_for_write({
            "clients": {"alice": {"incoming": 2048}},
            "credentials": {"alice": {VLESS_TAG: {"incoming": 2048}}},
        }, strict=True))
        with database.session() as conn:
            traffic = conn.execute(
                "SELECT incoming_bytes FROM credential_traffic_totals WHERE client_name = ? AND connection_tag = ?",
                ("alice", VLESS_TAG),
            ).fetchone()
            self.assertEqual(traffic["incoming_bytes"], 2048)
        before = db_dump()
        self.assertIn("No missing client credentials", self.run_client("repair").stdout)
        self.assert_unchanged(before, config, env)

    def test_repair_refuses_conflicting_legacy_credential(self):
        with database.write_session() as conn:
            conn.execute("DELETE FROM client_credentials WHERE client_name = 'alice'")
        self.vless_inbound["settings"]["clients"][0]["id"] = BOB_ID
        self.save_config()
        before, config, env = db_dump(), self.box.paths.config.read_bytes(), self.box.paths.server_env.read_bytes()
        result = self.run_client("repair", stdin="y\n", code=1)
        self.assertIn("Stored client data conflicts with config.json", result.stderr)
        self.assertNotIn(BOB_ID, result.stdout + result.stderr)
        self.assert_unchanged(before, config, env)

    def assert_repair_recovers_disabled_vless(self, *, already_repaired):
        self.vless_inbound["settings"]["clients"].clear()
        self.config["inbounds"][-2]["settings"]["users"] = [trojan("alice", STORED_PASSWORD)]
        self.config["inbounds"][-1]["settings"]["users"].clear()
        self.save_config()
        with database.write_session() as conn:
            original = repository.get_client(conn, "alice")
            if already_repaired:
                repository.save_credential(conn, "alice", TROJAN_TAG, {
                    "id": BOB_ID, "protocol": "trojan", "enabled": True,
                    "client": trojan("alice", STORED_PASSWORD),
                })
            else:
                conn.execute("UPDATE clients SET enabled = 0 WHERE name = 'alice'")
            conn.execute(
                "DELETE FROM client_credentials WHERE client_name = 'alice' AND connection_tag = ?",
                (VLESS_TAG,),
            )

        before, config, env = db_dump(), self.box.paths.config.read_bytes(), self.box.paths.server_env.read_bytes()
        preview = self.run_client("repair", stdin="n\n")
        self.assertIn(f"alice — {VLESS_TAG} (vless) [disabled]", preview.stdout)
        self.assert_unchanged(before, config, env)

        result = self.run_client("repair", stdin="y\n")
        restored_count = 1 if already_repaired else 2
        self.assertIn(f"Restored credentials in SQLite: {restored_count}", result.stdout)
        with database.session() as conn:
            entry = repository.get_client(conn, "alice")
            self.assertEqual(entry["id"], original["id"])
            self.assertEqual(entry["connection"], VLESS_TAG)
            self.assertEqual(set(entry["credentials"]), {VLESS_TAG, TROJAN_TAG})
            self.assertFalse(entry["credentials"][VLESS_TAG]["enabled"])
            self.assertTrue(entry["credentials"][TROJAN_TAG]["enabled"])
            vless_id = original["credentials"][VLESS_TAG]["id"]
            self.assertEqual(entry["credentials"][VLESS_TAG]["id"], vless_id)
            self.assertEqual(entry["credentials"][TROJAN_TAG]["client"]["password"], STORED_PASSWORD)

        repaired = db_dump()
        self.assertIn("No missing client credentials", self.run_client("repair").stdout)
        self.assert_unchanged(repaired, config, env)
        self.assertEqual(self.box.processes.calls, [])

        with running_xray(self.box):
            script_processes(self.box)
            link = self.run_client("link", "alice", "--connection", VLESS_TAG).stdout.strip()
            self.assertEqual(urlsplit(link).username, vless_id)
            self.run_client("enable", "alice", "--connection", VLESS_TAG)
        active_vless = next(inbound for inbound in self.box.config()["inbounds"] if inbound["tag"] == VLESS_TAG)
        self.assertEqual([item["id"] for item in active_vless["settings"]["clients"]], [vless_id])

    def test_repair_preserves_disabled_legacy_vless_when_adding_trojan(self):
        self.assert_repair_recovers_disabled_vless(already_repaired=False)

    def test_repeat_repair_finds_legacy_vless_hidden_by_prior_repair(self):
        self.assert_repair_recovers_disabled_vless(already_repaired=True)

    def test_repair_skips_legacy_credential_when_its_inbound_is_missing(self):
        self.config["inbounds"].remove(self.vless_inbound)
        self.config["inbounds"][-2]["settings"]["users"].clear()
        self.config["inbounds"][-1]["settings"]["users"].clear()
        self.save_config()
        with database.write_session() as conn:
            repository.save_credential(conn, "alice", TROJAN_TAG, {
                "id": BOB_ID, "protocol": "trojan", "enabled": False,
                "client": trojan("alice", STORED_PASSWORD),
            })
            conn.execute(
                "DELETE FROM client_credentials WHERE client_name = 'alice' AND connection_tag = ?",
                (VLESS_TAG,),
            )

        before, config, env = db_dump(), self.box.paths.config.read_bytes(), self.box.paths.server_env.read_bytes()
        result = self.run_client("repair", stdin="y\n")
        self.assertIn("No missing client credentials", result.stdout)
        self.assert_unchanged(before, config, env)
        with running_xray(self.box):
            script_processes(self.box)
            self.run_client("enable", "alice")

    def repair_with_legacy_primary_absent_from_config(self):
        with database.write_session() as conn:
            repository.save_credential(conn, "alice", TROJAN_TAG, {
                "id": BOB_ID, "protocol": "trojan", "enabled": True, "client": trojan("alice", STORED_PASSWORD),
            })
            conn.execute("DELETE FROM client_credentials WHERE client_name = 'alice' AND connection_tag = ?", (VLESS_TAG,))
            original = repository.list_clients(conn, normalize=False)["alice"]
        self.vless_inbound["settings"]["clients"].clear()
        self.config["inbounds"][-1]["settings"]["users"].append(trojan("alice", STORED_PASSWORD))
        self.save_config()
        self.run_client("repair", stdin="y\n")
        return original

    def test_repair_preserves_legacy_primary_absent_from_config(self):
        original = self.repair_with_legacy_primary_absent_from_config()
        self.run_client("set-payment", "alice", "paid")
        with database.session() as conn:
            restored = repository.list_clients(conn, normalize=False)["alice"]
            self.assertEqual(restored["connection"], VLESS_TAG)
            self.assertEqual(restored["client"], original["client"])
            self.assertEqual(restored["id"], original["id"])
            self.assertEqual(restored["paymentType"], "paid")
            self.assertEqual(set(restored["credentials"]), {VLESS_TAG, TROJAN_TAG, SECOND_TAG})
            self.assertFalse(restored["credentials"][VLESS_TAG]["enabled"])

    def test_remove_secondary_after_repair_preserves_remaining_credential_and_traffic(self):
        original = self.repair_with_legacy_primary_absent_from_config()
        self.assertTrue(traffic_repository.write_traffic_db_to_sqlite_for_write({
            "clients": {"alice": {"incoming": 3072}},
            "credentials": {"alice": {TROJAN_TAG: {"incoming": 1024}, SECOND_TAG: {"incoming": 2048}}},
        }, strict=True))
        with running_xray(self.box):
            script_processes(self.box)
            result = self.run_client("remove-connection", SECOND_TAG)
        self.assertIn("Kept clients with other connections: alice", result.stdout)
        self.assertNotIn(SECOND_TAG, [inbound["tag"] for inbound in self.box.config()["inbounds"]])
        with database.session() as conn:
            remaining = repository.list_clients(conn, normalize=False)["alice"]
            self.assertEqual((remaining["id"], remaining["connection"], remaining["client"]),
                             (original["id"], VLESS_TAG, original["client"]))
            vless = remaining["credentials"].pop(VLESS_TAG)
            self.assertEqual(vless["id"], original["client"]["id"])
            self.assertFalse(vless["enabled"])
            self.assertEqual(remaining["credentials"], original["credentials"])
            self.assertEqual(repository.get_traffic_entry(conn, "alice")["incoming"], 3072)
            traffic = conn.execute(
                "SELECT connection_tag, incoming_bytes FROM credential_traffic_totals WHERE client_name = 'alice'",
            ).fetchall()
            self.assertEqual([tuple(row) for row in traffic], [(TROJAN_TAG, 1024)])

    def test_decline_and_eof_cancel_without_taking_the_lock(self):
        before, config, env = db_dump(), self.box.paths.config.read_bytes(), self.box.paths.server_env.read_bytes()
        for answer in ("n\n", ""):
            self.assertIn("Repair cancelled", self.run_client("repair", stdin=answer).stdout)
            self.assertFalse(self.box.paths.manager_lock.exists())
            self.assert_unchanged(before, config, env)

    def test_repair_rechecks_credentials_after_confirmation(self):
        before = db_dump()
        answer = StringIO("y\n")

        def change_password(size=-1):
            self.assertFalse(self.box.paths.manager_lock.exists())
            self.config["inbounds"][-1]["settings"]["users"][0]["password"] = STORED_PASSWORD
            self.save_config()
            return "y\n"

        answer.readline = change_password
        result = self.run_client("repair", stdin=answer, code=1)
        self.assertIn("Repair candidates changed", result.stderr)
        self.assertEqual(db_dump(), before)
        self.assertEqual(self.box.processes.calls, [])

    def test_all_writes_roll_back_on_sqlite_failure_or_cancel(self):
        self.vless_inbound["settings"]["clients"].clear()
        self.config["inbounds"][-1]["settings"]["users"].append(trojan("alice", PASSWORD))
        self.save_config()
        with database.write_session() as conn:
            conn.execute("UPDATE clients SET enabled = 0 WHERE name = 'alice'")
            conn.execute("DELETE FROM client_credentials WHERE client_name = 'alice'")
            conn.execute("""
                CREATE TRIGGER reject_bob BEFORE INSERT ON client_credentials
                WHEN NEW.client_name = 'bob'
                BEGIN SELECT RAISE(ABORT, 'fixture SQLite failure'); END
            """)
        before, config, env = db_dump(), self.box.paths.config.read_bytes(), self.box.paths.server_env.read_bytes()
        result = self.run_client("repair", stdin="y\n", code=1)
        self.assertIn("fixture SQLite failure", result.stderr)
        self.assert_unchanged(before, config, env)
        with mock.patch.object(service.repository, "save_client", side_effect=KeyboardInterrupt):
            self.run_client("repair", stdin="y\n", code=130)
        self.assert_unchanged(before, config, env)

    def test_repair_rejects_an_ambiguous_credential_without_showing_its_value(self):
        self.config["inbounds"][-1]["settings"]["users"].append(trojan("other", SECOND_PASSWORD))
        self.save_config()
        before, config, env = db_dump(), self.box.paths.config.read_bytes(), self.box.paths.server_env.read_bytes()
        result = self.run_client("repair", stdin="y\n", code=1)
        self.assertIn("Ambiguous client or credential", result.stderr)
        self.assertNotIn(SECOND_PASSWORD, result.stdout + result.stderr)
        self.assert_unchanged(before, config, env)
