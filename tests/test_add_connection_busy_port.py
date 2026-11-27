"""VIREI-232. Отказ при занятом TCP-порте до записи нового подключения."""

import json
import sqlite3
import unittest
from contextlib import closing
from hashlib import sha256
from unittest.mock import patch

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_client_characterization import free_https_port
from tests.test_connection_menu_characterization import REALITY, TROJAN, script_menu_processes
from xray_vps_manager.caddy import sites as caddy_sites
from xray_vps_manager.clients import repository
from xray_vps_manager.core import process
from xray_vps_manager.db import database


class AddConnectionBusyPortTests(unittest.TestCase):
    PORT = 18100
    EXTERNAL_PORT = 8443

    def file_state(self, paths):
        return {path.name: (sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in paths}

    def run_add(self, box, protocol, *, menu=False, caddy=False, port=PORT):
        number = str(port)
        if menu:
            tls_choices = ("",) if caddy else ("n", str(self.EXTERNAL_PORT))
            if protocol == "reality":
                prefix, answers = REALITY, ("2", "1", "Reserve", number, "reserve.example", "3", "1")
            elif protocol == "vless-tls":
                prefix = REALITY
                answers = ("2", "2", "TLS", "tls.example", number, "1", "/secure", "", "n", "2", *tls_choices)
            else:
                prefix = TROJAN
                answers = ("2", "Trojan", number, "trojan.example", "/tunnel", "1", "2", *tls_choices)
            return box.run_menu((*prefix, *answers, "0", "0", "0"))

        if protocol == "reality":
            args = ("add-connection", "Reserve", number, "reserve.example")
        elif protocol == "vless-tls":
            tls_options = ("--install-caddy",) if caddy else ("--public-port", str(self.EXTERNAL_PORT))
            args = ("add-connection", "TLS", number, "tls.example", "--security", "tls", *tls_options)
        elif protocol == "trojan-tcp":
            args = (
                "add-trojan-connection", "Trojan", number, "trojan.example",
                "/etc/ssl/cert.pem", "/etc/ssl/key.pem", "chrome", "--transport", "tcp",
            )
        else:
            tls_options = () if caddy else ("--no-caddy", "--public-port", str(self.EXTERNAL_PORT))
            args = ("add-trojan-connection", "Trojan", number, "trojan.example", *tls_options)
        return box.run_cli("client", *args)

    def check_add(self, *, protocol, field, host, menu=False, caddy=False, conflict="system"):
        with Sandbox() as box, running_xray(box):
            script_menu_processes(box)
            free_https_port(box)
            working_files = (box.paths.config, box.paths.manager_db, box.paths.server_env)
            before = self.file_state(working_files)
            port = 5443 if conflict == "inbound" else self.PORT
            public_port = 443 if caddy else self.EXTERNAL_PORT

            def port_in_use(candidate, listen):
                self.assertEqual(listen, host)
                if candidate == public_port and field == "LOCAL_PORT":
                    return True
                self.assertEqual(candidate, port)
                return conflict == "system"

            with patch.object(process, "tcp_port_in_use", side_effect=port_in_use) as probe:
                result = self.run_add(box, protocol, menu=menu, caddy=caddy, port=port)

            self.assertEqual(result.exit_code, 0 if menu or not conflict else 1, result)
            if menu:
                self.assertNotIn("Ввод закрыт, выход из меню.", result.stdout)
            if conflict:
                reason = ("is already used by another inbound" if conflict == "inbound"
                          else "is already in use on this server by another process")
                self.assertIn(f"{field} {reason}: {port}", result.stdout + result.stderr)
                self.assertEqual(self.file_state(working_files), before)
                self.assertEqual(list(box.paths.config_dir.glob("config.json.bak.*")), [])
                self.assertFalse(box.paths.caddyfile.exists())
                self.assertFalse(box.paths.caddy_conf_dir.exists())
                self.assertFalse(any(call[0] in ("systemctl", "caddy") or "-test" in call
                                     for call in box.processes.calls), box.processes.calls)
            else:
                tag = {"reality": "vless-reality-2", "vless-tls": "vless-tls"}.get(protocol, "trojan-tls")
                inbound = next(item for item in box.config()["inbounds"] if item["tag"] == tag)
                self.assertEqual((inbound["port"], inbound["listen"]), (port, host))
                record = repository.load_db_sql()["connections"][tag]
                self.assertEqual(record["port"], public_port if field == "LOCAL_PORT" else port)
                self.assertIn(("systemctl", "restart", "xray"), box.processes.calls)
            if conflict == "inbound":
                probe.assert_not_called()
            else:
                self.assertTrue(probe.call_args_list)
                self.assertTrue(all(call.args == (port, host) for call in probe.call_args_list), probe.call_args_list)

    def test_reality_and_direct_trojan_use_public_inbound_port(self):
        for protocol, menu in (("reality", False), ("reality", True), ("trojan-tcp", False)):
            for conflict in ("system", "inbound", None):
                with self.subTest(protocol=protocol, menu=menu, conflict=conflict):
                    self.check_add(protocol=protocol, field="PORT", host="0.0.0.0", menu=menu, conflict=conflict)

    def test_vless_tls_and_trojan_ws_use_local_inbound_port(self):
        for protocol in ("vless-tls", "trojan-ws"):
            for menu in (False, True):
                for caddy in (False, True):
                    for conflict in ("system", "inbound", None):
                        with self.subTest(protocol=protocol, caddy=caddy, menu=menu, conflict=conflict):
                            self.check_add(protocol=protocol, field="LOCAL_PORT", host="127.0.0.1",
                                           menu=menu, caddy=caddy, conflict=conflict)

    def prepare_schema_7(self, db_path):
        with closing(sqlite3.connect(db_path)) as conn:
            conn.executescript("""
                PRAGMA journal_mode = DELETE;
                DROP INDEX idx_activity_alert_events_raw_ref_event;
                DROP INDEX idx_activity_blocklist_source_event;
                DELETE FROM schema_migrations WHERE version = 8;
                PRAGMA user_version = 7;
            """)

    def test_busy_port_does_not_migrate_old_database(self):
        with Sandbox() as box, running_xray(box):
            script_menu_processes(box)
            db_path = box.paths.manager_db
            self.prepare_schema_7(db_path)
            before = self.file_state((db_path,))

            with patch.object(process, "tcp_port_in_use", return_value=True), \
                    patch.object(database, "connect", wraps=database.connect) as connect:
                busy = self.run_add(box, "reality")
            connect.assert_not_called()
            self.assertEqual(busy.exit_code, 1)
            self.assertIn(f"PORT is already in use on this server by another process: {self.PORT}", busy.stderr)
            self.assertEqual(self.file_state((db_path,)), before)

            free = self.run_add(box, "reality")
            self.assertEqual(free.exit_code, 0, free)
            with closing(sqlite3.connect(db_path)) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone(), (8,))

    def test_config_port_conflicts_are_rejected_before_opening_sqlite(self):
        """232-R01. Отказ по конфигу сохраняет старую БД и приоритет ошибки Caddy."""
        public_caddy_error = "Caddy cannot listen on public port 443: it is already used by Xray inbound(s)"
        cases = (
            ("reality", False, 443, "PORT is already used by another inbound: 443"),
            ("trojan-ws", True, self.PORT, public_caddy_error),
            ("vless-tls", True, 443, public_caddy_error),
        )
        for protocol, caddy, port, message in cases:
            with self.subTest(protocol=protocol), Sandbox() as box, running_xray(box):
                script_menu_processes(box)
                working_files = (box.paths.config, box.paths.manager_db, box.paths.server_env)
                self.prepare_schema_7(box.paths.manager_db)
                before = self.file_state(working_files)

                with patch.object(process, "tcp_port_in_use", return_value=True) as probe, \
                        patch.object(database, "connect", wraps=database.connect) as connect:
                    busy = self.run_add(box, protocol, caddy=caddy, port=port)

                self.assertEqual(busy.exit_code, 1)
                self.assertIn(message, busy.stderr)
                connect.assert_not_called()
                probe.assert_not_called()
                self.assertEqual(self.file_state(working_files), before)
                self.assertEqual(list(box.paths.config_dir.glob("config.json.bak.*")), [])
                self.assertNotIn(("systemctl", "restart", "xray"), box.processes.calls)

    def test_string_ports_in_xray_config_take_priority_over_system_probe(self):
        """232-R02. Строковый порт и диапазоны принадлежат inbound Xray."""
        port_values = ("18100", "18099-18101", "18070, 18099-18101")
        for protocol, field in (("reality", "PORT"), ("trojan-ws", "LOCAL_PORT")):
            for value in port_values:
                with self.subTest(protocol=protocol, port=value), Sandbox() as box:
                    config = box.config()
                    config["inbounds"].append({
                        "tag": "socks-fixture",
                        "protocol": "socks",
                        "listen": "0.0.0.0",
                        "port": value,
                    })
                    box.paths.config.write_text(json.dumps(config))

                    with patch.object(process, "tcp_port_in_use", return_value=True) as probe:
                        result = self.run_add(box, protocol)

                    self.assertEqual(result.exit_code, 1)
                    self.assertIn(f"{field} is already used by another inbound: {self.PORT}", result.stderr)
                    probe.assert_not_called()

    def test_same_port_is_available_for_tcp_when_existing_inbound_only_listens_on_udp(self):
        """232-R04. UDP-only inbound не занимает TCP-порт при любом формате `port`."""
        port_values = (self.PORT, str(self.PORT), "18099-18101")
        for network in ("udp", "tcp", "tcp,udp"):
            for value in port_values:
                with self.subTest(network=network, port=value), Sandbox() as box, running_xray(box):
                    script_menu_processes(box)
                    config = box.config()
                    config["inbounds"].append({
                        "tag": "tunnel-fixture",
                        "protocol": "dokodemo-door",
                        "listen": "127.0.0.1",
                        "port": value,
                        "settings": {"address": "127.0.0.1", "port": 53, "network": network},
                    })
                    box.paths.config.write_text(json.dumps(config))

                    with patch.object(process, "tcp_port_in_use", return_value=False) as probe:
                        result = self.run_add(box, "trojan-ws")

                    if network == "udp":
                        self.assertEqual(result.exit_code, 0, result.stderr)
                        probe.assert_called_once_with(self.PORT, "127.0.0.1")
                        self.assertIn(("systemctl", "restart", "xray"), box.processes.calls)
                    else:
                        self.assertEqual(result.exit_code, 1)
                        self.assertIn(f"LOCAL_PORT is already used by another inbound: {self.PORT}", result.stderr)
                        probe.assert_not_called()

    def test_existing_caddy_site_takes_priority_over_system_port_conflict(self):
        """232-R03. CLI и меню сначала показывают конфликт существующего сайта."""
        for protocol, domain in (("vless-tls", "tls.example"), ("trojan-ws", "trojan.example")):
            for menu in (False, True):
                with self.subTest(protocol=protocol, menu=menu), Sandbox() as box:
                    script_menu_processes(box)
                    free_https_port(box)
                    site = caddy_sites.write_site_config(domain, 19000)
                    working_files = (box.paths.config, box.paths.manager_db, box.paths.server_env,
                                     box.paths.caddyfile, site)
                    before = self.file_state(working_files)

                    with patch.object(process, "tcp_port_in_use", return_value=True) as probe, \
                            patch.object(database, "connect", wraps=database.connect) as connect:
                        result = self.run_add(box, protocol, caddy=True, menu=menu)

                    self.assertEqual(result.exit_code, 0 if menu else 1)
                    self.assertIn(f"Caddy site config already exists for {domain}", result.stdout + result.stderr)
                    probe.assert_not_called()
                    connect.assert_not_called()
                    self.assertEqual(self.file_state(working_files), before)
                    self.assertEqual(list(box.paths.config_dir.glob("config.json.bak.*")), [])
                    self.assertFalse(any(call[0] in ("systemctl", "caddy") or "-test" in call
                                         for call in box.processes.calls), box.processes.calls)
