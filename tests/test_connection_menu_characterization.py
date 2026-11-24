"""VIREI-88 (T4b). Меню подключений и Caddy / TLS на состоянии перед шагом 4b."""

from contextlib import closing, contextmanager, redirect_stdout
from difflib import unified_diff
from glob import glob
from io import StringIO
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.support.sandbox import Sandbox
from tests.test_apply_characterization import running_xray
from tests.test_client_characterization import (
    is_caddy_candidate_validation, normalize_caddy_candidate_call, script_connection_caddy_checks,
    script_processes, snapshot as client_snapshot, write_config,
)
from xray_vps_manager.caddy import model as caddy_model
from xray_vps_manager.caddy import sites as caddy_sites
from xray_vps_manager.commands import test as test_command


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


def is_caddy_candidate_adapt(call):
    return (
        len(call) == 5 and tuple(call[:3]) == ("caddy", "adapt", "--config")
        and Path(call[3]).name == "Caddyfile"
        and Path(call[3]).parent.name.startswith("xray-caddy-candidate-")
        and call[4] == "--validate"
    )


def configure_caddy_https_port(box, port):
    caddyfile = box.paths.caddyfile
    caddyfile.parent.mkdir(parents=True, exist_ok=True)
    caddyfile.write_text(f"{{\n    https_port {port}\n}}\n")


def script_menu_processes(box):
    script_processes(box)
    script_connection_caddy_checks(box)
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

    def site_route(host, content):
        # Фикстуры меню используют явные loopback upstream. Ответ Caddy
        # оформлен как JSON с вложенным subroute для выбранного домена.
        lines = content.splitlines()
        start, end = caddy_model.first_site_block_line_span(lines)
        body = "\n".join(line for line in lines[start + 1:end] if not line.lstrip().startswith("#"))
        proxy = {
            "handler": "reverse_proxy",
            "upstreams": [{"dial": f"127.0.0.1:{match['port']}"}
                          for match in caddy_model.REVERSE_PROXY_RE.finditer(body)],
        }
        routes = [{"handle": [proxy]}]
        if dynamic := re.search(r"(?m)^[ \t]*dynamic a (\S+) ([0-9]+)", body):
            routes.append({"handle": [{
                "handler": "reverse_proxy",
                "dynamic_upstreams": {"source": "a", "name": dynamic[1], "port": dynamic[2]},
            }]})
        return {
            "match": [{"host": [host]}] if host else [],
            "handle": [{"handler": "subroute", "routes": routes}],
        }

    def caddy(call, **kwargs):
        if not (is_caddy_candidate_validation(call) or is_caddy_candidate_adapt(call)):
            raise AssertionError(f"Неизвестная команда Caddy: {call}")
        config = Path(call[3])
        assert config.is_file()
        if is_caddy_candidate_validation(call):
            return processes.ok()
        https_port = re.search(r"(?m)^\s*https_port[ \t]+([0-9]+)", config.read_text())
        default_port = https_port[1] if https_port else "443"
        servers = {}
        for path in sorted((config.parent / "conf.d").glob("*.caddy")):
            content = path.read_text()
            host, _, port = caddy_model.first_site_address(content).partition(":")
            bind = re.search(r"(?m)^[ \t]*bind[ \t]+([^#\r\n]+)", content)
            interfaces = bind[1].split() if bind else [""]
            listeners = [f"{interface}:{port or default_port}" for interface in interfaces]
            server = servers.setdefault(" ".join(listeners), {"listen": listeners, "routes": []})
            server["routes"].append(site_route(host, content))
        return processes.ok(json.dumps({"apps": {"http": {"servers": servers}}}))

    processes.script_program("caddy", caddy)

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
    archive_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(box.root).lstrip("/ ") + "/").lstrip("_")
    return value.replace(str(box.root), "<ROOT>").replace(
        f"caddy-site-backup-{archive_prefix}", "caddy-site-backup-<ROOT>_",
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
        actual_stderr = re.sub(r"\x1b\[[0-9;]*m", "", normalize_paths(box, result.stderr))
        self.case.assertEqual(actual_stderr, stderr, script)
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
            json.dumps(
                (*call[:3], "<CADDY_CANDIDATE>/Caddyfile", call[4]) if is_caddy_candidate_adapt(call)
                else normalize_caddy_candidate_call(call),
                ensure_ascii=False,
            ) + "\n"
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
            f"\n{label}\nexit: {result.exit_code}\nstdout:\n{output}stderr:\n{actual_stderr}"
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

    def assert_site_link(self, box, tag, domain, port):
        link = urlsplit(self.seed_cli(box, "link", "alice", "--connection", tag).strip())
        self.assertEqual((link.hostname, link.port), (domain, port))
        self.assertEqual(parse_qs(link.query)["sni"], [domain])
        listing = self.seed_cli(box, "connection-list")
        self.assertTrue(any(tag in row and domain in row for row in listing.splitlines()), listing)
        with closing(sqlite3.connect(box.paths.manager_db)) as conn:
            sni, extra = conn.execute(
                "SELECT sni, extra_json FROM reality_connections WHERE tag = ?", (tag,),
            ).fetchone()
        self.assertEqual((sni, json.loads(extra)["publicHost"]), (domain, domain))

    def assert_tls_diagnostics(self, domain, port, *, expected_warnings=()):
        diag = test_command.Diagnostics()
        test_command.check_client_db(diag)
        certificate = {"subjectAltName": (("DNS", domain),), "notAfter": "Dec 31 23:59:59 2027 GMT"}
        with (
            mock.patch("socket.create_connection") as connect,
            mock.patch("ssl.create_default_context") as tls_context,
            redirect_stdout(StringIO()) as output,
        ):
            socket = connect.return_value.__enter__.return_value
            wrap_socket = tls_context.return_value.wrap_socket
            wrap_socket.return_value.__enter__.return_value.getpeercert.return_value = certificate
            diag.check("TLS certificate diagnostics", lambda: test_command.check_tls_diagnostics(diag), fatal=False)
        self.assertEqual(tuple(diag.warnings), expected_warnings)
        if not expected_warnings:
            self.assertIn("TLS certificate diagnostics OK: direct=0, caddy=1", output.getvalue())
        connect.assert_called_once_with((domain, port), timeout=6)
        wrap_socket.assert_called_once_with(socket, server_hostname=domain)

    def test_caddy_domain_change_updates_vless_link(self):
        """XR-REV-053 исправлен: домен и SNI VLESS меняются вместе со штатным site."""
        with self.scenario("xr_rev_053_stale_domain") as (box, action):
            configure_caddy_https_port(box, 1443)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            self.seed_cli(box, "add", "alice", "--connection", "vless-tls")
            self.assert_site_link(box, "vless-tls", "old.example", 1443)
            # Штатный site проксирует все пути. Прежняя диагностика сравнивает
            # отсутствующий matcher с xhttpPath. Переименование сохраняет этот WARN.
            path_warning = "TLS certificate diagnostics: vless-tls: Caddy route path mismatch for {}: - != /vless-xhttp"
            self.assert_tls_diagnostics("old.example", 1443, expected_warnings=(path_warning.format("old.example"),))
            state = box.db_rows("client_credentials")
            output = action(CADDY_SITES, "7", "new.example")
            site, = box.paths.caddy_conf_dir.glob("*.caddy")
            self.assertEqual(site.name, "new.example.caddy")
            self.assertEqual(box.db_rows("client_credentials"), state)
            self.assertIn("VLESS/TLS/xHTTP и Trojan-WS", output)
            self.assert_site_link(box, "vless-tls", "new.example", 1443)
            self.assert_tls_diagnostics("new.example", 1443, expected_warnings=(path_warning.format("new.example"),))

    def test_caddy_domain_change_preserves_both_upstream_routes(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            configure_caddy_https_port(box, 1443)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            self.seed_cli(
                box, "add-trojan-connection", "WS", "18101", "old.example", "--ws-path", "/ws",
                "--no-caddy", "--public-port", "1443",
            )
            for tag in ("vless-tls", "trojan-tls"):
                self.seed_cli(box, "add", "alice", "--connection", tag)

            before = """# TLS endpoint old.example
old.example {
    @xhttp path /vless-xhttp*
    handle @xhttp {
        reverse_proxy h2c://127.0.0.1:18100
    }

    @trojan path /ws
    handle @trojan {
        reverse_proxy 127.0.0.1:18101
    }
}
"""
            directory = box.paths.caddy_conf_dir
            old_site = directory / "old.example.caddy"
            old_site.write_text(before)

            result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))

            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertFalse(old_site.exists())
            self.assertEqual(
                (directory / "new.example.caddy").read_text(),
                before.replace("\nold.example {", "\nnew.example {", 1),
            )
            for tag in ("vless-tls", "trojan-tls"):
                self.assert_site_link(box, tag, "new.example", 1443)

    def test_caddy_domain_change_updates_older_tls_records(self):
        connection_args = {
            "vless-tls": (
                "add-connection", "TLS", "18100", "old.example", "--security", "tls", "--transport", "xhttp",
            ),
            "trojan-tls": ("add-trojan-connection", "WS", "18101", "old.example", "--ws-path", "/ws"),
        }
        for tag, args in connection_args.items():
            for field in ("publicHost", "security"):
                with (
                    self.subTest(tag=tag, missing=field),
                    Sandbox() as box,
                    running_xray(box),
                    mock.patch("socket.socket"),
                ):
                    script_menu_processes(box)
                    configure_caddy_https_port(box, 1443)
                    self.seed_cli(box, *args, "--install-caddy", "--public-port", "1443")
                    self.seed_cli(box, "add", "alice", "--connection", tag)
                    with closing(sqlite3.connect(box.paths.manager_db)) as conn, conn:
                        conn.execute(
                            "UPDATE reality_connections SET extra_json = json_remove(extra_json, ?) WHERE tag = ?",
                            (f"$.{field}", tag),
                        )
                    link = urlsplit(self.seed_cli(box, "link", "alice", "--connection", tag).strip())
                    self.assertEqual((link.hostname, parse_qs(link.query)["sni"]), ("old.example", ["old.example"]))

                    result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))

                    self.assertEqual((result.exit_code, result.stderr), (0, ""))
                    self.assertIn("перевыпустите", result.stdout)
                    self.assert_site_link(box, tag, "new.example", 1443)

    def test_caddy_domain_change_refuses_existing_site(self):
        """XR-REV-063 исправлен: занятый домен отклоняется до записи и reload."""
        with self.scenario("xr_rev_063_existing_site") as (box, action):
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "a.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            self.seed_cli(
                box, "add-trojan-connection", "WS", "18101", "b.example",
                "--ws-path", "/ws", "--install-caddy", "--public-port", "2443",
            )
            state = snapshot(box)
            offset = len(box.processes.calls)
            action(
                CADDY_SITES, "7", "1", "b.example",
                stderr="ERROR: Caddy site config already exists for b.example: "
                "<ROOT>/etc/caddy/conf.d/b.example.caddy (127.0.0.1:18101, transport=http, path=/ws). "
                "Creating a new TLS connection would overwrite this site and may break existing traffic. "
                "Review, update, or delete the existing site config through Caddy / TLS -> Site configs first.\n",
            )
            self.assertEqual(snapshot(box), state)
            self.assertFalse(any(call[0] in ("caddy", "systemctl") for call in box.processes.calls[offset:]))

    def test_caddy_domain_change_checks_the_future_caddyfile_before_writes(self):
        for location in ("Caddyfile", "second_block", "selected_file"):
            with self.subTest(location=location), Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
                script_menu_processes(box)
                self.seed_cli(
                    box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                    "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
                )
                conflict = 'new.example {\n    respond "existing"\n}\n'
                if location == "Caddyfile":
                    caddyfile = box.paths.caddyfile
                    caddyfile.write_text(caddyfile.read_text() + "\n" + conflict)
                    selection = ()
                elif location == "second_block":
                    other_site = box.paths.caddy_conf_dir / "separate.caddy"
                    other_site.write_text('keeper.example {\n    respond "keeper"\n}\n\n' + conflict)
                    selection = ("1",)
                else:
                    original = box.paths.caddy_conf_dir / "old.example.caddy"
                    selected = original.rename(original.with_name("new.example.caddy"))
                    selected.write_text(selected.read_text() + "\n" + conflict)
                    selection = ()
                before = snapshot(box)
                db_before = box.paths.manager_db.read_bytes()
                offset = len(box.processes.calls)
                checked = []

                def validate_future(call, **kwargs):
                    self.assertTrue(is_caddy_candidate_adapt(call))
                    caddyfile = Path(call[3])
                    sites = caddyfile.parent / "conf.d"
                    main_config = caddyfile.read_text()
                    self.assertIn(f"import {sites}/*.caddy", main_config)
                    self.assertNotIn(str(box.paths.caddy_conf_dir), main_config)
                    self.assertFalse((sites / "old.example.caddy").exists())
                    self.assertIn("new.example {", (sites / "new.example.caddy").read_text())
                    configs = [main_config, *(path.read_text() for path in sites.glob("*.caddy"))]
                    self.assertEqual(sum(text.count("new.example {") for text in configs), 2)
                    self.assertEqual(snapshot(box), before)
                    self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
                    checked.append(caddyfile)
                    return box.processes.result(1, stderr="duplicate site address new.example")

                box.processes.script_program("caddy", validate_future)
                result = box.run_menu((*CADDY_SITES, "7", *selection, "new.example", "0", "0", "0", "0"))

                self.assertEqual(result.exit_code, 0)
                self.assertIn("duplicate site address new.example", result.stderr)
                self.assertEqual(snapshot(box), before)
                self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
                self.assertFalse(any(call[0] == "systemctl" for call in box.processes.calls[offset:]))
                temp_file, = checked
                self.assertFalse(temp_file.exists())

    def test_caddy_domain_change_checks_nested_absolute_import_before_writes(self):
        """140-R03: вложенный glob должен видеть будущие файлы, а не рабочие."""
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            main = box.paths.caddyfile
            nested = main.parent / "snippets" / "extra.caddy"
            nested.parent.mkdir()
            pattern = (
                f'import "{box.paths.caddy_conf_dir}/new*.caddy"\n'
                'import ../conf.d/old.example.caddy.bak.*\n'
                'nested.example {\n    respond <<MESSAGE\n'
                '    import /literal/response.caddy\n    MESSAGE\n}\n'
            )
            nested.write_text(pattern)
            main.write_text(main.read_text() + "\nimport snippets/extra.caddy\n")
            before = snapshot(box)
            db_before = box.paths.manager_db.read_bytes()
            offset = len(box.processes.calls)

            def validate_future(call, **kwargs):
                self.assertTrue(is_caddy_candidate_adapt(call))
                candidate = Path(call[3]).parent
                imported = candidate / "snippets" / "extra.caddy"
                lines = imported.read_text().splitlines()
                _, sites_pattern = shlex.split(lines[0])
                self.assertEqual(sites_pattern, f"{candidate}/conf.d/new*.caddy")
                self.assertEqual(glob(sites_pattern), [str(candidate / "conf.d/new.example.caddy")])
                backup, = (candidate / "conf.d").glob("old.example.caddy.bak.*")
                self.assertEqual(backup.read_bytes(), (box.paths.caddy_conf_dir / "old.example.caddy").read_bytes())
                self.assertIn("    import /literal/response.caddy", lines)
                self.assertEqual(snapshot(box), before)
                self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
                return box.processes.result(1, stderr="duplicate site address new.example")

            box.processes.script_program("caddy", validate_future)
            result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))

            self.assertEqual(result.exit_code, 0)
            self.assertIn("duplicate site address new.example", result.stderr)
            self.assertEqual(snapshot(box), before)
            self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
            self.assertEqual(nested.read_text(), pattern)
            self.assertFalse(any(call[0] == "systemctl" for call in box.processes.calls[offset:]))

    def test_caddy_domain_change_rejects_imports_it_cannot_stage_before_writes(self):
        for import_type in ("outside", "symlink", "environment"):
            with self.subTest(import_type=import_type), Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
                script_menu_processes(box)
                self.seed_cli(
                    box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                    "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
                )
                main = box.paths.caddyfile
                directory = box.paths.caddy_conf_dir
                (main.parent / "alias").symlink_to(directory, target_is_directory=True)
                patterns = {
                    "outside": "../../new*.caddy",
                    "symlink": "alias/new*.caddy",
                    "environment": "{$VIREI_TEST_CADDY_IMPORT:conf.d/new*.caddy}",
                }
                main.write_text(main.read_text() + f"\nimport {patterns[import_type]}\n")
                before = snapshot(box)
                db_before = box.paths.manager_db.read_bytes()
                offset = len(box.processes.calls)

                result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))

                self.assertEqual(result.exit_code, 0)
                self.assertIn("Не удалось безопасно подготовить импорты Caddy", result.stderr)
                self.assertEqual(snapshot(box), before)
                self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
                self.assertFalse(any(call[0] in ("caddy", "systemctl") for call in box.processes.calls[offset:]))

    def test_caddy_domain_change_can_reuse_the_selected_site_path(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            configure_caddy_https_port(box, 1443)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            self.seed_cli(box, "add", "alice", "--connection", "vless-tls")
            original = box.paths.caddy_conf_dir / "old.example.caddy"
            selected = original.rename(original.with_name("new.example.caddy"))
            before = selected.read_text()
            reload = ("systemctl", "reload", "caddy")
            box.processes.script({reload: [
                box.processes.result(1, stderr="reload rejected"), box.processes.ok(), box.processes.ok(),
            ]})
            answers = (*CADDY_SITES, "7", "new.example", "0", "0", "0", "0")

            failed = box.run_menu(answers)

            self.assertEqual(failed.exit_code, 0)
            self.assertIn("reload rejected", failed.stderr)
            self.assertEqual(selected.read_text(), before)
            self.assert_site_link(box, "vless-tls", "old.example", 1443)

            result = box.run_menu(answers)
            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertEqual(selected.read_text(), before.replace("old.example {", "new.example {", 1))
            self.assertEqual(list(selected.parent.glob("*.caddy")), [selected])
            backups = list(selected.parent.glob("new.example.caddy.bak.*"))
            self.assertTrue(backups)
            self.assertTrue(all(backup.read_text() == before for backup in backups))
            self.assert_site_link(box, "vless-tls", "new.example", 1443)

    def test_caddy_domain_change_preserves_concurrent_edit_of_original_site(self):
        """140-R10: проверенный кандидат не должен затирать правку администратора."""
        for edit_during in ("adapt", "before_backup", "before_delete", "before_overwrite"):
            with self.subTest(edit_during=edit_during), Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
                script_menu_processes(box)
                configure_caddy_https_port(box, 1443)
                self.seed_cli(
                    box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                    "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
                )
                self.seed_cli(box, "add", "alice", "--connection", "vless-tls")
                main = box.paths.caddyfile
                directory = box.paths.caddy_conf_dir
                original = directory / "old.example.caddy"
                target = directory / "new.example.caddy"
                if edit_during == "before_overwrite":
                    original = original.rename(target)
                if edit_during in ("before_delete", "before_overwrite"):
                    configure_caddy_https_port(box, 1443)  # Импорт ещё не установлен.
                main_before = main.read_bytes()
                original_bytes = original.read_bytes()
                admin_edit = original_bytes.replace(b"old.example {", b"old.example {\n    header X-Admin yes", 1)
                db_before = box.db_rows("reality_connections")
                offset = len(box.processes.calls)
                adapt = box.processes._programs["caddy"]
                ensure_import = caddy_sites.ensure_caddyfile_import
                require_absent = caddy_sites.require_site_config_absent
                state_at_edit = []

                def edit_after_database_write():
                    with closing(sqlite3.connect(box.paths.manager_db)) as conn:
                        sni, metadata = conn.execute(
                            "SELECT sni, extra_json FROM reality_connections WHERE tag = 'vless-tls'",
                        ).fetchone()
                    state_at_edit.append((sni, json.loads(metadata)["publicHost"]))
                    original.write_bytes(admin_edit)

                def edit_after_adapt(call, **kwargs):
                    result = adapt(call, **kwargs)
                    if edit_during == "adapt" and is_caddy_candidate_adapt(call):
                        original.write_bytes(admin_edit)
                    return result

                def edit_before_backup(domain, *args, **kwargs):
                    if edit_during == "before_backup" and kwargs.get("replacing") is None:
                        edit_after_database_write()
                    return require_absent(domain, *args, **kwargs)

                def edit_after_import(path, *args, **kwargs):
                    result = ensure_import(path, *args, **kwargs)
                    if path == main and edit_during in ("before_delete", "before_overwrite"):
                        edit_after_database_write()
                    return result

                box.processes.script_program("caddy", edit_after_adapt)
                with (
                    mock.patch.object(caddy_sites, "require_site_config_absent", side_effect=edit_before_backup),
                    mock.patch.object(caddy_sites, "ensure_caddyfile_import", side_effect=edit_after_import),
                ):
                    result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))

                self.assertEqual(result.exit_code, 0)
                self.assertIn("Caddy site изменился", result.stderr)
                self.assertEqual(original.read_bytes(), admin_edit)
                self.assertEqual(list(directory.glob("*.caddy")), [original])
                self.assertEqual(main.read_bytes(), main_before)
                self.assertEqual(box.db_rows("reality_connections"), db_before)
                self.assert_site_link(box, "vless-tls", "old.example", 1443)
                expected_state_at_edit = [] if edit_during == "adapt" else [("new.example", "new.example")]
                self.assertEqual(state_at_edit, expected_state_at_edit)
                backups = list(directory.glob("*.caddy.bak.*"))
                if edit_during in ("before_delete", "before_overwrite"):
                    backup, = backups
                    self.assertEqual(backup.read_bytes(), original_bytes)
                else:
                    self.assertEqual(backups, [])
                self.assertFalse(any(call[0] == "systemctl" for call in box.processes.calls[offset:]))

    def test_caddy_domain_change_compensates_interruption_during_site_mutation(self):
        """140-R11: Ctrl+C до или сразу после изменения файла сохраняет прежний site."""
        for same_path in (False, True):
            for interrupt_after in (False, True):
                with (
                    self.subTest(same_path=same_path, interrupt_after=interrupt_after),
                    Sandbox() as box,
                    running_xray(box),
                    mock.patch("socket.socket"),
                ):
                    script_menu_processes(box)
                    configure_caddy_https_port(box, 1443)
                    self.seed_cli(
                        box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                        "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
                    )
                    self.seed_cli(box, "add", "alice", "--connection", "vless-tls")
                    directory = box.paths.caddy_conf_dir
                    original = directory / "old.example.caddy"
                    target = directory / "new.example.caddy"
                    source = original.rename(target) if same_path else original
                    configure_caddy_https_port(box, 1443)  # Импорт ещё не установлен.
                    before = client_snapshot(box)
                    offset = len(box.processes.calls)
                    replace = caddy_sites.replace_caddyfile
                    interrupted = False

                    def interrupt(path, content, **kwargs):
                        nonlocal interrupted
                        if path == source and not interrupted:
                            interrupted = True
                            if interrupt_after:
                                replace(path, content, **kwargs)
                            raise KeyboardInterrupt
                        return replace(path, content, **kwargs)

                    with mock.patch.object(caddy_sites, "replace_caddyfile", side_effect=interrupt):
                        result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))

                    self.assertTrue(interrupted)
                    self.assertEqual(result.exit_code, 130)
                    self.assertEqual(client_snapshot(box), before)
                    self.assert_site_link(box, "vless-tls", "old.example", 1443)
                    self.assertFalse(any(call[0] == "systemctl" for call in box.processes.calls[offset:]))

    def test_caddy_domain_change_preserves_an_explicit_public_port(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "8443",
            )
            self.seed_cli(
                box, "add-trojan-connection", "WS", "18101", "old.example", "--ws-path", "/ws",
                "--no-caddy", "--public-port", "9443",
            )
            for tag in ("vless-tls", "trojan-tls"):
                self.seed_cli(box, "add", "alice", "--connection", tag)

            directory = box.paths.caddy_conf_dir
            previous = directory / "old.example.caddy"
            previous.write_text("""old.example:8443 {
    @xhttp path /vless-xhttp*
    handle @xhttp {
        reverse_proxy h2c://127.0.0.1:18100
    }
    handle /ws {
        reverse_proxy 127.0.0.1:18101
    }
}
""")
            other = directory / "separate.caddy"
            other_config = "old.example:9443 {\n    reverse_proxy 127.0.0.1:18101\n}\n"
            other.write_text(other_config)
            before = snapshot(box)
            skipped = box.run_menu((*CADDY_SITES, "7", "1", "", "0", "0", "0", "0"))
            self.assertEqual((skipped.exit_code, skipped.stderr), (0, ""))
            self.assertIn("Домен не изменён.", skipped.stdout)
            self.assertEqual(snapshot(box), before)

            result = box.run_menu((*CADDY_SITES, "7", "1", "new.example", "0", "0", "0", "0"))

            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertIn("old.example:8443 -> new.example:8443", result.stdout)
            self.assertIn("new.example:8443 {", (directory / "new.example.caddy").read_text())
            self.assertFalse(previous.exists())
            self.assertEqual(other.read_text(), other_config)
            self.assert_site_link(box, "vless-tls", "new.example", 8443)
            self.assert_site_link(box, "trojan-tls", "old.example", 9443)
            self.assert_tls_diagnostics("new.example", 8443)

    def test_caddy_domain_change_refuses_an_ambiguous_site_address(self):
        with Sandbox() as box, running_xray(box):
            script_menu_processes(box)
            site = caddy_sites.write_site_config("old.example", 18100)
            site.write_text(site.read_text().replace("old.example {", "old.example, alias.example {", 1))
            before = snapshot(box)
            offset = len(box.processes.calls)

            result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))

            self.assertEqual(result.exit_code, 0)
            self.assertIn("Формат адреса Caddy site не поддерживается", result.stderr)
            self.assertEqual(snapshot(box), before)
            self.assertFalse(any(call[0] in ("caddy", "systemctl") for call in box.processes.calls[offset:]))

    def test_caddy_domain_change_restores_trojan_on_reload_failure_and_can_retry(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            configure_caddy_https_port(box, 2443)
            self.seed_cli(
                box, "add-trojan-connection", "WS", "18101", "old.example",
                "--ws-path", "/ws", "--install-caddy", "--public-port", "2443",
            )
            self.seed_cli(box, "add", "alice", "--connection", "trojan-tls")
            before = client_snapshot(box)
            old_site = box.paths.caddy_conf_dir / "old.example.caddy"
            new_site = box.paths.caddy_conf_dir / "new.example.caddy"
            reload = ("systemctl", "reload", "caddy")

            def reject_reload(*args, **kwargs):
                self.assertFalse(old_site.exists())
                self.assertIn("path /ws", new_site.read_text())
                self.assertIn("new.example", box.db_rows("reality_connections"))
                return box.processes.result(1, stderr="reload rejected")

            box.processes.script({reload: [reject_reload, box.processes.ok(), box.processes.ok()]})
            offset = len(box.processes.calls)
            answers = (*CADDY_SITES, "7", "new.example", "0", "0", "0", "0")
            failed = box.run_menu(answers)
            self.assertEqual(failed.exit_code, 0)
            self.assertIn("reload rejected", failed.stderr)
            self.assertNotIn("Caddy site domain changed", failed.stdout)
            self.assertEqual(client_snapshot(box), before)
            self.assertEqual(box.processes.calls[offset:].count(reload), 2)
            self.assert_site_link(box, "trojan-tls", "old.example", 2443)

            succeeded = box.run_menu(answers)
            self.assertEqual((succeeded.exit_code, succeeded.stderr), (0, ""))
            self.assertIn("VLESS/TLS/xHTTP и Trojan-WS", succeeded.stdout)
            self.assert_site_link(box, "trojan-tls", "new.example", 2443)
            self.assert_tls_diagnostics("new.example", 2443)
            self.assertEqual(box.processes.calls[offset:].count(reload), 3)
            self.assertNotIn(("systemctl", "restart", "xray"), box.processes.calls[offset:])
            self.assertEqual(new_site.read_text(), before["etc/caddy/conf.d/old.example.caddy"].replace(
                "old.example {", "new.example {", 1,
            ))

    def test_caddy_domain_change_rollback_preserves_site_recreated_by_admin(self):
        """140-R10: при сбое reload исходный путь может быть снова занят."""
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            configure_caddy_https_port(box, 1443)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            self.seed_cli(box, "add", "alice", "--connection", "vless-tls")
            directory = box.paths.caddy_conf_dir
            original = directory / "old.example.caddy"
            before = original.read_bytes()
            admin_edit = before.replace(b"old.example {", b"old.example {\n    header X-Admin recent", 1)
            db_before = box.db_rows("reality_connections")

            def reject_reload(*args, **kwargs):
                self.assertFalse(original.exists())
                original.write_bytes(admin_edit)
                return box.processes.result(1, stderr="reload rejected")

            box.processes.script({("systemctl", "reload", "caddy"): [reject_reload, box.processes.ok()]})
            result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))

            self.assertEqual(result.exit_code, 0)
            self.assertIn("reload rejected", result.stderr)
            self.assertIn("Сохранён файл, появившийся по прежнему пути Caddy site", result.stderr)
            self.assertEqual(original.read_bytes(), admin_edit)
            self.assertEqual(list(directory.glob("*.caddy")), [original])
            backup, = directory.glob("old.example.caddy.bak.*")
            self.assertEqual(backup.read_bytes(), before)
            self.assertEqual(box.db_rows("reality_connections"), db_before)
            self.assert_site_link(box, "vless-tls", "old.example", 1443)

    def test_caddy_domain_change_rollback_preserves_admin_edit_of_new_site(self):
        """140-R10: сохранение нового site без смены inode меняет ожидаемые байты."""
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            configure_caddy_https_port(box, 1443)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            self.seed_cli(box, "add", "alice", "--connection", "vless-tls")
            directory = box.paths.caddy_conf_dir
            original = directory / "old.example.caddy"
            target = directory / "new.example.caddy"
            before = original.read_bytes()
            admin_edit = before.replace(b"old.example {", b"new.example {\n    header X-Admin recent", 1)
            db_before = box.db_rows("reality_connections")

            def reject_reload(*args, **kwargs):
                inode = target.stat().st_ino
                target.write_bytes(admin_edit)
                self.assertEqual(target.stat().st_ino, inode)
                return box.processes.result(1, stderr="reload rejected")

            box.processes.script({("systemctl", "reload", "caddy"): [reject_reload, box.processes.ok()]})
            result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))

            self.assertEqual(result.exit_code, 0)
            self.assertIn("reload rejected", result.stderr)
            self.assertIn("Сохранена его текущая версия", result.stderr)
            self.assertEqual(original.read_bytes(), before)
            self.assertEqual(target.read_bytes(), admin_edit)
            self.assertEqual(box.db_rows("reality_connections"), db_before)
            self.assert_site_link(box, "vless-tls", "old.example", 1443)

    def test_caddy_domain_change_validates_candidate_before_working_writes(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            before = snapshot(box)

            def reject_candidate(call, **kwargs):
                self.assertTrue(is_caddy_candidate_adapt(call))
                shadow_site = Path(call[3]).parent / "conf.d" / "new.example.caddy"
                self.assertIn("new.example {", shadow_site.read_text())
                self.assertEqual(snapshot(box), before)
                return box.processes.result(1, stderr="candidate rejected")

            box.processes.script_program("caddy", reject_candidate)
            offset = len(box.processes.calls)
            result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))
            self.assertIn("candidate rejected", result.stderr)
            self.assertEqual(snapshot(box), before)
            self.assertFalse(any(call[0] == "systemctl" for call in box.processes.calls[offset:]))

    def test_caddy_domain_change_only_updates_records_served_by_selected_site(self):
        """140-R07: отдельный прокси на том же домене сохраняет свои ссылки."""
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            configure_caddy_https_port(box, 1443)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            self.seed_cli(
                box, "add-connection", "Second", "18101", "old.example", "--security", "tls",
                "--transport", "xhttp", "--public-port", "2443",
            )
            separate_proxy = box.root / "etc" / "Caddyfile.separate"
            proxy_text = "old.example:2443 {\n    reverse_proxy h2c://127.0.0.1:18101\n}\n"
            separate_proxy.write_text(proxy_text)
            for tag in ("vless-tls", "vless-tls-2"):
                self.seed_cli(box, "add", "alice", "--connection", tag)
            config = box.config()
            selected_inbound, = (inbound for inbound in config["inbounds"] if inbound.get("tag") == "vless-tls")
            selected_inbound["port"] = "18100"
            write_config(box, config)
            with closing(sqlite3.connect(box.paths.manager_db)) as conn, conn:
                conn.execute("UPDATE reality_connections SET extra_json = 'broken' WHERE tag = 'vless-reality'")
                conn.execute("""
                    INSERT INTO reality_connections(tag, name, port, sni, dest, fingerprint, extra_json)
                    SELECT 'vless-tls-orphan', 'Orphan', port, sni, dest, fingerprint, extra_json
                    FROM reality_connections WHERE tag = 'vless-tls'
                """)
            before = box.db_rows("reality_connections")
            self.assertEqual(before.count("old.example"), 6)
            result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))
            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            expected = "".join(
                line.replace("old.example", "new.example") if line.startswith('["vless-tls",') else line
                for line in before.splitlines(keepends=True)
            )
            self.assertEqual(box.db_rows("reality_connections"), expected)
            self.assertEqual(separate_proxy.read_text(), proxy_text)
            self.assert_site_link(box, "vless-tls", "new.example", 1443)
            self.assert_site_link(box, "vless-tls-2", "old.example", 2443)

    def test_caddy_domain_change_rejects_unverified_public_port_before_writes(self):
        """140-R07: общий upstream не подтверждает связь с внешним TLS-прокси."""
        for address in ("old.example", "old.example:8443"):
            with self.subTest(address=address), Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
                script_menu_processes(box)
                configure_caddy_https_port(box, 1443)
                self.seed_cli(
                    box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                    "--transport", "xhttp", "--install-caddy", "--public-port", "2443",
                )
                self.seed_cli(box, "add", "alice", "--connection", "vless-tls")
                site = box.paths.caddy_conf_dir / "old.example.caddy"
                site.write_text(site.read_text().replace("old.example {", f"{address} {{", 1))
                external = box.root / "etc" / "Caddyfile.external"
                proxy = "old.example:2443 {\n    reverse_proxy h2c://127.0.0.1:18100\n}\n"
                external.write_text(proxy)
                before = snapshot(box)
                db_before = box.paths.manager_db.read_bytes()
                offset = len(box.processes.calls)

                result = box.run_menu((*CADDY_SITES, "7", "new.other", "0", "0", "0", "0"))

                self.assertEqual(result.exit_code, 0)
                self.assertIn("Проверьте маршруты для подключений: vless-tls", result.stderr)
                self.assertEqual(snapshot(box), before)
                self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
                self.assertEqual(external.read_text(), proxy)
                self.assertFalse(any(call[0] == "systemctl" for call in box.processes.calls[offset:]))
                self.assert_site_link(box, "vless-tls", "old.example", 2443)

    def test_caddy_domain_change_checks_listener_address_for_public_link(self):
        """140-R07: публичный маршрут требует подтверждения привязки слушателя."""
        cases = (
            ("127.0.0.1", "192.0.2.10", False, False),
            ("[::1]", "192.0.2.10", False, False),
            ("[::ffff:127.0.0.1]", "192.0.2.10", False, False),
            ("localhost", "192.0.2.10", False, False),
            ("10.0.0.5", "192.0.2.11", False, False),
            ("192.0.2.10", "192.0.2.11", False, False),
            ("127.0.0.1", "192.0.2.10", True, False),
            ("127.0.0.1", "0.0.0.0", True, True),
            ("192.0.2.10", "edge-public.example", True, False),
            ("edge-public.example", "192.0.2.10", True, False),
        )
        for bind, alternate_bind, alternate_in_caddyfile, renamed in cases:
            with (
                self.subTest(bind=bind, alternate_bind=alternate_bind, imported=alternate_in_caddyfile),
                Sandbox() as box,
                running_xray(box),
                mock.patch("socket.socket"),
            ):
                script_menu_processes(box)
                self.seed_cli(
                    box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                    "--transport", "xhttp", "--install-caddy", "--public-port", "8443",
                )
                self.seed_cli(box, "add", "alice", "--connection", "vless-tls")
                directory = box.paths.caddy_conf_dir
                original = directory / "old.example.caddy"
                original.write_text(
                    original.read_text().replace("old.example {", f"old.example:8443 {{\n    bind {bind}", 1)
                )
                public_site = (
                    directory / "z-public.caddy" if alternate_in_caddyfile else box.root / "etc" / "Caddyfile.external"
                )
                public_config = (
                    f"old.example:8443 {{\n    bind {alternate_bind}\n    reverse_proxy h2c://127.0.0.1:18100\n}}\n"
                )
                public_site.write_text(public_config)
                selection = ("1",) if alternate_in_caddyfile else ()
                before = snapshot(box)
                db_before = box.paths.manager_db.read_bytes()
                offset = len(box.processes.calls)

                result = box.run_menu((*CADDY_SITES, "7", *selection, "new.other", "0", "0", "0", "0"))

                self.assertEqual(result.exit_code, 0)
                self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
                self.assertEqual(public_site.read_text(), public_config)
                self.assert_site_link(box, "vless-tls", "old.example", 8443)
                if renamed:
                    self.assertEqual(result.stderr, "")
                    self.assertFalse(original.exists())
                    self.assertTrue((directory / "new.other.caddy").is_file())
                else:
                    self.assertIn("Проверьте маршруты для подключений: vless-tls", result.stderr)
                    self.assertEqual(snapshot(box), before)
                    self.assertFalse(any(call[0] == "systemctl" for call in box.processes.calls[offset:]))

    def test_caddy_domain_change_checks_listener_for_new_connection_address(self):
        """140-R07: адрес новой ссылки должен подтверждаться привязкой выбранного site."""
        old_host = "192.0.2.10"
        new_ip = "192.0.2.20"
        for binding, new_host, renamed in (
            (old_host, new_ip, False),
            (old_host, "new.other", False),
            ("tcp6/[::]", new_ip, False),
            ("tcp4/0.0.0.0", new_ip, True),
            (f"{old_host} {new_ip}", new_ip, True),
        ):
            with (
                self.subTest(binding=binding, new_host=new_host),
                Sandbox() as box,
                running_xray(box),
                mock.patch("socket.socket"),
            ):
                script_menu_processes(box)
                self.seed_cli(
                    box, "add-connection", "TLS", "18100", old_host, "--security", "tls",
                    "--transport", "xhttp", "--install-caddy", "--public-port", "8443",
                )
                self.seed_cli(box, "add", "alice", "--connection", "vless-tls")
                directory = box.paths.caddy_conf_dir
                original = directory / f"{old_host}.caddy"
                original.write_text(
                    original.read_text().replace(f"{old_host} {{", f"{old_host}:8443 {{\n    bind {binding}", 1)
                )
                before = snapshot(box)
                db_before = box.paths.manager_db.read_bytes()
                offset = len(box.processes.calls)

                result = box.run_menu((*CADDY_SITES, "7", new_host, "0", "0", "0", "0"))

                self.assertEqual(result.exit_code, 0)
                if renamed:
                    self.assertEqual(result.stderr, "")
                    self.assertFalse(original.exists())
                    self.assertIn(f"bind {binding}", (directory / f"{new_host}.caddy").read_text())
                    self.assert_site_link(box, "vless-tls", new_host, 8443)
                else:
                    self.assertIn("Проверьте маршруты для подключений: vless-tls", result.stderr)
                    self.assertEqual(snapshot(box), before)
                    self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
                    self.assertFalse(any(call[0] == "systemctl" for call in box.processes.calls[offset:]))
                    self.assert_site_link(box, "vless-tls", old_host, 8443)

    def test_caddy_domain_change_uses_public_port_when_sites_share_an_upstream(self):
        """140-R07: разные Caddy listeners могут проксировать в один inbound."""
        cases = (
            ("old.example", 443, "new.other"),
            ("old.example", 2443, "old.example"),
            ("old.example", 3443, None),
            ("*.example", 2443, "old.example"),
            ("", 2443, "old.example"),
        )
        for host, port, expected_domain in cases:
            with self.subTest(host=host, port=port), Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
                script_menu_processes(box)
                config = box.config()
                reality, = (inbound for inbound in config["inbounds"] if inbound.get("tag") == "vless-reality")
                reality["port"] = 4443
                write_config(box, config)
                self.seed_cli(
                    box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                    "--transport", "xhttp", "--install-caddy", "--public-port", str(port),
                )
                self.seed_cli(box, "add", "alice", "--connection", "vless-tls")
                sites_dir = box.paths.caddy_conf_dir
                alternate = sites_dir / "z-2443.caddy"
                proxy = f"{host}:2443 {{\n    reverse_proxy h2c://127.0.0.1:18100\n}}\n"
                alternate.write_text(proxy)
                before = snapshot(box)
                offset = len(box.processes.calls)

                result = box.run_menu((*CADDY_SITES, "7", "1", "new.other", "0", "0", "0", "0"))

                self.assertEqual(result.exit_code, 0)
                self.assertEqual(alternate.read_text(), proxy)
                if expected_domain is None:
                    self.assertIn("Проверьте маршруты для подключений: vless-tls", result.stderr)
                    self.assertEqual(snapshot(box), before)
                    self.assertFalse(any(call[0] == "systemctl" for call in box.processes.calls[offset:]))
                else:
                    self.assertEqual(result.stderr, "")
                    self.assertTrue((sites_dir / "new.other.caddy").exists())
                    self.assert_site_link(box, "vless-tls", expected_domain, port)

    def test_caddy_domain_change_rejects_new_domain_on_another_listener(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            self.seed_cli(
                box, "add-trojan-connection", "WS", "18101", "old.example", "--ws-path", "/ws",
                "--no-caddy", "--public-port", "9443",
            )
            directory = box.paths.caddy_conf_dir
            (directory / "other-old.caddy").write_text(
                "old.example:9443 {\n    reverse_proxy 127.0.0.1:18101\n}\n",
            )
            (directory / "other-new.caddy").write_text(
                "new.example:9443 {\n    @alternate path /alternate\n"
                "    reverse_proxy @alternate 127.0.0.1:18101\n}\n",
            )
            before = snapshot(box)
            offset = len(box.processes.calls)

            result = box.run_menu((*CADDY_SITES, "7", "1", "new.example", "0", "0", "0", "0"))

            self.assertEqual(result.exit_code, 0)
            self.assertIn("Новый домен new.example используется в нескольких блоках Caddy", result.stderr)
            self.assertEqual(snapshot(box), before)
            self.assertFalse(any(call[0] == "systemctl" for call in box.processes.calls[offset:]))

    def test_caddy_domain_change_preserves_known_alternate_with_dynamic_upstream(self):
        """140-R12: динамический маршрут выбранного site не мешает сохранить доказанную отдельную ссылку."""
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "8443",
            )
            self.seed_cli(
                box, "add-trojan-connection", "WS", "18101", "old.example", "--ws-path", "/ws",
                "--no-caddy", "--public-port", "9443",
            )
            for tag in ("vless-tls", "trojan-tls"):
                self.seed_cli(box, "add", "alice", "--connection", tag)
            directory = box.paths.caddy_conf_dir
            original = directory / "old.example.caddy"
            selected_config = """old.example:8443 {
    handle /dynamic {
        reverse_proxy {
            dynamic a backend.example 18102
        }
    }
    handle {
        reverse_proxy h2c://127.0.0.1:18100
    }
}
"""
            original.write_text(selected_config)
            other = directory / "z-other.caddy"
            other_config = "old.example:9443 {\n    reverse_proxy 127.0.0.1:18101\n}\n"
            other.write_text(other_config)

            result = box.run_menu((*CADDY_SITES, "7", "1", "new.other", "0", "0", "0", "0"))

            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertFalse(original.exists())
            self.assertEqual(
                (directory / "new.other.caddy").read_text(),
                selected_config.replace("old.example:8443", "new.other:8443", 1),
            )
            self.assertEqual(other.read_text(), other_config)
            self.assert_site_link(box, "vless-tls", "new.other", 8443)
            self.assert_site_link(box, "trojan-tls", "old.example", 9443)

    def test_caddy_domain_change_rejects_unresolved_proxy_routes_before_writes(self):
        with Sandbox() as box, running_xray(box), mock.patch("socket.socket"):
            script_menu_processes(box)
            configure_caddy_https_port(box, 1443)
            self.seed_cli(
                box, "add-connection", "TLS", "18100", "old.example", "--security", "tls",
                "--transport", "xhttp", "--install-caddy", "--public-port", "1443",
            )
            self.seed_cli(
                box, "add-trojan-connection", "WS", "18101", "old.example", "--ws-path", "/ws",
                "--no-caddy", "--public-port", "2443",
            )
            site = box.paths.caddy_conf_dir / "old.example.caddy"
            site.write_text("""old.example {
    handle /ws {
        reverse_proxy {
            dynamic a backend.example 18101
        }
    }
    handle {
        reverse_proxy h2c://127.0.0.1:18100
    }
}
""")
            before = snapshot(box)
            offset = len(box.processes.calls)

            result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))

            self.assertEqual(result.exit_code, 0)
            self.assertIn("Проверьте маршруты для подключений: trojan-tls", result.stderr)
            self.assertEqual(snapshot(box), before)
            self.assertFalse(any(call[0] == "systemctl" for call in box.processes.calls[offset:]))

    def test_caddy_domain_change_renames_site_without_linked_connections(self):
        """Прямой Trojan с тем же доменом использует собственный сертификат."""
        with Sandbox() as box, running_xray(box):
            script_menu_processes(box)
            self.seed_cli(
                box, "add-trojan-connection", "Direct", "9443", "old.example", "/fixture/cert.pem", "/fixture/key.pem",
            )
            self.seed_cli(box, "add", "alice", "--connection", "trojan-tls")
            self.assert_site_link(box, "trojan-tls", "old.example", 9443)
            old_site = caddy_sites.write_site_config(
                "old.example", 18099, upstream_transport="http", route_path="/ws",
            )
            selected = old_site.with_name("custom-name.caddy")
            old_site.rename(selected)
            original = selected.read_text()
            db_before = box.paths.manager_db.read_bytes()
            config_before = box.paths.config.read_bytes()
            result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))
            self.assertEqual((result.exit_code, result.stderr), (0, ""))
            self.assertIn("Caddy site domain changed: old.example -> new.example", result.stdout)
            self.assertNotIn("перевыпустите", result.stdout)
            new_site, = box.paths.caddy_conf_dir.glob("*.caddy")
            self.assertEqual(new_site.name, "new.example.caddy")
            self.assertEqual(new_site.read_text(), original.replace("old.example {", "new.example {", 1))
            self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
            self.assertEqual(box.paths.config.read_bytes(), config_before)
            self.assert_site_link(box, "trojan-tls", "old.example", 9443)

    def test_caddy_domain_change_uses_restart_fallback_for_standalone_site(self):
        for restart_succeeds in (True, False):
            with self.subTest(restart_succeeds=restart_succeeds), Sandbox() as box, running_xray(box):
                script_menu_processes(box)
                old_site = caddy_sites.write_site_config("old.example", 18100)
                new_site = old_site.with_name("new.example.caddy")
                before = client_snapshot(box)
                db_before = box.paths.manager_db.read_bytes()
                attempts = []

                def reload(*args, **kwargs):
                    applying_new_site = new_site.is_file()
                    attempts.append(("reload", applying_new_site))
                    return box.processes.result(1 if applying_new_site else 0)

                def restart(*args, **kwargs):
                    applying_new_site = new_site.is_file()
                    attempts.append(("restart", applying_new_site))
                    return box.processes.ok() if restart_succeeds else box.processes.result(
                        1, stderr="restart rejected",
                    )

                box.processes.script({"systemctl reload caddy": reload, "systemctl restart caddy": restart})
                result = box.run_menu((*CADDY_SITES, "7", "new.example", "0", "0", "0", "0"))

                self.assertEqual(result.exit_code, 0)
                self.assertEqual(box.paths.manager_db.read_bytes(), db_before)
                if restart_succeeds:
                    self.assertEqual(attempts, [("reload", True), ("restart", True)])
                    self.assertEqual(result.stderr, "")
                    self.assertIn("Caddy site domain changed: old.example -> new.example", result.stdout)
                    self.assertTrue(new_site.is_file())
                    self.assertFalse(old_site.exists())
                else:
                    self.assertEqual(attempts, [("reload", True), ("restart", True), ("reload", False)])
                    self.assertIn("restart rejected", result.stderr)
                    self.assertEqual(client_snapshot(box), before)

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

            # Сценарий проверяет ручной ввод при отсутствии стандартных site roots.
            exists = Path.exists
            fallback_roots = set(box.paths.caddy_site_roots)

            def exists_in_fixture(path):
                return path not in fallback_roots and exists(path)

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
