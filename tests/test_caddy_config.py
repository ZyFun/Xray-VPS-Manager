import subprocess
import unittest
import warnings
from pathlib import Path
import tempfile

from tests.support.sandbox import Sandbox
from tests.test_client_command_read import script_caddy
from xray_vps_manager.system import units as system_units
from xray_vps_manager.caddy import model, random_tls, service, sites


class CaddyConfigTests(unittest.TestCase):
    def test_public_listener_checks_bound_ip_against_connection_address(self) -> None:
        for bind, public_host, expected in (
            (":8443", "old.example", model.PublicListener.CONFIRMED),
            ("tcp4/0.0.0.0:8443", "192.0.2.10", model.PublicListener.CONFIRMED),
            ("tcp6/[::]:8443", "2001:db8::10", model.PublicListener.CONFIRMED),
            ("tcp6/[::]:8443", "192.0.2.10", model.PublicListener.ABSENT),
            ("tcp6/:8443", "192.0.2.10", model.PublicListener.ABSENT),
            ("tcp4/0.0.0.0:8443", "old.example", model.PublicListener.UNVERIFIED),
            ("tcp6/[::]:8443", "old.example", model.PublicListener.UNVERIFIED),
            ("tcp4/0.0.0.0:8443", "2001:db8::10", model.PublicListener.ABSENT),
            ("192.0.2.10:8443", "old.example", model.PublicListener.UNVERIFIED),
            ("192.0.2.10:8443", "192.0.2.11", model.PublicListener.UNVERIFIED),
            ("192.0.2.10:8443", "192.0.2.10", model.PublicListener.CONFIRMED),
            ("[::ffff:192.0.2.10]:8443", "192.0.2.10", model.PublicListener.CONFIRMED),
            ("127.0.0.1:8443", "127.0.0.1", model.PublicListener.ABSENT),
        ):
            with self.subTest(bind=bind, public_host=public_host):
                route = model.SiteProxyRoute((bind,), frozenset({18100}), False)
                self.assertEqual(route.public_listener(8443, public_host), expected)

    def test_site_rename_routes_selects_host_and_listener(self) -> None:
        def route(host, *handlers):
            return {
                "match": [{"host": [host]}],
                "handle": [{"handler": "subroute", "routes": [{"handle": handlers}]}],
            }

        def proxy(*addresses):
            return {"handler": "reverse_proxy", "upstreams": [{"dial": address} for address in addresses]}

        local_proxy = proxy("127.0.0.1:18100", "tcp/127.0.0.1:18101")
        routes = [
            route("selected.example", local_proxy),
            route("other.example", proxy("127.0.0.1:18102", "{vars.backend}")),
        ]
        adapted = {"apps": {"http": {"servers": {
            "server": {"listen": [":8443"], "routes": routes},
            "other_port": {
                "listen": ["127.0.0.1:9443"], "routes": [route("selected.example", proxy("127.0.0.1:18103"))],
            },
        }}}}

        with self.assertRaisesRegex(ValueError, "используется в нескольких блоках Caddy"):
            model.site_rename_routes(adapted, "selected.example", "other.example")
        selected, remaining = model.site_rename_routes(adapted, "selected.example", "other.example", public_port=8443)
        self.assertEqual(selected, model.SiteProxyRoute((":8443",), frozenset({18100, 18101}), False))
        self.assertEqual(remaining, (model.SiteProxyRoute((":8443",), frozenset({18102}), True),))
        local_proxy["dynamic_upstreams"] = {"source": "a"}
        selected, _ = model.site_rename_routes(adapted, "selected.example", "other.example", public_port=8443)
        self.assertTrue(selected.incomplete)
        selected, _ = model.site_rename_routes(adapted, "missing.example", "other.example")
        self.assertEqual(selected, model.SiteProxyRoute((), frozenset(), True))

    def test_site_block_terminates_tls_and_proxies_h2c_to_local_xhttp(self) -> None:
        block = model.caddy_site_block(
            "api.example.com",
            10000,
            tls_min_version="tls1.2",
            tls_max_version="tls1.2",
        )

        self.assertIn("api.example.com {", block)
        self.assertIn("protocols tls1.2 tls1.2", block)
        self.assertIn("reverse_proxy h2c://127.0.0.1:10000", block)

    def test_default_tls_versions_omit_protocol_override(self) -> None:
        block = model.caddy_site_block(
            "api.example.com",
            10000,
            tls_min_version="default",
            tls_max_version="default",
        )

        self.assertNotIn("protocols", block)
        self.assertIn("reverse_proxy h2c://127.0.0.1:10000", block)

    def test_site_block_can_proxy_trojan_websocket_path(self) -> None:
        block = model.caddy_site_block(
            "vpn.example.com",
            10100,
            tls_min_version="tls1.2",
            tls_max_version="tls1.3",
            upstream_transport="http",
            route_path="/trojan-private",
        )

        self.assertIn("vpn.example.com {", block)
        self.assertIn("protocols tls1.2 tls1.3", block)
        self.assertIn("@xray_path {", block)
        self.assertIn("path /trojan-private", block)
        self.assertIn("header Connection *Upgrade*", block)
        self.assertIn("header Upgrade websocket", block)
        self.assertIn("handle @xray_path {", block)
        self.assertIn("reverse_proxy 127.0.0.1:10100", block)
        self.assertIn("try_files {path} /index.html", block)
        self.assertIn("file_server", block)

    def test_site_block_can_proxy_xhttp_path_with_static_fallback(self) -> None:
        block = model.caddy_site_block(
            "files.example.com",
            10300,
            upstream_transport="xhttp",
            route_path="/api/v1/sync",
        )

        self.assertIn("@xray_path path /api/v1/sync*", block)
        self.assertIn("reverse_proxy h2c://127.0.0.1:10300", block)
        self.assertIn("try_files {path} /index.html", block)
        self.assertIn("file_server", block)

    def test_site_block_does_not_duplicate_xhttp_path_wildcard(self) -> None:
        block = model.caddy_site_block(
            "files.example.com",
            10300,
            upstream_transport="xhttp",
            route_path="/api/v1/sync*",
        )

        self.assertIn("@xray_path path /api/v1/sync*", block)
        self.assertNotIn("/api/v1/sync**", block)

    def test_site_block_uses_json_fallback_for_api_subdomain(self) -> None:
        block = model.caddy_site_block(
            "api.example.com",
            10300,
            upstream_transport="xhttp",
            route_path="/api/v1/sync",
        )

        self.assertIn("header Content-Type application/json", block)
        self.assertIn('respond "{\\"error\\":\\"not_found\\"}" 404', block)
        self.assertNotIn("try_files {path} /index.html", block)

    def test_update_site_tls_config_preserves_static_site_from_default(self) -> None:
        with Sandbox() as box:
            script_caddy(box)
            root = box.root
            static_root = root / "site"
            static_root.mkdir()
            path = root / "site.example.com.caddy"
            path.write_text(
                "site.example.com {\n"
                f"    root * {static_root}\n"
                "    file_server\n"
                "}\n"
            )

            result = service.update_site_tls_config(
                path,
                tls_min_version="tls1.3",
                tls_max_version="tls1.3",
            )

            content = path.read_text()
            backup_exists = result.backup.exists()

        self.assertEqual(result.path, path)
        self.assertIn("protocols tls1.3 tls1.3", content)
        self.assertIn(f"root * {static_root}", content)
        self.assertIn("file_server", content)
        self.assertTrue(backup_exists)

    def test_update_site_tls_config_removes_protocol_override_for_default(self) -> None:
        with Sandbox() as box:
            script_caddy(box)
            path = box.root / "site.example.com.caddy"
            path.write_text(
                "site.example.com {\n"
                "    tls {\n"
                "        protocols tls1.2 tls1.2\n"
                "    }\n"
                "\n"
                "    root * /var/www/site\n"
                "    file_server\n"
                "}\n"
            )

            service.update_site_tls_config(
                path,
                tls_min_version="default",
                tls_max_version="default",
            )

            content = path.read_text()

        self.assertNotIn("protocols", content)
        self.assertNotIn("tls {", content)
        self.assertIn("root * /var/www/site", content)
        self.assertIn("file_server", content)

    def test_tls_version_choices_map_to_protocol_pairs(self) -> None:
        self.assertEqual(model.tls_version_label("tls1.2", "tls1.3"), "TLS 1.2 + TLS 1.3")
        self.assertEqual(model.tls_version_choice("tls13").tls_min_version, "tls1.3")
        self.assertEqual(model.tls_version_choice_key("default", "default"), "default")

    def test_tls_version_pair_rejects_reversed_range(self) -> None:
        with self.assertRaises(ValueError):
            model.caddy_site_block("api.example.com", 10000, tls_min_version="tls1.3", tls_max_version="tls1.2")

    def test_random_tls_pair_moves_away_from_current_strict_version(self) -> None:
        self.assertEqual(
            random_tls.choose_random_tls_pair("tls1.2", "tls1.2", chooser=lambda options: options[0]),
            ("tls1.3", "tls1.3"),
        )
        self.assertEqual(
            random_tls.choose_random_tls_pair("tls1.3", "tls1.3", chooser=lambda options: options[0]),
            ("tls1.2", "tls1.2"),
        )

    def test_random_tls_pair_randomizes_non_strict_current_profile(self) -> None:
        self.assertEqual(
            random_tls.choose_random_tls_pair("default", "default", chooser=lambda options: options[-1]),
            ("tls1.3", "tls1.3"),
        )

    def test_next_random_tls_label_describes_next_strict_switch(self) -> None:
        self.assertEqual(random_tls.next_random_tls_label("tls1.2", "tls1.2"), "TLS 1.3")
        self.assertEqual(random_tls.next_random_tls_label("tls1.3", "tls1.3"), "TLS 1.2")
        self.assertEqual(random_tls.next_random_tls_label("tls1.2", "tls1.3"), "Случайно: TLS 1.2 или TLS 1.3")

    def test_random_tls_env_and_systemd_units_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "caddy-random-tls.d"
            env_path = config_dir / "api.example.com.env"
            systemd_dir = root / "systemd"

            config = random_tls.write_random_tls_config("api.example.com", 10300, env_path)
            units = system_units.write_random_tls_systemd_units(systemd_dir)

            self.assertEqual(config.domain, "api.example.com")
            self.assertEqual(config.local_port, 10300)
            self.assertEqual(random_tls.read_random_tls_config(env_path), config)
            self.assertIn("TLS_RANDOM_DOMAIN=api.example.com", env_path.read_text())
            service = units["service"].read_text()
            timer = units["timer"].read_text()
            self.assertEqual(env_path, random_tls.random_tls_env_path("api.example.com", config_dir))
            self.assertEqual(random_tls.random_tls_timer_instance("api.example.com"), "xray-caddy-random-tls@api.example.com.timer")
            self.assertIn("EnvironmentFile=/usr/local/etc/xray/caddy-random-tls.d/%i.env", service)
            self.assertIn("ExecStart=/usr/local/sbin/xray-vps-manager caddy random-tls-run --domain %i --quiet", service)
            self.assertIn("OnUnitActiveSec=15min", timer)
            self.assertIn("RandomizedDelaySec=45min", timer)
            self.assertIn("Unit=xray-caddy-random-tls@%i.service", timer)

    def test_apply_random_tls_switch_updates_selected_site(self) -> None:
        with Sandbox() as box:
            script_caddy(box)
            sites.write_site_config("api.example.com", 10300)

            result = random_tls.apply_random_tls_switch(
                random_tls.RandomTlsConfig("api.example.com", 10300),
                chooser=lambda options: options[0],
            )

            site = sites.site_config_for_domain("api.example.com")
            self.assertEqual(result.previous_tls_min_version, "tls1.2")
            self.assertEqual(result.tls_min_version, "tls1.3")
            self.assertEqual((site.local_port, site.upstream_transport, site.match_path), (10300, "xhttp", ""))
            self.assertEqual((site.tls_min_version, site.tls_max_version), ("tls1.3", "tls1.3"))
            self.assertEqual(result.path, site.path)
            self.assertEqual(box.processes.calls, [
                ("caddy", "validate", "--config", str(box.paths.caddyfile)),
                ("systemctl", "enable", "--now", "caddy"),
                ("systemctl", "reload", "caddy"),
            ])

    def test_parse_site_config_reads_domain_tls_and_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api.example.com.caddy"
            path.write_text(model.caddy_site_block("api.example.com", 10300))

            item = sites.parse_site_config(path)

        self.assertEqual(item.domain, "api.example.com")
        self.assertEqual(item.local_port, 10300)
        self.assertEqual(item.tls_min_version, "tls1.2")
        self.assertEqual(item.tls_max_version, "tls1.2")
        self.assertEqual(item.upstream_transport, "xhttp")
        self.assertEqual(item.match_path, "")
        self.assertRegex(item.modified_at, r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2} UTC$")

    def test_parse_site_config_reads_trojan_websocket_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vpn.example.com.caddy"
            path.write_text(
                model.caddy_site_block(
                    "vpn.example.com",
                    10100,
                    upstream_transport="http",
                    route_path="/trojan-private",
                )
            )

            item = sites.parse_site_config(path)

        self.assertEqual(item.domain, "vpn.example.com")
        self.assertEqual(item.local_port, 10100)
        self.assertEqual(item.upstream_transport, "http")
        self.assertEqual(item.match_path, "/trojan-private")

    def test_parse_site_config_normalizes_xhttp_wildcard_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "files.example.com.caddy"
            path.write_text(
                "files.example.com {\n"
                "    @xray_path path /api/v1/sync*\n"
                "    handle @xray_path {\n"
                "        reverse_proxy h2c://127.0.0.1:10300\n"
                "    }\n"
                "}\n"
            )

            item = sites.parse_site_config(path)

        self.assertEqual(item.local_port, 10300)
        self.assertEqual(item.upstream_transport, "xhttp")
        self.assertEqual(item.match_path, "/api/v1/sync")

    def test_parse_site_config_reads_block_path_matcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vpn.example.com.caddy"
            path.write_text(
                "vpn.example.com {\n"
                "    @xray_path {\n"
                "        path /private-ws\n"
                "        header Connection *Upgrade*\n"
                "        header Upgrade websocket\n"
                "    }\n"
                "    handle @xray_path {\n"
                "        reverse_proxy 127.0.0.1:10100\n"
                "    }\n"
                "}\n"
            )

            item = sites.parse_site_config(path)

        self.assertEqual(item.local_port, 10100)
        self.assertEqual(item.upstream_transport, "http")
        self.assertEqual(item.match_path, "/private-ws")

    def test_require_site_config_absent_refuses_existing_domain_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf_dir = Path(tmp)
            path = conf_dir / "vpn.example.com.caddy"
            path.write_text(
                model.caddy_site_block(
                    "vpn.example.com",
                    10100,
                    upstream_transport="http",
                    route_path="/private-ws",
                )
            )

            with self.assertRaises(FileExistsError) as raised:
                sites.require_site_config_absent("vpn.example.com", conf_dir)

        message = str(raised.exception)
        self.assertIn("Caddy site config already exists for vpn.example.com", message)
        self.assertIn("127.0.0.1:10100", message)
        self.assertIn("path=/private-ws", message)

    def test_require_site_config_absent_allows_new_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf_dir = Path(tmp)
            (conf_dir / "api.example.com.caddy").write_text(model.caddy_site_block("api.example.com", 10300))

            sites.require_site_config_absent("vpn.example.com", conf_dir)

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

            self.assertTrue(sites.remove_site_block_from_caddyfile(":80", path))

            content = path.read_text()
        self.assertNotIn(":80 {", content)
        self.assertIn("import /etc/caddy/conf.d/*.caddy", content)

    def test_config_backup_and_restore_caddy_settings(self) -> None:
        """XR-REV-047: конфигурация из TAR восстанавливается без предупреждения, с правами Caddy."""
        self.enterContext(warnings.catch_warnings(action="error", category=DeprecationWarning))
        with Sandbox() as box:
            root = box.root
            script_caddy(box)
            box.processes.script({"caddy version": box.processes.ok("v2.10.0")})
            backup_dir = root / "backups"
            caddyfile = root / "etc" / "caddy" / "Caddyfile"
            conf_dir = root / "etc" / "caddy" / "conf.d"
            site = conf_dir / "api.example.com.caddy"
            caddyfile.parent.mkdir(parents=True)
            conf_dir.mkdir()
            caddyfile.write_text("import /etc/caddy/conf.d/*.caddy\n")
            site.write_text(model.caddy_site_block("api.example.com", 10300))

            archive = sites.create_config_backup(
                backup_dir=backup_dir,
                caddyfile_path=caddyfile,
                conf_dir=conf_dir,
            )
            caddyfile.write_text(":80 {\n    file_server\n}\n")
            site.write_text(model.caddy_site_block("api.example.com", 10400))

            restored_from, pre_backup, restored = service.restore_config_backup(
                archive.name,
                backup_dir=backup_dir,
                caddyfile_path=caddyfile,
                conf_dir=conf_dir,
            )

            self.assertEqual(restored_from, archive)
            self.assertTrue(pre_backup.exists())
            self.assertEqual(caddyfile.read_text(), "import /etc/caddy/conf.d/*.caddy\n")
            self.assertIn("reverse_proxy h2c://127.0.0.1:10300", site.read_text())
            self.assertEqual(restored, [caddyfile, conf_dir])
            self.assertEqual(caddyfile.stat().st_mode & 0o777, 0o644)
            self.assertEqual(site.stat().st_mode & 0o777, 0o644)
            self.assertEqual(conf_dir.stat().st_mode & 0o777, 0o755)

    def test_delete_config_backup_refuses_outside_backup_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backups"
            backup_dir.mkdir()
            outside = root / "outside.tar.gz"
            outside.write_text("not really an archive")

            with self.assertRaises(ValueError):
                sites.delete_config_backup(str(outside), backup_dir)

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

            candidates = sites.site_root_candidates(caddyfile_path=caddyfile, conf_dir=conf_dir)

        self.assertEqual(candidates[0], static_root)

    def test_site_backup_and_restore_site_files(self) -> None:
        """XR-REV-047: сайт из TAR восстанавливается без предупреждения, с правами для раздачи файлов."""
        self.enterContext(warnings.catch_warnings(action="error", category=DeprecationWarning))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backups"
            site_root = root / "site"
            nested = site_root / "assets"
            nested.mkdir(parents=True)
            (site_root / "index.html").write_text("before")
            (nested / "app.css").write_text("body{}")

            archive, _ = sites.create_site_backup(site_root, backup_dir=backup_dir)
            (site_root / "index.html").write_text("after")
            (nested / "app.css").unlink()

            restored_from, pre_backup, restored_root = sites.restore_site_backup(
                archive.name,
                backup_dir=backup_dir,
                target_root=site_root,
            )

            self.assertEqual(restored_from, archive)
            self.assertTrue(pre_backup.exists())
            self.assertEqual(restored_root, site_root.resolve())
            self.assertEqual((site_root / "index.html").read_text(), "before")
            self.assertEqual((nested / "app.css").read_text(), "body{}")
            self.assertEqual((nested / "app.css").stat().st_mode & 0o777, 0o644)
            self.assertEqual(nested.stat().st_mode & 0o777, 0o755)

    def test_delete_site_backup_refuses_outside_backup_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backups"
            backup_dir.mkdir()
            outside = root / "outside.tar.gz"
            outside.write_text("not really an archive")

            with self.assertRaises(ValueError):
                sites.delete_site_backup(str(outside), backup_dir)


class SiteBackupRotationTests(unittest.TestCase):
    """После успешной правки сайта Caddy рядом с файлом остаётся не больше N копий (VIREI-241)."""

    def setUp(self) -> None:
        self.box = self.enterContext(Sandbox())
        self.conf_dir = self.box.paths.caddy_conf_dir
        self.conf_dir.mkdir(parents=True)
        script_caddy(self.box)

    def write_site(self, domain: str = "api.example.com") -> Path:
        path = self.conf_dir / model.site_filename(domain)
        path.write_text(model.caddy_site_block(domain, 10300))
        return path

    def make_copies(self, path: Path, count: int) -> list:
        copies = []
        for index in range(count):
            copy = path.with_name(f"{path.name}.bak.2026010100000000{index:04d}")
            copy.write_text(f"old {index}\n")
            copies.append(copy)
        return copies

    def copies_of(self, path: Path) -> list:
        prefix = f"{path.name}.bak."
        return sorted(
            item for item in path.parent.iterdir() if item.name.startswith(prefix) and item.name[len(prefix):].isdigit()
        )

    def make_foreign_files(self) -> list:
        other = self.write_site("other.example.com")
        foreign = [other, *self.make_copies(other, 7)]
        for name in (
            "api.example.com.caddy.bak.telegram.20250101000000",
            "x.api.example.com.caddy.bak.20250101000000000000",
            "api.example.com.caddy.bak.20250101000000000000.tar.gz",
        ):
            path = self.conf_dir / name
            path.write_text("foreign\n")
            foreign.append(path)
        return foreign

    def test_update_site_config_keeps_newest_copies_after_success(self) -> None:
        path = self.write_site()
        old = self.make_copies(path, 7)
        foreign = self.make_foreign_files()

        result = service.update_site_config("api.example.com", 10300, tls_min_version="tls1.3", tls_max_version="tls1.3")

        copies = self.copies_of(path)
        self.assertEqual(len(copies), sites.SITE_BACKUP_KEEP)
        self.assertEqual(copies, sorted([*old[-4:], result.backup]))
        self.assertTrue(result.backup.exists())
        self.assertIn("protocols tls1.3 tls1.3", path.read_text())
        self.assertTrue(all(item.exists() for item in foreign))

    def test_update_site_config_keeps_current_copy_when_clock_went_back(self) -> None:
        path = self.write_site()
        future = [path.with_name(f"{path.name}.bak.2099010100000000{index:04d}") for index in range(7)]
        for copy in future:
            copy.write_text("future\n")

        result = service.update_site_config("api.example.com", 10300, tls_min_version="tls1.3", tls_max_version="tls1.3")

        self.assertTrue(result.backup.exists())
        self.assertEqual(self.copies_of(path), sorted([result.backup, *future[-4:]]))

    def test_update_site_config_keeps_copies_below_limit(self) -> None:
        path = self.write_site()
        old = self.make_copies(path, 3)

        result = service.update_site_config("api.example.com", 10300, tls_min_version="tls1.3", tls_max_version="tls1.3")

        self.assertEqual(self.copies_of(path), sorted([*old, result.backup]))

    def test_update_site_config_failure_restores_file_and_keeps_copies(self) -> None:
        path = self.write_site()
        original = path.read_text()
        self.make_copies(path, 7)

        validate = ("caddy", "validate", "--config", self.box.paths.caddyfile)
        self.box.processes.script({validate: self.box.processes.result(1)})
        with self.assertRaises(subprocess.CalledProcessError):
            service.update_site_config("api.example.com", 10300, tls_min_version="tls1.3", tls_max_version="tls1.3")

        self.assertEqual(path.read_text(), original)
        self.assertEqual(len(self.copies_of(path)), 8)

    def test_update_site_tls_config_keeps_newest_copies_after_success(self) -> None:
        path = self.write_site()
        old = self.make_copies(path, 7)

        result = service.update_site_tls_config(path, tls_min_version="tls1.3", tls_max_version="tls1.3")

        self.assertEqual(self.copies_of(path), sorted([*old[-4:], result.backup]))

    def test_update_site_tls_config_failure_restores_file_and_keeps_copies(self) -> None:
        path = self.write_site()
        original = path.read_text()
        self.make_copies(path, 7)

        validate = ("caddy", "validate", "--config", self.box.paths.caddyfile)
        self.box.processes.script({validate: self.box.processes.result(1)})
        with self.assertRaises(subprocess.CalledProcessError):
            service.update_site_tls_config(path, tls_min_version="tls1.3", tls_max_version="tls1.3")

        self.assertEqual(path.read_text(), original)
        self.assertEqual(len(self.copies_of(path)), 8)

    def test_delete_site_config_keeps_newest_copies_of_deleted_site(self) -> None:
        path = self.write_site()
        old = self.make_copies(path, 7)
        foreign = self.make_foreign_files()

        backup = sites.delete_site_config("api.example.com", self.conf_dir)

        self.assertFalse(path.exists())
        self.assertEqual(self.copies_of(path), sorted([*old[-4:], backup]))
        self.assertTrue(all(item.exists() for item in foreign))

    def test_random_tls_switch_prunes_accumulated_copies(self) -> None:
        path = self.write_site()
        self.make_copies(path, 60)
        self.write_site("other.example.com")

        result = random_tls.apply_random_tls_switch(
            random_tls.RandomTlsConfig("api.example.com", 10300),
            chooser=lambda options: options[0],
        )

        copies = self.copies_of(path)
        self.assertEqual(len(copies), sites.SITE_BACKUP_KEEP)
        self.assertEqual(copies[-1], result.backup)


if __name__ == "__main__":
    unittest.main()
