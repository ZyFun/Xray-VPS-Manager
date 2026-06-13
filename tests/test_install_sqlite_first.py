from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"


class InstallSQLiteFirstTests(unittest.TestCase):
    def test_install_script_initializes_sqlite_as_primary_state(self) -> None:
        content = INSTALL_SH.read_text()

        self.assertIn("MANAGER_SQLITE_READS_ENABLED=true", content)
        self.assertIn("MANAGER_SQLITE_WRITES_ENABLED=true", content)
        self.assertIn("settings.set_metadata(connection, \"jsonImport.completed\", \"true\")", content)
        self.assertIn("connections.upsert_connection(", content)
        self.assertIn("clients.upsert_client(", content)
        self.assertIn("chown root:xray /usr/local/etc/xray/manager.db", content)
        self.assertNotIn("cat >/usr/local/etc/xray/clients.json", content)

    def test_install_script_moves_old_legacy_state_out_of_runtime_paths(self) -> None:
        content = INSTALL_SH.read_text()

        for path in (
            "/usr/local/etc/xray/clients.json",
            "/usr/local/etc/xray/traffic.json",
            "/usr/local/etc/xray/activity.json",
            "/usr/local/etc/xray/activity-exceptions.json",
            "/usr/local/etc/xray/telegram-bot.json",
            "/usr/local/etc/xray/manager.db",
        ):
            self.assertIn(f"backup_and_remove_state_file {path}", content)


if __name__ == "__main__":
    unittest.main()
