"""R1.4: канон, доставка units, drift и безопасные режимы CLI."""
import io
import os
import stat
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests.support.sandbox import Sandbox, ProcessResult
from xray_vps_manager.commands import manager_update
from xray_vps_manager.commands import test as diagnostics
from xray_vps_manager.core import process
from xray_vps_manager.core.errors import StateError, ValidationError
from xray_vps_manager.system import units, unit_sync


class UnitsSyncTests(unittest.TestCase):
    def setUp(self):
        self.server = Sandbox().__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.directory = self.server.paths.systemd_dir

    def install(self):
        result = self.server.run_cli("units", "sync", "--no-systemctl")
        self.assertEqual((result.exit_code, result.stderr), (0, ""))
        return result

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode))
                for p in self.directory.iterdir()} if self.directory.exists() else {}

    def test_check_on_absent_directory_is_read_only_and_reports_all_ten(self):
        before = self.server.paths.manager_lock.exists()
        result = self.server.run_cli("units", "sync", "--check")
        self.assertEqual(result.exit_code, 1)
        for name in units.UNIT_NAMES:
            self.assertIn(name, result.stdout)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.server.paths.manager_lock.exists(), before)
        self.assertEqual(self.server.processes.calls, [])

    def test_no_systemctl_matches_all_golden_files_and_sets_mode(self):
        self.install()
        golden = Path(__file__).parent / "fixtures" / "units"
        self.assertEqual(set(self.snapshot()), set(units.UNIT_NAMES))
        for name in units.UNIT_NAMES:
            with self.subTest(name=name):
                self.assertEqual((self.directory / name).read_bytes(), (golden / name).read_bytes())
                self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o644)
        self.assertEqual(self.server.processes.calls, [])

    def test_second_sync_and_check_do_not_write_or_call_systemctl(self):
        self.install()
        before = self.snapshot()
        self.assertEqual(self.server.run_cli("units", "sync").exit_code, 0)
        self.assertEqual(self.server.run_cli("units", "sync", "--check").exit_code, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.server.processes.calls, [])

    def test_check_reports_content_and_mode_drift_without_repair(self):
        self.install()
        (self.directory / "xray.service").write_text("old\n")
        (self.directory / "xray-client-expire.timer").chmod(0o600)
        before = self.snapshot()
        result = self.server.run_cli("units", "sync", "--check", "--no-systemctl")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("xray.service, xray-client-expire.timer", result.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.server.processes.calls, [])

    def test_sync_changes_only_drifted_files_and_leaves_inactive_units_inactive(self):
        self.install()
        changed = ("xray.service", "xray-telegram-poller.service", "xray-client-expire.timer")
        for name in changed:
            (self.directory / name).write_text("old\n")
        before = self.snapshot()
        active = {"xray.service"}
        restarted = []
        def systemctl(command):
            if command == ["systemctl", "daemon-reload"]:
                return ProcessResult()
            if command[:2] == ["systemctl", "try-restart"]:
                if command[2] in active:
                    restarted.append(command[2])
                return ProcessResult()
            return None
        with process.fake(systemctl) as calls:
            self.assertEqual(unit_sync.sync_units(), list(changed))
        self.assertEqual(restarted, ["xray.service"])
        self.assertEqual(active, {"xray.service"})
        self.assertEqual(calls, [["systemctl", "daemon-reload"],
                                *[["systemctl", "try-restart", name] for name in changed]])
        for name in set(units.UNIT_NAMES) - set(changed):
            self.assertEqual(self.snapshot()[name], before[name])

    def test_changed_oneshots_are_reloaded_without_interrupting_current_run(self):
        names = ("xray-traffic-sync.service", "xray-client-expire.service",
                 "xray-raw-log-rotate.service", "xray-caddy-random-tls@.service")
        for name in names:
            with self.subTest(name=name):
                self.install()
                target = self.directory / name
                target.write_text("old\n")
                before = self.snapshot()
                # Выполняющийся oneshot находится в activating. Любая попытка
                # restart здесь могла бы прервать его и ждать manager.lock.
                def systemctl(command):
                    self.assertEqual(command, ["systemctl", "daemon-reload"])
                    return ProcessResult()
                with process.fake(systemctl) as calls:
                    self.assertEqual(unit_sync.sync_units(), [name])
                self.assertEqual(calls, [["systemctl", "daemon-reload"]])
                self.assertEqual(target.read_text(), units.unit_text(name))
                for unchanged in set(units.UNIT_NAMES) - {name}:
                    self.assertEqual(self.snapshot()[unchanged], before[unchanged])

    def test_template_update_restarts_only_discovered_active_instances(self):
        self.install()
        name = "xray-caddy-random-tls@.timer"
        (self.directory / name).write_text("old\n")
        instance = "xray-caddy-random-tls@api.example.com.timer"
        query = ["systemctl", "list-units", "--state=active", "--plain", "--no-legend", "--no-pager",
                 "xray-caddy-random-tls@*.timer"]
        self.server.processes.script({
            ("systemctl", "daemon-reload"): ProcessResult(),
            tuple(query): ProcessResult(stdout=instance + " loaded active waiting description\n"),
            ("systemctl", "try-restart", instance): ProcessResult(),
        })
        self.assertEqual(unit_sync.sync_units(), [name])
        self.assertEqual(self.server.processes.calls, [tuple(["systemctl", "daemon-reload"]),
                                                      tuple(query), ("systemctl", "try-restart", instance)])

    def test_custom_raw_schedule_is_shared_with_compatible_cli(self):
        self.server.paths.server_env.write_text("export manager_timezone = Europe/Berlin # зона с DST\nXRAY_RAW_LOG_ROTATE_TIME=04:30\n")
        self.install()
        timer = self.directory / "xray-raw-log-rotate.timer"
        self.assertEqual(timer.read_text(), unit_sync.raw_log_timer_unit())
        self.assertIn("04:30:00 Europe/Berlin", timer.read_text())
        self.assertIn("OnCalendar=*-*-* 00:00:00 Europe/Berlin\n", (self.directory / "xray-client-expire.timer").read_text())

    def test_sync_upgrades_existing_server_midnight_timer_only(self):
        self.install()
        name = "xray-client-expire.timer"
        timer = self.directory / name
        current = timer.read_text()
        old = current.replace("00:00:00 Europe/Moscow", "00:00:00")
        self.assertNotEqual(current, old)
        timer.write_text(old)
        before = self.snapshot()
        check = self.server.run_cli("units", "sync", "--check")
        self.assertEqual(check.stdout.strip(), "Отличающиеся systemd units: " + name)
        self.assertEqual(check.exit_code, 1)
        self.assertEqual(self.snapshot(), before)
        commands = [("systemctl", "daemon-reload"), ("systemctl", "try-restart", name)]
        self.server.processes.script({command: ProcessResult() for command in commands})
        self.assertEqual(self.server.run_cli("units", "sync").exit_code, 0)
        self.assertEqual(timer.read_text(), current)
        self.assertEqual(self.server.run_cli("units", "sync", "--check").exit_code, 0)
        self.assertEqual(self.server.processes.calls, commands)

    def test_expire_calendar_falls_back_to_local_and_only_warns_for_invalid_zone(self):
        timer = self.directory / "xray-client-expire.timer"
        for zone in ("", "local", "Missing/Zone", "../UTC"):
            with self.subTest(zone=zone):
                self.server.paths.server_env.write_text("MANAGER_TIMEZONE=Europe/Moscow\n")
                self.install()
                self.assertIn("OnCalendar=*-*-* 00:00:00 Europe/Moscow\n", timer.read_text())
                self.server.paths.server_env.write_text(f"MANAGER_TIMEZONE={zone}\n")
                before = self.snapshot()
                drift = self.server.run_cli("units", "sync", "--check")
                self.assertEqual(drift.exit_code, 1)
                self.assertIn(timer.name, drift.stdout)
                self.assertEqual(self.snapshot(), before)
                installed = self.server.run_cli("units", "sync", "--no-systemctl")
                self.assertEqual(installed.exit_code, 0)
                self.assertIn("OnCalendar=*-*-* 00:00:00\n", timer.read_text())
                before = self.snapshot()
                clean = self.server.run_cli("units", "sync", "--check")
                self.assertEqual(clean.exit_code, 0)
                self.assertEqual(self.snapshot(), before)
                for result in (drift, installed, clean):
                    warnings = result.stderr.splitlines()
                    if zone in ("", "local"):
                        self.assertEqual(warnings, [])
                    else:
                        self.assertEqual(len(warnings), 1)
                        self.assertIn(f"MANAGER_TIMEZONE invalid: {zone} (server local time)", warnings[0])
                        self.assertNotIn("invalid", result.stdout)
                self.assertEqual(self.server.processes.calls, [])

    def test_unknown_argument_does_not_write(self):
        result = self.server.run_cli("units", "sync", "--bogus")
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.server.processes.calls, [])

    def test_unknown_unit_name_is_rejected(self):
        with self.assertRaises(ValidationError):
            units.unit_text("../other.service")

    def test_symlink_is_rejected_without_following_or_systemctl(self):
        self.directory.mkdir(parents=True)
        target = self.server.root / "unrelated"
        target.write_text("keep")
        (self.directory / "xray.service").symlink_to(target)
        result = self.server.run_cli("units", "sync", "--no-systemctl")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("ERROR:", result.stderr)
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(self.server.processes.calls, [])

    def test_unreadable_unit_path_fails_before_writes(self):
        self.directory.mkdir(parents=True)
        (self.directory / "xray-client-expire.timer").mkdir()
        result = self.server.run_cli("units", "sync", "--no-systemctl")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Не удалось прочитать unit", result.stderr)
        self.assertFalse((self.directory / "xray.service").exists())
        self.assertEqual(self.server.processes.calls, [])

    def test_failed_reload_restores_previous_file_and_retry_can_apply(self):
        self.install()
        target = self.directory / "xray.service"
        target.write_text("old\n")
        self.server.processes.script({
            ("systemctl", "daemon-reload"): [ProcessResult(returncode=1, stderr="reload failed"), ProcessResult()],
            ("systemctl", "try-restart", "xray.service"): ProcessResult(),
        })
        result = self.server.run_cli("units", "sync")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("reload failed", result.stderr)
        self.assertEqual(target.read_text(), "old\n")
        self.assertEqual(self.server.run_cli("units", "sync").exit_code, 0)
        self.assertEqual(target.read_text(), units.unit_text("xray.service"))

    def test_failed_restart_restores_files_and_reloads_previous_units(self):
        self.install()
        target = self.directory / "xray.service"
        target.write_text("old\n")
        self.server.processes.script({
            ("systemctl", "daemon-reload"): ProcessResult(),
            ("systemctl", "try-restart", "xray.service"): ProcessResult(returncode=1, stderr="restart failed"),
        })
        result = self.server.run_cli("units", "sync")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(target.read_text(), "old\n")
        self.assertEqual(self.server.processes.calls[-1], ("systemctl", "daemon-reload"))

    def test_timeout_is_reported_and_preserves_retry(self):
        self.install()
        target = self.directory / "xray.service"
        target.write_text("old\n")
        self.server.processes.script({("systemctl", "daemon-reload"): subprocess.TimeoutExpired("systemctl", 30)})
        result = self.server.run_cli("units", "sync")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Истекло время", result.stderr)
        self.assertEqual(target.read_text(), "old\n")

    def test_random_tls_parameters_are_preserved(self):
        self.assertIn("EnvironmentFile=/custom/%i.env", units.random_tls_service_unit(Path("/custom")))
        self.assertIn("Unit=custom.service", units.random_tls_timer_unit("custom.service"))

    def test_diagnostics_reports_drift_as_warning_without_changes(self):
        self.install()
        (self.directory / "xray.service").write_text("old\n")
        before = self.snapshot()
        diag = diagnostics.Diagnostics()
        with redirect_stdout(io.StringIO()):
            diag.check("Systemd unit drift", diagnostics.check_unit_drift, fatal=False)
        self.assertEqual(diag.failures, [])
        self.assertEqual(len(diag.warnings), 1)
        self.assertIn("xray.service", diag.warnings[0])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.server.processes.calls, [])

    def test_update_invokes_installed_wrapper_for_all_units(self):
        package = self.server.paths.lib_dir / "xray_vps_manager"
        (package / "system").mkdir(parents=True)
        (package / "system" / "units.py").write_text("# installed package marker\n")
        command = (str(self.server.paths.sbin_dir / "xray-vps-manager"), "units", "sync")
        self.server.processes.script({command: ProcessResult()})
        with redirect_stdout(io.StringIO()):
            manager_update.sync_raw_log_units()
        self.assertEqual(self.server.processes.calls, [command])

    def test_delivery_uses_replaced_package_and_legacy_rollback(self):
        release = self.server.root / "release"
        (release / "xray_vps_manager" / "system").mkdir(parents=True)
        (release / "xray_vps_manager" / "system" / "units.py").write_text("# new generator\n")
        manager_update.install_python_package(release)
        new_command = (str(self.server.paths.sbin_dir / "xray-vps-manager"), "units", "sync")
        old_command = (str(self.server.paths.sbin_dir / "xray-activity"), "raw-log-timer-sync")
        self.server.paths.sbin_dir.mkdir(parents=True)
        (self.server.paths.sbin_dir / "xray-activity").write_text("#!/custom/python\n")
        self.server.processes.script({new_command: ProcessResult(), old_command: ProcessResult()})
        backup = self.server.root / "old-backup"
        old_package = backup / "package" / "xray_vps_manager"
        old_package.mkdir(parents=True)
        (old_package / "__init__.py").write_text("# legacy package\n")
        with redirect_stdout(io.StringIO()):
            manager_update.sync_raw_log_units()
            manager_update.restore_backup(backup)
            manager_update.sync_raw_log_units()
        self.assertEqual(self.server.processes.calls, [new_command, old_command])
        self.assertFalse((self.server.paths.lib_dir / "xray_vps_manager" / "system" / "units.py").exists())

    def test_write_permission_error_reports_failure_without_systemctl(self):
        if os.getuid() == 0:
            self.skipTest("Проверка запрета записи требует непривилегированного пользователя")
        self.directory.mkdir(parents=True)
        self.directory.chmod(0o555)
        try:
            result = self.server.run_cli("units", "sync", "--no-systemctl")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("Не удалось записать unit", result.stderr)
            self.assertEqual(list(self.directory.iterdir()), [])
            self.assertEqual(self.server.processes.calls, [])
        finally:
            self.directory.chmod(0o755)

    def test_update_sync_failure_propagates_to_update_rollback(self):
        package = self.server.paths.lib_dir / "xray_vps_manager"
        (package / "system").mkdir(parents=True)
        (package / "system" / "units.py").write_text("# installed package marker\n")
        command = (str(self.server.paths.sbin_dir / "xray-vps-manager"), "units", "sync")
        self.server.processes.script({command: ProcessResult(returncode=1, stderr="units failed")})
        with self.assertRaisesRegex(manager_update.ManagerUpdateError, "units failed"):
            manager_update.sync_raw_log_units()


if __name__ == "__main__":
    unittest.main()
