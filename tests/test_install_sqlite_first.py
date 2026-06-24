from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"


class InstallSQLiteFirstTests(unittest.TestCase):
    def test_install_script_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(INSTALL_SH)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_script_initializes_sqlite_as_primary_state(self) -> None:
        content = INSTALL_SH.read_text()

        self.assertNotIn("MANAGER_SQLITE_READS_ENABLED=true", content)
        self.assertNotIn("MANAGER_SQLITE_WRITES_ENABLED=true", content)
        self.assertIn("settings.set_metadata(connection, \"jsonImport.completed\", \"true\")", content)
        self.assertIn("connections.upsert_connection(", content)
        self.assertIn("clients.upsert_client(", content)
        self.assertIn("client_crud.add_client(", content)
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
        self.assertIn("install_packages=(ca-certificates curl unzip openssl python3 tzdata)", content)
        self.assertIn("install_packages+=(caddy)", content)
        self.assertIn('apt_get_with_lock_retry install -y "${install_packages[@]}"', content)

    def test_install_script_supports_initial_protocol_choice(self) -> None:
        content = INSTALL_SH.read_text()

        self.assertIn('INITIAL_PROTOCOL="${INITIAL_PROTOCOL:-vless}"', content)
        self.assertIn("prompt_initial_protocol()", content)
        self.assertIn('echo "  1) vless', content)
        self.assertIn('echo "  2) trojan', content)
        self.assertIn('echo "  3) both', content)
        self.assertIn('INITIAL_PROTOCOL="both"', content)
        self.assertIn('PORT must not be 443 when INITIAL_PROTOCOL=both', content)
        self.assertIn("TROJAN_DOMAIN", content)
        self.assertIn("TROJAN_WS_PATH", content)

    def test_install_script_initial_trojan_uses_caddy_and_multi_credential_db(self) -> None:
        content = INSTALL_SH.read_text()

        self.assertIn('"tag": "trojan-tls"', content)
        self.assertIn('"protocol": "trojan"', content)
        self.assertIn('"listen": "127.0.0.1"', content)
        self.assertIn('"security": "none"', content)
        self.assertIn('caddy.setup_caddy_for_trojan_ws(', content)
        self.assertIn('"protocol": "trojan"', content)
        self.assertIn('"caddy": True', content)
        self.assertIn("TROJAN_CLIENT_URI=", content)
        self.assertIn("VLESS_CLIENT_URI=", content)
        self.assertIn('client_uri_security="TLS"', content)
        self.assertIn('client_uri_transport="ws"', content)

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

    def test_install_script_installs_disabled_caddy_random_tls_units(self) -> None:
        content = INSTALL_SH.read_text()

        self.assertIn("cat >/etc/systemd/system/xray-caddy-random-tls@.service", content)
        self.assertIn("ExecStart=/usr/local/sbin/xray-vps-manager caddy random-tls-run --domain %i --quiet", content)
        self.assertIn("cat >/etc/systemd/system/xray-caddy-random-tls@.timer", content)
        self.assertIn("OnUnitActiveSec=15min", content)
        self.assertIn("RandomizedDelaySec=45min", content)
        self.assertIn("Unit=xray-caddy-random-tls@%i.service", content)
        self.assertNotIn("systemctl enable --now xray-caddy-random-tls@api.example.com.timer", content)

    def test_install_script_prompts_xhttp_mode_from_numbered_list(self) -> None:
        content = INSTALL_SH.read_text()

        self.assertIn("prompt_xhttp_mode()", content)
        self.assertIn('echo "  1) auto"', content)
        self.assertIn('echo "  2) packet-up"', content)
        self.assertIn('echo "  3) stream-up"', content)
        self.assertIn('echo "  4) stream-one"', content)
        self.assertIn('read -r -p "XHTTP_MODE [${default_mode}] (номер из списка): " input', content)
        self.assertIn('prompt_xhttp_mode "$XHTTP_MODE"', content)
        self.assertNotIn('read -r -p "XHTTP_MODE [${XHTTP_MODE}]: " xhttp_mode_input', content)


if __name__ == "__main__":
    unittest.main()
