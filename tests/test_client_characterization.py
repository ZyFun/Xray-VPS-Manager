"""VIREI-87 (T4a). Публичный CLI и наблюдаемое состояние перед переносом шага 4a."""

from contextlib import closing
from difflib import unified_diff
from io import StringIO
import json
import os
import sqlite3
import unittest
from unittest import mock

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from xray_vps_manager.commands.arguments import CLIENT
from xray_vps_manager.core.paths import Paths


XRAY = "/usr/local/bin/xray"
PUBLIC_KEY = "B" * 43
PRIVATE_KEY = "C" * 43
ALICE_KEY = "00000000-0000-4000-8000-000000000001"
TABLES = (
    "clients", "client_credentials", "reality_connections", "cascade_routes",
    "client_traffic_limits", "client_traffic_limit_state", "traffic_totals",
    "traffic_history", "credential_traffic_totals", "credential_traffic_history",
)

# Первое имя каждой группы — команда из usage. Остальные имена проходят
# тот же сценарий с теми же ожиданиями stdout, stderr, argv и состояния.
SCENARIOS = {
    "client_lifecycle": (
        ("add",), ("list",), ("link",), ("key",),
        ("disable", "off"), ("enable", "on"),
        ("move-connection", "move-client", "move"), ("remove", "delete", "del", "rm"),
    ),
    "access_and_payment": (
        ("set-days", "access-days"), ("extend-days", "prolong-days"),
        ("set-payment", "payment-type"), ("expire-due",),
    ),
    "connections": (
        ("connection-list",), ("add-connection",), ("add-trojan-connection",),
        ("update-trojan-connection", "trojan-connection-update"),
        ("connection-transport", "set-connection-transport"),
        ("connection-xhttp-extra", "set-connection-xhttp-extra"),
        ("connection-rename", "rename-connection"), ("remove-connection", "delete-connection"),
    ),
    "trojan_credentials": (
        ("trojan-password-check", "validate-trojan-passwords"),
        ("rotate-trojan-password", "trojan-password-rotate"),
    ),
    "limits": (("limit-list",), ("set-limit",), ("clear-limit",), ("enforce-limits",)),
    "routes": (("route",), ("sync-routes",)),
    "timezone": (("timezone",), ("set-timezone",)),
    "traffic": (
        ("traffic-summary",), ("traffic-day",), ("traffic-week",), ("traffic-month",),
        ("traffic-period",),
    ),
}


class TerminalInput(StringIO):
    def isatty(self):
        return True


def snapshot(box):
    """Отображаем только изменения публичных файлов и относящихся к ним строк БД."""
    paths = box.paths
    state = {
        "config.json": json.dumps(box.config(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "server.env": paths.server_env.read_text(),
        "SQLite": box.db_rows(*TABLES),
        "config backups": "".join(f"{path.name}\n" for path in sorted(paths.config_dir.glob("config.json.bak.*"))),
    }
    files = [paths.initial_link, paths.caddyfile, *sorted(paths.caddy_conf_dir.glob("*.caddy"))]
    files += sorted(paths.systemd_dir.glob("xray-raw-log-rotate.*"))
    for path in files:
        if path.is_file():
            state[str(path.relative_to(box.root))] = path.read_text()
    return state


class Transcript:
    def __init__(self, test, box, aliases):
        self.test = test
        self.box = box
        self.aliases = aliases
        self.commands = set()
        self.entries = []

    def run(self, command, *args, stdin="", exit_code=0, raises=None):
        box = self.box
        self.commands.add(command)
        before = snapshot(box)
        offset = len(box.processes.calls)
        argv = (self.aliases.get(command, command), *args)
        if raises is not None:
            error_type, message = raises
            result = box.run_cli("client", *argv, stdin=stdin, capture_exceptions=(error_type,))
            self.test.assertIsInstance(result.exception, error_type, argv)
            self.test.assertRegex(str(result.exception), message)
            status = f"exception: {type(result.exception).__name__}: {result.exception}"
        else:
            result = box.run_cli("client", *argv, stdin=stdin)
            self.test.assertEqual(result.exit_code, exit_code, (argv, result.stdout, result.stderr))
            status = f"exit: {result.exit_code}"
        output = f"{status}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}\n"
        calls = box.processes.calls[offset:]
        # Сопоставить путь бинарника Sandbox с серверным путём в эталоне T4a.
        processes = "".join(
            json.dumps((str(Paths.default().xray_bin), *call[1:])
                       if call[:3] == (str(box.paths.xray_bin), "api", "statsquery") else call) + "\n"
            for call in calls
        )
        after = snapshot(box)
        changes = []
        for name in sorted(before.keys() | after.keys()):
            changes.extend(unified_diff(
                before.get(name, "").splitlines(keepends=True),
                after.get(name, "").splitlines(keepends=True),
                fromfile=f"before/{name}", tofile=f"after/{name}", n=0,
            ))
        label = json.dumps((command, *args), ensure_ascii=False)
        self.entries.append(f"\n{label}\n{output}processes:\n{processes}state:\n{''.join(changes)}")
        return result

    def assert_golden(self, name):
        self.box.assert_golden(f"client/t4a/{name}", {"transcript.txt": "".join(self.entries)})


def script_processes(box):
    processes = box.processes
    path = box.paths.config
    processes.script({
        (str(box.paths.xray_bin), "run", "-test", "-format", "json", "-config", str(path.with_suffix(".json.tmp"))): processes.ok(),
        (str(box.paths.xray_bin), "run", "-test", "-config", str(path)): processes.ok(),
        "systemctl stop xray": processes.ok(),
        "systemctl reset-failed xray": processes.ok(),
        "systemctl restart xray": processes.ok(),
        (XRAY, "uuid"): [
            processes.ok(f"00000000-0000-4000-8000-{index:012d}\n")
            for index in range(2, 12)
        ],
        (XRAY, "x25519"): processes.ok(f"PrivateKey: {PRIVATE_KEY}\nPublicKey: {PUBLIC_KEY}\n"),
        (XRAY, "x25519", "-i", PRIVATE_KEY): processes.ok(f"PublicKey: {PUBLIC_KEY}\n"),
        (XRAY, "x25519", "-i", "A" * 43): processes.ok(f"PublicKey: {'A' * 43}\n"),
        (str(box.paths.xray_bin), "api", "statsquery", "--server=127.0.0.1:10085", "-pattern", "user>>>"): processes.ok(),
        "openssl rand -hex 8": processes.ok("fedcba9876543210\n"),
        "systemctl daemon-reload": processes.ok(),
        "systemctl enable --now xray-raw-log-rotate.timer": processes.ok(),
        "systemctl restart xray-raw-log-rotate.timer": processes.ok(),
    })


def write_config(box, config):
    box.paths.config.write_text(json.dumps(config) + "\n")


def seed_traffic(box):
    with closing(sqlite3.connect(box.paths.manager_db)) as conn, conn:
        conn.executescript("""
            INSERT INTO traffic_totals(client_name,incoming_bytes,outgoing_bytes)
            VALUES ('alice',4294967296,1073741824);
            INSERT INTO traffic_history(client_name,bucket_date,bucket_hour,incoming_bytes,outgoing_bytes)
            VALUES ('alice','2026-09-30',23,536870912,0),
                   ('alice','2026-10-05',10,1048576,2048),
                   ('alice','2026-10-06',14,1073741824,8192),
                   ('alice','2026-10-06',15,2147483648,1024);
        """)


def add_reality(run, name="Second", port="8443"):
    run("add-connection", name, port, "cdn.example", "firefox", "tcp")


def add_trojan(run, *args):
    run("add-trojan-connection", "Trojan", "9443", "trojan.example", *args)


class ClientCharacterizationTests(unittest.TestCase):
    def check(self, scenario):
        name = scenario.__name__
        groups = SCENARIOS.get(name, ())
        for index in range(max(map(len, groups), default=1)):
            aliases = {group[0]: group[index] for group in groups if 0 < index < len(group)}
            with self.subTest(aliases=aliases), Sandbox() as box, running_xray(box), \
                    mock.patch.dict(os.environ, {"XRAY_MANAGER_DEBUG": "0", "UPDATE_GOLDEN": os.getenv("UPDATE_GOLDEN", "0") if not aliases else "0"}):
                script_processes(box)
                transcript = Transcript(self, box, aliases)
                scenario(box, transcript.run)
                self.assertLessEqual({group[0] for group in groups}, transcript.commands)
                transcript.assert_golden(name)

    def test_cli_inventory(self):
        with Sandbox() as box:
            help_result = box.run_cli("client", "--help")
            self.assertEqual((help_result.exit_code, help_result.stderr), (0, ""))
            box.assert_golden("client/t4a/usage", {
                "transcript.txt": f"stdout:\n{help_result.stdout}exit: {help_result.exit_code}\n",
            })
        usage = [line.split()[1] for line in help_result.stdout.splitlines() if line.startswith("  xray-client ")]
        groups = [group for scenario in SCENARIOS.values() for group in scenario]
        aliases = [name for group in groups for name in group]
        self.assertEqual(len(usage), 37)
        self.assertEqual(len(groups), 35)
        self.assertEqual(len(aliases), 52)
        self.assertCountEqual(usage, [group[0] for group in groups] + ["add-connection", "add-trojan-connection"])
        self.assertCountEqual(aliases, CLIENT.keys())

    def test_client_lifecycle(self):
        self.check(self.client_lifecycle)

    def client_lifecycle(self, box, run):
        seed_traffic(box)
        run("add", "bob", "5", "--protocol", "vless", "--payment", "paid")
        run("add", "carol", stdin=TerminalInput("3\n"))
        run("add", "dave", stdin="99\n")
        add_reality(run)
        run("add", "bob", "--connection", "vless-reality-2")
        run("list")
        run("link", "bob")
        run("link", "bob", "--connection", "vless-reality-2")
        run("key", "bob")
        for argv in (("list",), ("link", "bob"), ("key", "bob")):
            run(*argv, "--plain", exit_code=2)
        run("disable", "bob", "--connection", "vless-reality-2")
        run("enable", "bob", "--connection", "vless-reality-2")
        run("disable", "alice")
        run("enable", "alice")
        run("move-connection", "alice", "Second")
        run("disable", "alice")
        run("move-connection", "alice", "Reality")
        run("enable", "alice")
        run("remove", "alice")

    def test_access_and_payment(self):
        self.check(self.access_and_payment)

    def access_and_payment(self, box, run):
        box.paths.sbin_dir.mkdir(parents=True)
        telegram = box.paths.sbin_dir / "xray-telegram"
        telegram.touch()
        box.processes.script({
            (telegram, "notify-access", "alice", "--quiet"): box.processes.ok(),
            (telegram, "payment-amount"): box.processes.ok("Payment amount: 42\n"),
        })
        run("set-days", "alice", "3")
        run("extend-days", "alice", "2")
        run("set-payment", "alice", "paid")
        run("expire-due")
        with closing(sqlite3.connect(box.paths.manager_db)) as conn, conn:
            conn.execute("UPDATE clients SET expires_at = '2026-10-05T21:00:00Z' WHERE name = 'alice'")
        run("expire-due")
        run("add", "bob", "1")
        with closing(sqlite3.connect(box.paths.manager_db)) as conn, conn:
            conn.execute("UPDATE clients SET expires_at = '2026-10-05T21:00:00Z' WHERE name = 'bob'")
        run("expire-due", "--quiet")

    def test_connections(self):
        self.check(self.connections)

    def connections(self, box, run):
        box.processes.executables["caddy"] = "/usr/bin/caddy"
        ok = box.processes.ok()
        box.processes.script({
            ("caddy", "validate", "--config", box.paths.caddyfile): ok,
            "systemctl enable --now caddy": ok,
            "systemctl reload caddy": ok,
        })
        run("connection-list")
        add_reality(run)
        run("connection-transport", "Second", "xhttp", "--xhttp-path", "/alternate", "--xhttp-mode", "packet-up")
        run("connection-xhttp-extra", "Second", "--xhttp-extra-json", '{"xPaddingBytes":"100-200"}')
        run("connection-xhttp-extra", "Second", "--clear-xhttp-extra")
        run("connection-rename", "Second", "Alternate")
        run("add-connection", "TLS", "18080", "tls.example", "--security", "tls", "--transport", "xhttp",
            "--xhttp-path", "/secure", "--public-port", "1443", "--install-caddy")
        add_trojan(run, "--ws-path", "/tunnel", "--public-port", "1443")
        run("update-trojan-connection", "Trojan", "--ws-path", "/new", "--fingerprint", "firefox")
        run("add-trojan-connection", "TCP", "10443", "tcp.example", "/fixture/cert.pem", "/fixture/key.pem",
            "safari")
        run("connection-list")
        run("remove-connection", "vless-reality")

    def test_trojan_credentials(self):
        self.check(self.trojan_credentials)

    def trojan_credentials(self, box, run):
        add_trojan(run, "--no-caddy")
        run("add", "bob", "4", "--protocol", "trojan")
        run("trojan-password-check")
        run("link", "bob", "--connection", "trojan-tls")
        run("key", "bob")
        config = box.config()
        trojan = next(inbound for inbound in config["inbounds"] if inbound["tag"] == "trojan-tls")
        trojan["settings"]["clients"][0]["password"] = "changed-in-config"
        write_config(box, config)
        run("trojan-password-check", exit_code=1)
        run("rotate-trojan-password", "bob", "--connection", "trojan-tls")
        run("trojan-password-check")
        run("key", "bob")
        run("disable", "bob", "--connection", "trojan-tls")
        run("rotate-trojan-password", "bob")
        run("trojan-password-check")

    def test_limits(self):
        self.check(self.limits)

    def limits(self, box, run):
        seed_traffic(box)
        self.install_traffic_sync(box)
        run("set-limit", "alice", "daily", "8")
        run("limit-list")
        with closing(sqlite3.connect(box.paths.manager_db)) as conn, conn:
            conn.execute("UPDATE client_traffic_limits SET limit_bytes = 1073741824 WHERE client_name = 'alice'")
        run("enforce-limits", "--quiet")
        run("limit-list")
        run("clear-limit", "alice")
        run("enforce-limits", "--sync")

    def test_routes(self):
        self.check(self.routes)

    def routes(self, box, run):
        config = box.config()
        for name, address in (("cascade-old", "192.0.2.1"), ("cascade-new", "192.0.2.2")):
            config["outbounds"].append({
                "tag": name, "protocol": "socks", "settings": {"servers": [{"address": address, "port": 1080}]},
            })
        config["routing"]["rules"].append({
            "type": "field", "network": "tcp,udp", "outboundTag": "cascade-old",
        })
        write_config(box, config)
        box.processes.script({
            (str(box.paths.xray_bin), "api", "bi", "--server=127.0.0.1:10085", "-json",
             "client-route-000000000000400080000000"): box.processes.ok('{"balancer":{"override":{}}}'),
            (str(box.paths.xray_bin), "api", "bo", "--server=127.0.0.1:10085", "-b", "client-route-000000000000400080000000", "cascade-new"): box.processes.ok(),
        })
        run("sync-routes")
        run("route", "alice")
        run("route", "alice", "cascade-new")
        run("route", "alice", "cascade-new")
        run("sync-routes")

    def test_timezone(self):
        self.check(self.timezone)

    def timezone(self, box, run):
        run("set-days", "alice", "3")
        run("timezone")
        run("set-timezone", "America/New_York")
        run("timezone")
        run("set-timezone", "server")
        run("timezone")

    def test_traffic(self):
        self.check(self.traffic)

    def traffic(self, box, run):
        seed_traffic(box)
        self.install_traffic_sync(box)
        run("traffic-summary")
        run("traffic-day", "alice")
        run("traffic-week", "alice")
        run("traffic-month", "alice")
        run("traffic-period", "alice", "2026-09-30", "2026-10-06")

    @staticmethod
    def install_traffic_sync(box):
        box.paths.sbin_dir.mkdir(parents=True)
        path = box.paths.sbin_dir / "xray-traffic-sync"
        path.touch()
        box.processes.script({(path, "--quiet"): box.processes.ok()})

    def test_move_changes_client_uuid(self):
        """Текущее поведение, баг XR-REV-062."""
        self.check(self.move_changes_client_uuid)

    def move_changes_client_uuid(self, box, run):
        add_reality(run)
        add_reality(run, "Third", "8444")
        run("add", "alice", "--connection", "vless-reality-2")
        run("remove-connection", "vless-reality")
        self.assertEqual(run("key", "alice").stdout.strip(), f"vpn-key:{ALICE_KEY}")
        run("move-connection", "alice", "Third")
        self.assertEqual(run("key", "alice").stdout.strip(), "vpn-key:00000000-0000-4000-8000-000000000002")

    def test_trojan_only_in_config(self):
        """Текущее поведение, баг XR-REV-059 (остаток)."""
        self.check(self.trojan_only_in_config)

    def trojan_only_in_config(self, box, run):
        add_trojan(run, "--no-caddy")
        config = box.config()
        trojan = next(inbound for inbound in config["inbounds"] if inbound["tag"] == "trojan-tls")
        trojan["settings"]["clients"].append({"password": "fixture-only", "email": "bob", "level": 0})
        write_config(box, config)
        run("list")
        run("link", "bob", exit_code=1)
        for command, args in (
            ("set-days", ("3",)), ("extend-days", ("3",)), ("set-payment", ("paid",)),
            ("set-limit", ("daily", "1")),
        ):
            run(command, "bob", *args, raises=(ValueError, "Client has no UUID: bob"))
        original = box.paths.config.read_bytes()
        result = run("disable", "bob", exit_code=1)
        self.assertIn("Client has no UUID: bob", result.stderr)
        self.assertEqual(box.paths.config.read_bytes(), original)

    def test_client_edge_cases(self):
        """Текущее поведение, баг XR-REV-073 (a–e)."""
        self.check(self.client_edge_cases)

    def client_edge_cases(self, box, run):
        run("set-days", "alice", "3")
        add_reality(run)
        run("add", "alice", "90", "--connection", "vless-reality-2")
        run("disable", "alice", "--connection", "vless-reality-2")
        run("disable", "alice", "--connection", "vless-reality-2", exit_code=1)
        run("enable", "alice", "--connection", "vless-reality", exit_code=1)
        run("set-days", "alice", "+5", exit_code=1)
        run("extend-days", "alice", "+5")
        config = box.config()
        inbound = next(inbound for inbound in config["inbounds"] if inbound["tag"] == "vless-reality")
        inbound["settings"]["clients"].append({"id": "00000000-0000-4000-8000-000000000099", "email": "orphan"})
        write_config(box, config)
        run("enable", "orphan", exit_code=1)
        config = box.config()
        config["inbounds"][1]["streamSettings"]["realitySettings"]["serverNames"] = []
        write_config(box, config)
        run("link", "alice", "--connection", "vless-reality", raises=(IndexError, "list index out of range"))

    def test_clear_limit_preserves_disabled_status(self):
        """Текущее поведение, баг XR-REV-037."""
        self.check(self.clear_limit_preserves_disabled_status)

    def clear_limit_preserves_disabled_status(self, box, run):
        seed_traffic(box)
        run("set-limit", "alice", "daily", "1")
        run("clear-limit", "alice")
        run("enforce-limits")
        run("list")
        self.assertEqual(box.config()["inbounds"][1]["settings"]["clients"], [])


if __name__ == "__main__":
    unittest.main()
