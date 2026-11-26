"""Порядок и компенсация изменения подключений через публичный xray-client."""

from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_client_characterization import (
    CADDY_ACTIVATION_STATE, free_https_port, is_caddy_candidate_validation, script_processes, write_config,
)
from tests.test_client_command_read import script_caddy
from tests.test_client_manager_lock import lock_is_free
from tests.test_connection_menu_characterization import script_menu_processes
from xray_vps_manager.caddy import model as caddy_model, sites as caddy_sites
from xray_vps_manager.clients import connections, connections_service, links, repository
from xray_vps_manager.core import fs, locks
from xray_vps_manager.core.errors import ValidationError
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import client_traffic_handoff, client_undo
from xray_vps_manager.xray.client_routes import client_balancer_tag
from xray_vps_manager.xray.config import INBOUND_TAG, TROJAN_INBOUND_TAG, find_inbound_by_tag


TLS = ("add-connection", "TLS", "18100", "tls.example", "--security", "tls",
       "--transport", "xhttp", "--install-caddy")
TROJAN = ("add-trojan-connection", "Trojan", "18101", "trojan.example")
RELOAD = ("systemctl", "reload", "caddy")
ENABLE_NOW = ("systemctl", "enable", "--now", "caddy")
START = ("systemctl", "start", "caddy")
STOP = ("systemctl", "stop", "caddy")
DISABLE = ("systemctl", "disable", "caddy")
ENABLE_RUNTIME = ("systemctl", "enable", "--runtime", "caddy")
CADDY_BEFORE_INSTALL = ("systemctl", "show", "--property=LoadState,ActiveState,UnitFileState", "caddy")


def state(box):
    paths = box.paths
    files = (paths.config, paths.server_env, paths.initial_link, paths.caddyfile,
             *sorted(paths.caddy_conf_dir.glob("*.caddy")))
    return (
        {path: path.read_bytes() for path in files if path.exists()},
        box.db_rows("reality_connections", "clients", "client_credentials"),
    )


def seed_starter_file(box, tag=INBOUND_TAG):
    result = box.run_cli("client", "link", "alice", "--connection", tag)
    assert (result.exit_code, result.stderr) == (0, "")
    uri = result.stdout.strip()
    box.paths.initial_link.parent.mkdir(parents=True, exist_ok=True)
    box.paths.initial_link.write_text(f"INITIAL_PROTOCOL=vless\nCLIENT_URI={uri}\n")
    return uri


class RealityMenuConnectionTests(unittest.TestCase):
    def test_link_helpers_use_same_default_fingerprint_for_config_only_connection(self):
        with Sandbox() as box:
            script_processes(box)
            config = box.config()
            client_id = find_inbound_by_tag(config, INBOUND_TAG)["settings"]["clients"][0]["id"]
            for stored, expected in ((None, "firefox"), ("safari", "safari")):
                for helper in ("link_for", "client_link_lines", "connection_fingerprint"):
                    with self.subTest(stored=stored, helper=helper):
                        db = repository.load_db_sql()
                        if stored is None:
                            del db["connections"][INBOUND_TAG]
                        else:
                            db["connections"][INBOUND_TAG]["fingerprint"] = stored
                        if helper == "connection_fingerprint":
                            actual = connections.connection_fingerprint(
                                config, db, INBOUND_TAG, default_fingerprint="firefox",
                            )
                        else:
                            if helper == "link_for":
                                uri = links.link_for(
                                    config, client_id, "alice", INBOUND_TAG, db, default_fingerprint="firefox",
                                )
                            else:
                                uri = next(links.client_link_lines(config, db, "alice", default_fingerprint="firefox"))
                            actual = parse_qs(urlsplit(uri).query)["fp"][0]
                        self.assertEqual(actual, expected)

    def test_empty_inbound_skips_link_reads_and_updates_server_env(self):
        with Sandbox() as box:
            config = box.config()
            find_inbound_by_tag(config, INBOUND_TAG)["settings"]["clients"] = []
            write_config(box, config)
            box.paths.initial_link.parent.mkdir(parents=True, exist_ok=True)
            box.paths.initial_link.write_text("CLIENT_URI=starter\n")

            with mock.patch.object(repository, "load_db_sql", wraps=repository.load_db_sql) as full_read, \
                    mock.patch.object(repository, "get_client", wraps=repository.get_client) as read_client:
                connections_service.update_reality_fingerprint(INBOUND_TAG, "firefox")

            full_read.assert_not_called()
            read_client.assert_not_called()
            self.assertEqual(box.paths.initial_link.read_text(), "CLIENT_URI=starter\n")
            self.assertEqual(box.env()["FINGERPRINT"], "firefox")
            self.assertEqual(repository.load_db_sql()["connections"][INBOUND_TAG]["fingerprint"], "firefox")

    def test_starter_link_reads_only_the_saved_client_and_linked_connections(self):
        config_only = "vless-reality-2"
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            commands = (
                ("add-connection", "Config only", "8443", "second.example"),
                ("add", "alice", "--connection", config_only),
                ("add-trojan-connection", "Trojan", "18101", "trojan.example", "--no-caddy"),
                ("add", "alice", "--connection", TROJAN_INBOUND_TAG),
                ("add", "bob", "10", "--connection", INBOUND_TAG),
            )
            for args in commands:
                result = box.run_cli("client", *args)
                self.assertEqual((result.exit_code, result.stderr), (0, ""))
            with database.write_session() as conn:
                repository.delete_connection(conn, config_only)
                repository.save_connection(conn, "unused", {"name": "Unused", "port": 8444})
            seed_starter_file(box)
            config = box.config()
            inbound = find_inbound_by_tag(config, INBOUND_TAG)
            inbound["settings"]["clients"].reverse()
            write_config(box, config)

            with mock.patch.object(repository, "load_db_sql", wraps=repository.load_db_sql) as full_read, \
                    mock.patch.object(repository, "get_client", wraps=repository.get_client) as read_client, \
                    mock.patch.object(repository, "get_connection", wraps=repository.get_connection) as read_connection:
                change = connections_service.update_reality_fingerprint(INBOUND_TAG, "firefox")

            self.assertIsNone(change.link_error)
            full_read.assert_not_called()
            self.assertEqual([call.args[1] for call in read_client.call_args_list], ["alice"])
            read_tags = {call.args[1] for call in read_connection.call_args_list}
            self.assertIn(INBOUND_TAG, read_tags)
            self.assertNotIn("unused", read_tags)
            fields = dict(line.split("=", 1) for line in box.paths.initial_link.read_text().splitlines())
            current = box.run_cli("client", "link", "alice", "--connection", INBOUND_TAG)
            self.assertEqual(fields["CLIENT_URI"], current.stdout.strip())
            self.assertEqual(fields["VLESS_CLIENT_URI"], current.stdout.strip())
            self.assertEqual(fields["TROJAN_CLIENT_URI"], "")
            self.assertEqual(fields["FINGERPRINT"], "firefox")
            self.assertNotIn(config_only, repository.load_db_sql()["connections"])

    def test_initial_link_selection_visits_active_trojan_clients_linearly(self):
        class CountedClients(list):
            visits = 0

            def __iter__(self):
                for item in super().__iter__():
                    self.visits += 1
                    yield item

        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            for args in (
                ("add-trojan-connection", "Trojan", "18101", "trojan.example", "--no-caddy"),
                ("add", "alice", "--connection", TROJAN_INBOUND_TAG),
            ):
                result = box.run_cli("client", *args)
                self.assertEqual((result.exit_code, result.stderr), (0, ""))
            saved = repository.load_db_sql()["clients"]["alice"]["credentials"]
            identities = {
                "vless": {saved[INBOUND_TAG]["id"]},
                "trojan": {saved[TROJAN_INBOUND_TAG]["client"]["password"]},
            }
            for count, from_sqlite in ((1000, True), (2000, False)):
                with self.subTest(count=count, from_sqlite=from_sqlite):
                    config = box.config()
                    inbound = find_inbound_by_tag(config, TROJAN_INBOUND_TAG)
                    starter, = inbound["settings"]["clients"]
                    if from_sqlite:
                        starter["password"] = "fixture-active-password"
                    measured = CountedClients([starter, *(
                        {"email": f"fixture-{i}", "password": f"fixture-password-{i}"}
                        for i in range(count - 1)
                    )])
                    inbound["settings"]["clients"] = measured
                    db = repository.load_initial_link_state(config, identities)
                    if not from_sqlite:
                        del db["clients"]["alice"]["credentials"][TROJAN_INBOUND_TAG]
                    measured.visits = 0

                    selected = links.initial_link_credentials(config, db, "both", identities=identities)

                    self.assertEqual({row["name"] for row in selected.values()}, {"alice"})
                    self.assertLessEqual(measured.visits, 6 * count)

    def test_refresh_keeps_previous_trojan_link_without_confirmed_public_endpoint(self):
        for remove_record in (True, False):
            with self.subTest(remove_record=remove_record), Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
                script_menu_processes(box)
                for args in (
                    ("add-trojan-connection", "Trojan", "18101", "trojan.example", "--no-caddy"),
                    ("add", "alice", "--connection", TROJAN_INBOUND_TAG),
                ):
                    result = box.run_cli("client", *args)
                    self.assertEqual((result.exit_code, result.stderr), (0, ""))
                vless = seed_starter_file(box)
                trojan = box.run_cli("client", "link", "alice", "--connection", TROJAN_INBOUND_TAG)
                self.assertEqual((trojan.exit_code, trojan.stderr), (0, ""))
                self.assertEqual(urlsplit(trojan.stdout.strip()).port, 443)
                path = box.paths.initial_link
                path.write_text(
                    f"INITIAL_PROTOCOL=both\nCLIENT_URI={vless}\nVLESS_CLIENT_URI={vless}\n"
                    f"TROJAN_CLIENT_URI={trojan.stdout}UUID={urlsplit(vless).username}\n"
                    "TROJAN_DOMAIN=trojan.example\nTROJAN_LOCAL_PORT=18101\nTROJAN_PUBLIC_PORT=443\n"
                )
                before = path.read_bytes()
                with database.write_session() as conn:
                    if remove_record:
                        repository.delete_connection(conn, TROJAN_INBOUND_TAG)
                    else:
                        record = repository.get_connection(conn, TROJAN_INBOUND_TAG)
                        repository.save_connection(conn, TROJAN_INBOUND_TAG, dict(record, publicHost="", sni=""))
                inbound = find_inbound_by_tag(box.config(), TROJAN_INBOUND_TAG)
                self.assertEqual((inbound["listen"], inbound["port"]), ("127.0.0.1", 18101))

                changed = box.run_menu(["2", "1", "5", "5443", "updated.example", "0", "0", "0"])

                self.assertEqual(changed.exit_code, 0)
                self.assertIn("PORT обновлён: 5443", changed.stdout)
                self.assertIn("Не удалось восстановить стартовые ссылки", changed.stderr)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(box.env()["PORT"], "5443")

    def test_conflicting_protocol_fields_keep_the_start_file(self):
        for mode, swapped in (("vless", False), ("both", True)):
            with self.subTest(mode=mode, swapped=swapped), Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
                script_menu_processes(box)
                for args in (
                    ("add-trojan-connection", "Trojan", "18101", "trojan.example", "--no-caddy"),
                    ("add", "alice", "--connection", TROJAN_INBOUND_TAG),
                ):
                    result = box.run_cli("client", *args)
                    self.assertEqual((result.exit_code, result.stderr), (0, ""))
                vless = seed_starter_file(box)
                result = box.run_cli("client", "link", "alice", "--connection", TROJAN_INBOUND_TAG)
                self.assertEqual((result.exit_code, result.stderr), (0, ""))
                trojan = result.stdout.strip()
                vless_field, trojan_field = (trojan, vless) if swapped else (vless, trojan)
                path = box.paths.initial_link
                path.write_text(
                    f"INITIAL_PROTOCOL={mode}\nCLIENT_URI={vless}\nVLESS_CLIENT_URI={vless_field}\n"
                    f"TROJAN_CLIENT_URI={trojan_field}\nUUID={urlsplit(vless).username}\n",
                )
                before = path.read_bytes()

                changed = box.run_menu(["2", "1", "5", "5443", "updated.example", "0", "0", "0"])

                self.assertEqual(changed.exit_code, 0)
                self.assertIn("PORT обновлён: 5443", changed.stdout)
                self.assertIn(f"противоречит формату INITIAL_PROTOCOL={mode}", changed.stderr)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(box.env()["PORT"], "5443")

    def test_conflicting_primary_uri_keeps_the_start_file(self):
        for mode in ("vless", "both"):
            with self.subTest(mode=mode), Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
                script_menu_processes(box)
                for args in (
                    ("add-trojan-connection", "Trojan", "18101", "trojan.example", "--no-caddy"),
                    ("add", "alice", "--connection", TROJAN_INBOUND_TAG),
                ):
                    result = box.run_cli("client", *args)
                    self.assertEqual((result.exit_code, result.stderr), (0, ""))
                vless = seed_starter_file(box)
                result = box.run_cli("client", "link", "alice", "--connection", TROJAN_INBOUND_TAG)
                self.assertEqual((result.exit_code, result.stderr), (0, ""))
                trojan = result.stdout.strip()
                named_trojan = trojan if mode == "both" else ""
                path = box.paths.initial_link
                path.write_text(
                    f"INITIAL_PROTOCOL={mode}\nCLIENT_URI={trojan}\nVLESS_CLIENT_URI={vless}\n"
                    f"TROJAN_CLIENT_URI={named_trojan}\nUUID={urlsplit(vless).username}\n"
                )
                before = path.read_bytes()

                changed = box.run_menu(["2", "1", "5", "5443", "updated.example", "0", "0", "0"])

                self.assertEqual(changed.exit_code, 0)
                self.assertIn("PORT обновлён: 5443", changed.stdout)
                self.assertIn(f"противоречит формату INITIAL_PROTOCOL={mode}", changed.stderr)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(box.env()["PORT"], "5443")

    def test_repairs_historical_multiline_file_with_saved_starter_uuid(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            name = "first.custom"
            for args in (
                ("add-trojan-connection", "Trojan", "18101", "trojan.example", "--no-caddy"),
                ("add", name, "--connection", INBOUND_TAG),
                ("add", name, "--connection", TROJAN_INBOUND_TAG),
                ("disable", name),
            ):
                result = box.run_cli("client", *args)
                self.assertEqual((result.exit_code, result.stderr), (0, ""))
            old = box.run_cli("client", "link", name)
            self.assertEqual((old.exit_code, old.stderr), (0, ""))
            vless = box.run_cli("client", "link", name, "--connection", INBOUND_TAG)
            self.assertEqual((vless.exit_code, vless.stderr), (0, ""))
            starter_uuid = urlsplit(vless.stdout.strip()).username
            box.paths.initial_link.parent.mkdir(parents=True, exist_ok=True)
            path = box.paths.initial_link
            legacy = f"CLIENT_URI={old.stdout}PROTOCOL=VLESS\nUUID={starter_uuid}\nCUSTOM=keep=value\n"
            path.write_text(legacy)
            box.paths.server_env.write_text(box.paths.server_env.read_text() + "INITIAL_PROTOCOL=both\n")

            result = box.run_menu(["2", "1", "5", "5443", "updated.example", "0", "0", "0"])

            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            lines = box.paths.initial_link.read_text().splitlines()
            fields = dict(line.split("=", 1) for line in lines)
            self.assertEqual(len(lines), len(fields))
            for tag, field in ((INBOUND_TAG, "VLESS_CLIENT_URI"), (TROJAN_INBOUND_TAG, "TROJAN_CLIENT_URI")):
                uri = box.run_cli("client", "link", name, "--connection", tag)
                self.assertEqual((uri.exit_code, uri.stderr), (0, ""))
                self.assertEqual(fields[field], uri.stdout.strip())
            self.assertEqual(fields["CLIENT_URI"], fields["VLESS_CLIENT_URI"])
            self.assertEqual(fields["INITIAL_PROTOCOL"], "both")
            self.assertEqual(fields["UUID"], starter_uuid)
            self.assertEqual(fields["CUSTOM"], "keep=value")
            self.assertEqual(fields["CREATED"], "")

            path.write_text(f"INITIAL_PROTOCOL=both\n{legacy}")
            result = box.run_menu(["2", "1", "5", "6443", "updated.example", "0", "0", "0"])
            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            fields = dict(line.split("=", 1) for line in path.read_text().splitlines())
            self.assertEqual(fields["CLIENT_URI"], fields["VLESS_CLIENT_URI"])
            self.assertEqual(urlsplit(fields["CLIENT_URI"]).port, 6443)

    def test_truncated_file_with_another_client_is_not_rewritten_as_installer_file(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            for args in (("add", "second", "--connection", INBOUND_TAG), ("disable", "alice")):
                result = box.run_cli("client", *args)
                self.assertEqual((result.exit_code, result.stderr), (0, ""))
            inbound = find_inbound_by_tag(box.config(), INBOUND_TAG)
            client, = inbound["settings"]["clients"]
            self.assertTrue(client["email"].startswith("second|"))
            link = box.run_cli("client", "link", "second", "--connection", INBOUND_TAG)
            self.assertEqual((link.exit_code, link.stderr), (0, ""))
            path = box.paths.initial_link
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"CLIENT_URI={link.stdout}PORT=443\nPROTOCOL=VLESS\nSECURITY=REALITY\n"
                "TRANSPORT=tcp\nFLOW=xtls-rprx-vision\n"
                "SNI=www.example.com\nDEST=www.example.com:443\nFINGERPRINT=chrome\n"
            )
            before = path.read_bytes()
            box.paths.server_env.write_text(box.paths.server_env.read_text() + "INITIAL_PROTOCOL=vless\n")
            box.processes.calls.clear()

            result = box.run_menu(["2", "1", "5", "5443", "updated.example", "0", "0", "0"])

            self.assertEqual(result.exit_code, 0)
            self.assertIn("PORT обновлён: 5443", result.stdout)
            self.assertIn("Не удалось подтвердить исходного стартового клиента", result.stderr)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(box.env()["PORT"], "5443")
            box.assert_restarted(times=1)

    def test_conflicting_identity_keeps_the_link_and_reports_applied_endpoint(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            seed_starter_file(box)
            path = box.paths.initial_link
            path.write_text(path.read_text() + "UUID=00000000-0000-4000-8000-000000000099\n")
            before = path.read_bytes()
            result = box.run_menu(["2", "1", "5", "5443", "updated.example", "0", "0", "0"])

            self.assertEqual(result.exit_code, 0)
            self.assertIn("PORT обновлён: 5443", result.stdout)
            self.assertIn("Не удалось восстановить стартовые ссылки", result.stderr)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(box.env()["PORT"], "5443")
            box.assert_restarted(times=1)

    def test_historical_file_does_not_switch_to_surviving_vless_credential(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            for args in (
                ("add-connection", "Second", "8443", "second.example"),
                ("add", "alice", "--connection", "vless-reality-2"),
            ):
                result = box.run_cli("client", *args)
                self.assertEqual((result.exit_code, result.stderr), (0, ""))
            old = box.run_cli("client", "link", "alice")
            self.assertEqual((old.exit_code, old.stderr), (0, ""))
            path = box.paths.initial_link
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"CLIENT_URI={old.stdout}PORT=443\nPROTOCOL=VLESS\n")
            before = path.read_bytes()
            removed = box.run_cli("client", "remove-connection", INBOUND_TAG)
            self.assertEqual((removed.exit_code, removed.stderr), (0, ""))

            result = box.run_menu(["2", "1", "5", "5443", "updated.example", "0", "0", "0"])

            self.assertEqual(result.exit_code, 0)
            self.assertIn("PORT обновлён: 5443", result.stdout)
            self.assertIn("Не удалось подтвердить исходного стартового клиента", result.stderr)
            self.assertEqual(path.read_bytes(), before)

    def test_endpoint_updates_link_for_disabled_client_with_legacy_credential(self):
        for keep_json_id in (True, False):
            with self.subTest(keep_json_id=keep_json_id), Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
                script_menu_processes(box)
                original = seed_starter_file(box)
                path = box.paths.initial_link
                path.write_text(path.read_text() + f"UUID={urlsplit(original).username}\n")
                disabled = box.run_cli("client", "disable", "alice")
                self.assertEqual((disabled.exit_code, disabled.stderr), (0, ""))
                with database.write_session() as conn:
                    conn.execute(
                        "DELETE FROM client_credentials WHERE client_name = ? AND connection_tag = ?",
                        ("alice", INBOUND_TAG),
                    )
                    if not keep_json_id:
                        conn.execute(
                            "UPDATE clients SET xray_client_json = json_remove(xray_client_json, '$.id') WHERE name = ?",
                            ("alice",),
                        )
                clients_before = box.db_rows("clients", "client_credentials")
                self.assertFalse(find_inbound_by_tag(box.config(), INBOUND_TAG)["settings"]["clients"])
                listed = box.run_cli("client", "link", "alice", "--connection", INBOUND_TAG)
                self.assertEqual((listed.exit_code, listed.stderr, listed.stdout.strip()), (0, "", original))

                changed = box.run_menu(["2", "1", "5", "5443", "updated.example", "0", "0", "0"])

                self.assertEqual((changed.exit_code, changed.stderr), (0, ""))
                current = box.run_cli("client", "link", "alice", "--connection", INBOUND_TAG)
                self.assertEqual((current.exit_code, current.stderr), (0, ""))
                fields = dict(line.split("=", 1) for line in path.read_text().splitlines())
                self.assertEqual(fields["CLIENT_URI"], current.stdout.strip())
                self.assertEqual((fields["PORT"], fields["SNI"]), ("5443", "updated.example"))
                self.assertEqual(box.db_rows("clients", "client_credentials"), clients_before)

    def test_cli_transport_and_xhttp_extra_refresh_starter_file(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            seed_starter_file(box)
            for args in (
                ("connection-transport", INBOUND_TAG, "xhttp", "--xhttp-path", "/first"),
                ("connection-xhttp-extra", INBOUND_TAG, "--xhttp-extra-json", '{"noGRPCHeader":true}'),
            ):
                with self.subTest(action=args[0]):
                    changed = box.run_cli("client", *args)
                    self.assertEqual((changed.exit_code, changed.stderr), (0, ""))
                    current = box.run_cli("client", "link", "alice", "--connection", INBOUND_TAG)
                    self.assertEqual((current.exit_code, current.stderr), (0, ""))
                    fields = dict(line.split("=", 1) for line in box.paths.initial_link.read_text().splitlines())
                    self.assertEqual(fields["CLIENT_URI"], current.stdout.strip())
                    self.assertEqual(fields["VLESS_CLIENT_URI"], current.stdout.strip())
                    self.assertEqual(fields["TRANSPORT"], "xhttp")
                    self.assertEqual(fields["XHTTP_PATH"], "/first")
            self.assertIn("extra", parse_qs(urlsplit(current.stdout.strip()).query))

    def test_xhttp_extra_refreshes_starter_file_after_vless_move_to_tls(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            original = seed_starter_file(box)
            change = connections_service.update_reality_fingerprint(INBOUND_TAG, "chrome")
            self.assertIsNone(change.link_error)
            path = box.paths.initial_link
            before = dict(line.split("=", 1) for line in path.read_text().splitlines())
            reality_fields = ("PUBLIC_KEY", "SHORT_ID", "DEST", "FLOW")
            self.assertTrue(all(before[key] for key in reality_fields))
            for args in (
                ("add-connection", "TLS", "18100", "site.example", "--security", "tls",
                 "--transport", "xhttp", "--public-port", "8443", "--xhttp-path", "/tls",
                 "--xhttp-mode", "packet-up"),
                ("move-connection", "alice", "vless-tls"),
            ):
                result = box.run_cli("client", *args)
                self.assertEqual((result.exit_code, result.stderr), (0, ""))

            changed = box.run_cli(
                "client", "connection-xhttp-extra", "vless-tls", "--xhttp-extra-json", '{"noGRPCHeader":true}',
            )

            self.assertEqual((changed.exit_code, changed.stderr), (0, ""))
            current = box.run_cli("client", "link", "alice", "--connection", "vless-tls")
            self.assertEqual((current.exit_code, current.stderr), (0, ""))
            fields = dict(line.split("=", 1) for line in path.read_text().splitlines())
            self.assertEqual(fields["CLIENT_URI"], current.stdout.strip())
            self.assertEqual(fields["VLESS_CLIENT_URI"], current.stdout.strip())
            self.assertEqual(fields["UUID"], urlsplit(original).username)
            self.assertIn("extra", parse_qs(urlsplit(fields["CLIENT_URI"]).query))
            expected = {
                "INITIAL_PROTOCOL": "vless", "PROTOCOL": "vless", "SECURITY": "TLS", "TRANSPORT": "xhttp",
                "SERVER": "site.example", "PORT": "8443", "SNI": "site.example",
                "XHTTP_PATH": "/tls", "XHTTP_MODE": "packet-up", **dict.fromkeys(reality_fields, ""),
            }
            self.assertEqual({key: fields[key] for key in expected}, expected)

    def test_start_file_transport_fields_match_server_env_after_transport_switches(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            seed_starter_file(box)
            keys = ("GRPC_SERVICE_NAME", "XHTTP_PATH", "XHTTP_MODE")
            scenarios = (
                (("grpc", "--grpc-service-name", "fixture-grpc"), ("fixture-grpc", "", "")),
                (("xhttp", "--xhttp-path", "/fixture", "--xhttp-mode", "packet-up"), ("", "/fixture", "packet-up")),
                (("tcp",), ("", "", "")),
            )
            for args, expected in scenarios:
                with self.subTest(transport=args[0]):
                    changed = box.run_cli("client", "connection-transport", INBOUND_TAG, *args)
                    self.assertEqual((changed.exit_code, changed.stderr), (0, ""))
                    fields = dict(line.split("=", 1) for line in box.paths.initial_link.read_text().splitlines())
                    self.assertEqual(fields["TRANSPORT"], args[0])
                    self.assertEqual(tuple(fields[key] for key in keys), expected)
                    env = box.env()
                    self.assertEqual(tuple(env[key] for key in keys), expected)
                    current = box.run_cli("client", "link", "alice", "--connection", INBOUND_TAG)
                    self.assertEqual((current.exit_code, current.stderr, current.stdout.strip()),
                                     (0, "", fields["CLIENT_URI"]))

    def test_initial_link_rename_error_preserves_the_file_after_endpoint_change(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            seed_starter_file(box)
            path = box.paths.initial_link
            path.chmod(0o600)
            before = path.read_bytes()
            real_replace = os.replace

            def reject_initial_link(source, destination):
                if destination == path:
                    self.assertEqual(source.stat().st_mode & 0o777, 0o600)
                    raise PermissionError("test rename failure")
                real_replace(source, destination)

            with mock.patch.object(fs.os, "replace", side_effect=reject_initial_link):
                result = box.run_menu(["2", "1", "5", "5443", "updated.example", "0", "0", "0"])

            self.assertEqual(result.exit_code, 0)
            self.assertIn("PORT обновлён: 5443", result.stdout)
            self.assertIn("Не удалось записать стартовые ссылки", result.stderr)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(path.with_name("." + path.name + ".new").exists())
            self.assertEqual(box.env()["PORT"], "5443")
            box.assert_restarted(times=1)

    def test_applied_connection_writes_server_env_on_link_refresh_interrupt(self):
        for scenario in ("endpoint", "transport"):
            with self.subTest(scenario=scenario), Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
                script_menu_processes(box)
                seed_starter_file(box)
                file_before = box.paths.initial_link.read_bytes()

                with mock.patch.object(connections_service, "_refresh_initial_link", side_effect=KeyboardInterrupt) as interrupt:
                    if scenario == "endpoint":
                        result = box.run_menu(["2", "1", "5", "5443", "updated.example", "0", "0", "0"])
                    else:
                        result = box.run_cli("client", "connection-transport", INBOUND_TAG,
                                             "grpc", "--grpc-service-name", "fixture-grpc")

                interrupt.assert_called_once()
                self.assertEqual(result.exit_code, 130)
                self.assertEqual(box.paths.initial_link.read_bytes(), file_before)
                box.assert_restarted(times=1)
                inbound = find_inbound_by_tag(box.config(), INBOUND_TAG)
                record = repository.load_db_sql()["connections"][INBOUND_TAG]
                env = box.env()
                if scenario == "endpoint":
                    reality = inbound["streamSettings"]["realitySettings"]
                    self.assertEqual(inbound["port"], 5443)
                    self.assertEqual(reality["serverNames"], ["updated.example"])
                    self.assertEqual((record["port"], record["sni"]), (5443, "updated.example"))
                    self.assertEqual((env["PORT"], env["REALITY_SNI"], env["REALITY_DEST"]),
                                     ("5443", "updated.example", "updated.example:443"))
                else:
                    self.assertEqual(inbound["streamSettings"]["network"], "grpc")
                    self.assertEqual((record["transport"], record["grpcServiceName"]), ("grpc", "fixture-grpc"))
                    self.assertEqual((env.get("REALITY_TRANSPORT"), env.get("GRPC_SERVICE_NAME")), ("grpc", "fixture-grpc"))

    def test_endpoint_without_port_or_sni_is_rejected_before_lock_and_writes(self):
        with Sandbox() as box, running_xray(box):
            script_menu_processes(box)
            config = box.config()
            inbound = find_inbound_by_tag(config, INBOUND_TAG)
            inbound["streamSettings"]["realitySettings"]["dest"] = "custom.example:8443"
            write_config(box, config)
            before = state(box)

            with mock.patch.object(locks, "acquire_flock", wraps=locks.acquire_flock) as acquire:
                with self.assertRaises(ValidationError):
                    connections_service.update_reality_endpoint(INBOUND_TAG)

            acquire.assert_not_called()
            self.assertEqual(state(box), before)
            self.assertEqual(list(box.paths.config_dir.glob("config.json.bak.*")), [])
            self.assertEqual(box.processes.calls, [])

    def test_invalid_fingerprint_is_rejected_before_lock_and_writes(self):
        with Sandbox() as box:
            box.paths.initial_link.parent.mkdir(parents=True, exist_ok=True)
            box.paths.initial_link.write_text("CLIENT_URI=starter\n")
            before = state(box)

            with mock.patch.object(locks, "acquire_flock", wraps=locks.acquire_flock) as acquire:
                for fingerprint in ("", "  ", "firfox"):
                    with self.subTest(fingerprint=fingerprint):
                        with self.assertRaises(ValidationError):
                            connections_service.update_reality_fingerprint(INBOUND_TAG, fingerprint)
                        self.assertEqual(state(box), before)

            acquire.assert_not_called()
            self.assertEqual(box.processes.calls, [])

    def test_endpoint_and_fingerprint_keep_inbound_db_env_and_starter_links_aligned(self):
        secondary = "vless-reality-2"
        for tag in (INBOUND_TAG, secondary):
            with self.subTest(tag=tag), Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
                script_menu_processes(box)
                for args in (("add-connection", "Second", "8443", "second.example"),
                             ("add", "alice", "--connection", secondary)):
                    result = box.run_cli("client", *args)
                    self.assertEqual((result.exit_code, result.stderr), (0, ""))
                with database.write_session() as conn:
                    # Старая строка без собственного fingerprint берёт его из server.env.
                    conn.execute("UPDATE reality_connections SET fingerprint = '' WHERE tag = ?", (secondary,))
                original_env = box.paths.server_env.read_bytes()
                seed_starter_file(box, tag)
                other_tag = secondary if tag == INBOUND_TAG else INBOUND_TAG
                other = find_inbound_by_tag(box.config(), other_tag)
                other_record = repository.load_db_sql()["connections"][other_tag]
                port, sni = ((443, "www.example.com") if tag == INBOUND_TAG else (8443, "second.example"))
                selection = "1" if tag == INBOUND_TAG else "2"
                scenarios = (
                    ("3", ["5443"], 5443, sni, "chrome"),
                    ("4", ["sni.example"], 5443, "sni.example", "chrome"),
                    ("5", ["6443", "both.example"], 6443, "both.example", "chrome"),
                    ("6", ["2"], 6443, "both.example", "firefox"),
                )
                for option, answers, port, sni, fingerprint in scenarios:
                    with self.subTest(option=option):
                        original_config = box.paths.config.read_bytes()
                        box.processes.calls.clear()
                        result = box.run_menu(["2", "1", option, selection, *answers, "0", "0", "0"])
                        self.assertEqual((result.exit_code, result.stderr), (0, ""))
                        box.assert_restarted(times=0 if option == "6" else 1)
                        if option == "6":
                            self.assertEqual(box.paths.config.read_bytes(), original_config)

                        inbound = find_inbound_by_tag(box.config(), tag)
                        reality = inbound["streamSettings"]["realitySettings"]
                        dest = f"{sni}:443"
                        self.assertEqual((inbound["port"], reality["serverNames"], reality["dest"]), (port, [sni], dest))
                        records = repository.load_db_sql()["connections"]
                        self.assertEqual(tuple(records[tag][key] for key in ("port", "sni", "dest")), (port, sni, dest))
                        self.assertEqual(records[other_tag], other_record)
                        self.assertEqual(find_inbound_by_tag(box.config(), other_tag), other)
                        if option == "6":
                            self.assertEqual(records[tag]["fingerprint"], fingerprint)
                        if tag == INBOUND_TAG:
                            env = box.env()
                            self.assertEqual(tuple(env[key] for key in ("PORT", "REALITY_SNI", "REALITY_DEST", "FINGERPRINT")),
                                             (str(port), sni, dest, fingerprint))
                        else:
                            self.assertEqual(box.paths.server_env.read_bytes(), original_env)

                        fields = dict(line.split("=", 1) for line in box.paths.initial_link.read_text().splitlines())
                        uri = urlsplit(fields["CLIENT_URI"])
                        self.assertEqual(uri.port, port)
                        query = parse_qs(uri.query)
                        self.assertEqual(query["sni"], [sni])
                        self.assertEqual(query["fp"], [fingerprint])

    def test_rejected_candidate_compensates_committed_record_before_touching_env_and_link(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            box.paths.initial_link.parent.mkdir(parents=True, exist_ok=True)
            box.paths.initial_link.write_text("CLIENT_URI=starter\n")
            before = state(box)
            candidate = box.paths.config.with_suffix(".json.tmp")

            def reject(*args, **kwargs):
                record = repository.load_db_sql()["connections"][INBOUND_TAG]
                self.assertEqual((record["port"], record["sni"], record["dest"]), (5443, "changed.example", "changed.example:443"))
                self.assertEqual(state(box)[0], before[0])
                self.assertFalse(lock_is_free(box.paths.manager_lock))
                return box.processes.result(1, stderr="candidate rejected")

            box.processes.script({(str(box.paths.xray_bin), "run", "-test", "-format", "json", "-config", str(candidate)): reject})

            result = box.run_menu(["2", "1", "5", "5443", "changed.example", "0", "0", "0"])

            self.assertIn("candidate rejected", result.stderr)
            self.assertEqual(state(box), before)
            box.assert_restarted(times=0)

    def test_fingerprint_cancelled_at_commit_restores_state_and_exits_menu_with_130(self):
        original_write = database.write_session
        for committed in (False, True):
            with self.subTest(committed=committed), Sandbox() as box:
                script_menu_processes(box)
                before = state(box)
                calls = 0

                @contextmanager
                def interrupt_first_write(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    first = calls == 1
                    with original_write(*args, **kwargs) as conn:
                        yield conn
                        if first and not committed:
                            raise KeyboardInterrupt
                    if first and committed:
                        raise KeyboardInterrupt

                with mock.patch.object(database, "write_session", side_effect=interrupt_first_write):
                    result = box.run_menu(["2", "1", "6", "2", "0", "0", "0"])

                self.assertEqual((result.exit_code, result.stderr), (130, ""))
                self.assertEqual(result.stdout.count("Отменено"), 1)
                self.assertEqual(state(box), before)
                box.assert_restarted(times=0)


class ConnectionCaddyCompensationTests(unittest.TestCase):
    def start(self, box):
        free_https_port(box)
        script_processes(box)
        script_caddy(box)
        seed_starter_file(box)

    def test_managed_caddy_add_rejects_custom_public_port_before_writes(self):
        for args in (TLS, (*TROJAN, "--install-caddy")):
            with self.subTest(protocol=args[0]), Sandbox() as box, running_xray(box):
                self.start(box)
                before = state(box)
                db_before = box.paths.manager_db.read_bytes()

                result = box.run_cli("client", *args, "--public-port", "8443")

                self.assertEqual(result.exit_code, 1)
                self.assertIn("Managed Caddy requires PUBLIC_PORT 443 (received 8443).", result.stderr)
                self.assertIn("Use --public-port 443", result.stderr)
                self.assertEqual(state(box), before)
                self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
                self.assertFalse(box.paths.caddy_conf_dir.exists())
                self.assertFalse(list(box.paths.config_dir.glob("config.json.bak.*")))
                self.assertEqual(box.processes.calls, [])

    def test_trojan_update_requires_443_when_it_rebuilds_the_managed_site(self):
        with Sandbox() as box, running_xray(box):
            self.start(box)
            created = box.run_cli("client", *TROJAN)
            self.assertEqual((created.exit_code, created.stderr), (0, ""))
            occupied = box.run_cli("client", "add-connection", "Reality 8443", "8443", "cdn.example")
            self.assertEqual((occupied.exit_code, occupied.stderr), (0, ""))
            for legacy in (False, True):
                with self.subTest(legacy=legacy):
                    site = box.paths.caddy_conf_dir / "trojan.example.caddy"
                    if legacy:
                        site.write_text(site.read_text().replace("trojan.example {", "trojan.example:8443 {", 1))
                        with database.write_session() as conn:
                            record = repository.get_connection(conn, TROJAN_INBOUND_TAG)
                            record.update(port=8443, publicPort=8443)
                            repository.save_connection(conn, TROJAN_INBOUND_TAG, record)
                    before = state(box)
                    backups = sorted(box.paths.config_dir.glob("config.json.bak.*"))
                    db_before = box.paths.manager_db.read_bytes()
                    box.processes.calls.clear()

                    options = () if legacy else ("--public-port", "8443")
                    result = box.run_cli("client", "update-trojan-connection", "Trojan", "--ws-path", "/new", *options)

                    self.assertEqual(result.exit_code, 1)
                    self.assertIn("Managed Caddy requires PUBLIC_PORT 443 (received 8443).", result.stderr)
                    self.assertIn("Use --public-port 443", result.stderr)
                    self.assertEqual(state(box), before)
                    self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
                    self.assertEqual(sorted(box.paths.config_dir.glob("config.json.bak.*")), backups)
                    self.assertEqual(box.processes.calls, [])

    def test_external_trojan_tls_update_allows_custom_public_port(self):
        with Sandbox() as box, running_xray(box):
            self.start(box)
            self.assertEqual(box.run_cli("client", *TROJAN, "--no-caddy", "--public-port", "8443").exit_code, 0)
            result = box.run_cli("client", "update-trojan-connection", "Trojan", "--public-port", "9443")
            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertIn("PUBLIC_PORT: 9443", result.stdout)
            self.assertFalse(box.paths.caddyfile.exists())

    def test_recovered_trojan_file_keeps_credential_uuid_through_password_rotation(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_processes(box)
            name = "starter.trojan"
            for args in ((*TROJAN, "--no-caddy"), ("add", name, "--connection", TROJAN_INBOUND_TAG)):
                result = box.run_cli("client", *args)
                self.assertEqual((result.exit_code, result.stderr), (0, ""))
            credential = repository.load_db_sql()["clients"][name]["credentials"][TROJAN_INBOUND_TAG]
            original = box.run_cli("client", "link", name, "--connection", TROJAN_INBOUND_TAG)
            self.assertEqual((original.exit_code, original.stderr), (0, ""))
            path = box.paths.initial_link
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"INITIAL_PROTOCOL=trojan\nCLIENT_URI={original.stdout}TROJAN_CLIENT_URI={original.stdout}")

            recovered = box.run_cli("client", "update-trojan-connection", "Trojan", "--ws-path", "/recovered")

            self.assertEqual((recovered.exit_code, recovered.stderr), (0, ""))
            before_rotation = dict(line.split("=", 1) for line in path.read_text().splitlines())
            self.assertEqual(before_rotation["UUID"], credential["id"])
            rotated = box.run_cli("client", "rotate-trojan-password", name, "--connection", TROJAN_INBOUND_TAG)
            self.assertEqual((rotated.exit_code, rotated.stderr), (0, ""))
            changed = box.run_cli("client", "update-trojan-connection", "Trojan", "--ws-path", "/rotated")
            self.assertEqual((changed.exit_code, changed.stderr), (0, ""))
            current = box.run_cli("client", "link", name, "--connection", TROJAN_INBOUND_TAG)
            self.assertEqual((current.exit_code, current.stderr), (0, ""))
            fields = dict(line.split("=", 1) for line in path.read_text().splitlines())
            self.assertEqual(fields["TROJAN_CLIENT_URI"], current.stdout.strip())
            self.assertEqual(fields["CLIENT_URI"], fields["TROJAN_CLIENT_URI"])
            self.assertNotEqual(urlsplit(fields["CLIENT_URI"]).username,
                                urlsplit(before_rotation["CLIENT_URI"]).username)
            self.assertEqual(fields["TROJAN_WS_PATH"], "/rotated")
            self.assertEqual(fields["UUID"], credential["id"])

    def test_trojan_only_updates_starter_file_with_configured_name(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_processes(box)
            name = "first.custom"
            for args in (
                (*TROJAN, "--no-caddy"),
                ("add", name, "--connection", TROJAN_INBOUND_TAG),
            ):
                result = box.run_cli("client", *args)
                self.assertEqual((result.exit_code, result.stderr), (0, ""))
            config = box.config()
            config["inbounds"].remove(find_inbound_by_tag(config, INBOUND_TAG))
            write_config(box, config)
            with database.write_session() as conn:
                repository.delete_connection(conn, INBOUND_TAG)
            box.paths.server_env.write_text(box.paths.server_env.read_text() + "INITIAL_PROTOCOL=trojan\n")
            install_env = {
                "INSTALL_CLIENT_NAME": name,
                "INSTALL_UUID": repository.load_db_sql()["clients"][name]["id"],
                "INSTALL_PUBLIC_KEY": "A" * 43,
                "INSTALL_SHORT_ID": "aabbccdd",
                "INSTALL_CREATED": "2026-10-01T00:00:00Z",
            }
            with mock.patch.dict(os.environ, install_env):
                installed = box.run_cli("client", "link", "--initial-file")
            self.assertEqual((installed.exit_code, installed.stderr), (0, ""))
            original = box.paths.initial_link.read_bytes()
            rotated = box.run_cli("client", "rotate-trojan-password", name, "--connection", TROJAN_INBOUND_TAG)
            self.assertEqual((rotated.exit_code, rotated.stderr), (0, ""))
            self.assertEqual(box.paths.initial_link.read_bytes(), original)

            changed = box.run_cli(
                "client", "update-trojan-connection", "Trojan", "--domain", "changed.example",
                "--public-port", "9443", "--ws-path", "/new", "--fingerprint", "firefox",
            )

            self.assertEqual((changed.exit_code, changed.stderr), (0, ""))
            current = box.run_cli("client", "link", name, "--connection", TROJAN_INBOUND_TAG)
            self.assertEqual((current.exit_code, current.stderr), (0, ""))
            fields = dict(line.split("=", 1) for line in box.paths.initial_link.read_text().splitlines())
            self.assertEqual(fields["CLIENT_URI"], current.stdout.strip())
            self.assertEqual(fields["TROJAN_CLIENT_URI"], current.stdout.strip())
            self.assertEqual(fields["VLESS_CLIENT_URI"], "")
            self.assertEqual(fields["INITIAL_PROTOCOL"], "trojan")
            self.assertEqual(fields["SECURITY"], "TLS")
            self.assertEqual(fields["TRANSPORT"], "ws")
            self.assertEqual(fields["TROJAN_DOMAIN"], "changed.example")
            self.assertEqual(fields["TROJAN_LOCAL_PORT"], "18101")
            self.assertEqual(fields["TROJAN_PUBLIC_PORT"], "9443")
            self.assertEqual(fields["TROJAN_WS_PATH"], "/new")
            self.assertEqual(fields["FINGERPRINT"], "firefox")
            self.assertEqual(fields["UUID"], install_env["INSTALL_UUID"])
            self.assertEqual(fields["PUBLIC_KEY"], install_env["INSTALL_PUBLIC_KEY"])
            self.assertEqual(fields["SHORT_ID"], install_env["INSTALL_SHORT_ID"])
            self.assertEqual(fields["CREATED"], install_env["INSTALL_CREATED"])

    def test_external_trojan_tls_update_allows_public_port_used_by_local_reality(self):
        with Sandbox() as box, running_xray(box):
            script_processes(box)
            self.assertEqual(find_inbound_by_tag(box.config(), INBOUND_TAG)["port"], 443)
            created = box.run_cli("client", *TROJAN, "--no-caddy", "--public-port", "443")
            self.assertEqual((created.exit_code, created.stderr), (0, ""))

            result = box.run_cli("client", "update-trojan-connection", "Trojan", "--fingerprint", "firefox")

            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            record = repository.load_db_sql()["connections"][TROJAN_INBOUND_TAG]
            self.assertEqual((record["fingerprint"], record["publicPort"], record["localPort"]), ("firefox", 443, 18101))
            self.assertFalse(box.paths.caddyfile.exists())

            before = state(box)
            rejected = box.run_cli("client", "update-trojan-connection", "Trojan", "--local-port", "443")

            self.assertEqual(rejected.exit_code, 1)
            self.assertIn("LOCAL_PORT is already used by another inbound: 443", rejected.stderr)
            self.assertEqual(state(box), before)

    def test_site_candidate_rejected_before_any_working_write(self):
        for args, domain in ((TLS, "tls.example"), (TROJAN, "trojan.example")):
            with self.subTest(domain=domain), Sandbox() as box, running_xray(box):
                self.start(box)
                before = state(box)
                checked = []

                def validate(call, **kwargs):
                    self.assertTrue(is_caddy_candidate_validation(call), call)
                    candidate = Path(call[3])
                    self.assertIn(f"{domain} {{", candidate.read_text())
                    self.assertEqual(state(box), before)
                    checked.append(candidate)
                    return box.processes.result(1, stderr="candidate rejected")

                box.processes.script_program("caddy", validate)
                result = box.run_cli("client", *args)
                self.assertEqual(result.exit_code, 1)
                self.assertIn("candidate rejected", result.stderr)
                self.assertEqual(len(box.processes.calls), 1)
                self.assertEqual(state(box), before)
                self.assertFalse(box.paths.caddy_conf_dir.exists())
                self.assertFalse(list(box.paths.config_dir.glob("config.json.bak.*")))
                self.assertFalse(checked[0].exists())

    def test_create_uses_cascade_committed_during_caddy_preflight(self):
        with Sandbox() as box, running_xray(box):
            self.start(box)
            config = box.config()
            config["outbounds"].extend(
                {"tag": tag, "protocol": "socks", "settings": {"servers": [{"address": "192.0.2.1", "port": 1080}]}}
                for tag in ("cascade-old", "cascade-new")
            )
            config["routing"]["rules"].append({"type": "field", "network": "tcp,udp", "outboundTag": "cascade-old"})
            write_config(box, config)
            alice = repository.load_db()["clients"]["alice"]
            balancer = client_balancer_tag("alice", alice)
            with database.write_session() as conn:
                conn.execute("UPDATE clients SET selected_cascade_tag = 'cascade-old' WHERE name = 'alice'")

            def validate_caddy(call, **kwargs):
                self.assertTrue(is_caddy_candidate_validation(call), call)
                with closing(sqlite3.connect(box.paths.manager_db, timeout=0)) as conn, conn:
                    conn.execute("UPDATE clients SET selected_cascade_tag = 'cascade-new' WHERE name = 'alice'")
                return box.processes.ok()

            def validate_xray(*args, **kwargs):
                with closing(sqlite3.connect(box.paths.manager_db, timeout=0)) as conn, conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute("BEGIN IMMEDIATE")
                    self.assertIsNotNone(repository.get_connection(conn, "trojan-tls"))
                return box.processes.ok()

            box.processes.script_program("caddy", validate_caddy)
            candidate = box.paths.config.with_suffix(".json.tmp")
            box.processes.script({
                (box.paths.xray_bin, "run", "-test", "-format", "json", "-config", candidate): validate_xray,
            })
            result = box.run_cli("client", *TROJAN)

            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual(repository.load_db()["clients"]["alice"]["selectedCascadeTag"], "cascade-new")
            selector = next(item["selector"] for item in box.config()["routing"]["balancers"] if item["tag"] == balancer)
            self.assertEqual(selector, ["cascade-new"])
            box.assert_restarted(times=1)

    def test_first_caddy_install_waits_for_xray_and_starts_with_checked_site(self):
        for failure in ("xray", "reload", None):
            with self.subTest(failure=failure), Sandbox() as box, running_xray(box):
                self.start(box)
                box.processes.executables.pop("caddy")
                box.processes.executables.update({"apt-get": "/usr/bin/apt-get", "dpkg-deb": "/usr/bin/dpkg-deb"})
                before = state(box)
                events = []
                archive_paths = []
                site = box.paths.caddy_conf_dir / "trojan.example.caddy"
                packaged_caddyfile = ":80 {\n    file_server\n}\n"
                unit = {"active": False, "enabled": False}

                def validate_staged_site(call, **kwargs):
                    self.assertTrue(is_caddy_candidate_validation(("caddy", *call[1:])), call)
                    self.assertEqual(state(box), before)
                    self.assertIn("trojan.example", Path(call[-1]).read_text())
                    events.append("validate site")
                    return box.processes.ok()

                def apt(call, **kwargs):
                    if call[1:] == ["download", "caddy"]:
                        archive = kwargs["cwd"] / "caddy_1.2.3_linux.deb"
                        archive.write_bytes(b"fixture package")
                        archive_paths.append(archive)
                        return box.processes.ok()
                    self.assertEqual(call[1:], ["install", "-y", str(archive_paths[0])])
                    self.assertEqual(kwargs["env"]["SYSTEMD_OFFLINE"], "1")
                    self.assertIsNotNone(repository.load_db()["connections"].get("trojan-tls"))
                    box.assert_restarted(times=1)
                    self.assertFalse(site.exists())
                    box.paths.caddyfile.parent.mkdir(parents=True)
                    box.paths.caddyfile.write_text(packaged_caddyfile)
                    box.processes.executables["caddy"] = "/usr/bin/caddy"
                    unit["enabled"] = True
                    events.append("install package")
                    return box.processes.ok()

                def extract(call, **kwargs):
                    self.assertEqual(call[:3], ["dpkg-deb", "--extract", str(archive_paths[0])])
                    binary = Path(call[-1]) / "usr" / "bin" / "caddy"
                    binary.parent.mkdir(parents=True)
                    binary.touch()
                    box.processes.script_program(str(binary), validate_staged_site)
                    return box.processes.ok()

                def validate_xray(*args, **kwargs):
                    self.assertNotIn("caddy", box.processes.executables)
                    self.assertFalse(box.paths.caddyfile.exists())
                    events.append("validate Xray")
                    if failure == "xray":
                        return box.processes.result(1, stderr="Xray rejected fixture")
                    return box.processes.ok()

                def show_before_install(*args, **kwargs):
                    self.assertNotIn("caddy", box.processes.executables)
                    self.assertEqual(unit, {"active": False, "enabled": False})
                    events.append("read initial activation")
                    return box.processes.ok("LoadState=not-found\nActiveState=inactive\n")

                def validate_installed_caddy(*args, **kwargs):
                    self.assertIn("trojan.example", site.read_text())
                    self.assertIn(f"import {box.paths.caddy_conf_dir}/*.caddy", box.paths.caddyfile.read_text())
                    events.append("validate working Caddyfile")
                    return box.processes.ok()

                def start(*args, **kwargs):
                    self.assertEqual(events[-1], "validate working Caddyfile")
                    events.append("start Caddy")
                    unit["active"] = True
                    return box.processes.ok()

                def reload(*args, **kwargs):
                    self.assertEqual(unit, {"active": True, "enabled": True})
                    if failure == "reload":
                        return box.processes.result(1, stderr="Caddy rejected reload")
                    return box.processes.ok()

                def stop(*args, **kwargs):
                    self.assertEqual(box.paths.caddyfile.read_text(), packaged_caddyfile)
                    self.assertFalse(site.exists())
                    unit["active"] = False
                    events.append("stop Caddy")
                    return box.processes.ok()

                def disable(*args, **kwargs):
                    unit["enabled"] = False
                    events.append("disable Caddy")
                    return box.processes.ok()

                box.processes.script_program("apt-get", apt)
                box.processes.script_program("dpkg-deb", extract)
                candidate = box.paths.config.with_suffix(".json.tmp")
                box.processes.script({
                    "apt-get update": box.processes.ok(),
                    (box.paths.xray_bin, "run", "-test", "-format", "json", "-config", candidate): validate_xray,
                    ("caddy", "validate", "--config", box.paths.caddyfile): validate_installed_caddy,
                    CADDY_BEFORE_INSTALL: show_before_install,
                    CADDY_ACTIVATION_STATE: box.processes.ok("ActiveState=inactive\nUnitFileState=enabled\n"),
                    ENABLE_NOW: start,
                    RELOAD: reload,
                    STOP: stop,
                    DISABLE: disable,
                })
                result = box.run_cli("client", *TROJAN)

                expected = ["validate site", "validate Xray"]
                if failure is None:
                    self.assertEqual(result.exit_code, 0, result.stderr)
                    expected.extend(["read initial activation", "install package", "validate working Caddyfile",
                                     "start Caddy"])
                    self.assertIn(RELOAD, box.processes.calls)
                    self.assertEqual(unit, {"active": True, "enabled": True})
                elif failure == "reload":
                    self.assertEqual(result.exit_code, 1, result.stderr)
                    self.assertIn("Caddy rejected reload", result.stderr)
                    expected.extend(["read initial activation", "install package", "validate working Caddyfile",
                                     "start Caddy", "stop Caddy", "disable Caddy"])
                    self.assertEqual(unit, {"active": False, "enabled": False})
                    self.assertIn("caddy", box.processes.executables)
                    files, rows = state(box)
                    self.assertEqual(files.pop(box.paths.caddyfile), packaged_caddyfile.encode())
                    self.assertEqual((files, rows), before)
                    box.assert_restarted(times=2)
                else:
                    self.assertEqual(result.exit_code, 1, result.stderr)
                    self.assertIn("Xray rejected fixture", result.stderr)
                    self.assertNotIn("caddy", box.processes.executables)
                    self.assertEqual(state(box), before)
                    box.assert_restarted(times=0)
                self.assertEqual(events, expected)
                self.assertFalse(archive_paths[0].parent.exists())

    def test_trojan_update_declines_if_preflight_validated_different_site(self):
        with Sandbox() as box, running_xray(box):
            self.start(box)
            created = box.run_cli("client", *TROJAN)
            self.assertEqual(created.exit_code, 0, created.stderr)
            before_files, _ = state(box)
            box.processes.calls.clear()
            updated_rows = []

            def change_tls_profile(call, **kwargs):
                self.assertTrue(is_caddy_candidate_validation(call), call)
                with closing(sqlite3.connect(box.paths.manager_db, timeout=0)) as conn, conn:
                    conn.execute("""UPDATE reality_connections
                                    SET extra_json = json_set(extra_json, '$.tlsMinVersion', 'tls1.3')
                                    WHERE tag = 'trojan-tls'""")
                updated_rows.append(state(box)[1])
                return box.processes.ok()

            box.processes.script_program("caddy", change_tls_profile)
            result = box.run_cli("client", "update-trojan-connection", "Trojan", "--ws-path", "/new")

            self.assertEqual(result.exit_code, 1, result.stderr)
            self.assertIn("Connection settings changed after Caddy validation", result.stderr)
            self.assertEqual(state(box), (before_files, updated_rows[0]))
            self.assertEqual(len(box.processes.calls), 1)
            box.assert_restarted(times=0)

    def test_tls_add_reload_failure_restores_xray_sqlite_and_original_caddyfile(self):
        with Sandbox() as box, running_xray(box):
            self.start(box)
            caddyfile = box.paths.caddyfile
            caddyfile.parent.mkdir(parents=True)
            caddyfile.write_text(":80 {\n    respond \"existing\"\n}\n")
            site = box.paths.caddy_conf_dir / "tls.example.caddy"
            original_files, original_rows = state(box)
            validated = []

            def check_candidate(call, **kwargs):
                self.assertTrue(is_caddy_candidate_validation(call), call)
                self.assertEqual(state(box), (original_files, original_rows))
                validated.append(Path(call[3]).read_bytes())
                return box.processes.ok()

            def check_xray(call, **kwargs):
                files, rows = state(box)
                self.assertEqual(files, original_files)
                self.assertIn("vless-tls", rows)
                self.assertEqual(len(validated), 1)
                return box.processes.ok()

            def reject_reload(call, **kwargs):
                files, rows = state(box)
                self.assertNotEqual(files[box.paths.config], original_files[box.paths.config])
                self.assertIn("vless-tls", rows)
                self.assertEqual(files[site], validated[0])
                self.assertEqual(files[box.paths.initial_link], original_files[box.paths.initial_link])
                self.assertEqual(files[box.paths.server_env], original_files[box.paths.server_env])
                return box.processes.result(1, stderr="reload rejected\n")

            def reload_old_site(call, **kwargs):
                self.assertFalse(site.exists())
                self.assertEqual(caddyfile.read_bytes(), original_files[caddyfile])
                return box.processes.ok()

            box.processes.script_program("caddy", check_candidate)
            candidate_config = box.paths.config.with_suffix(".json.tmp")
            box.processes.script({
                (box.paths.xray_bin, "run", "-test", "-format", "json", "-config", candidate_config): check_xray,
                RELOAD: [reject_reload, reload_old_site],
            })
            result = box.run_cli("client", *TLS)
            self.assertEqual(result.exit_code, 1)
            self.assertIn("reload rejected", result.stderr)
            self.assertEqual(state(box), (original_files, original_rows))
            self.assertEqual(box.processes.calls.count(RELOAD), 2)
            self.assertNotIn(("systemctl", "restart", "caddy"), box.processes.calls)
            box.assert_restarted(times=2)

    def test_reload_failure_restores_trojan_site_on_update_and_rename(self):
        for options in ((), ("--domain", "replacement.example")):
            with self.subTest(options=options), Sandbox() as box, running_xray(box):
                self.start(box)
                created = box.run_cli("client", *TROJAN)
                self.assertEqual((created.exit_code, created.stderr), (0, ""))
                original_files, original_rows = state(box)
                site = box.paths.caddy_conf_dir / "trojan.example.caddy"
                new_site = box.paths.caddy_conf_dir / "replacement.example.caddy" if options else site
                box.processes.calls.clear()

                def reject_reload(call, **kwargs):
                    self.assertIn("path /updated", new_site.read_text())
                    self.assertEqual(site.exists(), not options)
                    self.assertIn("updated", box.db_rows("reality_connections"))
                    return box.processes.result(1, stderr="reload rejected\n")

                box.processes.script({RELOAD: [reject_reload, box.processes.ok()]})
                result = box.run_cli("client", "update-trojan-connection", "Trojan",
                                     "--ws-path", "/updated", *options)
                self.assertEqual(result.exit_code, 1)
                self.assertIn("reload rejected", result.stderr)
                self.assertEqual(state(box), (original_files, original_rows))
                self.assertEqual(box.processes.calls.count(RELOAD), 2)
                box.assert_restarted(times=2)

    def test_reload_failure_restores_caddy_service_and_enablement_independently(self):
        for active, enablement, has_caddyfile, activate, rollback in (
            (False, "disabled", False, ENABLE_NOW, [STOP, DISABLE]),
            (False, "disabled", True, ENABLE_NOW, [STOP, DISABLE]),
            (True, "disabled", True, ENABLE_NOW, [RELOAD, DISABLE]),
            (False, "enabled", True, ENABLE_NOW, [STOP]),
            (True, "enabled-runtime", True, ENABLE_NOW, [RELOAD, DISABLE, ENABLE_RUNTIME]),
            (False, "static", True, START, [STOP]),
        ):
            with (
                self.subTest(active=active, enablement=enablement, has_caddyfile=has_caddyfile),
                Sandbox() as box,
                running_xray(box),
            ):
                self.start(box)
                caddyfile = box.paths.caddyfile
                if has_caddyfile:
                    caddyfile.parent.mkdir(parents=True)
                    caddyfile.write_text(":80 {\n    file_server\n}\n")
                before = state(box)
                unit = {"active": active, "enablement": enablement}

                def show(*args, **kwargs):
                    return box.processes.ok(
                        f"ActiveState={'active' if unit['active'] else 'inactive'}\n"
                        f"UnitFileState={unit['enablement']}\n"
                    )

                def enable_now(*args, **kwargs):
                    unit.update(active=True, enablement="enabled")
                    return box.processes.ok()

                def start(*args, **kwargs):
                    unit["active"] = True
                    return box.processes.ok()

                def reload_rejected(*args, **kwargs):
                    expected_enablement = "enabled" if activate == ENABLE_NOW else enablement
                    self.assertEqual(unit, {"active": True, "enablement": expected_enablement})
                    return box.processes.result(1, stderr="reload rejected\n")

                def stop(*args, **kwargs):
                    unit["active"] = False
                    return box.processes.ok()

                def disable(*args, **kwargs):
                    unit["enablement"] = "disabled"
                    return box.processes.ok()

                def enable_runtime(*args, **kwargs):
                    unit["enablement"] = "enabled-runtime"
                    return box.processes.ok()

                box.processes.script({
                    CADDY_ACTIVATION_STATE: show,
                    ENABLE_NOW: enable_now,
                    START: start,
                    RELOAD: [reload_rejected, box.processes.ok()],
                    STOP: stop,
                    DISABLE: disable,
                    ENABLE_RUNTIME: enable_runtime,
                })
                result = box.run_cli("client", *TLS)

                self.assertEqual(result.exit_code, 1, result.stderr)
                self.assertIn("reload rejected", result.stderr)
                self.assertEqual(state(box), before)
                self.assertEqual(unit, {"active": active, "enablement": enablement})
                relevant = {CADDY_ACTIVATION_STATE, ENABLE_NOW, START, RELOAD, STOP, DISABLE, ENABLE_RUNTIME}
                self.assertEqual([call for call in box.processes.calls if call in relevant],
                                 [CADDY_ACTIVATION_STATE, activate, RELOAD, *rollback])
                box.assert_restarted(times=2)

    def test_reload_rollback_preserves_independent_caddyfile_edit(self):
        for has_import in (False, True):
            with self.subTest(has_import=has_import), Sandbox() as box, running_xray(box):
                self.start(box)
                caddyfile = box.paths.caddyfile
                caddyfile.parent.mkdir(parents=True)
                import_line = f"import {box.paths.caddy_conf_dir}/*.caddy"
                initial = ":80 {\n    file_server\n}\n"
                if has_import:
                    initial += import_line + "\n"
                caddyfile.write_text(initial)
                caddyfile.chmod(0o640)
                before_files, before_rows = state(box)

                def change_and_fail(*args, **kwargs):
                    self.assertIn(import_line, caddyfile.read_text())
                    self.assertEqual(caddyfile.stat().st_mode & 0o777, 0o640)
                    self.assertTrue(caddy_sites.remove_site_block_from_caddyfile(":80", caddyfile))
                    return box.processes.result(1, stderr="synthetic reload failure\n")

                box.processes.script({RELOAD: [change_and_fail, box.processes.ok()]})
                result = box.run_cli("client", *TLS)

                self.assertEqual(result.exit_code, 1, result.stderr)
                after_files, after_rows = state(box)
                self.assertNotIn(b":80 {", after_files.pop(caddyfile))
                before_files.pop(caddyfile)
                self.assertEqual((after_files, after_rows), (before_files, before_rows))
                self.assertIn(import_line, caddyfile.read_text())
                self.assertEqual(box.processes.calls.count(RELOAD), 2)
                box.assert_restarted(times=2)
                conflict = "Файл изменился после начала операции"
                if has_import:
                    self.assertNotIn(conflict, result.stderr)
                else:
                    self.assertIn(conflict, result.stderr)

    def test_caddyfile_staging_is_private_before_first_byte_and_preserves_published_mode(self):
        with Sandbox() as box:
            caddyfile = box.paths.caddyfile
            caddyfile.parent.mkdir(parents=True, mode=0o755)
            original = b':80 {\n    respond "fixture"\n}\n'
            caddyfile.write_bytes(original)
            caddyfile.chmod(0o640)
            ownership = caddyfile.stat().st_uid, caddyfile.stat().st_gid
            open_file = os.open
            observed = []

            def observe_open(path, flags, mode=0o777, **kwargs):
                fd = open_file(path, flags, mode, **kwargs)
                if Path(path).name == ".Caddyfile.new":
                    info = os.fstat(fd)
                    observed.append((info.st_mode & 0o777, info.st_size, caddyfile.read_bytes()))
                return fd

            previous_umask = os.umask(0o022)
            try:
                with mock.patch.object(os, "open", observe_open):
                    caddy_sites.ensure_caddyfile_import()
            finally:
                os.umask(previous_umask)

            self.assertEqual(observed, [(0o600, 0, original)])
            self.assertIn(b"import ", caddyfile.read_bytes())
            published = caddyfile.stat()
            self.assertEqual(published.st_mode & 0o777, 0o640)
            self.assertEqual((published.st_uid, published.st_gid), ownership)
            self.assertFalse(caddyfile.with_name(".Caddyfile.new").exists())

    def test_create_and_rename_refuse_site_that_appeared_during_xray_restart(self):
        for rename in (False, True):
            with self.subTest(rename=rename), Sandbox() as box, running_xray(box):
                self.start(box)
                args = TLS
                domain = "tls.example"
                if rename:
                    created = box.run_cli("client", *TROJAN)
                    self.assertEqual((created.exit_code, created.stderr), (0, ""))
                    domain = "replacement.example"
                    args = ("update-trojan-connection", "Trojan", "--domain", domain, "--ws-path", "/new")
                before_files, before_rows = state(box)
                box.processes.calls.clear()
                concurrent_content = caddy_model.caddy_site_block(domain, 18111)
                # Проверяем тот же домен под другим именем файла при переименовании.
                site_path = box.paths.caddy_conf_dir / ("manual.caddy" if rename else f"{domain}.caddy")

                def restart(*args, **kwargs):
                    self.assertFalse(lock_is_free(box.paths.manager_lock))
                    if not site_path.exists():
                        site_path.parent.mkdir(parents=True, exist_ok=True)
                        site_path.write_text(concurrent_content)
                    return box.processes.ok()

                box.processes.script({"systemctl restart xray": restart})
                result = box.run_cli("client", *args)

                self.assertEqual(result.exit_code, 1, result.stderr)
                self.assertIn("already exists", result.stderr)
                self.assertEqual(site_path.read_text(), concurrent_content)
                expected_files = {**before_files, site_path: concurrent_content.encode()}
                self.assertEqual(state(box), (expected_files, before_rows))
                self.assertNotIn(RELOAD, box.processes.calls)
                box.assert_restarted(times=2)

    def test_site_created_after_conflict_check_is_not_overwritten_or_removed_by_rollback(self):
        with Sandbox() as box, running_xray(box):
            self.start(box)
            before_files, before_rows = state(box)
            target = caddy_sites.site_config_path("tls.example")
            concurrent_content = caddy_model.caddy_site_block("tls.example", 18111)
            publish = os.link

            def competing_create(candidate, destination):
                self.assertEqual(destination, target)
                target.write_text(concurrent_content)
                publish(candidate, destination)

            with mock.patch("os.link", side_effect=competing_create):
                result = box.run_cli("client", *TLS)

            self.assertEqual(result.exit_code, 1, result.stderr)
            self.assertEqual(target.read_text(), concurrent_content)
            self.assertEqual(state(box), ({**before_files, target: concurrent_content.encode()}, before_rows))
            self.assertNotIn(RELOAD, box.processes.calls)
            box.assert_restarted(times=2)

    def test_trojan_without_previous_domain_refuses_site_created_during_xray_restart(self):
        with Sandbox() as box, running_xray(box):
            self.start(box)
            config = box.config()
            config["inbounds"].append(connections.make_trojan_ws_inbound(TROJAN_INBOUND_TAG, 18101, "/legacy"))
            write_config(box, config)
            self.assertNotIn(TROJAN_INBOUND_TAG, repository.load_db()["connections"])
            before_files, before_rows = state(box)
            target = caddy_sites.site_config_path("replacement.example")
            concurrent_content = caddy_model.caddy_site_block("replacement.example", 18111)

            def restart(*args, **kwargs):
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(concurrent_content)
                return box.processes.ok()

            box.processes.script({"systemctl restart xray": restart})
            result = box.run_cli(
                "client", "update-trojan-connection", TROJAN_INBOUND_TAG, "--domain", "replacement.example",
                "--public-port", "443",
            )

            self.assertEqual(result.exit_code, 1, result.stderr)
            self.assertIn("already exists", result.stderr)
            self.assertEqual(state(box), ({**before_files, target: concurrent_content.encode()}, before_rows))
            self.assertNotIn(RELOAD, box.processes.calls)
            box.assert_restarted(times=2)

    def test_manual_caddy_site_write_reloads_under_same_manager_lock(self):
        with Sandbox() as box:
            script_menu_processes(box)

            def reload(*args, **kwargs):
                self.assertFalse(lock_is_free(box.paths.manager_lock))
                return box.processes.ok()

            box.processes.script({RELOAD: reload})
            result = box.run_menu(["2", "4", "2", "4", "manual.example", "18111", "2", "0", "0", "0", "0"])

            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertIn(RELOAD, box.processes.calls)
            self.assertTrue(caddy_sites.site_config_path("manual.example").is_file())


class ConnectionRenameTests(unittest.TestCase):
    def test_rename_only_writes_the_selected_connection(self):
        with Sandbox() as box, running_xray(box):
            script_processes(box)
            added = box.run_cli("client", "add-connection", "Second", "8443", "second.example")
            self.assertEqual(added.exit_code, 0, added.stderr)
            with database.write_session() as conn:
                self.assertTrue(repository.delete_connection(conn, "vless-reality-2"))
                conn.execute("""CREATE TRIGGER reject_other_connection BEFORE INSERT ON reality_connections
                                WHEN new.tag <> 'vless-reality'
                                BEGIN SELECT RAISE(ABORT, 'unrelated connection'); END""")
            before_files, _ = state(box)
            box.processes.calls.clear()

            result = box.run_cli("client", "connection-rename", "vless-reality", "Renamed")

            self.assertEqual(result.exit_code, 0, result.stderr)
            records = repository.load_db()["connections"]
            self.assertNotIn("vless-reality-2", records)
            self.assertEqual(records["vless-reality"]["name"], "Renamed")
            self.assertEqual(state(box)[0], before_files)
            self.assertEqual(box.processes.calls, [])


class ConnectionSettingsTests(unittest.TestCase):
    def test_remove_connection_spools_linked_activity_in_bounded_batches_and_cleans_up(self):
        count = 600
        tables = ("activity_events", "activity_event_risks", "activity_alert_events")
        for accept_xray in (False, True):
            with self.subTest(accept_xray=accept_xray), Sandbox() as box, running_xray(box):
                script_processes(box)
                added = box.run_cli("client", "add-connection", "Second", "8443", "second.example")
                self.assertEqual(added.exit_code, 0, added.stderr)
                for name in ("bob", "carol"):
                    added = box.run_cli("client", "add", name, "--connection", "vless-reality-2")
                    self.assertEqual(added.exit_code, 0, added.stderr)
                box.processes.calls.clear()

                with database.write_session() as conn:
                    for name in ("alice", "bob", "carol"):
                        conn.executemany(
                            "INSERT INTO activity_events(event_time, client_name, raw_json) VALUES (?, ?, ?)",
                            (("2026-10-05T00:00:00Z", name, '{"index":' + str(number) + '}') for number in range(count)),
                        )
                    conn.execute("INSERT INTO activity_event_risks SELECT id, 'fixture' FROM activity_events")
                    conn.execute("""
                        INSERT INTO activity_alert_events
                            (event_time, client_name, risk, dedupe_key, first_seen_at, last_seen_at, raw_ref_event_id)
                        SELECT 'now', 'alice', 'fixture', CAST(id AS TEXT), 'now', 'now', id
                        FROM activity_events WHERE client_name IN ('bob', 'carol')
                    """)
                before = state(box)
                activity_before = box.db_rows(*tables)
                batch_sizes = {"capture": [], "restore": []}
                batches = client_undo._batches

                def observe_batches(cursor):
                    filename = Path(cursor.connection.execute("PRAGMA database_list").fetchone()[2])
                    phase = "capture" if filename == box.paths.manager_db else "restore"
                    activity_rows = any(column[0] == "raw_json" for column in cursor.description)
                    for batch in batches(cursor):
                        if activity_rows:
                            batch_sizes[phase].append(len(batch))
                        yield batch

                def validate_xray(*args, **kwargs):
                    self.assertEqual(set(repository.load_db()["clients"]), {"alice"})
                    backup, = box.paths.manager_db.parent.glob("xray-client-undo-*/cascade.sqlite3")
                    self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                    with closing(sqlite3.connect(backup)) as conn:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM activity_events").fetchone(), (2 * count,))
                    if accept_xray:
                        return box.processes.ok()
                    return box.processes.result(1, stderr="synthetic Xray rejection")

                candidate = box.paths.config.with_suffix(".json.tmp")
                box.processes.script({
                    (box.paths.xray_bin, "run", "-test", "-format", "json", "-config", candidate): validate_xray,
                })
                with mock.patch.object(client_undo, "_batches", observe_batches):
                    result = box.run_cli("client", "remove-connection", "Second")

                self.assertEqual(result.exit_code, 0 if accept_xray else 1, result.stderr)
                if accept_xray:
                    with database.session() as conn:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0], count)
                        self.assertEqual(conn.execute(
                            "SELECT COUNT(*) FROM activity_alert_events WHERE raw_ref_event_id IS NULL"
                        ).fetchone()[0], 2 * count)
                    self.assertEqual(batch_sizes["restore"], [])
                else:
                    self.assertIn("synthetic Xray rejection", result.stderr)
                    self.assertEqual(state(box), before)
                    self.assertEqual(box.db_rows(*tables), activity_before)
                    self.assertEqual(sum(batch_sizes["restore"]), 2 * count)
                    self.assertGreater(len(batch_sizes["restore"]), 1)
                self.assertEqual(sum(batch_sizes["capture"]), 2 * count)
                self.assertGreater(len(batch_sizes["capture"]), 1)
                self.assertLessEqual(max(batch_sizes["capture"] + batch_sizes["restore"]), client_undo._BATCH_SIZE)
                self.assertEqual(list(box.paths.manager_db.parent.glob("xray-client-undo-*")), [])
                with database.session() as conn:
                    self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_remove_connection_leaves_state_intact_if_rollback_copy_fails(self):
        with Sandbox() as box, running_xray(box):
            script_processes(box)
            self.assertEqual(box.run_cli("client", "add-connection", "Second", "8443", "second.example").exit_code, 0)
            self.assertEqual(box.run_cli("client", "add", "bob", "--connection", "vless-reality-2").exit_code, 0)
            box.processes.calls.clear()
            before = state(box)

            error = sqlite3.OperationalError("fixture disk is full")
            with mock.patch.object(client_undo._CascadeBackup, "_capture_query", side_effect=error):
                result = box.run_cli("client", "remove-connection", "Second")

            self.assertEqual(result.exit_code, 1, result.stderr)
            self.assertIn("fixture disk is full", result.stderr)
            self.assertEqual(state(box), before)
            self.assertEqual(box.processes.calls, [])
            self.assertEqual(list(box.paths.manager_db.parent.glob("xray-client-undo-*")), [])

    def test_remove_waits_for_parser_only_if_a_client_record_will_be_deleted(self):
        tag = "vless-reality-2"
        for affected in ("empty", "detached", "deleted"):
            with self.subTest(affected=affected), Sandbox() as box, running_xray(box):
                script_processes(box)
                added = box.run_cli("client", "add-connection", "Second", "8443", "second.example")
                self.assertEqual(added.exit_code, 0, added.stderr)
                if affected != "empty":
                    name = "alice" if affected == "detached" else "bob"
                    added = box.run_cli("client", "add", name, "--connection", tag)
                    self.assertEqual(added.exit_code, 0, added.stderr)
                    with database.write_session() as conn:
                        conn.execute("INSERT INTO activity_events(event_time, client_name) VALUES (?, ?)",
                                     ("2026-10-05T00:00:00Z", name))
                original = state(box)
                activity = box.db_rows("activity_events")
                box.processes.calls.clear()

                with locks.activity_lock(purpose="fixture activity parser"):
                    result = box.run_cli("client", "remove-connection", "Second")
                    self.assertFalse(lock_is_free(box.paths.activity_lock))

                if affected == "deleted":
                    self.assertEqual(result.exit_code, 1, result.stderr)
                    self.assertIn("fixture activity parser", result.stderr)
                    self.assertEqual(state(box), original)
                    self.assertEqual(box.processes.calls, [])
                else:
                    self.assertEqual(result.exit_code, 0, result.stderr)
                    self.assertNotIn(tag, repository.load_db()["connections"])
                    self.assertEqual(tuple(repository.load_db()["clients"]["alice"]["credentials"]), ("vless-reality",))
                    box.assert_restarted(times=1)
                self.assertEqual(box.db_rows("activity_events"), activity)

    def test_remove_aborts_if_new_client_requires_parser_lock_after_preview(self):
        with Sandbox() as box, running_xray(box):
            script_processes(box)
            added = box.run_cli("client", "add-connection", "Second", "8443", "second.example")
            self.assertEqual(added.exit_code, 0, added.stderr)
            box.processes.calls.clear()
            before = state(box)[0]
            write_session = database.write_session

            @contextmanager
            def add_client_before_write():
                with write_session() as conn:
                    repository.save_client(conn, "bob", {
                        "connection": "vless-reality-2", "id": "b0000000-0000-4000-8000-000000000002",
                    })
                with write_session() as conn:
                    yield conn

            with mock.patch.object(database, "write_session", add_client_before_write):
                result = box.run_cli("client", "remove-connection", "Second")

            self.assertEqual(result.exit_code, 1, result.stderr)
            self.assertIn("Client records changed before connection removal", result.stderr)
            self.assertIn("bob", repository.load_db()["clients"])
            self.assertIn("vless-reality-2", repository.load_db()["connections"])
            self.assertEqual(state(box)[0], before)
            self.assertEqual(box.processes.calls, [])

    def test_remove_primary_connection_cancel_at_traffic_commit_keeps_files_consistent(self):
        for after_commit in (False, True):
            with self.subTest(after_commit=after_commit), Sandbox() as box, running_xray(box):
                script_menu_processes(box)
                for args in (
                    ("add-connection", "Second", "8443", "second.example"),
                    ("add", "alice", "--connection", "vless-reality-2"),
                ):
                    prepared = box.run_cli("client", *args)
                    self.assertEqual((prepared.exit_code, prepared.stderr), (0, ""))
                link = box.paths.initial_link
                seed_starter_file(box)
                before = state(box)
                write_session = database.write_session
                interrupted = False

                @contextmanager
                def interrupt_settlement(*args, **kwargs):
                    nonlocal interrupted
                    with write_session(*args, **kwargs) as conn:
                        handoff = client_traffic_handoff.load(conn)
                        yield conn
                        settling = not interrupted and bool(handoff) and not client_traffic_handoff.load(conn)
                        if settling and not after_commit:
                            interrupted = True
                            raise KeyboardInterrupt
                    if settling:
                        interrupted = True
                        raise KeyboardInterrupt

                with mock.patch.object(database, "write_session", side_effect=interrupt_settlement):
                    result = box.run_menu(["2", "1", "9", "1", "y", "0", "0", "0"])

                self.assertTrue(interrupted)
                self.assertEqual(result.exit_code, 130, result.stderr)
                if after_commit:
                    inbounds = {item.get("tag"): item.get("port") for item in box.config()["inbounds"]}
                    self.assertNotIn("vless-reality", inbounds)
                    self.assertEqual(inbounds["vless-reality-2"], 8443)
                    self.assertNotIn('"vless-reality",', box.db_rows("reality_connections"))
                    self.assertEqual(box.env()["PORT"], "8443")
                    self.assertEqual(link.read_bytes(), before[0][link])
                else:
                    self.assertEqual(state(box), before)
                with database.session() as conn:
                    self.assertEqual(client_traffic_handoff.load(conn), {})

    def test_menu_key_derivation_failure_keeps_transports_and_does_not_print_secret(self):
        for debug in ("0", "1"):
            with self.subTest(debug=debug), Sandbox() as box, running_xray(box), \
                    mock.patch.dict(os.environ, {"XRAY_MENU_DEBUG": debug}):
                script_menu_processes(box)
                original = seed_starter_file(box)
                inbound = next(item for item in box.config()["inbounds"] if item.get("tag") == "vless-reality")
                marker = inbound["streamSettings"]["realitySettings"]["privateKey"]
                with database.write_session() as conn:
                    conn.execute("UPDATE reality_connections SET public_key='' WHERE tag='vless-reality'")
                box.processes.script({
                    ("/usr/local/bin/xray", "x25519", "-i", marker): box.processes.result(1, stdout=marker, stderr=marker),
                })
                changed = box.run_menu(["2", "1", "7", "2", "fixture-grpc", "0", "0", "0"])

                self.assertEqual(changed.exit_code, 0)
                self.assertNotIn(marker, changed.stdout + changed.stderr)
                self.assertIn("TRANSPORT: grpc", changed.stdout)
                self.assertIn("REALITY_TRANSPORT=grpc", box.paths.server_env.read_text())
                self.assertEqual(box.paths.initial_link.read_text(), f"INITIAL_PROTOCOL=vless\nCLIENT_URI={original}\n")
                box.assert_restarted(times=1)

    def test_legacy_fingerprint_stops_transport_change_and_removal_before_writes(self):
        for args, tag in (
            (("connection-transport", "vless-reality", "grpc"), "vless-reality"),
            (("remove-connection", "vless-reality"), "vless-reality-2"),
        ):
            with self.subTest(command=args[0]), Sandbox() as box, running_xray(box):
                script_processes(box)
                if args[0] == "remove-connection":
                    added = box.run_cli("client", "add-connection", "Second", "8443", "second.example")
                    self.assertEqual((added.exit_code, added.stderr), (0, ""))
                with database.write_session() as conn:
                    conn.execute("UPDATE reality_connections SET fingerprint='legacy-fp' WHERE tag=?", (tag,))
                before = state(box)
                commands = len(box.processes.calls)

                result = box.run_cli("client", *args)

                self.assertEqual(result.exit_code, 1)
                self.assertIn("FINGERPRINT must be one of:", result.stderr)
                self.assertEqual(state(box), before)
                self.assertEqual(len(box.processes.calls), commands)

    def test_menu_transport_updates_server_env_when_starter_link_cannot_be_written(self):
        with Sandbox() as box, running_xray(box):
            script_menu_processes(box)
            box.paths.initial_link.mkdir(parents=True)
            changed = box.run_menu(["2", "1", "7", "2", "fixture-grpc", "0", "0", "0"])

            self.assertEqual(changed.exit_code, 0)
            self.assertIn("TRANSPORT: grpc", changed.stdout)
            self.assertIn(str(box.paths.initial_link), changed.stderr)
            self.assertIn("REALITY_TRANSPORT=grpc", box.paths.server_env.read_text())
            self.assertIn("GRPC_SERVICE_NAME=fixture-grpc", box.paths.server_env.read_text())
            self.assertIn("fixture-grpc", box.db_rows("reality_connections"))
            box.assert_restarted(times=1)


if __name__ == "__main__":
    unittest.main()
