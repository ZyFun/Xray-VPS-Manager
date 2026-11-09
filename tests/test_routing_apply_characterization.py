"""T3. Перезапуски Xray и сохранённый порядок outbound в командах маршрутизации."""

import base64
import json
import socket
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_default_outbound import legacy_config, VLESS_LINK
from xray_vps_manager.commands import warp

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

    def test_warp_enable_and_disable(self):
        """Текущее поведение, баг XR-REV-019 d: проба, возврат и применение конфига."""
        original = self.path.read_bytes()
        self.cli("warp", "enable")
        enabled = self.path.read_bytes()
        self.assertEqual(len(self.restarts), 3)
        temporary, restored, applied = self.restarts
        self.assert_probe(temporary, "warp-test-socks", 10809, "warp-out")
        self.assertEqual(restored, original)
        self.assertEqual(applied, enabled)
        self.assertEqual(warp.current_catchall_tag(json.loads(enabled)), "warp-out")

        self.restarts.clear()
        start = len(self.box.processes.calls)
        output = self.cli("warp", "disable")
        disabled = self.path.read_bytes()
        self.assertEqual(len(self.restarts), 3)
        applied, temporary, restored = self.restarts
        self.assertEqual(applied, disabled)
        self.assert_probe(temporary, "warp-verify-disabled-socks", 10819)
        restart = ("systemctl", "restart", "xray")
        trace = curl(10819, TRACE)
        probe_calls = [cmd for cmd in self.box.processes.calls[start:] if cmd in (restart, trace)]
        self.assertEqual(probe_calls, [restart, restart, trace, restart])
        self.assertIn("warp=off", output)
        self.assertEqual(restored, disabled)
        for snapshot in (temporary, disabled):
            config = json.loads(snapshot)
            self.assertEqual(warp.current_catchall_tag(config), "")
            self.assertEqual(config["outbounds"][0]["tag"], "direct")

    def test_warp_enable_four_ipv4_candidates(self):
        """Текущее поведение, баг XR-REV-019 d: пять проб и применение дают 11 рестартов."""
        candidates = [f"192.0.2.{n}:2408" for n in range(1, 5)]
        processes = self.box.processes
        processes.script({curl(10809, TRACE): [processes.result(1)] * 8 + [processes.ok("warp=on")]})
        addresses = [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", (endpoint.split(":")[0], 2408))
                     for endpoint in candidates]
        original = self.path.read_bytes()
        with mock.patch("socket.getaddrinfo", return_value=addresses):
            self.cli("warp", "enable")

        self.assertEqual(len(self.restarts), 11)
        self.assertEqual(self.restarts[1:10:2], [original] * 5)
        endpoints = []
        for snapshot in self.restarts[::2]:
            outbound = next(item for item in json.loads(snapshot)["outbounds"] if item["tag"] == "warp-out")
            endpoints.append(outbound["settings"]["peers"][0]["endpoint"])
        self.assertEqual(endpoints, [PROFILE_ENDPOINT, *candidates, candidates[-1]])
        for snapshot in self.restarts[:10:2]:
            self.assert_probe(snapshot, "warp-test-socks", 10809, "warp-out")
        self.assertEqual(self.restarts[-1], self.path.read_bytes())
        self.assertEqual(warp.current_catchall_tag(self.box.config()), "warp-out")

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
