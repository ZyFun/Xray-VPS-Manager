"""VIREI-124, вариант В: no-op писателей и обязательный restart установленного комплекта."""

from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from io import StringIO
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

from tests.support.sandbox import Sandbox
from tests.test_client_manager_lock import foreign_lock_holder, lock_is_free, script_apply
from tests.test_restart_verification import FakeClock, FakeSocket
from tests.test_routing_apply_compensation import legacy_telegram_config
from xray_vps_manager.clients import repository, routes, service
from xray_vps_manager.commands import backup, client, menu_reality_actions, menu_xray_actions, update
from xray_vps_manager.core import fs, systemd
from xray_vps_manager.core.errors import ApplyConfigError, StateError, LockTimeout
from xray_vps_manager.routing import torrent
from xray_vps_manager.xray import apply as mechanism


@contextmanager
def live(box):
    script_apply(box)
    with mock.patch.object(systemd, "time", FakeClock()), \
            mock.patch("socket.create_connection", return_value=FakeSocket()):
        yield


class WriterAdaptersTests(unittest.TestCase):
    def test_route_test_timeout_keeps_working_file_and_compensates_db(self):
        with Sandbox() as box:
            config = box.config()
            config["outbounds"].extend({"tag": tag, "protocol": "freedom"}
                                       for tag in ("cascade-old", "cascade-new"))
            config["routing"]["rules"].append({"type": "field", "network": "tcp,udp", "outboundTag": "cascade-old"})
            db = repository.load_db()
            db["clients"]["alice"]["selectedCascadeTag"] = "cascade-old"
            routes.ensure_all_client_route_config(config, db)
            repository.save_db(db)
            box.paths.config.write_text(json.dumps(config))
            original = box.paths.config.read_bytes()
            old_db = repository.load_db()
            candidate = box.paths.config.with_suffix(".json.tmp")
            command = (str(box.paths.xray_bin), "run", "-test", "-format", "json", "-config", str(candidate))

            def timeout(argv, **kwargs):
                self.assertEqual(box.paths.config.read_bytes(), original)
                self.assertFalse(list(box.paths.config_dir.glob("config.json.bak.*")))
                self.assertEqual(kwargs["timeout"], 30)
                raise subprocess.TimeoutExpired(argv, 30)
            box.processes.script({command: timeout})
            result = box.run_cli("client", "route", "alice", "cascade-new")
            self.assertEqual(result.exit_code, 1, result.stderr)
            self.assertIn("timed out", result.stderr)
            self.assertFalse(candidate.exists())
            self.assertFalse(list(box.paths.config_dir.glob("config.json.bak.*")))
            self.assertEqual(box.paths.config.read_bytes(), original)
            self.assertEqual(repository.load_db(), old_db)
            self.assertEqual(box.processes.calls, [command])

    def normalized(self, box):
        config = box.config()
        routes.ensure_all_client_route_config(config, repository.load_db())
        box.paths.config.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
        return config

    def test_noop_writers_do_not_backup_or_restart_but_client_saves_metadata(self):
        for module in (client, menu_reality_actions, menu_xray_actions):
            with self.subTest(module=module.__name__), Sandbox() as box, live(box):
                config = self.normalized(box)
                original = box.paths.config.read_bytes()
                if module is client:
                    db = repository.load_db()
                    db["clients"]["alice"]["paymentType"] = "paid"
                    result = client.save_config_restart_xray_and_db(config, db)
                    self.assertEqual(repository.load_db()["clients"]["alice"]["paymentType"], "paid")
                else:
                    result = module.apply_config(config)
                self.assertIsNone(result)
                self.assertEqual(box.paths.config.read_bytes(), original)
                self.assertEqual(list(box.paths.config_dir.glob("config.json.bak.*")), [])
                box.assert_restarted(times=0)
                self.assertEqual(len(box.processes.calls), 1)

    def test_noop_db_failure_does_not_attempt_restore_without_backup(self):
        with Sandbox() as box, live(box):
            config = self.normalized(box)
            with mock.patch.object(repository, "save_db", side_effect=OSError("db full")), \
                    self.assertRaises(StateError) as raised:
                service.apply_config_and_save_db(config, repository.load_db())
            self.assertIn("Config was not changed", str(raised.exception))
            self.assertNotIn("None", str(raised.exception))
            box.assert_restarted(times=0)

    def test_cancellation_before_db_write_keeps_db_and_restores_config_once(self):
        for action in ("stop", "restart", "show"):
            with self.subTest(action=action), Sandbox() as box, live(box):
                original = box.paths.config.read_bytes()
                db = repository.load_db()
                db["clients"]["alice"]["paymentType"] = "paid"
                config = box.config()
                config["inbounds"][1]["port"] = 8443
                command = ("systemctl", "show", "--property=ActiveState,SubState,NRestarts", "xray") if action == "show" else ("systemctl", action, "xray")
                replies = [KeyboardInterrupt(), box.processes.ok("ActiveState=active\nSubState=running\nNRestarts=0\n")]
                # show после отмены должен отвечать всё окно проверки отката.
                replies.extend([replies[-1]] * 20)
                box.processes.script({command: replies})
                with self.assertRaises(KeyboardInterrupt):
                    client.save_config_restart_xray_and_db(config, db)
                self.assertEqual(box.paths.config.read_bytes(), original)
                self.assertEqual(repository.load_db()["clients"]["alice"]["paymentType"], "free")
                self.assertEqual(len(list(box.paths.config_dir.glob("config.json.bak.*"))), 1)
                box.assert_restarted(times=1 if action == "stop" else 2)

    def test_torrent_applies_under_manager_lock_and_noop_prints_no_none(self):
        with Sandbox() as box, live(box):
            checks = []
            replace = fs.replace_file
            def observed(path, *args, **kwargs):
                checks.append(not lock_is_free(box.paths.manager_lock))
                return replace(path, *args, **kwargs)
            with mock.patch.object(fs, "replace_file", side_effect=observed):
                result = torrent.set_blocked(False)
            self.assertTrue(result.changed)
            self.assertTrue(checks and all(checks))
            output = StringIO()
            with redirect_stdout(output):
                menu_xray_actions.set_torrent_block(False)
            self.assertNotIn("None", output.getvalue())
            box.assert_restarted(times=1)

    def test_torrent_lock_contention_leaves_config_untouched(self):
        with Sandbox() as box, live(box):
            original = box.paths.config.read_bytes()
            with foreign_lock_holder(box.paths.manager_lock, "another writer"), \
                    mock.patch("xray_vps_manager.core.locks.acquire_flock", side_effect=LockTimeout("busy")), \
                    self.assertRaises(LockTimeout):
                torrent.set_blocked(False)
            self.assertEqual(box.paths.config.read_bytes(), original)
            self.assertEqual(box.processes.calls, [])


class LegacyTelegramWriterTests(unittest.TestCase):
    """R01: обновление с Telegram перед api не блокирует постоянных писателей."""

    def assert_applied(self, box, original, other_rules):
        rules = box.config()["routing"]["rules"]
        self.assertEqual(rules[0]["outboundTag"], "api")
        self.assertEqual(rules[1:], other_rules)
        backups = list(box.paths.config_dir.glob("config.json.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        box.assert_restarted(times=1)

    def test_client_add_disable_and_expire_accept_legacy_telegram_order(self):
        for operation in ("add", "disable", "expire-due"):
            with self.subTest(operation=operation), Sandbox() as box, live(box):
                config = legacy_telegram_config(box)
                if operation == "expire-due":
                    db = repository.load_db()
                    db["clients"]["alice"]["expiresAt"] = "2020-01-01T00:00:00Z"
                    repository.save_db(db)
                original = box.paths.config.read_bytes()
                others = deepcopy([rule for rule in config["routing"]["rules"]
                                   if rule.get("outboundTag") != "api"])
                box.processes.script({"/usr/local/bin/xray uuid": box.processes.ok(
                    "22222222-2222-4222-8222-222222222222\n")})
                args = ("add", "bob", "1") if operation == "add" else (
                    ("disable", "alice") if operation == "disable" else ("expire-due",))
                result = box.run_cli("client", *args)
                self.assertEqual(result.exit_code, 0, result.stderr)
                db = repository.load_db()
                if operation == "add":
                    self.assertIn("bob", db["clients"])
                else:
                    self.assertFalse(db["clients"]["alice"]["enabled"])
                self.assert_applied(box, original, others)

    def test_reality_writer_preserves_telegram_route_and_other_rules(self):
        with Sandbox() as box, live(box):
            config = legacy_telegram_config(box)
            original = box.paths.config.read_bytes()
            others = deepcopy([rule for rule in config["routing"]["rules"]
                               if rule.get("outboundTag") != "api"])
            config["log"]["loglevel"] = "debug"
            menu_reality_actions.apply_config(config)
            self.assert_applied(box, original, others)

    def test_torrent_toggle_accepts_legacy_telegram_order(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked), Sandbox() as box, live(box):
                config = legacy_telegram_config(box)
                if blocked:
                    config["routing"]["rules"] = [rule for rule in config["routing"]["rules"]
                                                   if "bittorrent" not in rule.get("protocol", [])]
                    box.paths.config.write_text("  " + json.dumps(config) + "\n\n")
                original = box.paths.config.read_bytes()
                telegram_rule = deepcopy(config["routing"]["rules"][0])
                result = torrent.set_blocked(blocked)
                self.assertTrue(result.changed)
                rules = box.config()["routing"]["rules"]
                self.assertEqual(rules[0]["outboundTag"], "api")
                self.assertIn(telegram_rule, rules)
                self.assertEqual(any("bittorrent" in rule.get("protocol", []) for rule in rules), blocked)
                self.assertEqual(result.backup.read_bytes(), original)
                box.assert_restarted(times=1)

    def test_failed_writers_restore_legacy_bytes_and_client_database(self):
        for writer in ("client", "reality", "torrent"):
            with self.subTest(writer=writer), Sandbox() as box, live(box):
                config = legacy_telegram_config(box)
                original = box.paths.config.read_bytes()
                old_db = repository.load_db()
                box.processes.script({"systemctl restart xray": [
                    box.processes.result(1), box.processes.result(1), box.processes.ok()]})
                if writer == "client":
                    result = box.run_cli("client", "disable", "alice")
                    self.assertEqual(result.exit_code, 1)
                    self.assertIn("Restored backup", result.stderr)
                elif writer == "reality":
                    config["log"]["loglevel"] = "debug"
                    with self.assertRaises(SystemExit) as raised:
                        menu_reality_actions.apply_config(config)
                    self.assertIn("Restored backup", str(raised.exception))
                else:
                    with self.assertRaises(ApplyConfigError) as raised:
                        torrent.set_blocked(False)
                    self.assertTrue(raised.exception.restored)
                self.assertEqual(box.paths.config.read_bytes(), original)
                self.assertEqual(repository.load_db(), old_db)
                box.assert_restarted(times=3)


class InstalledConfigTests(unittest.TestCase):
    def test_manual_menu_check_keeps_existing_streaming_callback(self):
        with Sandbox() as box:
            calls = []
            menu_xray_actions.check_config(calls.append)
            self.assertEqual(calls, [[str(box.paths.xray_bin), "run", "-test", "-config", str(box.paths.config)]])
            self.assertEqual(box.processes.calls, [])

    def test_backup_and_update_always_restart_without_reformat_or_backup(self):
        for callback in (backup.test_and_restart, update.restart_xray):
            with self.subTest(callback=callback.__name__), Sandbox() as box, live(box):
                original = json.dumps(box.config(), separators=(",", ":")).encode()
                box.paths.config.write_bytes(original)
                result = callback()
                self.assertFalse(result.changed)
                self.assertTrue(result.restarted)
                self.assertIsNone(result.backup)
                self.assertEqual(result.verified_ports, (10085, 443))
                self.assertEqual(box.paths.config.read_bytes(), original)
                self.assertEqual(list(box.paths.config_dir.glob("config.json.*")), [])
                box.assert_restarted(times=1)

    def test_installed_failure_belongs_to_outer_operation(self):
        with Sandbox() as box, live(box):
            original = box.paths.config.read_bytes()
            box.processes.script({"systemctl show --property=ActiveState,SubState,NRestarts xray":
                                  box.processes.ok("ActiveState=failed\nSubState=failed\nNRestarts=0\n")})
            with self.assertRaises(ApplyConfigError) as raised:
                mechanism.restart_installed_config()
            self.assertFalse(raised.exception.restored)
            self.assertIsNone(raised.exception.backup)
            box.assert_restarted(times=1)
            self.assertEqual(box.paths.config.read_bytes(), original)


class UpdateOperationTests(unittest.TestCase):
    @contextmanager
    def prepared(self):
        with Sandbox() as box, live(box):
            for directory in (box.paths.xray_bin.parent, box.paths.asset_dir, box.paths.sbin_dir):
                directory.mkdir(parents=True, exist_ok=True)
            box.paths.xray_bin.write_bytes(b"old-binary")
            for name in update.ASSET_NAMES:
                (box.paths.asset_dir / name).write_bytes(b"old-" + name.encode())
            diagnostic = box.paths.sbin_dir / "xray-test"
            diagnostic.touch()
            box.processes.script({
                (str(diagnostic),): box.processes.ok(),
                (str(box.paths.xray_bin), "version"): box.processes.ok("Xray 1.0.0\n"),
            })
            box.http.script({update.GITHUB_API: box.http.json({"tag_name": "v2.0.0"})})
            work = box.root / "download"
            work.mkdir()
            archive = work / "Xray-linux-64.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                for name in ("xray", *update.ASSET_NAMES):
                    stream.writestr(name, b"new-" + name.encode())
            (work / "Xray-linux-64.zip.dgst").write_text("SHA2-256= " + hashlib.sha256(archive.read_bytes()).hexdigest())
            candidate = work / "xray/xray"
            for url, name in ((update.LATEST_ZIP, "Xray-linux-64.zip"), (update.LATEST_DGST, "Xray-linux-64.zip.dgst")):
                box.processes.script({("curl", "-fL", "--retry", "3", "--connect-timeout", "20", "--max-time", "240", "-o", str(work / name), url): box.processes.ok()})
            def preflight(argv, **kwargs):
                self.assertEqual(box.paths.xray_bin.read_bytes(), b"old-binary")
                self.assertEqual((box.paths.asset_dir / "geoip.dat").read_bytes(), b"old-geoip.dat")
                self.assertEqual(kwargs["env"]["XRAY_LOCATION_ASSET"], str(work / "xray"))
                return box.processes.ok()
            for binary in (candidate, box.paths.xray_bin):
                # Installed проверяется отдельно без env, кандидат — со staged assets.
                command = (str(binary), "run", "-test", "-config", str(box.paths.config))
                if binary == candidate:
                    box.processes.script({command: preflight})
                else:
                    def installed_or_assets(argv, **kwargs):
                        if kwargs.get("env"):
                            return preflight(argv, **kwargs)
                        return box.processes.ok()
                    box.processes.script({command: installed_or_assets})
            box.processes.script({(str(candidate), "version"): box.processes.ok("Xray 2.0.0\n")})
            @contextmanager
            def directory(*args, **kwargs):
                yield str(work)
            with mock.patch.object(tempfile, "TemporaryDirectory", side_effect=directory):
                yield box

    def test_binary_and_asset_updates_restart_under_lock_without_touching_config(self):
        for command in ("--update", "--update-assets"):
            with self.subTest(command=command), self.prepared() as box:
                original = box.paths.config.read_bytes()
                locks = []
                def restart(argv, **kwargs):
                    locks.append(not lock_is_free(box.paths.manager_lock))
                    return box.processes.ok()
                box.processes.script({"systemctl restart xray": restart})
                result = box.run_cli("update", command)
                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(locks, [True])
                self.assertEqual(box.paths.config.read_bytes(), original)
                self.assertEqual(list(box.paths.config_dir.glob("config.json.*")), [])
                self.assertEqual((box.paths.asset_dir / "geoip.dat").read_bytes(), b"new-geoip.dat")
                self.assertEqual(box.paths.xray_bin.read_bytes(), b"new-xray" if command == "--update" else b"old-binary")

    def test_cancellation_after_install_restores_owned_files_before_exit_130(self):
        for command in ("--update", "--update-assets"):
            for phase in ("test", "restart", "verify"):
                with self.subTest(command=command, phase=phase), self.prepared() as box:
                    original = box.paths.config.read_bytes()
                    if phase == "test":
                        argv = (str(box.paths.xray_bin), "run", "-test", "-config", str(box.paths.config))
                        calls = 0
                        def response(args, **kwargs):
                            nonlocal calls
                            if kwargs.get("env"):
                                return box.processes.ok()
                            calls += 1
                            if calls == 1:
                                raise KeyboardInterrupt()
                            return box.processes.ok()
                        box.processes.script({argv: response})
                    else:
                        argv = "systemctl restart xray" if phase == "restart" else "systemctl show --property=ActiveState,SubState,NRestarts xray"
                        box.processes.script({argv: [KeyboardInterrupt()] + [box.processes.ok("ActiveState=active\nSubState=running\nNRestarts=0\n")] * 20})
                    result = box.run_cli("update", command)
                    self.assertEqual(result.exit_code, 130, result.stderr)
                    self.assertEqual(box.paths.xray_bin.read_bytes(), b"old-binary")
                    for name in update.ASSET_NAMES:
                        self.assertEqual((box.paths.asset_dir / name).read_bytes(), b"old-" + name.encode())
                    self.assertEqual(box.paths.config.read_bytes(), original)
                    self.assertEqual(list(box.paths.config_dir.glob("config.json.*")), [])
                    box.assert_restarted(times=1 if phase == "test" else 2)

    def test_candidate_preflight_failure_does_not_install_or_restart(self):
        with self.prepared() as box:
            candidate = box.root / "download/xray/xray"
            box.processes.script({(str(candidate), "run", "-test", "-config", str(box.paths.config)): box.processes.result(1, stderr="candidate rejected")})
            with self.assertRaises(RuntimeError):
                with redirect_stdout(StringIO()):
                    update.cmd_update()
            self.assertEqual(box.paths.xray_bin.read_bytes(), b"old-binary")
            box.assert_restarted(times=0)
