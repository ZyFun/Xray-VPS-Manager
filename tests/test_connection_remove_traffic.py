"""Регрессия XR-REV-003: удаление подключения не стирает трафик клиентов с другими credentials."""

from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest import mock

from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.commands import menu_reality_actions
from tests.support.sandbox import Sandbox
from tests.test_restart_verification import FakeClock, FakeSocket, show_output
from xray_vps_manager.core import systemd
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import traffic as sqlite_traffic

KEPT_TAG = "vless-reality"
REMOVED_TAG = "vless-reality-2"
DAY = "2026-10-12"


def reality_inbound(tag: str, port: int) -> dict:
    return {
        "tag": tag,
        "listen": "0.0.0.0",
        "port": port,
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "dest": "example.com:443",
                "serverNames": ["example.com"],
                "privateKey": "test-private-key",
                "shortIds": [""],
            },
        },
    }


def base_config() -> dict:
    return {
        "inbounds": [reality_inbound(KEPT_TAG, 443), reality_inbound(REMOVED_TAG, 8443),
                     {"tag": "api", "listen": "127.0.0.1", "port": 10085, "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1", "clients": []}}],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "blocked", "protocol": "blackhole"}],
        "api": {"tag": "api", "services": ["StatsService"]},
        "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]},
    }


def uuid_sequence():
    counter = iter(range(1, 100))
    return lambda: f"00000000-0000-4000-8000-{next(counter):012d}"


def build_clients(config: dict, db: dict) -> None:
    """alice — credentials на обоих подключениях, bob — только на удаляемом."""
    uuid_factory = uuid_sequence()
    client_crud.add_client(config, db, "alice", connection_tag=KEPT_TAG, uuid_factory=uuid_factory)
    client_crud.add_client(config, db, "alice", connection_tag=REMOVED_TAG, uuid_factory=uuid_factory)
    client_crud.add_client(config, db, "bob", connection_tag=REMOVED_TAG, uuid_factory=uuid_factory)


def traffic_entry(incoming: int, outgoing: int) -> dict:
    return {
        "incoming": incoming,
        "outgoing": outgoing,
        "history": {DAY: {"10": {"incoming": incoming, "outgoing": outgoing}}},
    }


class RemoveConnectionResultTest(unittest.TestCase):
    def test_result_separates_removed_and_detached_clients(self) -> None:
        config = base_config()
        db = {"connections": {}, "clients": {}}
        build_clients(config, db)

        result = client_connections.remove_connection(config, db, REMOVED_TAG)

        self.assertEqual(result.removed_client_names, ["bob"])
        self.assertEqual(result.detached_client_names, ["alice"])
        self.assertEqual(set(db["clients"]), {"alice"})
        self.assertEqual(set(db["clients"]["alice"]["credentials"]), {KEPT_TAG})


class RemoveConnectionTrafficTest(unittest.TestCase):
    def setUp(self) -> None:
        self.box = self.enterContext(Sandbox(db=None))
        self.db_path = self.box.paths.manager_db
        self.enterContext(mock.patch.object(systemd, "time", FakeClock()))
        self.enterContext(mock.patch("socket.create_connection", return_value=FakeSocket()))
        ok = self.box.processes.ok()
        self.box.processes.script({
            ("/usr/local/bin/xray", "run", "-test", "-config", str(self.box.paths.config)): ok,
            (str(self.box.paths.xray_bin), "run", "-test", "-format", "json", "-config", str(self.box.paths.config.with_suffix(".json.tmp"))): ok,
            ("systemctl", "stop", "xray"): ok,
            ("systemctl", "reset-failed", "xray"): ok,
            ("systemctl", "restart", "xray"): ok,
            ("systemctl", "show", "--property=ActiveState,SubState,NRestarts", "xray"):
                self.box.processes.ok(show_output()),
        })
        config = base_config()
        db = {"connections": {}, "clients": {}}
        build_clients(config, db)
        for record in db["connections"].values():
            record["publicKey"] = "A" * 43
        self.box.paths.config.write_text(json.dumps(config))
        client_repository.save_db(db)
        with database.write_session() as connection:
            sqlite_traffic.upsert_traffic_entry(connection, "alice", traffic_entry(3000, 6000))
            sqlite_traffic.upsert_credential_traffic_entry(connection, "alice", KEPT_TAG, traffic_entry(1000, 2000))
            sqlite_traffic.upsert_credential_traffic_entry(connection, "alice", REMOVED_TAG, traffic_entry(2000, 4000))
            sqlite_traffic.upsert_traffic_entry(connection, "bob", traffic_entry(500, 700))
            sqlite_traffic.upsert_credential_traffic_entry(connection, "bob", REMOVED_TAG, traffic_entry(500, 700))

    def run_command(self, *args) -> str:
        result = self.box.run_cli("client", *args)
        self.assertEqual(result.exit_code, 0, result.stderr)
        return result.stdout

    def read_traffic(self):
        connection = database.open_database(self.db_path)
        try:
            return {
                "totals": sqlite_traffic.list_traffic_entries(connection),
                "credentials": sqlite_traffic.list_credential_traffic_entries(connection),
                "history": {
                    name: sqlite_traffic.history_for_client(connection, name) for name in ("alice", "bob")
                },
                "credential_history": {
                    (name, tag): sqlite_traffic.credential_history_for_client(connection, name, tag)
                    for name, tag in (("alice", KEPT_TAG), ("alice", REMOVED_TAG), ("bob", REMOVED_TAG))
                },
            }
        finally:
            connection.close()

    def test_remove_connection_keeps_traffic_of_client_with_other_credentials(self) -> None:
        self.run_command("remove-connection", REMOVED_TAG)

        db = client_repository.load_db(db_path=self.db_path)
        self.assertEqual(set(db["clients"]), {"alice"})
        self.assertEqual(set(db["clients"]["alice"]["credentials"]), {KEPT_TAG})

        traffic = self.read_traffic()
        self.assertEqual(set(traffic["totals"]), {"alice"})
        self.assertEqual(traffic["totals"]["alice"]["incoming"], 3000)
        self.assertEqual(traffic["totals"]["alice"]["outgoing"], 6000)
        self.assertEqual(traffic["history"]["alice"], {DAY: {"10": {"incoming": 3000, "outgoing": 6000}}})
        self.assertEqual(set(traffic["credentials"]), {"alice"})
        self.assertEqual(set(traffic["credentials"]["alice"]), {KEPT_TAG})
        self.assertEqual(traffic["credentials"]["alice"][KEPT_TAG]["incoming"], 1000)
        self.assertEqual(
            traffic["credential_history"][("alice", KEPT_TAG)],
            {DAY: {"10": {"incoming": 1000, "outgoing": 2000}}},
        )

    def test_remove_connection_deletes_traffic_of_removed_credential_and_client(self) -> None:
        self.run_command("remove-connection", REMOVED_TAG)

        traffic = self.read_traffic()
        self.assertNotIn("bob", traffic["totals"])
        self.assertEqual(traffic["history"]["bob"], {})
        self.assertNotIn("bob", traffic["credentials"])
        self.assertEqual(traffic["credential_history"][("bob", REMOVED_TAG)], {})
        self.assertEqual(traffic["credential_history"][("alice", REMOVED_TAG)], {})

    def test_remove_connection_output_does_not_call_kept_client_removed(self) -> None:
        output = self.run_command("remove-connection", REMOVED_TAG)

        self.assertIn("Removed clients: bob\n", output)
        self.assertIn("Removed traffic history for deleted clients.\n", output)
        self.assertIn("Kept clients with other connections: alice\n", output)
        removed_line = next(line for line in output.splitlines() if line.startswith("Removed clients:"))
        self.assertNotIn("alice", removed_line)

    def test_remove_client_deletes_traffic_and_reports_it(self) -> None:
        output = self.run_command("remove", "bob")

        self.assertIn("Removed client: bob\n", output)
        self.assertIn("Removed traffic history.\n", output)
        traffic = self.read_traffic()
        self.assertNotIn("bob", traffic["totals"])
        self.assertEqual(traffic["history"]["bob"], {})
        self.assertNotIn("bob", traffic["credentials"])
        self.assertEqual(traffic["totals"]["alice"]["incoming"], 3000)

    def test_move_replaces_only_source_credential(self):
        stale = client_repository.load_db()
        self.run_command("move-connection", "bob", KEPT_TAG)
        client_repository.save_db(stale)
        db = client_repository.load_db()
        self.assertEqual(set(db["clients"]["bob"]["credentials"]), {KEPT_TAG})
        self.assertEqual(db["clients"]["bob"]["connection"], KEPT_TAG)
        self.assertEqual(set(db["clients"]["alice"]["credentials"]), {KEPT_TAG, REMOVED_TAG})
        self.assertNotIn(REMOVED_TAG, self.read_traffic()["credentials"].get("bob", {}))
        clients_by_tag = {inbound["tag"]: [c["email"].split("|")[0] for c in inbound["settings"]["clients"]]
                          for inbound in self.box.config()["inbounds"]}
        self.assertIn("bob", clients_by_tag[KEPT_TAG])
        self.assertNotIn("bob", clients_by_tag[REMOVED_TAG])

    def test_disabled_move_commits_credentials_without_restarting(self):
        self.run_command("disable", "bob")
        before_config = self.box.paths.config.read_bytes()
        before_restarts = self.box.processes.calls.count(("systemctl", "restart", "xray"))
        self.run_command("move-connection", "bob", KEPT_TAG)
        self.assertEqual(self.box.paths.config.read_bytes(), before_config)
        self.assertEqual(self.box.processes.calls.count(("systemctl", "restart", "xray")), before_restarts)
        with database.session() as conn:
            entry = client_repository.get_client(conn, "bob")
            self.assertFalse(entry["enabled"])
            self.assertEqual(set(entry["credentials"]), {KEPT_TAG})

    def test_failed_connection_delete_restores_config_credentials_and_traffic(self):
        before_config = self.box.paths.config.read_bytes()
        before_db = client_repository.load_db()
        before_traffic = self.read_traffic()
        with database.write_session() as conn:
            conn.execute("CREATE TRIGGER veto BEFORE DELETE ON reality_connections "
                         "BEGIN SELECT RAISE(ABORT, 'delete refused'); END")
        failed = self.box.run_cli("client", "remove-connection", REMOVED_TAG)
        self.assertEqual(failed.exit_code, 1)
        self.assertIn("delete refused", failed.stderr)
        self.assertEqual(self.box.paths.config.read_bytes(), before_config)
        self.assertEqual(client_repository.load_db(), before_db)
        self.assertEqual(self.read_traffic(), before_traffic)

    def seed_routes(self):
        self.enterContext(mock.patch("socket.create_connection", return_value=FakeSocket()))
        config = self.box.config()
        db = client_repository.load_db()
        for tag in ("cascade-old", "cascade-next"):
            config["outbounds"].append({"tag": tag, "protocol": "vless"})
            db["cascadeRoutes"][tag] = {"tag": tag, "label": tag}
        config["routing"]["rules"].append({"type": "field", "network": "tcp,udp", "outboundTag": "cascade-old"})
        db["clients"]["alice"]["selectedCascadeTag"] = "cascade-old"
        client_repository.save_db(db)
        self.box.paths.config.write_text(json.dumps(config))

    def test_route_remove_reassigns_to_active_cascade_then_clears_selection(self):
        self.seed_routes()
        for name, expected in (("old", "cascade-next"), ("next", "")):
            result = self.box.run_cli("cascade", "remove", name)
            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            with database.session() as conn:
                self.assertIsNone(client_repository.get_route(conn, "cascade-" + name))
                self.assertEqual(client_repository.get_client(conn, "alice")["selectedCascadeTag"], expected)

    def test_route_delete_failure_restores_config_and_sql_batch(self):
        self.seed_routes()
        before = client_repository.load_db()
        original_config = self.box.paths.config.read_bytes()
        with database.write_session() as conn:
            conn.execute("CREATE TRIGGER veto BEFORE DELETE ON cascade_routes "
                         "BEGIN SELECT RAISE(ABORT, 'route refused'); END")
        result = self.box.run_cli("cascade", "remove", "old")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("route refused", result.stderr)
        self.assertEqual(client_repository.load_db(), before)
        self.assertEqual(self.box.paths.config.read_bytes(), original_config)


class MenuDeleteConnectionWarningTest(unittest.TestCase):
    def run_menu(self, func, rows, tag) -> str:
        call = mock.Mock()
        output = StringIO()
        with mock.patch.object(menu_reality_actions, "connection_rows", return_value=rows), \
            mock.patch.object(menu_reality_actions, "choose_connection", return_value=tag), \
            redirect_stdout(output):
            func(call, lambda _prompt: False)
        call.assert_not_called()
        return output.getvalue()

    def assert_warning(self, output: str) -> None:
        self.assertNotIn("Все клиенты этого подключения также будут удалены", output)
        self.assertIn("Клиенты без других подключений будут удалены вместе с историей трафика.", output)
        self.assertIn("клиент и его трафик останутся", output)

    def test_vless_delete_warning_keeps_clients_with_other_connections(self) -> None:
        rows = [{"tag": KEPT_TAG, "name": "main"}, {"tag": REMOVED_TAG, "name": "second"}]

        self.assert_warning(self.run_menu(menu_reality_actions.delete_connection, rows, REMOVED_TAG))

    def test_trojan_delete_warning_keeps_clients_with_other_connections(self) -> None:
        rows = [{"tag": "trojan-tls", "name": "trojan"}]

        self.assert_warning(self.run_menu(menu_reality_actions.delete_trojan_connection, rows, "trojan-tls"))


if __name__ == "__main__":
    unittest.main()
