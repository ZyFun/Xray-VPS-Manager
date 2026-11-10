"""T3. VIREI-127: одна серия проб WARP и переход к итоговому конфигу за два рестарта."""

import base64
import json
import socket
from subprocess import TimeoutExpired
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_default_outbound import legacy_config, VLESS_LINK
from xray_vps_manager.commands import warp
from xray_vps_manager.xray.warp import endpoint_probe_config

PROFILE_ENDPOINT = "profile.example:2408"
TRACE = "https://www.cloudflare.com/cdn-cgi/trace"


def curl(port, url, *, bypass=False):
    options = ("--noproxy", "") if bypass else ()
    tls = ("-k",) if bypass else ()
    return ("curl", "-4", "--proxy", f"socks5h://127.0.0.1:{port}", *options,
            "--connect-timeout", "8", "--max-time", "20", *tls, "-sS", url)


class RoutingApplyTests(unittest.TestCase):
    def setUp(self):
        self.box = self.enterContext(Sandbox())
        self.path = self.box.paths.config
        self.restarts = []
        processes = self.box.processes
        ok = processes.ok()

        def record_restart(*args, **kwargs):
            self.restarts.append(self.path.read_bytes())
            return ok

        test_args = ("run", "-test", "-config", str(self.path))
        processes.script({
            ("/usr/local/bin/xray", *test_args): ok,
            (str(self.box.paths.xray_bin), *test_args): ok,
            (str(self.box.paths.xray_bin), "run", "-test", "-format", "json", "-config", str(self.path.with_suffix(".json.tmp"))): ok,
            "systemctl stop xray": ok,
            "systemctl reset-failed xray": ok,
            "systemctl restart xray": record_restart,
            curl(10809, TRACE): processes.ok("warp=on"),
            curl(10819, TRACE): processes.ok("warp=off"),
        })
        for url in ("https://ifconfig.me/ip", "https://icanhazip.com", "https://checkip.amazonaws.com"):
            processes.script({curl(10808, url): processes.ok("192.0.2.1"),
                              curl(10809, url): processes.ok("192.0.2.1"),
                              curl(10809, url, bypass=True): processes.ok("192.0.2.1")})
        self.enterContext(running_xray(self.box))
        self.enterContext(mock.patch("socket.getaddrinfo", return_value=[]))
        self.port_in_use = self.enterContext(mock.patch("xray_vps_manager.core.process.tcp_port_in_use", return_value=False))
        profile = self.path.parent / "warp" / "wgcf-profile.conf"
        profile.parent.mkdir()
        private = base64.b64encode(bytes(range(32))).decode("ascii")
        public = base64.b64encode(bytes(range(32, 64))).decode("ascii")
        profile.write_text(f"[Interface]\nPrivateKey={private}\nAddress=192.0.2.1/32\n"
                           f"[Peer]\nPublicKey={public}\nEndpoint={PROFILE_ENDPOINT}\n")

    def cli(self, *argv, stdin=""):
        result = self.box.run_cli(*argv, stdin=stdin)
        self.assertEqual(result.exit_code, 0, result.stderr)
        return result.stdout

    def assert_probe(self, snapshot, tag, port, target=None):
        config = json.loads(snapshot)
        inbounds = [(item["protocol"], item["listen"], item["port"])
                    for item in config["inbounds"] if item.get("tag") == tag]
        self.assertEqual(inbounds, [("socks", "127.0.0.1", port)])
        targets = [rule.get("outboundTag") for rule in config["routing"]["rules"]
                   if tag in rule.get("inboundTag", [])]
        self.assertEqual(targets, [target] if target else [])

    def backups(self):
        return {path.name: path.read_bytes() for path in self.path.parent.glob("config.json.bak.*")}

    def assert_clean_config(self, data):
        self.assertNotIn(b"warp-test-", data)
        self.assertNotIn(b"warp-verify-disabled-socks", data)

    def test_warp_enable_and_disable(self):
        """XR-REV-019 d исправлен: проба и чистый итог, без отдельного рестарта для возврата."""
        original = self.path.read_bytes().rstrip() + b"\n\n"
        self.path.write_bytes(original)
        self.cli("warp", "enable")
        enabled = self.path.read_bytes()
        temporary, applied = self.restarts
        self.assert_probe(temporary, "warp-test-1-socks", 10809, "warp-test-1")
        self.assertEqual(warp.current_catchall_tag(json.loads(temporary)), "")
        self.assertEqual(applied, enabled)
        self.assert_clean_config(enabled)
        self.assertEqual(list(self.backups().values()), [original])
        self.assertEqual(warp.current_catchall_tag(json.loads(enabled)), "warp-out")

        self.restarts.clear()
        start = len(self.box.processes.calls)
        output = self.cli("warp", "disable")
        disabled = self.path.read_bytes()
        temporary, applied = self.restarts
        self.assertEqual(applied, disabled)
        self.assert_probe(temporary, "warp-verify-disabled-socks", 10819)
        restart = ("systemctl", "restart", "xray")
        trace = curl(10819, TRACE)
        probe_calls = [cmd for cmd in self.box.processes.calls[start:] if cmd in (restart, trace)]
        self.assertEqual(probe_calls, [restart, trace, restart])
        self.assertIn("warp=off", output)
        self.assert_clean_config(disabled)
        self.assertCountEqual(self.backups().values(), [original, enabled])
        for snapshot in (temporary, disabled):
            config = json.loads(snapshot)
            self.assertEqual(warp.current_catchall_tag(config), "")
            self.assertEqual(config["outbounds"][0]["tag"], "direct")

    def test_warp_enable_four_ipv4_candidates(self):
        """XR-REV-019 d исправлен: пять кандидатов в одном запуске, затем один итоговый рестарт."""
        candidates = [f"192.0.2.{n}:2408" for n in range(1, 5)]
        processes = self.box.processes
        for port in range(10809, 10813):
            processes.script({curl(port, TRACE): processes.result(1)})
        processes.script({curl(10813, TRACE): processes.ok("warp=on")})
        addresses = [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", (endpoint.split(":")[0], 2408))
                     for endpoint in candidates]
        original = self.path.read_bytes()
        with mock.patch("socket.getaddrinfo", return_value=addresses):
            self.cli("warp", "enable")

        temporary, applied = self.restarts
        outbounds = {item["tag"]: item for item in json.loads(temporary)["outbounds"]}
        for index, endpoint in enumerate([PROFILE_ENDPOINT, *candidates], 1):
            tag = f"warp-test-{index}"
            self.assertEqual(outbounds[tag]["settings"]["peers"][0]["endpoint"], endpoint)
            self.assert_probe(temporary, f"{tag}-socks", 10808 + index, tag)
        calls = [cmd for cmd in processes.calls if cmd[0] == "curl"]
        self.assertEqual(calls, [curl(port, TRACE) for port in range(10809, 10813) for _ in range(2)]
                         + [curl(10813, TRACE)])
        self.assertEqual(applied, self.path.read_bytes())
        self.assert_clean_config(applied)
        config = self.box.config()
        outbound = next(item for item in config["outbounds"] if item["tag"] == "warp-out")
        self.assertEqual(outbound["settings"]["peers"][0]["endpoint"], candidates[-1])
        self.assertEqual(warp.current_catchall_tag(config), "warp-out")
        self.assertEqual(list(self.backups().values()), [original])

    def test_create_keeps_profile_when_no_probe_works_and_does_not_enable_warp(self):
        binary = self.box.paths.xray_bin.with_name("wgcf")
        binary.parent.mkdir(exist_ok=True)
        binary.touch()
        binary.chmod(0o755)
        self.path.parent.joinpath("warp/wgcf-account.toml").touch()
        self.box.processes.script({
            (str(binary), "generate"): self.box.processes.ok(),
            curl(10809, TRACE): self.box.processes.ok("warp=off"),
        })
        original = self.path.read_bytes()
        with mock.patch("ssl.create_default_context"):
            output = self.cli("warp", "create", "--no-pin-hosts")
        temporary, applied = self.restarts
        self.assert_probe(temporary, "warp-test-1-socks", 10809, "warp-test-1")
        self.assert_clean_config(applied)
        self.assertIn("No working WARP endpoint found", output)
        config = self.box.config()
        self.assertEqual(warp.current_catchall_tag(config), "")
        outbound = next(item for item in config["outbounds"] if item["tag"] == "warp-out")
        self.assertEqual(outbound["settings"]["peers"][0]["endpoint"], PROFILE_ENDPOINT)
        self.assertEqual(list(self.backups().values()), [original])

    def test_batch_skips_occupied_port_and_probes_profile_first(self):
        self.port_in_use.side_effect = lambda port, _host: port == 10810
        fallback = [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("192.0.2.1", 2408))]
        with mock.patch("socket.getaddrinfo", return_value=fallback):
            self.cli("warp", "enable")
        temporary, applied = self.restarts
        self.assert_probe(temporary, "warp-test-1-socks", 10809, "warp-test-1")
        self.assert_probe(temporary, "warp-test-2-socks", 10811, "warp-test-2")
        self.assertEqual([cmd for cmd in self.box.processes.calls if cmd[0] == "curl"], [curl(10809, TRACE)])
        self.assertIn(PROFILE_ENDPOINT.encode(), applied)
        self.assert_clean_config(applied)

    def test_enable_cleans_interrupted_batch_without_removing_foreign_tag(self):
        original = self.box.config()
        original["outbounds"].append({"tag": "warp-test-7", "protocol": "freedom"})
        outbound = {"tag": "warp-out", "protocol": "wireguard", "settings": {"peers": [{}]}}
        stale = endpoint_probe_config(original, outbound, [PROFILE_ENDPOINT], [10812])
        self.path.write_text(json.dumps(stale))
        stale_bytes = self.path.read_bytes()
        self.cli("warp", "enable")
        temporary, applied = self.restarts
        self.assert_probe(temporary, "warp-test-1-socks", 10809, "warp-test-1")
        self.assertNotIn(b"warp-test-1", applied)
        self.assertIn(b"warp-test-7", applied)
        self.assertEqual(list(self.backups().values()), [stale_bytes])

    def test_batch_preserves_old_tag_used_by_custom_balancer_and_avoids_its_selector(self):
        original = self.box.config()
        outbound = {"tag": "warp-out", "protocol": "wireguard", "settings": {"peers": [{}]}}
        referenced = endpoint_probe_config(original, outbound, [PROFILE_ENDPOINT], [10812])
        referenced["routing"]["balancers"] = [{"tag": "custom-path", "selector": ["warp-test-1"]}]
        referenced["routing"]["rules"].append({
            "type": "field", "domain": ["example.org"], "balancerTag": "custom-path",
        })
        self.path.write_text(json.dumps(referenced))
        self.cli("warp", "enable")
        temporary, applied = self.restarts
        self.assert_probe(temporary, "warp-test-2-socks", 10809, "warp-test-2")
        self.assertIn(b"warp-test-1", applied)
        self.assertNotIn(b"warp-test-2", applied)
        self.assertEqual(self.box.config()["routing"]["balancers"], referenced["routing"]["balancers"])

    def test_enable_with_same_config_does_not_create_second_backup(self):
        self.cli("warp", "enable")
        enabled = self.path.read_bytes()
        backups = self.backups()
        self.restarts.clear()
        self.cli("warp", "enable")
        temporary, restored = self.restarts
        self.assert_probe(temporary, "warp-test-1-socks", 10809, "warp-test-1")
        self.assertEqual(warp.current_catchall_tag(json.loads(temporary)), "warp-out")
        self.assertEqual(restored, enabled)
        self.assertEqual(self.path.read_bytes(), enabled)
        self.assertEqual(self.backups(), backups)

    def test_enable_failure_and_cancel_restore_original_without_backup(self):
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt()

        original = self.path.read_bytes()
        for response, code in ((self.box.processes.ok("warp=off"), 1), (interrupt, 130)):
            with self.subTest(code=code):
                self.restarts.clear()
                self.box.processes.script({curl(10809, TRACE): response})
                result = self.box.run_cli("warp", "enable")
                self.assertEqual(result.exit_code, code, result.stderr)
                temporary, restored = self.restarts
                self.assert_probe(temporary, "warp-test-1-socks", 10809, "warp-test-1")
                self.assertEqual(restored, original)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(self.backups(), {})

    def test_disable_failed_trace_keeps_disabled_config_even_when_repeated(self):
        self.cli("warp", "enable")
        enabled = self.path.read_bytes()
        self.box.processes.script({curl(10819, TRACE): self.box.processes.ok("warp=unknown")})
        self.restarts.clear()
        result = self.box.run_cli("warp", "disable")
        self.assertEqual(result.exit_code, 1)
        temporary, disabled = self.restarts
        self.assert_probe(temporary, "warp-verify-disabled-socks", 10819)
        self.assert_clean_config(disabled)
        self.assertEqual(warp.current_catchall_tag(json.loads(disabled)), "")
        backups = self.backups()
        self.assertEqual(len(backups), 2)
        self.assertIn(enabled, backups.values())

        self.restarts.clear()
        result = self.box.run_cli("warp", "disable")
        self.assertEqual(result.exit_code, 1)
        temporary, restored = self.restarts
        self.assert_probe(temporary, "warp-verify-disabled-socks", 10819)
        self.assertEqual(restored, disabled)
        self.assertEqual(self.path.read_bytes(), disabled)
        self.assertEqual(self.backups(), backups)

    def test_disable_normalizes_legacy_default_outbound_without_catchall(self):
        for old_default in ("bypass-ru", "warp-out"):
            with self.subTest(old_default=old_default):
                self.restarts.clear()
                legacy = legacy_config(cascade=False)
                if old_default == "warp-out":
                    legacy["outbounds"].insert(0, warp.warp_outbound_from_profile())
                self.assertEqual(legacy["outbounds"][0]["tag"], old_default)
                self.assertEqual(warp.current_catchall_tag(legacy), "")
                self.path.write_text(json.dumps(legacy))
                original = self.path.read_bytes()
                backups = self.backups()

                self.cli("warp", "disable")

                temporary, applied = self.restarts
                self.assert_probe(temporary, "warp-verify-disabled-socks", 10819)
                for snapshot in self.restarts:
                    self.assertEqual(json.loads(snapshot)["outbounds"][0]["tag"], "direct")
                self.assert_clean_config(applied)
                self.assertEqual(self.path.read_bytes(), applied)
                self.assertEqual([data for name, data in self.backups().items() if name not in backups], [original])

    def test_final_start_failure_restores_original_once(self):
        processes = self.box.processes

        def status(*args, **kwargs):
            state = "ActiveState=failed\nSubState=failed" if len(self.restarts) == 2 else "ActiveState=active\nSubState=running"
            return processes.ok(f"{state}\nNRestarts=0\n")

        processes.script({"systemctl show --property=ActiveState,SubState,NRestarts xray": status})
        original = self.path.read_bytes()
        result = self.box.run_cli("warp", "enable")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("did not start", result.stderr)
        temporary, failed_final, restored = self.restarts
        self.assert_probe(temporary, "warp-test-1-socks", 10809, "warp-test-1")
        self.assert_clean_config(failed_final)
        self.assertEqual(restored, original)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.backups().values()), [original])

    def test_disable_applies_clean_config_if_diagnostic_restart_fails(self):
        self.cli("warp", "enable")
        enabled = self.path.read_bytes()
        self.restarts.clear()
        processes = self.box.processes
        show = "systemctl show --property=ActiveState,SubState,NRestarts xray"
        failed = processes.ok("ActiveState=failed\nSubState=failed\nNRestarts=0\n")
        ready = processes.ok("ActiveState=active\nSubState=running\nNRestarts=0\n")
        processes.script({show: lambda *_a, **_kw: failed if len(self.restarts) == 1 else ready})
        result = self.box.run_cli("warp", "disable")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Temporary Xray did not start", result.stderr)
        temporary, disabled = self.restarts
        self.assert_probe(temporary, "warp-verify-disabled-socks", 10819)
        self.assert_clean_config(disabled)
        self.assertEqual(self.path.read_bytes(), disabled)
        self.assertEqual(warp.current_catchall_tag(json.loads(disabled)), "")
        self.assertIn(enabled, self.backups().values())
        self.assertNotIn(curl(10819, TRACE), processes.calls)

    def test_disable_applies_clean_config_if_diagnostic_validation_fails(self):
        processes = self.box.processes
        candidate = self.path.with_suffix(".json.tmp")
        check = (str(self.box.paths.xray_bin), "run", "-test", "-format", "json", "-config", str(candidate))
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                self.cli("warp", "enable")
                self.restarts.clear()
                marker = "diagnostic fixture: temporary candidate rejected"

                def validate(command, **_kwargs):
                    if b"warp-verify-disabled-socks" not in candidate.read_bytes():
                        return processes.ok()
                    if timeout:
                        raise TimeoutExpired(command, 30, stderr=marker)
                    return processes.result(1, stderr=marker)

                processes.script({check: validate})
                result = self.box.run_cli("warp", "disable")
                self.assertEqual(result.exit_code, 1)
                self.assertIn(marker, result.stderr)
                self.assertEqual(self.restarts, [self.path.read_bytes()])
                self.assert_clean_config(self.path.read_bytes())
                self.assertEqual(warp.current_catchall_tag(self.box.config()), "")

    def test_route_probes_restore_original_after_two_restarts(self):
        """Текущее поведение, баг XR-REV-028: проба и возврат рабочего конфига рвут сессии."""
        self.path.write_text(json.dumps(legacy_config()))
        for argv, tag, port, target in (
            (("warp", "test"), "warp-test-socks", 10809, "warp-out"),
            (("cascade", "test", "de"), "cascade-test-socks", 10808, "cascade-de"),
            (("bypass", "test", "ru"), "bypass-test-socks", 10809, "bypass-ru"),
        ):
            with self.subTest(argv=argv):
                self.restarts.clear()
                original = self.path.read_bytes()
                self.cli(*argv)
                self.assertEqual(len(self.restarts), 2)
                self.assert_probe(self.restarts[0], tag, port, target)
                if argv[0] == "bypass":
                    self.assert_probe(self.restarts[0], "bypass-route-test-socks", 18109)
                self.assertEqual(self.restarts[1], original)
                self.assertEqual(self.path.read_bytes(), original)

    def test_routing_commands_persist_direct_first(self):
        for argv in (
            ("bypass", "add", "eu", "--region", "DE"),
            ("cascade", "--disable"),
            ("cascade", "remove", "de"),
        ):
            with self.subTest(argv=argv):
                has_cascade = argv[0] == "cascade"
                config = legacy_config(cascade=has_cascade)
                if has_cascade:
                    config["routing"]["rules"].append({"type": "field", "network": "tcp,udp", "outboundTag": "cascade-de"})
                self.path.write_text(json.dumps(config))
                self.cli(*argv, stdin=VLESS_LINK + "\n")
                stored = self.box.config()
                tags = [outbound["tag"] for outbound in stored["outbounds"]]
                self.assertEqual(tags[0], "direct")
                self.assertEqual(warp.current_catchall_tag(stored), "")
                if has_cascade:
                    self.assertEqual("cascade-de" in tags, argv[1] == "--disable")
                else:
                    self.assertIn("bypass-eu", tags)
