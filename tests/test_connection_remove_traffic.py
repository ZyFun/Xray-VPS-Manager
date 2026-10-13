"""Регрессия XR-REV-003: удаление подключения не стирает трафик клиентов с другими credentials."""

from contextlib import contextmanager, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.clients import connections as client_connections
from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.commands import client as client_command
from xray_vps_manager.commands import menu_reality_actions
from xray_vps_manager.core import paths as core_paths
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import settings as sqlite_settings
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
        "inbounds": [reality_inbound(KEPT_TAG, 443), reality_inbound(REMOVED_TAG, 8443)],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "blocked", "protocol": "blackhole"}],
        "routing": {"rules": []},
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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "manager.db"

        config = base_config()
        db = {"connections": {}, "clients": {}}
        build_clients(config, db)
        (self.root / "config.json").write_text(json.dumps(config))

        connection = database.open_database(self.db_path)
        try:
            sqlite_settings.set_metadata(connection, "jsonImport.completed", "true")
        finally:
            connection.close()
        client_repository.save_db(db, db_path=self.db_path)

        connection = database.open_database(self.db_path)
        try:
            sqlite_traffic.upsert_traffic_entry(connection, "alice", traffic_entry(3000, 6000))
            sqlite_traffic.upsert_credential_traffic_entry(connection, "alice", KEPT_TAG, traffic_entry(1000, 2000))
            sqlite_traffic.upsert_credential_traffic_entry(connection, "alice", REMOVED_TAG, traffic_entry(2000, 4000))
            sqlite_traffic.upsert_traffic_entry(connection, "bob", traffic_entry(500, 700))
            sqlite_traffic.upsert_credential_traffic_entry(connection, "bob", REMOVED_TAG, traffic_entry(500, 700))
        finally:
            connection.close()

    @contextmanager
    def sandbox(self):
        with mock.patch.object(core_paths, "MANAGER_LOCK_PATH", self.root / "manager.lock"), \
            mock.patch.object(database, "MANAGER_DB_PATH", self.db_path), \
            mock.patch.object(client_command, "CONFIG_PATH", self.root / "config.json"), \
            mock.patch.object(client_command, "save_config", self.write_config), \
            mock.patch.object(client_command, "restart_xray_with_config_test", lambda: None), \
            mock.patch.object(client_command.client_connections, "server_env_values", return_value={}):
            yield

    def write_config(self, config: dict) -> Path:
        (self.root / "config.json").write_text(json.dumps(config))
        return self.root / "config.json.bak"

    def run_command(self, func, *args) -> str:
        output = StringIO()
        with self.sandbox(), redirect_stdout(output):
            func(*args)
        return output.getvalue()

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
        self.run_command(client_command.cmd_connection_remove, REMOVED_TAG)

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
        self.run_command(client_command.cmd_connection_remove, REMOVED_TAG)

        traffic = self.read_traffic()
        self.assertNotIn("bob", traffic["totals"])
        self.assertEqual(traffic["history"]["bob"], {})
        self.assertNotIn("bob", traffic["credentials"])
        self.assertEqual(traffic["credential_history"][("bob", REMOVED_TAG)], {})
        self.assertEqual(traffic["credential_history"][("alice", REMOVED_TAG)], {})

    def test_remove_connection_output_does_not_call_kept_client_removed(self) -> None:
        output = self.run_command(client_command.cmd_connection_remove, REMOVED_TAG)

        self.assertIn("Removed clients: bob\n", output)
        self.assertIn("Removed traffic history for deleted clients.\n", output)
        self.assertIn("Kept clients with other connections: alice\n", output)
        removed_line = next(line for line in output.splitlines() if line.startswith("Removed clients:"))
        self.assertNotIn("alice", removed_line)

    def test_remove_client_deletes_traffic_and_reports_it(self) -> None:
        output = self.run_command(client_command.cmd_remove, "bob")

        self.assertIn("Removed client: bob\n", output)
        self.assertIn("Removed traffic history.\n", output)
        traffic = self.read_traffic()
        self.assertNotIn("bob", traffic["totals"])
        self.assertEqual(traffic["history"]["bob"], {})
        self.assertNotIn("bob", traffic["credentials"])
        self.assertEqual(traffic["totals"]["alice"]["incoming"], 3000)


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
