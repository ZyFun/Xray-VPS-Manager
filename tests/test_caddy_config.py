import unittest
from pathlib import Path
import tempfile
from unittest import mock

from xray_vps_manager.xray import caddy


class CaddyConfigTests(unittest.TestCase):
    def test_site_block_terminates_tls_and_proxies_h2c_to_local_xhttp(self) -> None:
        block = caddy.caddy_site_block(
            "api.example.com",
            10000,
            tls_min_version="tls1.2",
            tls_max_version="tls1.2",
        )

        self.assertIn("api.example.com {", block)
        self.assertIn("protocols tls1.2 tls1.2", block)
        self.assertIn("reverse_proxy h2c://127.0.0.1:10000", block)

    def test_default_tls_versions_omit_protocol_override(self) -> None:
        block = caddy.caddy_site_block(
            "api.example.com",
            10000,
            tls_min_version="default",
            tls_max_version="default",
        )

        self.assertNotIn("protocols", block)
        self.assertIn("reverse_proxy h2c://127.0.0.1:10000", block)

    def test_tls_version_choices_map_to_protocol_pairs(self) -> None:
        self.assertEqual(caddy.tls_version_label("tls1.2", "tls1.3"), "TLS 1.2 + TLS 1.3")
        self.assertEqual(caddy.tls_version_choice("tls13").tls_min_version, "tls1.3")
        self.assertEqual(caddy.tls_version_choice_key("default", "default"), "default")

    def test_tls_version_pair_rejects_reversed_range(self) -> None:
        with self.assertRaises(ValueError):
            caddy.caddy_site_block("api.example.com", 10000, tls_min_version="tls1.3", tls_max_version="tls1.2")

    def test_random_tls_pair_moves_away_from_current_strict_version(self) -> None:
        self.assertEqual(
            caddy.choose_random_tls_pair("tls1.2", "tls1.2", chooser=lambda options: options[0]),
            ("tls1.3", "tls1.3"),
        )
        self.assertEqual(
            caddy.choose_random_tls_pair("tls1.3", "tls1.3", chooser=lambda options: options[0]),
            ("tls1.2", "tls1.2"),
        )

    def test_random_tls_pair_randomizes_non_strict_current_profile(self) -> None:
        self.assertEqual(
            caddy.choose_random_tls_pair("default", "default", chooser=lambda options: options[-1]),
            ("tls1.3", "tls1.3"),
        )

    def test_next_random_tls_label_describes_next_strict_switch(self) -> None:
        self.assertEqual(caddy.next_random_tls_label("tls1.2", "tls1.2"), "TLS 1.3")
        self.assertEqual(caddy.next_random_tls_label("tls1.3", "tls1.3"), "TLS 1.2")
        self.assertEqual(caddy.next_random_tls_label("tls1.2", "tls1.3"), "Случайно: TLS 1.2 или TLS 1.3")

    def test_random_tls_env_and_systemd_units_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / "caddy-random-tls.env"
            systemd_dir = root / "systemd"

            config = caddy.write_random_tls_config("api.example.com", 10300, env_path)
            units = caddy.write_random_tls_systemd_units(systemd_dir)

            self.assertEqual(config.domain, "api.example.com")
            self.assertEqual(config.local_port, 10300)
            self.assertEqual(caddy.read_random_tls_config(env_path), config)
            self.assertIn("TLS_RANDOM_DOMAIN=api.example.com", env_path.read_text())
            service = units["service"].read_text()
            timer = units["timer"].read_text()
            self.assertIn("ExecStart=/usr/local/sbin/xray-vps-manager caddy random-tls-run --quiet", service)
            self.assertIn("OnUnitActiveSec=15min", timer)
            self.assertIn("RandomizedDelaySec=45min", timer)

    def test_apply_random_tls_switch_updates_selected_site(self) -> None:
        site = caddy.SiteConfig(
            path=Path("/etc/caddy/conf.d/api.example.com.caddy"),
            domain="api.example.com",
            local_port=10300,
            tls_min_version="tls1.2",
            tls_max_version="tls1.2",
        )
        write_result = caddy.SiteWriteResult(site.path, Path("/tmp/site.bak"))

        with mock.patch.object(caddy, "site_config_for_domain", return_value=site):
            with mock.patch.object(caddy, "update_site_config", return_value=write_result) as update_site:
                result = caddy.apply_random_tls_switch(
                    caddy.RandomTlsConfig("api.example.com", 10300),
                    chooser=lambda options: options[0],
                )

        update_site.assert_called_once_with(
            "api.example.com",
            10300,
            tls_min_version="tls1.3",
            tls_max_version="tls1.3",
            runner=caddy.subprocess.run,
        )
        self.assertEqual(result.previous_tls_min_version, "tls1.2")
        self.assertEqual(result.tls_min_version, "tls1.3")

    def test_parse_site_config_reads_domain_tls_and_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api.example.com.caddy"
            path.write_text(caddy.caddy_site_block("api.example.com", 10300))

            item = caddy.parse_site_config(path)

        self.assertEqual(item.domain, "api.example.com")
        self.assertEqual(item.local_port, 10300)
        self.assertEqual(item.tls_min_version, "tls1.2")
        self.assertEqual(item.tls_max_version, "tls1.2")
        self.assertRegex(item.modified_at, r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2} UTC$")

    def test_remove_default_http_site_block_preserves_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Caddyfile"
            path.write_text(
                ":80 {\n"
                "    root * /usr/share/caddy\n"
                "    file_server\n"
                "}\n\n"
                "# Managed by Xray VPS Manager\n"
                "import /etc/caddy/conf.d/*.caddy\n"
            )

            self.assertTrue(caddy.remove_site_block_from_caddyfile(":80", path))

            content = path.read_text()
        self.assertNotIn(":80 {", content)
        self.assertIn("import /etc/caddy/conf.d/*.caddy", content)

    def test_config_backup_and_restore_caddy_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backups"
            caddyfile = root / "etc" / "caddy" / "Caddyfile"
            conf_dir = root / "etc" / "caddy" / "conf.d"
            site = conf_dir / "api.example.com.caddy"
            caddyfile.parent.mkdir(parents=True)
            conf_dir.mkdir()
            caddyfile.write_text("import /etc/caddy/conf.d/*.caddy\n")
            site.write_text(caddy.caddy_site_block("api.example.com", 10300))

            archive = caddy.create_config_backup(
                backup_dir=backup_dir,
                caddyfile_path=caddyfile,
                conf_dir=conf_dir,
                quiet=True,
            )
            caddyfile.write_text(":80 {\n    file_server\n}\n")
            site.write_text(caddy.caddy_site_block("api.example.com", 10400))

            restored_from, pre_backup, restored = caddy.restore_config_backup(
                archive.name,
                backup_dir=backup_dir,
                caddyfile_path=caddyfile,
                conf_dir=conf_dir,
                validator=lambda: None,
            )

            self.assertEqual(restored_from, archive)
            self.assertTrue(pre_backup.exists())
            self.assertEqual(caddyfile.read_text(), "import /etc/caddy/conf.d/*.caddy\n")
            self.assertIn("reverse_proxy h2c://127.0.0.1:10300", site.read_text())
            self.assertEqual(restored, [caddyfile, conf_dir])

    def test_delete_config_backup_refuses_outside_backup_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backups"
            backup_dir.mkdir()
            outside = root / "outside.tar.gz"
            outside.write_text("not really an archive")

            with self.assertRaises(ValueError):
                caddy.delete_config_backup(str(outside), backup_dir)

    def test_site_root_candidates_include_root_directives_and_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            caddyfile = root / "Caddyfile"
            conf_dir = root / "conf.d"
            static_root = root / "www"
            fallback = root / "usr-share-caddy"
            conf_dir.mkdir()
            static_root.mkdir()
            fallback.mkdir()
            caddyfile.write_text(
                "example.com {\n"
                f"    root * {static_root}\n"
                "    file_server\n"
                "}\n"
            )

            candidates = caddy.site_root_candidates(caddyfile_path=caddyfile, conf_dir=conf_dir)

        self.assertEqual(candidates[0], static_root)

    def test_site_backup_and_restore_site_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backups"
            site_root = root / "site"
            nested = site_root / "assets"
            nested.mkdir(parents=True)
            (site_root / "index.html").write_text("before")
            (nested / "app.css").write_text("body{}")

            archive = caddy.create_site_backup(site_root, backup_dir=backup_dir, quiet=True)
            (site_root / "index.html").write_text("after")
            (nested / "app.css").unlink()

            restored_from, pre_backup, restored_root = caddy.restore_site_backup(
                archive.name,
                backup_dir=backup_dir,
                target_root=site_root,
            )

            self.assertEqual(restored_from, archive)
            self.assertTrue(pre_backup.exists())
            self.assertEqual(restored_root, site_root.resolve())
            self.assertEqual((site_root / "index.html").read_text(), "before")
            self.assertEqual((nested / "app.css").read_text(), "body{}")

    def test_delete_site_backup_refuses_outside_backup_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backups"
            backup_dir.mkdir()
            outside = root / "outside.tar.gz"
            outside.write_text("not really an archive")

            with self.assertRaises(ValueError):
                caddy.delete_site_backup(str(outside), backup_dir)


if __name__ == "__main__":
    unittest.main()
