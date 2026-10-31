"""Регрессия XR-REV-002 и XR-REV-054: `direct` — первый outbound, маршрут по умолчанию считается по фактическому порядку.

Xray отправляет трафик без совпавшего правила в первый outbound. Сценарии 1–6 повторяют репро P1_002
из ревью 2026-10; конфиги в старом порядке пересобираются первой же записью маршрутизации.
"""

import copy
from contextlib import redirect_stdout
from io import StringIO
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xray_vps_manager.commands import menu_activity_actions
from xray_vps_manager.commands import set_cascade
from xray_vps_manager.commands import warp
from xray_vps_manager.db import database
from xray_vps_manager.xray import bypass as bypass_config
from xray_vps_manager.xray import cascade as cascade_config
from xray_vps_manager.xray import outbound_links

VLESS_LINK = "vless://11111111-1111-1111-1111-111111111111@example.com:443?encryption=none&security=none&type=tcp#Example"
CASCADE_LINK = "vless://22222222-2222-2222-2222-222222222222@cascade.example.com:443?encryption=none&security=none&type=tcp#Cascade"


def install_config() -> dict:
    """Стартовый config.json из install.sh: direct, blocked и правила api/bittorrent/private без catch-all."""
    return {
        "inbounds": [
            {"tag": "api", "listen": "127.0.0.1", "port": 10085, "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1"}},
        ],
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "blocked", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                {"type": "field", "protocol": ["bittorrent"], "outboundTag": "blocked"},
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "blocked"},
            ],
        },
    }


def vless_outbound(tag: str) -> dict:
    """Каскады и bypass ведут на разные upstream-серверы, как обычно и бывает."""
    link = CASCADE_LINK if cascade_config.is_cascade_tag(tag) else VLESS_LINK
    return outbound_links.parse_vless_outbound(link, tag)[0]


def add_cascade(config: dict, name: str) -> None:
    set_cascade.configure_cascade(config, vless_outbound(cascade_config.cascade_tag(name)))


def add_bypass(config: dict, name: str = "ru", region: str = "RU") -> None:
    """Изменения config.json из `xray-set-bypass add NAME --region CODE`."""
    tag = bypass_config.bypass_tag(name)
    bypass_config.upsert_bypass_outbound(config, vless_outbound(tag))
    bypass_config.ensure_geoip_domain_strategy(config, {})
    bypass_config.apply_bypass_route(config, tag, region)


def enable_warp(config: dict) -> None:
    """Состояние после `xray-warp enable`: outbound warp-out и catch-all на него, без профиля wgcf."""
    config["outbounds"].append({"tag": warp.WARP_OUTBOUND_TAG, "protocol": "wireguard", "settings": {}})
    warp.remove_managed_catchall_routes(config)
    warp.append_catchall_route(config, warp.WARP_OUTBOUND_TAG)


def outbound_tags(config: dict) -> list:
    return [outbound.get("tag") for outbound in config["outbounds"]]


def effective_default(config: dict) -> str:
    """Куда Xray отправит трафик без совпавшего правила: catch-all правило, иначе первый outbound."""
    return cascade_config.current_catchall_tag(config) or config["outbounds"][0]["tag"]


def legacy_config(*, cascade: bool = True) -> dict:
    """Конфиг, записанный до исправления: bypass (и каскад) впереди direct, catch-all нет (`--disable`)."""
    config = install_config()
    config["outbounds"] = [
        vless_outbound("bypass-ru"),
        *([vless_outbound("cascade-de")] if cascade else []),
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "blocked", "protocol": "blackhole"},
        dict(vless_outbound("bypass-ru"), tag="geoip-warning-RU"),
    ]
    config["routing"]["domainStrategy"] = "IPOnDemand"
    config["routing"]["rules"].append({"type": "field", "ip": ["geoip:ru"], "outboundTag": "geoip-warning-RU"})
    return config


class DefaultOutboundTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # Синхронизация GeoIP warning в каскадных операциях читает bypass_routes: временная пустая база.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_patch = mock.patch.object(database, "MANAGER_DB_PATH", Path(tmp.name) / "manager.db")
        db_patch.start()
        self.addCleanup(db_patch.stop)

    def assertDirectDefault(self, config: dict) -> None:
        self.assertEqual(outbound_tags(config)[0], "direct", outbound_tags(config))
        self.assertEqual(effective_default(config), "direct")


class DirectFirstScenarioTests(DefaultOutboundTestCase):
    """Сценарии репро P1_002: после операций без catch-all правила трафик идёт через direct."""

    def test_cascade_add_keeps_direct_first_and_disable_returns_default_to_direct(self) -> None:
        """1. Каскад добавлен и отключён (XR-REV-002)."""
        config = install_config()

        add_cascade(config, "de")

        self.assertEqual(outbound_tags(config)[0], "direct")
        self.assertEqual(effective_default(config), "cascade-de")

        set_cascade.disable_cascade(config)

        self.assertDirectDefault(config)
        self.assertEqual(set_cascade.current_route_label(config), "default direct")

    def test_bypass_add_without_cascade_does_not_change_default(self) -> None:
        """2. `xray-set-bypass add` без каскада и WARP (XR-REV-054)."""
        config = install_config()

        add_bypass(config)

        self.assertDirectDefault(config)
        self.assertTrue(
            bypass_config.outbounds_equivalent(
                bypass_config.outbound_by_tag(config, "geoip-warning-RU"),
                bypass_config.outbound_by_tag(config, "bypass-ru"),
            )
        )

    def test_removing_only_cascade_with_bypass_returns_default_to_direct(self) -> None:
        """3. Каскад и bypass, затем удаление единственного каскада."""
        config = install_config()
        add_cascade(config, "de")
        add_bypass(config)
        self.assertEqual(effective_default(config), "cascade-de")

        set_cascade.remove_cascade(config, "cascade-de")

        self.assertDirectDefault(config)

    def test_cascade_disable_with_bypass_returns_default_to_direct(self) -> None:
        """4. Каскад и bypass, затем `xray-set-cascade --disable`."""
        config = install_config()
        add_cascade(config, "de")
        add_bypass(config)

        set_cascade.disable_cascade(config)

        self.assertDirectDefault(config)

    def test_warp_disable_without_cascade_returns_default_to_direct(self) -> None:
        """5. Только bypass, затем WARP enable → disable."""
        config = install_config()
        add_bypass(config)
        enable_warp(config)
        self.assertEqual(effective_default(config), warp.WARP_OUTBOUND_TAG)

        warp.disable_warp_route(config)

        self.assertDirectDefault(config)

    def test_bypass_disable_and_remove_keep_direct_default(self) -> None:
        """6. Отключение bypass с сохранением outbound и удаление bypass."""
        for remove_outbound in (False, True):
            with self.subTest(remove_outbound=remove_outbound):
                config = install_config()
                add_bypass(config)

                bypass_config.disable_bypass_route(config, "bypass-ru", "RU", remove_outbound=remove_outbound, env_values={})

                self.assertDirectDefault(config)

    def test_geoip_warning_follows_direct_after_cascade_disable(self) -> None:
        """GeoIP warning дублирует фактический маршрут: после отключения каскада — direct."""
        config = install_config()
        add_cascade(config, "de")
        bypass_config.ensure_geoip_warning_outbound(config, "CN", bypass_config.current_catchall_source_outbound(config))
        bypass_config.ensure_geoip_warning_rule(config, "CN")
        self.assertTrue(
            bypass_config.outbounds_equivalent(
                bypass_config.outbound_by_tag(config, "geoip-warning-CN"),
                bypass_config.outbound_by_tag(config, "cascade-de"),
            )
        )

        set_cascade.disable_cascade(config)

        self.assertDirectDefault(config)
        self.assertTrue(
            bypass_config.outbounds_equivalent(
                bypass_config.outbound_by_tag(config, "geoip-warning-CN"),
                bypass_config.outbound_by_tag(config, "direct"),
            )
        )


class ExistingConfigRebuildTests(DefaultOutboundTestCase):
    """Конфиг в старом порядке пересобирается первой же записью маршрутизации."""

    def test_routing_writes_put_direct_first(self) -> None:
        scenarios = {
            "xray-set-cascade add": lambda config: add_cascade(config, "nl"),
            "xray-set-cascade add (замена ссылки)": lambda config: add_cascade(config, "de"),
            "xray-set-cascade use": lambda config: set_cascade.set_active_cascade(config, "cascade-de"),
            "xray-set-cascade --disable": set_cascade.disable_cascade,
            "xray-set-cascade remove": lambda config: set_cascade.remove_cascade(config, "cascade-de"),
            "xray-set-bypass add (замена ссылки)": lambda config: add_bypass(config),
            "xray-set-bypass enable": lambda config: bypass_config.apply_bypass_route(config, "bypass-ru", "RU"),
            "xray-set-bypass disable": lambda config: bypass_config.disable_bypass_route(config, "bypass-ru", "RU", env_values={}),
            "xray-warp disable": warp.disable_warp_route,
        }
        for title, write in scenarios.items():
            with self.subTest(title):
                config = legacy_config()

                write(config)

                self.assertEqual(outbound_tags(config)[0], "direct", outbound_tags(config))
                self.assertEqual(outbound_tags(config).count("direct"), 1)

    def test_warp_disable_reports_change_when_only_order_is_rebuilt(self) -> None:
        """`xray-warp disable` без каскада записывает конфиг, если менялся только порядок outbounds."""
        config = legacy_config(cascade=False)

        self.assertTrue(warp.disable_warp_route(config))
        self.assertDirectDefault(config)
        self.assertFalse(warp.disable_warp_route(config))

    def test_bypass_replacement_keeps_position_after_direct(self) -> None:
        config = legacy_config()

        add_bypass(config)

        self.assertEqual(outbound_tags(config)[:2], ["direct", "bypass-ru"])


class ActualDefaultLabelTests(DefaultOutboundTestCase):
    """Статус и источник GeoIP warning считают маршрут по умолчанию по фактическому первому outbound."""

    def test_route_label_names_actual_first_outbound(self) -> None:
        config = legacy_config()

        self.assertEqual(set_cascade.current_route_label(config), "default bypass-ru")
        self.assertEqual(cascade_config.default_route_label(config), "default bypass-ru")
        self.assertEqual(cascade_config.default_route_label(install_config()), "default direct")

    def test_route_label_keeps_catchall_route(self) -> None:
        config = legacy_config()
        cascade_config.activate_cascade_route(config, "cascade-de")

        self.assertEqual(set_cascade.current_route_label(config), "cascade-de")

    def test_warning_source_is_actual_default_without_reordering(self) -> None:
        """Источник GeoIP warning не меняет путь трафика, даже если direct ещё не первый."""
        config = legacy_config()
        config["outbounds"].insert(0, config["outbounds"].pop(1))
        before = outbound_tags(config)
        self.assertEqual(before[0], "cascade-de")

        source = bypass_config.warning_source_outbound(config, "CN")

        self.assertEqual(source.get("tag"), "cascade-de")
        self.assertEqual(outbound_tags(config), before)

    def test_warning_source_never_copies_bypass_outbound(self) -> None:
        """64-R01: копия bypass-* выглядела бы маркером bypass и не удалялась бы при отключении GeoIP warning."""
        config = legacy_config()
        before = outbound_tags(config)

        source = bypass_config.warning_source_outbound(config, "CN")

        self.assertEqual(source.get("tag"), "direct")
        self.assertEqual(outbound_tags(config), before)

    def test_geoip_warning_on_legacy_default_can_be_disabled(self) -> None:
        """64-R01: GeoIP warning CN на старом конфиге отключается из меню целиком и не остаётся маркером bypass."""
        same_as_bypass = legacy_config()
        same_as_bypass["outbounds"][1] = dict(vless_outbound("bypass-ru"), tag="cascade-de")
        same_as_bypass["outbounds"].insert(0, same_as_bypass["outbounds"].pop(1))
        scenarios = {
            "первый bypass-ru": legacy_config(),
            "первый cascade-de с настройками bypass-ru": same_as_bypass,
        }
        for title, config in scenarios.items():
            with self.subTest(title):
                menu_activity_actions.apply_xray_geoip_warning_config(config, "CN")
                self.assertEqual(bypass_config.configured_bypass_for_warning(config, "CN"), "")

                # Ветка отключения в menu_activity_actions.disable_xray_geoip_routing_region.
                if not bypass_config.configured_bypass_for_warning(config, "CN"):
                    bypass_config.remove_geoip_warning_route(config, "CN")

                self.assertIsNone(bypass_config.outbound_by_tag(config, "geoip-warning-CN"))
                self.assertFalse(any(rule.get("outboundTag") == "geoip-warning-CN" for rule in config["routing"]["rules"]))
                self.assertEqual(bypass_config.configured_bypass_for_warning(config, "RU"), "bypass-ru")

    def test_warning_source_creates_direct_when_outbounds_are_empty(self) -> None:
        config = {"routing": {"rules": []}}

        source = bypass_config.current_catchall_source_outbound(config)

        self.assertEqual(source, {"tag": "direct", "protocol": "freedom"})
        self.assertEqual(outbound_tags(config), ["direct"])


class WarpProbeOrderTests(unittest.TestCase):
    """64-R02: временный конфиг проб WARP не меняет маршрут остальных клиентов."""

    def test_probes_keep_live_outbound_order(self) -> None:
        from tests import test_routing_manager_lock as routing_lock_tests

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        routing_lock_tests.make_routing_sandbox(root)
        live = legacy_config()
        (root / "config.json").write_text(json.dumps(live))
        probe_configs = []

        def restart() -> None:
            config = json.loads((root / "config.json").read_text())
            if any(inbound.get("tag") == warp.TEST_INBOUND_TAG for inbound in config.get("inbounds", [])):
                probe_configs.append(config)

        probes = {
            "probe_warp_endpoint": lambda: warp.probe_warp_endpoint(routing_lock_tests.WARP_ENDPOINT),
            "xray-warp test": warp.test_warp,
        }
        for title, probe in probes.items():
            with self.subTest(title):
                probe_configs.clear()
                with routing_lock_tests.routing_patches(root, restart=restart), redirect_stdout(StringIO()):
                    probe()

                self.assertEqual(len(probe_configs), 1)
                config = probe_configs[0]
                self.assertEqual(outbound_tags(config), [*outbound_tags(live), warp.WARP_OUTBOUND_TAG])
                self.assertEqual(effective_default(config), "bypass-ru")
                test_rule = config["routing"]["rules"][0]
                self.assertEqual(test_rule["inboundTag"], [warp.TEST_INBOUND_TAG])
                self.assertEqual(test_rule["outboundTag"], warp.WARP_OUTBOUND_TAG)
                self.assertEqual(json.loads((root / "config.json").read_text()), live)


class EnsureDirectFirstTests(unittest.TestCase):
    def test_creates_direct_at_index_zero(self) -> None:
        config = {"outbounds": [{"tag": "cascade-de", "protocol": "vless"}]}

        self.assertTrue(cascade_config.ensure_direct_first(config))
        self.assertEqual(outbound_tags(config), ["direct", "cascade-de"])
        self.assertFalse(cascade_config.ensure_direct_first(config))

    def test_moves_existing_direct_without_copying_it(self) -> None:
        direct = {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIPv4"}}
        config = {"outbounds": [{"tag": "bypass-ru"}, direct, {"tag": "blocked"}]}

        self.assertIs(cascade_config.ensure_direct_outbound(config), direct)
        self.assertEqual(outbound_tags(config), ["direct", "bypass-ru", "blocked"])

    def test_base_outbounds_keep_direct_first(self) -> None:
        config = {"outbounds": [{"tag": "warp-out"}]}

        cascade_config.ensure_base_outbounds(config)

        self.assertEqual(outbound_tags(config), ["direct", "warp-out", "blocked"])


class ActiveCascadeOrderTests(DefaultOutboundTestCase):
    """Активный каскад — первый среди `cascade-*` (решение VIREI-64, до VIREI-230), `direct` — первый outbound."""

    def test_use_moves_cascade_first_among_cascades_only(self) -> None:
        config = install_config()
        add_cascade(config, "main")
        add_cascade(config, "backup")

        set_cascade.set_active_cascade(config, "cascade-backup")

        self.assertEqual(outbound_tags(config)[0], "direct")
        self.assertEqual(cascade_config.cascade_tags(config), ["cascade-backup", "cascade-main"])

    def test_warp_disable_restores_last_selected_cascade(self) -> None:
        config = install_config()
        add_cascade(config, "main")
        add_cascade(config, "backup")
        set_cascade.set_active_cascade(config, "cascade-backup")
        enable_warp(config)

        warp.disable_warp_route(config)

        self.assertEqual(cascade_config.active_cascade_tag(config), "cascade-backup")
        self.assertEqual(outbound_tags(config)[0], "direct")

    def test_removing_active_cascade_selects_previous_one(self) -> None:
        config = install_config()
        for name in ("main", "backup", "nl"):
            add_cascade(config, name)
        set_cascade.set_active_cascade(config, "cascade-nl")
        set_cascade.set_active_cascade(config, "cascade-backup")

        set_cascade.remove_cascade(config, "cascade-backup")

        self.assertEqual(cascade_config.active_cascade_tag(config), "cascade-nl")
        self.assertEqual(outbound_tags(config)[0], "direct")

    def test_use_on_single_cascade_keeps_outbounds(self) -> None:
        config = install_config()
        add_cascade(config, "de")
        before = copy.deepcopy(config["outbounds"])

        set_cascade.set_active_cascade(config, "cascade-de")

        self.assertEqual(config["outbounds"], before)


if __name__ == "__main__":
    unittest.main()
