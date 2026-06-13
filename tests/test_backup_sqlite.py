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
            ("usr/local/etc/xray/manager.db", backup.MANAGER_DB_PATH, False),
            backup.BACKUP_FILES,
        )
        self.assertIn(
            ("usr/local/etc/xray/clients.json", backup.CLIENT_DB_PATH, False),
            backup.BACKUP_FILES,
        )

    def test_create_backup_includes_manager_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_dir = root / "etc" / "xray"
            config_dir.mkdir(parents=True)
            files = {
                "config.json": "{}\n",
                "clients.json": '{"clients": {}}\n',
                "server.env": "SERVER_ADDR=old.example.com\nSERVER_NAME=Virei\nSECURITY_AUDIT_LAST_RUN=2026-06-01T00:00:00Z\n",
                "manager.db": "sqlite bytes",
            }
            for name, content in files.items():
                (config_dir / name).write_text(content)

            backup_files = [
                ("usr/local/etc/xray/config.json", config_dir / "config.json", True),
                ("usr/local/etc/xray/clients.json", config_dir / "clients.json", False),
                ("usr/local/etc/xray/server.env", config_dir / "server.env", True),
                ("usr/local/etc/xray/manager.db", config_dir / "manager.db", False),
            ]

            with mock.patch.object(backup, "BACKUP_DIR", root / "backups"), mock.patch.object(
                backup, "BACKUP_FILES", backup_files
            ), mock.patch.object(backup, "BACKUP_DIRS", []), mock.patch.object(
                backup, "CLIENT_DB_PATH", config_dir / "clients.json"
            ), mock.patch.object(
                backup, "MANAGER_DB_PATH", config_dir / "manager.db"
            ):
                archive = backup.create_backup(quiet=True, sync=False)

            with tarfile.open(archive, "r:gz") as tar:
                names = set(tar.getnames())
                manager_db = tar.extractfile("usr/local/etc/xray/manager.db").read().decode()
                server_env = tar.extractfile("usr/local/etc/xray/server.env").read().decode()
                manifest = json.loads(tar.extractfile("manifest.json").read())

            self.assertIn("usr/local/etc/xray/manager.db", names)
            self.assertEqual(manager_db, "sqlite bytes")
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

    def test_create_backup_allows_sqlite_only_state_without_legacy_clients_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_dir = root / "etc" / "xray"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text("{}\n")
            (config_dir / "server.env").write_text("SERVER_NAME=Virei\n")
            (config_dir / "manager.db").write_text("sqlite bytes")

            backup_files = [
                ("usr/local/etc/xray/config.json", config_dir / "config.json", True),
                ("usr/local/etc/xray/clients.json", config_dir / "clients.json", False),
                ("usr/local/etc/xray/server.env", config_dir / "server.env", True),
                ("usr/local/etc/xray/manager.db", config_dir / "manager.db", False),
            ]

            with mock.patch.object(backup, "BACKUP_DIR", root / "backups"), mock.patch.object(
                backup, "BACKUP_FILES", backup_files
            ), mock.patch.object(backup, "BACKUP_DIRS", []), mock.patch.object(
                backup, "CLIENT_DB_PATH", config_dir / "clients.json"
            ), mock.patch.object(
                backup, "MANAGER_DB_PATH", config_dir / "manager.db"
            ):
                archive = backup.create_backup(quiet=True, sync=False)

            with tarfile.open(archive, "r:gz") as tar:
                names = set(tar.getnames())

            self.assertIn("usr/local/etc/xray/manager.db", names)
            self.assertNotIn("usr/local/etc/xray/clients.json", names)

    def test_create_backup_rejects_missing_sqlite_and_legacy_client_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_dir = root / "etc" / "xray"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text("{}\n")
            (config_dir / "server.env").write_text("SERVER_NAME=Virei\n")

            stderr = StringIO()
            with mock.patch.object(backup, "CLIENT_DB_PATH", config_dir / "clients.json"), mock.patch.object(
                backup, "MANAGER_DB_PATH", config_dir / "manager.db"
            ), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    backup.create_backup(quiet=True, sync=False)

            self.assertEqual(caught.exception.code, 1)
            self.assertIn("Neither", stderr.getvalue())

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
