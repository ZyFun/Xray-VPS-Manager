from pathlib import Path
from contextlib import redirect_stderr
from io import StringIO
import json
import sqlite3
import tarfile
import tempfile
import unittest
from unittest import mock

from xray_vps_manager.commands import backup
from xray_vps_manager.db import database


class BackupSQLiteTests(unittest.TestCase):
    def test_backup_files_include_manager_database_path(self) -> None:
        self.assertIn(
            ("usr/local/etc/xray/manager.db", backup.MANAGER_DB_PATH, True),
            backup.BACKUP_FILES,
        )
        self.assertNotIn("clients.json", "\n".join(item[0] for item in backup.BACKUP_FILES))

    def test_create_backup_includes_manager_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_dir = root / "etc" / "xray"
            config_dir.mkdir(parents=True)
            files = {
                "config.json": "{}\n",
                "server.env": "SERVER_ADDR=old.example.com\nSERVER_NAME=Virei\nSECURITY_AUDIT_LAST_RUN=2026-06-01T00:00:00Z\n",
                "manager.db": "sqlite bytes",
            }
            for name, content in files.items():
                (config_dir / name).write_text(content)

            backup_files = [
                ("usr/local/etc/xray/config.json", config_dir / "config.json", True),
                ("usr/local/etc/xray/server.env", config_dir / "server.env", True),
                ("usr/local/etc/xray/manager.db", config_dir / "manager.db", True),
            ]

            snapshot_path = root / "manager-snapshot.db"
            snapshot_path.write_text("sqlite snapshot")
            snapshot_dirs = []

            def create_snapshot(snapshot_dir: Path) -> Path:
                snapshot_dirs.append(snapshot_dir)
                return snapshot_path

            with mock.patch.object(backup, "BACKUP_DIR", root / "backups"), mock.patch.object(
                backup, "BACKUP_FILES", backup_files
            ), mock.patch.object(backup, "BACKUP_DIRS", []), mock.patch.object(
                backup, "MANAGER_DB_PATH", config_dir / "manager.db"
            ), mock.patch.object(
                backup, "create_manager_db_archive_snapshot", side_effect=create_snapshot
            ):
                archive = backup.create_backup(quiet=True, sync=False)

            with tarfile.open(archive, "r:gz") as tar:
                names = set(tar.getnames())
                manager_db = tar.extractfile("usr/local/etc/xray/manager.db").read().decode()
                server_env = tar.extractfile("usr/local/etc/xray/server.env").read().decode()
                manifest = json.loads(tar.extractfile("manifest.json").read())

            self.assertEqual(len(snapshot_dirs), 1)
            self.assertEqual(snapshot_dirs[0].parent, root / "backups")
            self.assertIn("usr/local/etc/xray/manager.db", names)
            self.assertEqual(manager_db, "sqlite snapshot")
            self.assertNotIn("SERVER_ADDR", server_env)
            self.assertNotIn("SECURITY_AUDIT_LAST_RUN", server_env)
            self.assertIn("SERVER_NAME=Virei", server_env)
            self.assertEqual(
                manifest["hostSpecificServerEnvKeysOmitted"],
                ["SERVER_ADDR", "SECURITY_AUDIT_LAST_RUN"],
            )
            self.assertNotIn("hostname", manifest)
            self.assertTrue(
                any(item["archive"] == "usr/local/etc/xray/manager.db" for item in manifest["files"])
            )

    def test_create_backup_includes_caddy_random_tls_env_and_enabled_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_dir = root / "etc" / "xray"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text("{}\n")
            (config_dir / "server.env").write_text("SERVER_NAME=Virei\n")
            (config_dir / "manager.db").write_text("sqlite bytes")
            random_tls_env = config_dir / "caddy-random-tls.env"
            random_tls_env.write_text("TLS_RANDOM_DOMAIN=api.example.com\nTLS_RANDOM_LOCAL_PORT=10300\n")
            random_tls_dir = config_dir / "caddy-random-tls.d"
            random_tls_dir.mkdir()
            (random_tls_dir / "api.example.com.env").write_text("TLS_RANDOM_DOMAIN=api.example.com\nTLS_RANDOM_LOCAL_PORT=10300\n")

            backup_files = [
                ("usr/local/etc/xray/config.json", config_dir / "config.json", True),
                ("usr/local/etc/xray/server.env", config_dir / "server.env", True),
                ("usr/local/etc/xray/manager.db", config_dir / "manager.db", True),
                (backup.CADDY_RANDOM_TLS_ENV_ARCNAME, random_tls_env, False),
            ]
            backup_dirs = [
                (backup.CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME, random_tls_dir, False),
            ]
            snapshot_path = root / "manager-snapshot.db"
            snapshot_path.write_text("sqlite snapshot")

            with mock.patch.object(backup, "BACKUP_DIR", root / "backups"), mock.patch.object(
                backup, "BACKUP_FILES", backup_files
            ), mock.patch.object(backup, "BACKUP_DIRS", backup_dirs), mock.patch.object(
                backup, "MANAGER_DB_PATH", config_dir / "manager.db"
            ), mock.patch.object(
                backup, "CADDY_RANDOM_TLS_ENV_PATH", random_tls_env
            ), mock.patch.object(
                backup, "CADDY_RANDOM_TLS_CONFIG_DIR", random_tls_dir
            ), mock.patch.object(
                backup, "create_manager_db_archive_snapshot", return_value=snapshot_path
            ), mock.patch.object(
                backup, "systemctl_is_enabled", return_value=True
            ):
                archive = backup.create_backup(quiet=True, sync=False)

            with tarfile.open(archive, "r:gz") as tar:
                names = set(tar.getnames())
                restored_env = tar.extractfile(backup.CADDY_RANDOM_TLS_ENV_ARCNAME).read().decode()
                manifest = json.loads(tar.extractfile("manifest.json").read())

            self.assertIn(backup.CADDY_RANDOM_TLS_ENV_ARCNAME, names)
            self.assertIn(f"{backup.CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME}/api.example.com.env", names)
            self.assertIn("TLS_RANDOM_DOMAIN=api.example.com", restored_env)
            self.assertEqual(
                manifest["caddyRandomTls"],
                {
                    "configured": True,
                    "enabled": True,
                    "envArchive": backup.CADDY_RANDOM_TLS_ENV_ARCNAME,
                    "configsArchive": backup.CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME,
                    "service": "xray-caddy-random-tls@.service",
                    "timer": "xray-caddy-random-tls@.timer",
                    "sites": [
                        {
                            "domain": "api.example.com",
                            "localPort": 10300,
                            "enabled": True,
                            "envArchive": f"{backup.CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME}/api.example.com.env",
                            "service": "xray-caddy-random-tls@api.example.com.service",
                            "timer": "xray-caddy-random-tls@api.example.com.timer",
                        }
                    ],
                },
            )

    def test_create_backup_includes_caddy_config_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_dir = root / "etc" / "xray"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text("{}\n")
            (config_dir / "server.env").write_text("SERVER_NAME=Virei\n")
            (config_dir / "manager.db").write_text("sqlite bytes")

            caddyfile = root / "etc" / "caddy" / "Caddyfile"
            conf_dir = root / "etc" / "caddy" / "conf.d"
            caddyfile.parent.mkdir(parents=True)
            conf_dir.mkdir()
            caddyfile.write_text("import /etc/caddy/conf.d/*.caddy\n")
            (conf_dir / "vpn.example.com.caddy").write_text("vpn.example.com {\n    reverse_proxy 127.0.0.1:10100\n}\n")

            backup_files = [
                ("usr/local/etc/xray/config.json", config_dir / "config.json", True),
                ("usr/local/etc/xray/server.env", config_dir / "server.env", True),
                ("usr/local/etc/xray/manager.db", config_dir / "manager.db", True),
            ]
            snapshot_path = root / "manager-snapshot.db"
            snapshot_path.write_text("sqlite snapshot")

            with mock.patch.object(backup, "BACKUP_DIR", root / "backups"), mock.patch.object(
                backup, "BACKUP_FILES", backup_files
            ), mock.patch.object(backup, "BACKUP_DIRS", []), mock.patch.object(
                backup, "CADDYFILE_PATH", caddyfile
            ), mock.patch.object(
                backup, "CADDY_CONF_DIR", conf_dir
            ), mock.patch.object(
                backup, "CADDY_CONFIG_FILES", [(backup.CADDYFILE_ARCNAME, caddyfile, False)]
            ), mock.patch.object(
                backup, "CADDY_CONFIG_DIRS", [(backup.CADDY_CONF_DIR_ARCNAME, conf_dir, False)]
            ), mock.patch.object(
                backup, "MANAGER_DB_PATH", config_dir / "manager.db"
            ), mock.patch.object(
                backup, "create_manager_db_archive_snapshot", return_value=snapshot_path
            ):
                archive = backup.create_backup(quiet=True, sync=False)

            with tarfile.open(archive, "r:gz") as tar:
                names = set(tar.getnames())
                manifest = json.loads(tar.extractfile("manifest.json").read())

            self.assertIn(backup.CADDYFILE_ARCNAME, names)
            self.assertIn(f"{backup.CADDY_CONF_DIR_ARCNAME}/vpn.example.com.caddy", names)
            self.assertEqual(
                manifest["caddyConfig"],
                {
                    "configured": True,
                    "caddyfileArchive": backup.CADDYFILE_ARCNAME,
                    "confDirArchive": backup.CADDY_CONF_DIR_ARCNAME,
                },
            )

    def test_create_backup_allows_sqlite_only_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_dir = root / "etc" / "xray"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text("{}\n")
            (config_dir / "server.env").write_text("SERVER_NAME=Virei\n")
            (config_dir / "manager.db").write_text("sqlite bytes")

            backup_files = [
                ("usr/local/etc/xray/config.json", config_dir / "config.json", True),
                ("usr/local/etc/xray/server.env", config_dir / "server.env", True),
                ("usr/local/etc/xray/manager.db", config_dir / "manager.db", True),
            ]

            snapshot_path = root / "manager-snapshot.db"
            snapshot_path.write_text("sqlite snapshot")

            with mock.patch.object(backup, "BACKUP_DIR", root / "backups"), mock.patch.object(
                backup, "BACKUP_FILES", backup_files
            ), mock.patch.object(backup, "BACKUP_DIRS", []), mock.patch.object(
                backup, "MANAGER_DB_PATH", config_dir / "manager.db"
            ), mock.patch.object(
                backup, "create_manager_db_archive_snapshot", return_value=snapshot_path
            ):
                archive = backup.create_backup(quiet=True, sync=False)

            with tarfile.open(archive, "r:gz") as tar:
                names = set(tar.getnames())

            self.assertIn("usr/local/etc/xray/manager.db", names)
            self.assertNotIn("usr/local/etc/xray/clients.json", names)

    def test_create_manager_db_archive_snapshot_uses_sqlite_backup_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            connection = database.open_database(db_path)
            try:
                connection.execute(
                    "INSERT INTO manager_metadata(key, value) VALUES ('sample', 'value')"
                )
                connection.commit()
            finally:
                connection.close()

            snapshot_dir = root / "snapshots"
            with mock.patch.object(backup, "MANAGER_DB_PATH", db_path):
                snapshot = backup.create_manager_db_archive_snapshot(snapshot_dir)

            self.assertTrue(snapshot.exists())
            self.assertEqual(snapshot.parent, snapshot_dir)
            self.assertNotEqual(snapshot, db_path)
            with sqlite3.connect(str(snapshot)) as restored:
                self.assertEqual(restored.execute("PRAGMA quick_check").fetchone()[0], "ok")
                row = restored.execute("SELECT value FROM manager_metadata WHERE key = 'sample'").fetchone()
            self.assertEqual(row[0], "value")

    def test_create_backup_rejects_missing_sqlite_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_dir = root / "etc" / "xray"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text("{}\n")
            (config_dir / "server.env").write_text("SERVER_NAME=Virei\n")

            stderr = StringIO()
            with mock.patch.object(backup, "MANAGER_DB_PATH", config_dir / "manager.db"), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    backup.create_backup(quiet=True, sync=False)

            self.assertEqual(caught.exception.code, 1)
            self.assertIn("manager.db", stderr.getvalue())

    def test_apply_restore_preserves_current_server_addr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            temp_dir = root / "restore"
            (temp_dir / "usr/local/etc/xray").mkdir(parents=True)
            (temp_dir / "usr/local/etc/xray/config.json").write_text("{}\n")
            (temp_dir / "usr/local/etc/xray/server.env").write_text(
                "SERVER_ADDR=old.example.com\n"
                "SERVER_NAME=Virei\n"
                "MANAGER_TIMEZONE=Europe/Moscow\n"
                "SECURITY_AUDIT_LAST_RUN=2026-06-01T00:00:00Z\n"
            )

            target_config_dir = root / "target" / "xray"
            target_config_dir.mkdir(parents=True)
            (target_config_dir / "server.env").write_text(
                "SERVER_ADDR=new.example.com\n"
                "SECURITY_AUDIT_LAST_RUN=2026-06-12T00:00:00Z\n"
            )
            config_target = target_config_dir / "config.json"
            server_env_target = target_config_dir / "server.env"
            backup_files = [
                ("usr/local/etc/xray/config.json", config_target, True),
                ("usr/local/etc/xray/server.env", server_env_target, True),
            ]

            with mock.patch.object(backup, "CONFIG_DIR", target_config_dir), mock.patch.object(
                backup, "BACKUP_FILES", backup_files
            ), mock.patch.object(backup, "BACKUP_DIRS", []), mock.patch.object(
                backup, "chown_xray"
            ), mock.patch.object(
                backup.shutil, "chown"
            ):
                restored = backup.apply_restore(temp_dir)

            values = dict(
                line.split("=", 1)
                for line in server_env_target.read_text().splitlines()
                if "=" in line
            )
            self.assertEqual(values["SERVER_ADDR"], "new.example.com")
            self.assertEqual(values["SECURITY_AUDIT_LAST_RUN"], "2026-06-12T00:00:00Z")
            self.assertEqual(values["SERVER_NAME"], "Virei")
            self.assertEqual(values["MANAGER_TIMEZONE"], "Europe/Moscow")
            self.assertIn(str(server_env_target), restored)

    def test_apply_restore_drops_host_specific_values_when_current_server_has_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            temp_dir = root / "restore"
            (temp_dir / "usr/local/etc/xray").mkdir(parents=True)
            (temp_dir / "usr/local/etc/xray/config.json").write_text("{}\n")
            (temp_dir / "usr/local/etc/xray/server.env").write_text(
                "SERVER_ADDR=old.example.com\n"
                "SERVER_NAME=Virei\n"
                "SECURITY_AUDIT_LAST_RUN=2026-06-01T00:00:00Z\n"
            )

            target_config_dir = root / "target" / "xray"
            config_target = target_config_dir / "config.json"
            server_env_target = target_config_dir / "server.env"
            backup_files = [
                ("usr/local/etc/xray/config.json", config_target, True),
                ("usr/local/etc/xray/server.env", server_env_target, True),
            ]

            with mock.patch.object(backup, "CONFIG_DIR", target_config_dir), mock.patch.object(
                backup, "BACKUP_FILES", backup_files
            ), mock.patch.object(backup, "BACKUP_DIRS", []), mock.patch.object(
                backup, "chown_xray"
            ), mock.patch.object(
                backup.shutil, "chown"
            ), mock.patch.dict(
                backup.os.environ, {}, clear=True
            ):
                backup.apply_restore(temp_dir)

            server_env = server_env_target.read_text()
            self.assertNotIn("SERVER_ADDR", server_env)
            self.assertNotIn("SECURITY_AUDIT_LAST_RUN", server_env)
            self.assertIn("SERVER_NAME=Virei", server_env)

    def test_apply_restore_restores_manager_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            temp_dir = root / "restore"
            (temp_dir / "usr/local/etc/xray").mkdir(parents=True)
            (temp_dir / "usr/local/etc/xray/config.json").write_text("{}\n")
            (temp_dir / "usr/local/etc/xray/manager.db").write_text("restored")

            target_config_dir = root / "target" / "xray"
            config_target = target_config_dir / "config.json"
            manager_db_target = target_config_dir / "manager.db"
            backup_files = [
                ("usr/local/etc/xray/config.json", config_target, True),
                ("usr/local/etc/xray/manager.db", manager_db_target, False),
            ]

            with mock.patch.object(backup, "CONFIG_DIR", target_config_dir), mock.patch.object(
                backup, "BACKUP_FILES", backup_files
            ), mock.patch.object(backup, "BACKUP_DIRS", []), mock.patch.object(
                backup, "chown_xray"
            ), mock.patch.object(
                backup.shutil, "chown"
            ):
                restored = backup.apply_restore(temp_dir)

            self.assertEqual(manager_db_target.read_text(), "restored")
            self.assertIn(str(manager_db_target), restored)

    def test_restore_caddy_config_if_present_restores_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            temp_dir = root / "restore"
            restored_caddyfile = temp_dir / backup.CADDYFILE_ARCNAME
            restored_conf_dir = temp_dir / backup.CADDY_CONF_DIR_ARCNAME
            restored_conf_dir.mkdir(parents=True)
            restored_caddyfile.write_text("import /etc/caddy/conf.d/*.caddy\n")
            (restored_conf_dir / "vpn.example.com.caddy").write_text(
                "vpn.example.com {\n    reverse_proxy 127.0.0.1:10100\n}\n"
            )

            target_caddyfile = root / "target" / "etc" / "caddy" / "Caddyfile"
            target_conf_dir = root / "target" / "etc" / "caddy" / "conf.d"
            target_conf_dir.mkdir(parents=True)
            target_caddyfile.write_text(":80 {\n    file_server\n}\n")
            (target_conf_dir / "old.example.com.caddy").write_text("old.example.com {\n    file_server\n}\n")
            calls = []

            with mock.patch.object(backup, "CADDYFILE_PATH", target_caddyfile), mock.patch.object(
                backup, "CADDY_CONF_DIR", target_conf_dir
            ):
                restored = backup.restore_caddy_config_if_present(temp_dir, validator=lambda: calls.append("validate"))

            self.assertEqual(calls, ["validate"])
            self.assertEqual(target_caddyfile.read_text(), "import /etc/caddy/conf.d/*.caddy\n")
            self.assertFalse((target_conf_dir / "old.example.com.caddy").exists())
            self.assertTrue((target_conf_dir / "vpn.example.com.caddy").exists())
            self.assertEqual(restored, [str(target_caddyfile), str(target_conf_dir)])

    def test_restore_caddy_config_if_present_ignores_old_archive_without_caddy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            validator = mock.Mock()

            restored = backup.restore_caddy_config_if_present(Path(tmp_dir), validator=validator)

        self.assertEqual(restored, [])
        validator.assert_not_called()

    def test_restore_caddy_random_tls_state_enables_site_timer_when_archive_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            temp_dir = root / "restore"
            restored_config_dir = temp_dir / backup.CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME
            restored_config_dir.mkdir(parents=True)
            (temp_dir / "usr/local/etc/xray/config.json").write_text("{}\n")
            (restored_config_dir / "api.example.com.env").write_text(
                "TLS_RANDOM_DOMAIN=api.example.com\nTLS_RANDOM_LOCAL_PORT=10300\n"
            )
            (temp_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "caddyRandomTls": {
                            "configured": True,
                            "enabled": True,
                            "configsArchive": backup.CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME,
                            "service": "xray-caddy-random-tls@.service",
                            "timer": "xray-caddy-random-tls@.timer",
                            "sites": [
                                {
                                    "domain": "api.example.com",
                                    "localPort": 10300,
                                    "enabled": True,
                                    "envArchive": f"{backup.CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME}/api.example.com.env",
                                    "service": "xray-caddy-random-tls@api.example.com.service",
                                    "timer": "xray-caddy-random-tls@api.example.com.timer",
                                }
                            ],
                        }
                    }
                )
            )

            target_config_dir = root / "target" / "xray" / "caddy-random-tls.d"
            backup_files = [
                ("usr/local/etc/xray/config.json", root / "target" / "xray" / "config.json", True),
            ]
            backup_dirs = [
                (backup.CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME, target_config_dir, False),
            ]
            calls = []

            def fake_systemctl(args, timeout=20):
                calls.append(args)
                return backup.subprocess.CompletedProcess(["systemctl", *args], 0, "", "")

            with mock.patch.object(backup, "BACKUP_FILES", backup_files), mock.patch.object(
                backup, "BACKUP_DIRS", backup_dirs
            ), mock.patch.object(backup, "CADDY_RANDOM_TLS_CONFIG_DIR", target_config_dir), mock.patch.object(
                backup, "chown_xray"
            ), mock.patch.object(
                backup.shutil, "chown"
            ), mock.patch.object(
                backup.xray_caddy, "write_random_tls_systemd_units"
            ), mock.patch.object(
                backup, "run_systemctl", side_effect=fake_systemctl
            ):
                restored = backup.apply_restore(temp_dir)
                messages = backup.restore_caddy_random_tls_state(temp_dir)

            target_env = target_config_dir / "api.example.com.env"
            self.assertEqual(target_env.read_text(), "TLS_RANDOM_DOMAIN=api.example.com\nTLS_RANDOM_LOCAL_PORT=10300\n")
            self.assertIn(str(target_config_dir), restored)
            self.assertIn(["daemon-reload"], calls)
            self.assertIn(["enable", "--now", "xray-caddy-random-tls@api.example.com.timer"], calls)
            self.assertEqual(messages[-1], "Caddy TLS randomizer timer restored for api.example.com: enabled")

    def test_restore_caddy_random_tls_state_converts_legacy_env_to_site_timer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            temp_dir = root / "restore"
            (temp_dir / "usr/local/etc/xray").mkdir(parents=True)
            (temp_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "caddyRandomTls": {
                            "configured": True,
                            "enabled": True,
                            "envArchive": backup.CADDY_RANDOM_TLS_ENV_ARCNAME,
                            "service": "xray-caddy-random-tls.service",
                            "timer": "xray-caddy-random-tls.timer",
                        }
                    }
                )
            )

            target_env = root / "target" / "xray" / "caddy-random-tls.env"
            target_config_dir = root / "target" / "xray" / "caddy-random-tls.d"
            target_env.parent.mkdir(parents=True)
            target_env.write_text("TLS_RANDOM_DOMAIN=api.example.com\nTLS_RANDOM_LOCAL_PORT=10300\n")
            calls = []

            def fake_systemctl(args, timeout=20):
                calls.append(args)
                return backup.subprocess.CompletedProcess(["systemctl", *args], 0, "", "")

            with mock.patch.object(backup, "CADDY_RANDOM_TLS_ENV_PATH", target_env), mock.patch.object(
                backup, "CADDY_RANDOM_TLS_CONFIG_DIR", target_config_dir
            ), mock.patch.object(
                backup.xray_caddy, "write_random_tls_systemd_units"
            ), mock.patch.object(
                backup, "run_systemctl", side_effect=fake_systemctl
            ):
                messages = backup.restore_caddy_random_tls_state(temp_dir)

            converted_env = target_config_dir / "api.example.com.env"
            self.assertEqual(converted_env.read_text(), "TLS_RANDOM_DOMAIN=api.example.com\nTLS_RANDOM_LOCAL_PORT=10300\n")
            self.assertIn(["disable", "--now", "xray-caddy-random-tls.timer"], calls)
            self.assertIn(["enable", "--now", "xray-caddy-random-tls@api.example.com.timer"], calls)
            self.assertEqual(messages[-1], "Caddy TLS randomizer timer restored for api.example.com: enabled")

    def test_restore_caddy_random_tls_state_ignores_old_archives_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_dir = Path(tmp_dir)
            calls = []

            with mock.patch.object(backup, "run_systemctl", side_effect=lambda args, timeout=20: calls.append(args)):
                messages = backup.restore_caddy_random_tls_state(temp_dir)

            self.assertEqual(messages, [])
            self.assertEqual(calls, [])

    def test_backup_manager_database_before_restore_creates_sqlite_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            db_path = root / "manager.db"
            connection = database.open_database(db_path)
            try:
                connection.execute(
                    "INSERT INTO manager_metadata(key, value) VALUES ('sample', 'value')"
                )
                connection.commit()
            finally:
                connection.close()

            with mock.patch.object(backup, "MANAGER_DB_PATH", db_path), mock.patch.object(
                backup, "BACKUP_DIR", root / "backups"
            ):
                backup_path = backup.backup_manager_db_before_restore()

            self.assertIsNotNone(backup_path)
            self.assertTrue(backup_path.exists())
            with sqlite3.connect(str(backup_path)) as restored:
                row = restored.execute("SELECT value FROM manager_metadata WHERE key = 'sample'").fetchone()
            self.assertEqual(row[0], "value")

    def test_backup_manager_database_before_restore_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with mock.patch.object(backup, "MANAGER_DB_PATH", root / "missing.db"):
                self.assertIsNone(backup.backup_manager_db_before_restore())


if __name__ == "__main__":
    unittest.main()
