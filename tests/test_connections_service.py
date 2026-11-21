"""Порядок и компенсация изменения подключений через публичный xray-client."""

from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_client_characterization import (
    CADDY_ACTIVATION_STATE, is_caddy_candidate_validation, script_processes, write_config,
)
from tests.test_client_command_read import script_caddy
from tests.test_client_manager_lock import lock_is_free
from tests.test_connection_menu_characterization import script_menu_processes
from xray_vps_manager.caddy import model as caddy_model, sites as caddy_sites
from xray_vps_manager.clients import connections, repository
from xray_vps_manager.core import locks
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import client_traffic_handoff, client_undo
from xray_vps_manager.xray.client_routes import client_balancer_tag
from xray_vps_manager.xray.config import TROJAN_INBOUND_TAG


TLS = ("add-connection", "TLS", "18100", "tls.example", "--security", "tls",
       "--transport", "xhttp", "--install-caddy", "--public-port", "1443")
TROJAN = ("add-trojan-connection", "Trojan", "18101", "trojan.example", "--public-port", "2443")
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


class ConnectionCaddyCompensationTests(unittest.TestCase):
    def start(self, box):
        script_processes(box)
        script_caddy(box)
        box.paths.initial_link.parent.mkdir(parents=True, exist_ok=True)
        box.paths.initial_link.write_text("CLIENT_URI=starter\n")

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
                link.parent.mkdir(parents=True, exist_ok=True)
                link.write_text("CLIENT_URI=starter-before-removal\n")
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
                    self.assertIn("PORT=8443", link.read_text())
                    self.assertIn(":8443?", link.read_text())
                else:
                    self.assertEqual(state(box), before)
                with database.session() as conn:
                    self.assertEqual(client_traffic_handoff.load(conn), {})

    def test_menu_key_derivation_failure_keeps_transports_and_does_not_print_secret(self):
        for debug in ("0", "1"):
            with self.subTest(debug=debug), Sandbox() as box, running_xray(box), \
                    mock.patch.dict(os.environ, {"XRAY_MENU_DEBUG": debug}):
                script_menu_processes(box)
                inbound = next(item for item in box.config()["inbounds"] if item.get("tag") == "vless-reality")
                marker = inbound["streamSettings"]["realitySettings"]["privateKey"]
                with database.write_session() as conn:
                    conn.execute("UPDATE reality_connections SET public_key='' WHERE tag='vless-reality'")
                box.processes.script({
                    ("/usr/local/bin/xray", "x25519", "-i", marker): box.processes.result(1, stdout=marker, stderr=marker),
                })
                box.paths.initial_link.parent.mkdir(parents=True, exist_ok=True)
                box.paths.initial_link.write_text("CLIENT_URI=starter\n")

                changed = box.run_menu(["2", "1", "7", "2", "fixture-grpc", "0", "0", "0"])

                self.assertEqual(changed.exit_code, 0)
                self.assertNotIn(marker, changed.stdout + changed.stderr)
                self.assertIn("TRANSPORT: grpc", changed.stdout)
                self.assertIn("REALITY_TRANSPORT=grpc", box.paths.server_env.read_text())
                self.assertEqual(box.paths.initial_link.read_text(), "CLIENT_URI=starter\n")
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
