"""Регрессия XR-REV-055 и XR-REV-019 (a): правила `client-route-*` существуют только при каскадном catch-all.

Клиентское правило стоит выше catch-all и отправляет клиента в каскад его страны (`selectedCascadeTag`).
При catch-all `warp-out` оно уводит клиента мимо WARP (репро P2_019, часть 1), без catch-all после
`xray-set-cascade --disable` — мимо direct (решение пользователя 2026-10-12). Запись client-DB таких правил
не возвращает; выбранные страны остаются в manager.db, и правила восстанавливаются, когда общим маршрутом
снова становится каскад: `xray-warp disable`, `xray-warp remove`, `xray-set-cascade use`.
"""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import test_client_manager_lock as client_lock_tests
from tests import test_routing_manager_lock as routing_lock_tests
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.commands import client as client_command
from xray_vps_manager.commands import set_cascade
from xray_vps_manager.commands import warp
from xray_vps_manager.db.storage import SQLiteReadUnavailable
from xray_vps_manager.xray import cascade as cascade_config
from xray_vps_manager.xray import client_routes
from xray_vps_manager.xray import outbound_links

VLESS_LINK = "vless://11111111-1111-1111-1111-111111111111@example.com:443?encryption=none&security=none&type=tcp#Example"
WARP_STALE_RULES_WARN = (
    "WARN: WARP включён, но в config.json остались клиентские правила client-route-* ({count}): "
    "трафик этих клиентов идёт мимо warp-out. Сними их командой xray-client sync-routes."
)
ROUTE_REFUSED_ERROR = (
    "ERROR: Выбор страны недоступен: общий маршрут сейчас не каскад "
    "(включён WARP или каскадный маршрут отключён). Выбери страну после возврата каскадного маршрута."
)


def base_config() -> dict:
    return {
        "inbounds": [{"tag": "vless-reality", "protocol": "vless", "settings": {"clients": []}}],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "blocked", "protocol": "blackhole"}],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "protocol": ["bittorrent"], "outboundTag": "blocked"},
            ],
        },
    }


def add_cascade(config: dict, name: str) -> None:
    tag = cascade_config.cascade_tag(name)
    set_cascade.configure_cascade(config, outbound_links.parse_vless_outbound(VLESS_LINK, tag)[0])


def client_entry(digit: str, name: str) -> dict:
    uuid = f"{digit * 8}-{digit * 4}-4{digit * 3}-8{digit * 3}-{digit * 12}"
    return {
        "id": uuid,
        "enabled": True,
        "connection": "vless-reality",
        "client": {"id": uuid, "email": f"{name}|created=2026-06-12T07:01:00Z"},
    }


def client_db(*names: str) -> dict:
    clients = {name: client_entry(str(index), name) for index, name in enumerate(names, start=1)}
    return {"clients": clients, "cascadeRoutes": {}}


def warp_outbound(endpoint_override=None) -> dict:
    return {"tag": warp.WARP_OUTBOUND_TAG, "protocol": "wireguard", "settings": {}}


def enable_warp(config: dict) -> None:
    """`xray-warp enable` без профиля wgcf и пробы endpoint."""
    with mock.patch.object(warp, "warp_outbound_from_profile", side_effect=warp_outbound):
        warp.enable_warp_route(config)


def client_outbound(config: dict, email: str) -> str:
    """Куда Xray отправит обычный tcp/udp-трафик клиента: первое совпавшее правило, иначе первый outbound."""
    balancers = {item["tag"]: item for item in config["routing"].get("balancers", [])}
    for rule in config["routing"]["rules"]:
        if email in rule.get("user", []):
            if rule.get("balancerTag"):
                return balancers[rule["balancerTag"]]["selector"][0]
            return rule["outboundTag"]
        if cascade_config.is_catchall_rule(rule):
            return rule["outboundTag"]
    return config["outbounds"][0]["tag"]


def client_outbounds(config: dict, db: dict) -> dict:
    return {
        name: client_outbound(config, client_routes.client_emails(name, entry)[0])
        for name, entry in db["clients"].items()
    }


def client_rule_tags(config: dict) -> list:
    return [
        rule["balancerTag"]
        for rule in config["routing"]["rules"]
        if client_routes.is_client_balancer_tag(rule.get("balancerTag"))
    ]


def selected_countries(db: dict) -> dict:
    return {name: entry.get("selectedCascadeTag") for name, entry in db["clients"].items()}


def clients_on_cascades() -> tuple:
    """Каскады de и nl, общий маршрут — nl (`xray-set-cascade use nl`), alice выбрала Германию."""
    config = base_config()
    db = client_db("alice", "bob")
    add_cascade(config, "de")
    add_cascade(config, "nl")
    set_cascade.set_active_cascade(config, "cascade-nl")
    client_routes.ensure_all_client_route_config(config, db)
    db["clients"]["alice"]["selectedCascadeTag"] = "cascade-de"
    client_routes.ensure_all_client_route_config(config, db)
    return config, db


def warp_status_lines(root: Path) -> list:
    """`xray-warp status` по config.json из root, без профиля wgcf."""
    stdout = StringIO()
    with mock.patch.object(warp, "CONFIG_PATH", root / "config.json"), \
        mock.patch.object(warp, "WGCF_BIN", root / "wgcf"), \
        mock.patch.object(warp, "WGCF_ACCOUNT", root / "wgcf-account.toml"), \
        mock.patch.object(warp, "WGCF_PROFILE", root / "wgcf-profile.conf"), \
        redirect_stdout(stdout):
        warp.print_status()
    return stdout.getvalue().splitlines()


def warp_config_with_stale_client_rules() -> tuple:
    """Конфиг прежней версии: после `xray-warp enable` запись client-DB вернула правила перед catch-all warp-out."""
    config, db = clients_on_cascades()
    config["outbounds"].append(warp_outbound())
    cascade_config.remove_managed_catchall_routes(config)
    cascade_config.append_catchall_route(config, warp.WARP_OUTBOUND_TAG)
    return config, db


class ClientRoutesUnderWarpTests(unittest.TestCase):
    def test_clients_start_on_selected_cascades(self) -> None:
        config, db = clients_on_cascades()

        self.assertEqual(client_outbounds(config, db), {"alice": "cascade-de", "bob": "cascade-nl"})
        self.assertEqual(len(client_rule_tags(config)), 2)

    def test_client_db_write_after_warp_enable_keeps_clients_on_warp(self) -> None:
        """Репро P2_019, часть 1: запись client-DB после `xray-warp enable` не возвращает клиентские правила."""
        config, db = clients_on_cascades()
        enable_warp(config)
        self.assertEqual(client_outbounds(config, db), {"alice": "warp-out", "bob": "warp-out"})

        # То же делает save_config_restart_xray_and_db при каждой записи client-DB (таймер, xray-client, xray-set-cascade add).
        changed = client_routes.ensure_all_client_route_config(config, db)

        self.assertFalse(changed)
        self.assertEqual(client_outbounds(config, db), {"alice": "warp-out", "bob": "warp-out"})
        self.assertEqual(client_rule_tags(config), [])
        self.assertEqual(config["routing"]["balancers"], [])
        self.assertEqual(selected_countries(db), {"alice": "cascade-de", "bob": "cascade-nl"})

    def test_client_db_write_removes_client_rules_left_under_warp(self) -> None:
        config, db = warp_config_with_stale_client_rules()
        self.assertEqual(client_outbounds(config, db), {"alice": "cascade-de", "bob": "cascade-nl"})

        self.assertTrue(client_routes.ensure_all_client_route_config(config, db))

        self.assertEqual(client_outbounds(config, db), {"alice": "warp-out", "bob": "warp-out"})
        self.assertEqual(client_rule_tags(config), [])
        self.assertEqual(config["routing"]["balancers"], [])
        self.assertEqual(selected_countries(db), {"alice": "cascade-de", "bob": "cascade-nl"})

    def test_country_choice_under_warp_does_not_route_client_past_warp(self) -> None:
        """`xray-client route` при WARP: правило клиента не создаётся, оставшееся от прежней версии снимается."""
        config, db = warp_config_with_stale_client_rules()
        alice = dict(db["clients"]["alice"], selectedCascadeTag="cascade-nl")

        self.assertTrue(client_routes.ensure_client_route_config(config, "alice", alice, "cascade-nl"))

        self.assertEqual(client_outbound(config, client_routes.client_emails("alice", alice)[0]), "warp-out")
        self.assertNotIn(client_routes.client_balancer_tag("alice", alice), client_rule_tags(config))
        self.assertEqual(client_outbounds(config, db)["bob"], "cascade-nl")
        self.assertEqual(alice["selectedCascadeTag"], "cascade-nl")

    def test_warp_disable_restores_client_routes_by_selected_country(self) -> None:
        """Репро P2_019, часть 2: `xray-warp disable` возвращает клиентов в каскады их стран."""
        config, db = clients_on_cascades()
        enable_warp(config)
        client_routes.ensure_all_client_route_config(config, db)

        self.assertTrue(warp.disable_warp_route(config, db))

        self.assertEqual(cascade_config.current_catchall_tag(config), "cascade-nl")
        self.assertEqual(client_outbounds(config, db), {"alice": "cascade-de", "bob": "cascade-nl"})

    def test_warp_remove_restores_client_routes_by_selected_country(self) -> None:
        config, db = clients_on_cascades()
        enable_warp(config)

        self.assertTrue(warp.remove_warp(config, db))

        self.assertNotIn(warp.WARP_OUTBOUND_TAG, [item["tag"] for item in config["outbounds"]])
        self.assertEqual(client_outbounds(config, db), {"alice": "cascade-de", "bob": "cascade-nl"})

    def test_client_added_under_warp_gets_active_cascade_after_disable(self) -> None:
        config, db = clients_on_cascades()
        enable_warp(config)
        db["clients"]["carol"] = client_entry("3", "carol")
        client_routes.ensure_all_client_route_config(config, db)
        self.assertEqual(db["clients"]["carol"].get("selectedCascadeTag", ""), "")

        warp.disable_warp_route(config, db)

        self.assertEqual(db["clients"]["carol"]["selectedCascadeTag"], "cascade-nl")
        self.assertEqual(client_outbounds(config, db), {"alice": "cascade-de", "bob": "cascade-nl", "carol": "cascade-nl"})

    def test_warp_disable_without_cascades_adds_no_client_rules(self) -> None:
        config = base_config()
        db = client_db("alice")
        enable_warp(config)

        warp.disable_warp_route(config, db)

        self.assertEqual(client_rule_tags(config), [])
        self.assertEqual(client_outbounds(config, db), {"alice": "direct"})


class ClientRoutesWithoutCatchallTests(unittest.TestCase):
    """Тот же механизм после `xray-set-cascade --disable`: каскадные outbounds остаются, catch-all нет."""

    def test_client_db_write_after_cascade_disable_keeps_default_direct(self) -> None:
        config, db = clients_on_cascades()
        set_cascade.disable_cascade(config)

        changed = client_routes.ensure_all_client_route_config(config, db)

        self.assertFalse(changed)
        self.assertEqual(client_outbounds(config, db), {"alice": "direct", "bob": "direct"})
        self.assertEqual(client_rule_tags(config), [])
        self.assertEqual(selected_countries(db), {"alice": "cascade-de", "bob": "cascade-nl"})

    def test_cascade_use_restores_client_routes_after_disable(self) -> None:
        config, db = clients_on_cascades()
        set_cascade.disable_cascade(config)
        client_routes.ensure_all_client_route_config(config, db)

        # Изменения config.json и client-DB из `xray-set-cascade use de`.
        set_cascade.set_active_cascade(config, "cascade-de")
        client_routes.ensure_all_client_route_config(config, db)

        self.assertEqual(client_outbounds(config, db), {"alice": "cascade-de", "bob": "cascade-nl"})


class WarpStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def status_lines(self, config: dict) -> list:
        (self.root / "config.json").write_text(json.dumps(config))
        return warp_status_lines(self.root)

    def test_status_warns_about_client_rules_under_warp(self) -> None:
        config, _db = warp_config_with_stale_client_rules()

        lines = self.status_lines(config)

        self.assertIn("enabled", [line.split(" : ", 1)[1] for line in lines if line.startswith("route ")])
        self.assertEqual(lines[-1], WARP_STALE_RULES_WARN.format(count=2))

    def test_status_has_no_warning_without_stale_client_rules(self) -> None:
        cases = {
            "WARP без клиентских правил": lambda config, db: (enable_warp(config), client_routes.ensure_all_client_route_config(config, db)),
            "каскад с клиентскими правилами": lambda config, db: None,
        }
        for title, prepare in cases.items():
            with self.subTest(title):
                config, db = clients_on_cascades()
                prepare(config, db)

                lines = self.status_lines(config)

                self.assertFalse([line for line in lines if line.startswith("WARN:")], lines)


class ClientDbWriteCommandTests(unittest.TestCase):
    """Те же сценарии командами: xray-warp, xray-set-cascade и запись client-DB командой xray-client."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # Каскады main (общий маршрут) и backup, bypass-ru, outbound warp-out без маршрута.
        routing_lock_tests.make_routing_sandbox(self.root)

    @contextmanager
    def commands(self):
        with routing_lock_tests.routing_patches(self.root), \
            client_lock_tests.sandbox_patches(self.root), \
            redirect_stdout(StringIO()):
            yield

    def add_client(self, name: str) -> None:
        client_command.cmd_add(name, prompt_for_access=False, connection_tag="vless-reality")

    def choose_country(self, name: str, tag: str) -> None:
        db = client_repository.load_db_sql()
        db["clients"][name]["selectedCascadeTag"] = tag
        client_repository.save_db(db)
        client_command.cmd_sync_routes()

    def stored(self) -> tuple:
        config = routing_lock_tests.stored_config(self.root)
        db = client_repository.load_db_sql(db_path=self.root / "manager.db")
        return config, db

    def test_warp_keeps_clients_on_warp_and_disable_restores_their_countries(self) -> None:
        with self.commands():
            self.add_client("alice")
            self.add_client("bob")
            self.choose_country("bob", "cascade-backup")
            warp.cmd_enable()
            # Запись client-DB при включённом WARP: новый клиент (минутный таймер пишет так же).
            self.add_client("carol")

        config, db = self.stored()
        self.assertEqual(client_outbounds(config, db), {"alice": "warp-out", "bob": "warp-out", "carol": "warp-out"})
        self.assertEqual(client_rule_tags(config), [])

        with self.commands():
            warp.cmd_disable()

        config, db = self.stored()
        self.assertEqual(cascade_config.current_catchall_tag(config), "cascade-main")
        self.assertEqual(client_outbounds(config, db), {"alice": "cascade-main", "bob": "cascade-backup", "carol": "cascade-main"})
        self.assertEqual(selected_countries(db), {"alice": "cascade-main", "bob": "cascade-backup", "carol": "cascade-main"})

    def test_cascade_disable_keeps_clients_direct_until_cascade_is_used_again(self) -> None:
        with self.commands():
            self.add_client("alice")
            self.add_client("bob")
            self.choose_country("bob", "cascade-backup")
            set_cascade.cmd_disable()
            self.add_client("carol")

        config, db = self.stored()
        self.assertEqual(client_outbounds(config, db), {"alice": "direct", "bob": "direct", "carol": "direct"})
        self.assertEqual(client_rule_tags(config), [])

        with self.commands():
            set_cascade.cmd_use(["backup"])

        config, db = self.stored()
        self.assertEqual(client_outbounds(config, db), {"alice": "cascade-main", "bob": "cascade-backup", "carol": "cascade-backup"})

    def test_warp_disable_without_client_db_still_disables_warp(self) -> None:
        """manager.db недоступна: WARP выключается, клиентские правила вернёт `xray-client sync-routes`."""
        with self.commands():
            self.add_client("alice")
            warp.cmd_enable()

        stderr = StringIO()
        missing = SQLiteReadUnavailable("SQLite manager database is missing.")
        with self.commands(), \
            mock.patch.object(client_repository, "load_db_sql", side_effect=missing), \
            redirect_stderr(stderr):
            warp.cmd_disable()

        config, db = self.stored()
        self.assertEqual(cascade_config.current_catchall_tag(config), "cascade-main")
        self.assertEqual(client_rule_tags(config), [])
        self.assertIn("WARN: client route DB sync skipped: SQLite manager database is missing.", stderr.getvalue().splitlines())

        with self.commands():
            client_command.cmd_sync_routes()

        config, db = self.stored()
        self.assertEqual(client_outbounds(config, db), {"alice": "cascade-main"})

    def leave_client_rule_under_warp(self) -> None:
        """config.json прежней версии: WARP включён, а правило клиента осталось перед catch-all warp-out."""
        config = routing_lock_tests.stored_config(self.root)
        cascade_config.remove_managed_catchall_routes(config)
        cascade_config.append_catchall_route(config, warp.WARP_OUTBOUND_TAG)
        (self.root / "config.json").write_text(json.dumps(config))

    def choose_country_refused(self, name: str, tag: str) -> tuple:
        """`xray-client route`, когда работающий Xray ещё держит balancer клиента: `api bo` прошёл бы успешно."""
        commands = []

        def xray_command(command, **_kwargs):
            commands.append(" ".join(str(item) for item in command[1:3]))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        stderr = StringIO()
        with self.commands(), \
            mock.patch.object(client_command, "run", side_effect=xray_command), \
            mock.patch.object(client_command, "run_capture", side_effect=xray_command), \
            redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                client_command.cmd_route(name, tag)
        return raised.exception.code, stderr.getvalue().splitlines(), commands

    def test_country_choice_under_warp_keeps_leftover_rule_for_sync_routes(self) -> None:
        """65-R01: `xray-client route` при WARP не трогает config.json, client-DB и работающий Xray.

        Правило прежней версии остаётся в config.json: `xray-warp status` о нём предупреждает,
        а `xray-client sync-routes` снимает его с перезапуском Xray.
        """
        with self.commands():
            self.add_client("alice")
        self.leave_client_rule_under_warp()
        before = (self.root / "config.json").read_text()

        code, stderr, commands = self.choose_country_refused("alice", "cascade-backup")

        self.assertEqual((code, stderr, commands), (1, [ROUTE_REFUSED_ERROR], []))
        self.assertEqual((self.root / "config.json").read_text(), before)
        self.assertEqual(selected_countries(self.stored()[1]), {"alice": "cascade-main"})
        self.assertEqual(warp_status_lines(self.root)[-1], WARP_STALE_RULES_WARN.format(count=1))

        restarts = []
        with routing_lock_tests.routing_patches(self.root), \
            client_lock_tests.sandbox_patches(self.root, restart=lambda: restarts.append("restart")), \
            redirect_stdout(StringIO()):
            client_command.cmd_sync_routes()

        config, db = self.stored()
        self.assertEqual(client_rule_tags(config), [])
        self.assertEqual(client_outbounds(config, db), {"alice": "warp-out"})
        self.assertEqual(restarts, ["restart"])

    def test_country_choice_after_cascade_disable_changes_nothing(self) -> None:
        with self.commands():
            self.add_client("alice")
            set_cascade.cmd_disable()
        before = (self.root / "config.json").read_text()

        code, stderr, commands = self.choose_country_refused("alice", "cascade-backup")

        self.assertEqual((code, stderr, commands), (1, [ROUTE_REFUSED_ERROR], []))
        self.assertEqual((self.root / "config.json").read_text(), before)
        self.assertEqual(selected_countries(self.stored()[1]), {"alice": "cascade-main"})


if __name__ == "__main__":
    unittest.main()
