"""VIREI-88 (T4b). Меню подключений и Caddy / TLS на состоянии перед шагом 4b."""

from contextlib import closing, contextmanager
from difflib import unified_diff
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import unittest
from unittest import mock
from urllib.parse import urlsplit

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_client_characterization import script_processes, snapshot as client_snapshot


REALITY = ("2", "1")
TROJAN = ("2", "2")
CONNECTIONS = ("2",)
CADDY_STATUS = ("2", "4", "1")
CADDY_SITES = ("2", "4", "2")
CADDY_SERVICE = ("2", "4", "3")
CADDY_BACKUPS = ("2", "4", "4")
CADDY_RANDOMIZER = ("2", "4", "5")
ACTION_START = re.compile(r"^={2,} .+ ={2,}$", re.MULTILINE)
ACTION_END = re.compile(r"^={2,}$", re.MULTILINE)
ARCHIVE_FOLDERS = ("xray_caddy_backups", "xray_caddy_site_backups")


def script_menu_processes(box):
    script_processes(box)
    processes = box.processes
    processes.executables["caddy"] = "/usr/bin/caddy"
    processes.script({
        "/usr/local/bin/xray version": processes.ok("Xray 26.1.1\n"),
        ("caddy", "validate", "--config", box.paths.caddyfile): processes.ok("Valid configuration\n"),
        "caddy version": processes.ok("v2.10.0\n"),
        "systemctl enable --now caddy": processes.ok(),
        "systemctl reload caddy": processes.ok(),
        "systemctl restart caddy": processes.ok(),
    })

    def client(argv, **kwargs):
        # run_cli захватывает потоки отдельного процесса. FakeProcesses затем
        # направляет их как обычный subprocess, включая PIPE при обновлении ссылки.
        result = box.run_cli("client", *argv[1:])
        return processes.result(result.exit_code, result.stdout, result.stderr)

    processes.script_program("xray-client", client)


def snapshot(box):
    state = client_snapshot(box)
    paths = box.paths
    files = (
        *paths.caddy_conf_dir.glob("*.caddy.bak.*"),
        *paths.caddyfile.parent.glob("Caddyfile.bak.*"),
        *paths.caddy_random_tls_dir.glob("*.env"),
        *paths.systemd_dir.glob("xray-caddy-random-tls@.*"),
        *box.root.glob("var/www/fixture/*.html"),
    )
    for path in files:
        state[str(path.relative_to(box.root))] = path.read_text()
    for folder in ARCHIVE_FOLDERS:
        state[folder] = "".join(f"{path.name}\n" for path in sorted((box.root / "root" / folder).glob("*.tar.gz")))
    return state


def normalize_paths(box, value):
    # В имени архива сайта абсолютный путь кодируется с подчёркиваниями.
    archive_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(box.root).strip("/ ")).strip("_")
    return value.replace(str(box.root), "<ROOT>").replace(
        f"caddy-site-backup-{archive_prefix}_", "caddy-site-backup-<ROOT>_",
    )


def normalize_output(box, output, *, backup=False):
    # Длина tmp-пути влияет на ширину таблиц. Сохраняем подписи и значения
    # ячеек, убирая только выравнивание и ширину границ.
    output = normalize_paths(box, output)
    if backup:
        # Статистика tar.gz зависит от mtime, сжатия и абсолютного tmp-пути.
        output = re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC", "<MTIME>", output)
        output = re.sub(
            r"(Size: |\|\s*)\d+(?:\.\d+)?(?:B|KB|MB|GB)(?=\s*(?:\||$))",
            r"\1<SIZE>", output, flags=re.MULTILINE,
        )
    rows = []
    for line in output.splitlines():
        if line.startswith("|") and line.endswith("|"):
            line = "|".join(part.strip() for part in line.split("|"))
        elif re.fullmatch(r"\+(?:-+\+)+", line):
            line = re.sub(r"-+", "-", line)
        rows.append(line.rstrip())
    return "\n".join(rows) + "\n"


class MenuTranscript:
    def __init__(self, case, box, name):
        self.case = case
        self.box = box
        self.name = name
        self.entries = []

    def action(self, menu, *answers, stderr=""):
        box = self.box
        before = snapshot(box)
        offset = len(box.processes.calls)
        script = [*menu, *answers, *(["0"] * (len(menu) + 1))]
        result = box.run_menu(script)
        self.case.assertEqual(box.processes.unexpected, [], script)
        self.case.assertEqual(result.exit_code, 0, script)
        self.case.assertNotIn("Ввод закрыт, выход из меню.", result.stdout, script)
        self.case.assertNotIn("Неизвестный пункт меню.", result.stdout, script)
        self.case.assertEqual(result.stderr.replace(str(box.root), "<ROOT>"), stderr, script)
        actions = list(ACTION_START.finditer(result.stdout))
        self.case.assertEqual(len(actions), len(menu), result.stdout)
        start = actions[-1].start()
        end = ACTION_END.search(result.stdout, actions[-1].end())
        self.case.assertIsNotNone(end, result.stdout)
        # Заголовки и перерисовки вышестоящих меню общие для каждого запуска.
        output = normalize_output(box, result.stdout[start:end.end()], backup=menu == CADDY_BACKUPS)
        # На шаге 4b меню вызовет сценарии напрямую. Фиксируем системные
        # команды внутри них, включая порядок, без промежуточного запуска CLI.
        calls = "".join(
            json.dumps(call, ensure_ascii=False) + "\n"
            for call in box.processes.calls[offset:]
            if call[0] != "xray-client"
        )
        after = snapshot(box)
        changes = []
        for name in sorted(before.keys() | after.keys()):
            changes.extend(unified_diff(
                before.get(name, "").splitlines(keepends=True),
                after.get(name, "").splitlines(keepends=True),
                fromfile=f"before/{name}", tofile=f"after/{name}", n=0,
            ))
        label = normalize_paths(box, json.dumps([*menu, *answers], ensure_ascii=False))
        state = normalize_paths(box, "".join(changes))
        self.entries.append(
            f"\n{label}\nexit: {result.exit_code}\nstdout:\n{output}stderr:\n{result.stderr}"
            f"processes:\n{calls}state:\n{state}"
        )
        return output

    def assert_golden(self):
        self.box.assert_golden(f"connections/t4b/{self.name}", {"transcript.txt": "".join(self.entries)})


class ConnectionMenuCharacterizationTests(unittest.TestCase):
    @contextmanager
    def scenario(self, name):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"), \
                mock.patch.dict(os.environ, {"XRAY_MENU_DEBUG": "0", "XRAY_MANAGER_DEBUG": "0"}):
            script_menu_processes(box)
            box.paths.initial_link.parent.mkdir(parents=True, exist_ok=True)
            transcript = MenuTranscript(self, box, name)
            yield box, transcript.action
            transcript.assert_golden()

    def seed_cli(self, box, *args):
        result = box.run_cli("client", *args)
        self.assertEqual((result.exit_code, result.stderr), (0, ""), (args, result.stdout, result.stderr))
        return result.stdout

    def script_tls_probe(self, box, domain, *, available=True):
        responses = (
            box.processes.ok("Protocol version: TLSv1.2\n") if available
            else box.processes.result(1, stderr="Connection refused\n"),
            box.processes.result(1, stderr="Protocol version not supported\n") if available
            else box.processes.result(1, stderr="Connection refused\n"),
        )
        for version, response in zip(("tls1_2", "tls1_3"), responses, strict=True):
            box.processes.script({
                ("timeout", "10", "openssl", "s_client", "-connect", f"{domain}:443",
                 "-servername", domain, f"-{version}", "-brief"): response,
            })

    def test_reality_menu(self):
        with self.scenario("reality_menu") as (box, action):
            action(REALITY, "1")
            action(REALITY, "3", "5443")
            action(REALITY, "4", "sni.example")
            action(REALITY, "5", "6443", "other-sni.example")
            action(REALITY, "6", "2")
            action(REALITY, "7", "2", "fixture-grpc")
            action(REALITY, "7", "3", "/upload", "2", "n")
            action(
                REALITY, "8", "1", "2", "150-300", "",  # выбор, редактирование, padding, время потока
                "y", "512000", "50", "12",  # packet-up
                "y", "y", "n", "n",  # noGRPCHeader, noSSEHeader, без custom headers
                "8-12", "", "", "", "", "15",  # XMUX
                "y", "download.example", "", "", "", "", "", "/download", "", "", "n", "n",
            )
            action(REALITY, "8", "1", "1")
            action(REALITY, "7", "1")
            action(REALITY, "9")  # последнее VLESS-подключение
            action(REALITY, "2", "1", "Reserve", "7443", "reserve.example", "3", "1")
            self.seed_cli(box, "add", "bob", "10", "--connection", "vless-reality-2")
            action(REALITY, "10", "2", "Fallback")
            action(REALITY, "9", "2", "n")
            action(REALITY, "9", "2", "y")
            self.assertIn("FINGERPRINT=firefox", box.paths.initial_link.read_text())
            action(CONNECTIONS, "3")

    def test_trojan_menu(self):
        with self.scenario("trojan_menu") as (box, action):
            action(TROJAN, "1")
            action(TROJAN, "2", "Trojan", "18101", "trojan.example", "", "/tunnel", "3", "2", "")
            action(REALITY, "3", "5443")
            action(TROJAN, "2", "Trojan", "18101", "trojan.example", "", "/tunnel", "3", "2", "")
            self.seed_cli(box, "add", "alice", "--connection", "trojan-tls")
            action(TROJAN, "1")
            action(TROJAN, "4", "1", "Renamed Trojan")
            action(TROJAN, "3", "1", "n")
            action(TROJAN, "3", "1", "y")

    def test_menu_can_create_tls_connections_without_caddy(self):
        with self.scenario("no_caddy") as (box, action):
            action(REALITY, "2", "2", "TLS", "tls.example", "18100", "", "1", "/secure", "", "n", "2", "n")
            action(TROJAN, "2", "Trojan", "18101", "trojan.example", "", "/tunnel", "1", "2", "n")
            inbounds = {item["tag"]: item for item in box.config()["inbounds"]}
            self.assertEqual(inbounds["vless-reality"]["port"], 443)
            self.assertEqual((inbounds["vless-tls"]["listen"], inbounds["vless-tls"]["port"]), ("127.0.0.1", 18100))
            self.assertEqual((inbounds["trojan-tls"]["listen"], inbounds["trojan-tls"]["port"]), ("127.0.0.1", 18101))
            self.assertFalse(box.paths.caddyfile.exists())
            self.assertFalse(box.paths.caddy_conf_dir.exists())

    def test_multi_protocol_initial_link(self):
        """Текущее поведение, баг XR-REV-029."""
        with self.scenario("xr_rev_029_initial_link") as (box, action):
            self.seed_cli(box, "add-trojan-connection", "Trojan", "18101", "trojan.example", "--no-caddy")
            self.seed_cli(box, "add", "alice", "--connection", "trojan-tls")
            vless = self.seed_cli(box, "link", "alice", "--connection", "vless-reality").strip()
            trojan = self.seed_cli(box, "link", "alice", "--connection", "trojan-tls").strip()
            path = box.paths.initial_link
            path.write_text(
                f"INITIAL_PROTOCOL=both\nCLIENT_URI={vless}\nVLESS_CLIENT_URI={vless}\n"
                f"TROJAN_CLIENT_URI={trojan}\nUUID=00000000-0000-4000-8000-000000000001\n"
            )
            path.chmod(0o644)
            action(REALITY, "5", "5443", "updated.example")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            lines = path.read_text().splitlines()
            self.assertEqual(lines[:3], ["CLIENT_URI=[trojan trojan-tls]", trojan, "[vless vless-reality]"])
            self.assertEqual(urlsplit(lines[3]).port, 5443)
            self.assertTrue({"INITIAL_PROTOCOL", "VLESS_CLIENT_URI", "TROJAN_CLIENT_URI", "UUID"}.isdisjoint(
                line.partition("=")[0] for line in lines
            ))
            action(CONNECTIONS, "3")

    def test_caddy_keeps_default_listener_with_custom_public_port(self):
        """Текущее поведение, баг XR-REV-064."""
        with self.scenario("xr_rev_064_public_port") as (box, action):
            action(REALITY, "2", "2", "TLS", "tls.example", "18100", "1443", "1", "/secure", "", "n", "2", "")
            action(TROJAN, "2", "Trojan", "18101", "trojan.example", "2443", "/tunnel", "1", "2", "")
            for tag, domain, port in (("vless-tls", "tls.example", 1443), ("trojan-tls", "trojan.example", 2443)):
                with self.subTest(tag=tag):
                    self.seed_cli(box, "add", "alice", "--connection", tag)
                    link = urlsplit(self.seed_cli(box, "link", "alice", "--connection", tag).strip())
                    self.assertEqual((link.hostname, link.port), (domain, port))
                    site = (box.paths.caddy_conf_dir / f"{domain}.caddy").read_text()
                    self.assertEqual(site.splitlines()[0], f"{domain} {{")
                    with closing(sqlite3.connect(box.paths.manager_db)) as connection:
                        row, = connection.execute(
                            "SELECT extra_json FROM reality_connections WHERE tag = ?", (tag,),
                        ).fetchone()
                    self.assertEqual(json.loads(row)["publicPort"], port)
            self.assertNotIn("https_port", box.paths.caddyfile.read_text())

    def test_caddy_domain_change_keeps_old_link(self):
        """Текущее поведение, баг XR-REV-053."""
        with self.scenario("xr_rev_053_stale_domain") as (box, action):
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            self.seed_cli(box, "add", "alice", "--connection", "vless-tls")
            before = self.seed_cli(box, "link", "alice", "--connection", "vless-tls")
            state = box.db_rows("reality_connections", "client_credentials")
            action(CADDY_SITES, "7", "new.example")
            site, = box.paths.caddy_conf_dir.glob("*.caddy")
            self.assertIn("new.example {", site.read_text())
            self.assertEqual(box.db_rows("reality_connections", "client_credentials"), state)
            self.assertEqual(self.seed_cli(box, "link", "alice", "--connection", "vless-tls"), before)
            self.assertEqual(urlsplit(before.strip()).hostname, "old.example")

    def test_caddy_domain_change_replaces_existing_site(self):
        """Текущее поведение, баг XR-REV-063."""
        with self.scenario("xr_rev_063_existing_site") as (box, action):
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "a.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            self.seed_cli(
                box, "add-trojan-connection", "WS", "18101", "b.example",
                "--ws-path", "/ws", "--install-caddy", "--public-port", "2443",
            )
            old_target = (box.paths.caddy_conf_dir / "b.example.caddy").read_text()
            state = box.db_rows("reality_connections")
            action(CADDY_SITES, "7", "1", "b.example")
            remaining, = box.paths.caddy_conf_dir.glob("*.caddy")
            self.assertIn("b.example {", remaining.read_text())
            self.assertIn("h2c://127.0.0.1:18100", remaining.read_text())
            self.assertNotIn("18101", remaining.read_text())
            backup, = box.paths.caddy_conf_dir.glob("b.example.caddy.bak.*")
            self.assertEqual(backup.read_text(), old_target)
            self.assertEqual(box.db_rows("reality_connections"), state)

    def test_manual_tls_check_uses_443_for_direct_trojan(self):
        """Текущее поведение, баг XR-REV-052."""
        with self.scenario("xr_rev_052_manual_handshake") as (box, action):
            self.seed_cli(
                box, "add-trojan-connection", "Direct", "9443", "direct.example",
                "/fixture/cert.pem", "/fixture/key.pem",
            )
            self.assertFalse(list(box.paths.caddy_conf_dir.glob("*.caddy")))
            self.assertIn("9443", box.db_rows("reality_connections"))
            self.script_tls_probe(box, "direct.example", available=False)
            output = action(CADDY_STATUS, "5", "direct.example")
            self.assertEqual(output.count("Connection refused"), 2)

    def test_caddy_site_management(self):
        with self.scenario("caddy_sites") as (box, action):
            self.seed_cli(box, "add-trojan-connection", "WS", "18101", "ws.example", "--ws-path", "/ws", "--no-caddy")
            action(CADDY_SITES, "3", "1", "3")
            action(CADDY_SITES, "1")
            action(CADDY_SITES, "2")
            action(CADDY_SITES, "5", "4")
            action(CADDY_SITES, "6", "18102")
            action(CADDY_SITES, "8", "1", "n")
            action(CADDY_SITES, "8", "1", "y")

    def test_caddy_status_and_service(self):
        with self.scenario("caddy_status_and_service") as (box, action):
            processes = box.processes
            processes.script({
                "apt-get update": processes.ok(),
                "apt-get install -y caddy": processes.ok(),
                "systemctl --no-pager --full status caddy.service": processes.ok("Active: active (running)\n"),
                "journalctl -u caddy -n 120 --no-pager": processes.ok("caddy: fixture log\n"),
            })
            del processes.executables["caddy"]
            processes.executables["apt-get"] = "/usr/bin/apt-get"
            box.paths.caddyfile.parent.mkdir(parents=True)
            box.paths.caddyfile.write_text(":80 {\n    file_server\n}\n")
            action(CADDY_STATUS, "2")
            action(CADDY_STATUS, "1")
            action(CADDY_STATUS, "3")
            action(CADDY_STATUS, "4")
            action(CADDY_STATUS, "6")
            action(CADDY_SITES, "4", "manual.example", "18100", "2")
            self.script_tls_probe(box, "manual.example")
            action(CADDY_STATUS, "5")
            action(CADDY_SITES, "9", "y")
            processes.script({"systemctl reload caddy": processes.result(1)})
            action(CADDY_SERVICE, "1")
            action(CADDY_SERVICE, "2")

    def test_caddy_config_and_site_backups(self):
        with self.scenario("caddy_backups") as (box, action):
            site_root = box.root / "var/www/fixture"
            site_root.mkdir(parents=True)
            index = site_root / "index.html"
            index.write_text("original page\n")
            box.paths.caddyfile.parent.mkdir(parents=True)
            original = 'backup.example {\n    respond "original"\n}\n'
            box.paths.caddyfile.write_text(original)

            action(CADDY_BACKUPS, "1")
            saved_config, = (box.root / "root/xray_caddy_backups").glob("*.tar.gz")
            action(CADDY_BACKUPS, "2")
            box.paths.caddyfile.write_text(original.replace("backup.example", "changed.example"))
            action(CADDY_BACKUPS, "3", "1", "y")
            self.assertEqual(box.paths.caddyfile.read_text(), original)
            action(CADDY_BACKUPS, "4", "1", "y")
            self.assertFalse(saved_config.exists())

            # Автопоиск стандартных site roots не зависит от папок хоста.
            exists = Path.exists
            host_roots = {Path(path) for path in ("/usr/share/caddy", "/var/www", "/var/www/html", "/srv/www")}

            def exists_in_fixture(path):
                return path not in host_roots and exists(path)

            with mock.patch.object(Path, "exists", exists_in_fixture):
                action(CADDY_BACKUPS, "5", "M", str(site_root))
                saved_site, = (box.root / "root/xray_caddy_site_backups").glob("*.tar.gz")
                action(CADDY_BACKUPS, "6")
                index.write_text("changed page\n")
                action(CADDY_BACKUPS, "7", "1", "", "y")
                self.assertEqual(index.read_text(), "original page\n")
                action(CADDY_BACKUPS, "8", "1", "y")
                self.assertFalse(saved_site.exists())

    def test_caddy_randomizer_is_enabled_explicitly(self):
        with self.scenario("caddy_randomizer") as (box, action):
            processes = box.processes
            timer = "xray-caddy-random-tls@random.example.timer"
            service = "xray-caddy-random-tls@random.example.service"
            processes.script({
                "systemctl is-enabled xray-caddy-random-tls.timer": processes.result(1, "disabled\n"),
                "systemctl disable --now xray-caddy-random-tls.timer": processes.ok(),
                "systemctl stop xray-caddy-random-tls.service": processes.ok(),
                f"systemctl enable --now {timer}": processes.ok(),
                f"systemctl show {timer} --property=NextElapseUSecRealtime --value": processes.ok("n/a\n"),
                f"systemctl list-timers --all --no-legend --no-pager {timer}": processes.ok(),
                f"systemctl is-enabled {timer}": processes.ok("enabled\n"),
                f"systemctl is-active {timer}": processes.ok("active\n"),
                f"systemctl is-active {service}": processes.result(3, "inactive\n"),
                f"systemctl disable --now {timer}": processes.ok(),
                f"systemctl stop {service}": processes.ok(),
            })
            action(CADDY_SITES, "4", "random.example", "18100", "2")
            action(CADDY_RANDOMIZER, "1")
            action(CADDY_RANDOMIZER, "2", "n")
            self.assertFalse(box.paths.caddy_random_tls_dir.exists())
            action(CADDY_RANDOMIZER, "2", "y")
            action(CADDY_RANDOMIZER, "1")
            action(CADDY_RANDOMIZER, "4")
            action(CADDY_RANDOMIZER, "3", "y")


if __name__ == "__main__":
    unittest.main()
