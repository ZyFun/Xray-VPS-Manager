from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"


class InstallSQLiteFirstTests(unittest.TestCase):
    def test_install_script_initializes_sqlite_as_primary_state(self) -> None:
        content = INSTALL_SH.read_text()

        self.assertNotIn("MANAGER_SQLITE_READS_ENABLED=true", content)
        self.assertNotIn("MANAGER_SQLITE_WRITES_ENABLED=true", content)
        self.assertIn("settings.set_metadata(connection, \"jsonImport.completed\", \"true\")", content)
        self.assertIn("connections.upsert_connection(", content)
        self.assertIn("clients.upsert_client(", content)
        self.assertIn("chown root:xray /usr/local/etc/xray/manager.db", content)
        self.assertNotIn("cat >/usr/local/etc/xray/clients.json", content)
        self.assertNotIn("activity-exceptions.json", content)
        self.assertNotIn("/usr/local/etc/xray/activity/clients", content)

    def test_install_script_resets_existing_manager_db_before_recreating_it(self) -> None:
        content = INSTALL_SH.read_text()

        self.assertIn("backup_and_remove_manager_db /usr/local/etc/xray/manager.db", content)
        self.assertNotIn("backup_and_remove_state_file", content)

    def test_install_script_retries_apt_when_lock_is_busy(self) -> None:
        content = INSTALL_SH.read_text()

        self.assertIn("apt_get_with_lock_retry()", content)
        self.assertIn("Could not get lock|Unable to lock|Could not open lock", content)
        self.assertIn("apt_get_with_lock_retry update", content)
        self.assertIn("apt_get_with_lock_retry install -y ca-certificates curl unzip openssl python3 tzdata", content)

    def test_install_script_supports_alternative_xray_sources(self) -> None:
        content = INSTALL_SH.read_text()

        self.assertIn('XRAY_SOURCE="${XRAY_SOURCE:-github}"', content)
        self.assertIn("validate_xray_source()", content)
        self.assertIn("prompt_xray_source()", content)
        self.assertIn("XRAY_ZIP_URL is required when XRAY_SOURCE=custom", content)
        self.assertIn("XRAY_LOCAL_ZIP is required when XRAY_SOURCE=local", content)
        self.assertIn("prepare_xray_archive()", content)
        self.assertIn("Downloading Xray archive from ${XRAY_SOURCE}: attempt ${attempt}/${max_attempts}, retries left: ${retries_left}", content)
        self.assertIn("Xray archive download failed with exit code ${status}. Retries left: 0.", content)
        self.assertIn('curl -fL --connect-timeout 20 --max-time 240 -o "$target_dir/Xray-linux-64.zip" "$XRAY_ZIP_URL"', content)
        self.assertIn('cp -f "$XRAY_LOCAL_ZIP" "$target_dir/Xray-linux-64.zip"', content)


if __name__ == "__main__":
    unittest.main()
