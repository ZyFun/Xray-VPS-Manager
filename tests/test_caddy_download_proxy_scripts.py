from pathlib import Path
import os
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install-caddy-download-proxy.sh"
CADDY_MENU = ROOT / "caddy-menu"


class CaddyDownloadProxyScriptTests(unittest.TestCase):
    def test_installer_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(INSTALLER)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_menu_wrapper_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(CADDY_MENU)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_installs_caddy_menu_without_xray_runtime(self) -> None:
        content = INSTALLER.read_text(encoding="utf-8")

        self.assertIn('INSTALLED_MENU_PATH="${INSTALLED_MENU_PATH:-/usr/local/sbin/caddy-menu}"', content)
        self.assertIn('install -m 0755 "$SELF_PATH" "$INSTALLED_MENU_PATH"', content)
        self.assertIn("apt_get_with_lock_retry install -y ca-certificates curl caddy", content)
        self.assertNotIn("/usr/local/bin/xray", content)
        self.assertNotIn("manager.db", content)
        self.assertNotIn("xray.service", content)
        self.assertNotIn("xray-telegram", content)

    def test_proxy_config_contains_required_caddy_reverse_proxy_options(self) -> None:
        content = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("reverse_proxy $(upstream_url) {", content)
        self.assertIn("header_up Host ${UPSTREAM_DOMAIN}", content)
        self.assertIn("tls_server_name ${UPSTREAM_DOMAIN}", content)
        self.assertIn("flush_interval -1", content)
        self.assertIn("import ${CADDY_CONF_DIR}/*.caddy", content)
        self.assertNotIn("encode zstd gzip", content)

    def test_configure_flow_is_interactive_and_descriptive(self) -> None:
        content = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("Что сейчас настраивается", content)
        self.assertIn("Изменить DOWNLOAD_DOMAIN", content)
        self.assertIn("Изменить UPSTREAM_DOMAIN", content)
        self.assertIn("Preview Caddyfile", content)
        self.assertIn("Preview JSON downloadSettings", content)
        self.assertIn("Применить настройки: записать Caddy config, validate и reload", content)
        self.assertIn("Публичный домен второго сервера", content)
        self.assertIn("Основной xHTTP/TLS домен первого сервера", content)
        self.assertIn("TLS_FINGERPRINT [${TLS_FINGERPRINT}] (номер или значение)", content)
        self.assertIn('4) TLS_FINGERPRINT="ios"', content)
        self.assertIn("Для маскировки под iOS-приложение выбирай ios.", content)
        self.assertIn('prompt_with_validation UPSTREAM_PORT "UPSTREAM_PORT" "${UPSTREAM_PORT:-443}" port', content)
        self.assertNotIn("Выбери UPSTREAM_PORT:", content)
        self.assertIn('prompt_with_validation XHTTP_PATH "XHTTP_PATH" "$XHTTP_PATH" path', content)
        self.assertNotIn("Выбери XHTTP_PATH:", content)
        self.assertIn("TLS_ALPN [${TLS_ALPN}] (номер или список)", content)
        self.assertIn("1) h2 (рекомендуется для XHTTP через Caddy)", content)
        self.assertIn("TLS_PROFILE [${TLS_PROFILE}] (номер или значение)", content)
        self.assertIn("4) TLS_PROFILE=\"tls13\"", content)
        self.assertIn("TLS / Randomizer", content)
        self.assertIn("random-tls-run", content)

    def test_print_download_settings_uses_download_domain_and_xhttp_path(self) -> None:
        env = {
            **os.environ,
            "DOWNLOAD_DOMAIN": "cdn.example.com",
            "UPSTREAM_DOMAIN": "api.example.com",
            "XHTTP_PATH": "/api/v1/sync",
            "XHTTP_MODE": "auto",
            "TLS_ALPN": "h2,http/1.1",
        }
        result = subprocess.run(
            [str(INSTALLER), "menu", "print-download-settings"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"address": "cdn.example.com"', result.stdout)
        self.assertIn('"serverName": "cdn.example.com"', result.stdout)
        self.assertIn('"path": "/api/v1/sync"', result.stdout)
        self.assertIn('"mode": "auto"', result.stdout)
        self.assertIn('"alpn": ["h2", "http/1.1"]', result.stdout)

    def test_preview_caddyfile_prints_interactive_proxy_config(self) -> None:
        env = {
            **os.environ,
            "DOWNLOAD_DOMAIN": "cdn.example.com",
            "UPSTREAM_DOMAIN": "api.example.com",
            "XHTTP_PATH": "/api/v1/sync",
        }
        result = subprocess.run(
            [str(INSTALLER), "menu", "preview-caddyfile"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Preview Caddy site config", result.stdout)
        self.assertIn("cdn.example.com {", result.stdout)
        self.assertIn("reverse_proxy https://api.example.com", result.stdout)
        self.assertIn("header_up Host api.example.com", result.stdout)
        self.assertIn("tls_server_name api.example.com", result.stdout)

    def test_preview_caddyfile_includes_selected_tls_profile(self) -> None:
        env = {
            **os.environ,
            "DOWNLOAD_DOMAIN": "cdn.example.com",
            "UPSTREAM_DOMAIN": "api.example.com",
            "TLS_PROFILE": "tls13",
        }
        result = subprocess.run(
            [str(INSTALLER), "menu", "preview-caddyfile"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("protocols tls1.3 tls1.3", result.stdout)

    def test_random_tls_units_match_main_timer_cadence(self) -> None:
        content = INSTALLER.read_text(encoding="utf-8")

        self.assertIn('RANDOM_TLS_SERVICE_NAME="${RANDOM_TLS_SERVICE_NAME:-xhttp-download-proxy-random-tls.service}"', content)
        self.assertIn("ExecStart=${INSTALLED_MENU_PATH} random-tls-run --quiet", content)
        self.assertIn("OnUnitActiveSec=15min", content)
        self.assertIn("RandomizedDelaySec=45min", content)
        self.assertIn("AccuracySec=1min", content)
        self.assertIn("TLS_PROFILE=\"$next\"", content)
        self.assertIn("tls12)", content)
        self.assertIn("tls13)", content)

    def test_show_describes_what_each_setting_controls(self) -> None:
        env = {
            **os.environ,
            "DOWNLOAD_DOMAIN": "cdn.example.com",
            "UPSTREAM_DOMAIN": "api.example.com",
            "XHTTP_PATH": "/api/v1/sync",
        }
        result = subprocess.run(
            [str(INSTALLER), "menu", "show"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Что сейчас настраивается", result.stdout)
        self.assertIn("DOWNLOAD_DOMAIN=cdn.example.com", result.stdout)
        self.assertIn("Публичный домен второго сервера", result.stdout)
        self.assertIn("UPSTREAM_DOMAIN=api.example.com", result.stdout)
        self.assertIn("Основной xHTTP/TLS домен первого сервера", result.stdout)


if __name__ == "__main__":
    unittest.main()
