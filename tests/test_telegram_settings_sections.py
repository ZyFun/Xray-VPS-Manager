"""Регрессия XR-REV-058: настройки Telegram пишут только изменённые секции.

Poller, admin-панель и уведомления пишут подписки и state через save_db_sections без manager.lock.
Если между load и save настройки другой процесс записал подписку, ключ напоминания или state,
сохранение настройки не должно их стирать.
"""

import ast
from contextlib import contextmanager, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_telegram_manager_lock as lock_tests
from xray_vps_manager.commands import telegram as telegram_command
from xray_vps_manager.telegram import settings as telegram_settings
from xray_vps_manager.telegram import setup as telegram_setup

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "xray_vps_manager"
REAL_LOAD_DB_SQL = telegram_settings.load_db_sql
POLLER_SECTIONS = ("clientSubscriptions", "clientSubscriptionState", "adminState", "dailySummaryState", "geoipState")


class PollerBetweenLoadAndSave:
    """После каждого чтения настроек «poller» записывает свои секции — до того, как настройка сохранится."""

    def __init__(self) -> None:
        self.count = 0

    def load_db_sql(self, *args, **kwargs):
        snapshot = REAL_LOAD_DB_SQL(*args, **kwargs)
        self.inject()
        return snapshot

    def inject(self) -> None:
        self.count += 1
        marker = f"poller-{self.count}"
        current = REAL_LOAD_DB_SQL()
        current["clientSubscriptions"][f"70{self.count}"] = {
            "client": "",
            "clientId": f"00000000-0000-4000-8000-00000000000{self.count}",
            "chatLabel": f"@user{self.count}",
            "subscribedAt": "2026-10-15T08:00:00Z",
            "enabled": True,
        }
        current["clientSubscriptionState"]["expiryReminders"][f"reminder-{self.count}"] = "2026-10-15T08:00:00Z"
        current["adminState"][marker] = self.count
        current["dailySummaryState"][marker] = self.count
        current["geoipState"][marker] = self.count
        telegram_settings.save_db_sections(current, POLLER_SECTIONS)

    def chats(self) -> set:
        return {f"70{index}" for index in range(1, self.count + 1)}

    def reminders(self) -> set:
        return {f"reminder-{index}" for index in range(1, self.count + 1)}

    def markers(self) -> set:
        return {f"poller-{index}" for index in range(1, self.count + 1)}


class TelegramSettingsSectionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        lock_tests.make_telegram_sandbox(self.root)
        telegram_settings.save_db_sections(
            {"paymentTransferMethod": "phone", "paymentPhone": "+79990000000", "paymentBank": "Bank"},
            ("paymentTransferMethod", "paymentPhone", "paymentBank"),
            db_path=self.root / "manager.db",
        )
        self.poller = PollerBetweenLoadAndSave()

    @contextmanager
    def patches(self, **kwargs):
        with lock_tests.telegram_patches(self.root, **kwargs) as sent, \
            mock.patch.object(telegram_settings, "load_db_sql", side_effect=self.poller.load_db_sql), \
            redirect_stdout(StringIO()):
            yield sent

    def stored(self) -> dict:
        return lock_tests.stored_settings(self.root)

    def assert_poller_writes_kept(self, *, geoip_reset: bool = False) -> dict:
        self.assertGreater(self.poller.count, 0)
        stored = self.stored()
        self.assertEqual(set(stored["clientSubscriptions"]), self.poller.chats())
        self.assertTrue(self.poller.reminders() <= set(stored["clientSubscriptionState"]["expiryReminders"]))
        self.assertTrue(self.poller.markers() <= set(stored["adminState"]))
        self.assertTrue(self.poller.markers() <= set(stored["dailySummaryState"]))
        if geoip_reset:
            # Законная перезапись своей секции: initialize_geoip_offsets задаёт новую точку отсчёта GeoIP целиком.
            self.assertEqual(stored["geoipState"]["sentIds"], [])
            self.assertIn("sqliteLastEventId", stored["geoipState"])
        else:
            self.assertTrue(self.poller.markers() <= set(stored["geoipState"]))
        return stored

    def test_set_bot_name_keeps_poller_writes(self) -> None:
        with self.patches():
            telegram_setup.set_bot_name("Helper")

        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["botName"], "Helper")

    def test_set_payment_amount_keeps_poller_writes(self) -> None:
        with self.patches():
            telegram_command.set_payment_amount("1500 $")

        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["paymentTotalAmount"], "1500")
        self.assertEqual(stored["paymentCurrency"], "$")
        self.assertEqual(stored["paymentAmount"], "1500 $")

    def test_set_domain_annual_amount_keeps_poller_writes(self) -> None:
        with self.patches():
            telegram_command.set_domain_annual_amount("1200 €")

        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["paymentDomainAnnualAmount"], "1200")
        self.assertEqual(stored["paymentCurrency"], "€")

    def test_set_payment_rounding_keeps_poller_writes(self) -> None:
        with self.patches():
            telegram_command.set_payment_rounding("step", "50")

        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["paymentRoundingMode"], "step")
        self.assertEqual(stored["paymentRoundingStep"], "50")

    def test_set_payment_rounding_none_keeps_step(self) -> None:
        telegram_settings.save_db_sections(
            {"paymentRoundingMode": "step", "paymentRoundingStep": "50"},
            ("paymentRoundingMode", "paymentRoundingStep"),
            db_path=self.root / "manager.db",
        )
        with self.patches():
            telegram_command.set_payment_rounding("none")

        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["paymentRoundingMode"], "none")
        self.assertEqual(stored["paymentRoundingStep"], "50")

    def test_set_payment_details_keeps_poller_writes_and_clears_previous_method(self) -> None:
        with self.patches():
            telegram_command.set_payment_details("card", "2200 0000 0000 0000")

        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["paymentTransferMethod"], "card")
        self.assertEqual(stored["paymentCard"], "2200 0000 0000 0000")
        # Смена способа оплаты очищает реквизиты прежнего способа — эти ключи тоже должны записаться.
        self.assertEqual(stored["paymentPhone"], "")
        self.assertEqual(stored["paymentBank"], "")

    def test_set_enabled_keeps_poller_writes_and_resets_geoip_offsets(self) -> None:
        telegram_settings.save_db_sections({"enabled": False}, ("enabled",), db_path=self.root / "manager.db")
        with self.patches():
            telegram_setup.set_enabled(True)

        stored = self.assert_poller_writes_kept(geoip_reset=True)
        self.assertTrue(stored["enabled"])

    def test_set_disabled_keeps_poller_writes_and_geoip_state(self) -> None:
        with self.patches():
            telegram_setup.set_enabled(False)

        stored = self.assert_poller_writes_kept()
        self.assertFalse(stored["enabled"])

    def test_set_route_mode_direct_keeps_poller_writes(self) -> None:
        telegram_settings.save_db_sections({"routeMode": "cascade"}, ("routeMode",), db_path=self.root / "manager.db")
        with self.patches():
            telegram_setup.set_route_mode("direct")

        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["routeMode"], "direct")

    def test_set_route_mode_cascade_keeps_poller_writes_made_during_restart(self) -> None:
        def apply_config(_config):
            self.poller.inject()
            return "config.json.bak"

        with self.patches(), \
            mock.patch.object(telegram_setup, "apply_config", side_effect=apply_config), \
            mock.patch.object(telegram_setup, "wait_for_tcp", return_value=True):
            telegram_setup.set_route_mode("cascade")

        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["routeMode"], "cascade")

    def test_set_route_mode_cascade_rollback_keeps_poller_writes(self) -> None:
        def apply_config(_config):
            self.poller.inject()
            raise RuntimeError("New config failed.")

        with self.patches(), mock.patch.object(telegram_setup, "apply_config", side_effect=apply_config):
            with self.assertRaises(RuntimeError):
                telegram_setup.set_route_mode("cascade")

        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["routeMode"], "direct")

    def test_ask_token_if_missing_keeps_poller_writes(self) -> None:
        with self.patches(), mock.patch("builtins.input", return_value="654321:new-token"):
            db = telegram_setup.ask_token_if_missing({"token": ""})

        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["token"], "654321:new-token")
        self.assertEqual(db["token"], "654321:new-token")

    def test_adopt_existing_cascade_route_keeps_poller_writes(self) -> None:
        config = json.loads((self.root / "config.json").read_text())
        telegram_setup.ensure_telegram_proxy_config(config)
        (self.root / "config.json").write_text(json.dumps(config))

        with self.patches(), mock.patch("builtins.input", return_value="y"):
            telegram_setup.maybe_adopt_existing_cascade_route(self.stored())

        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["routeMode"], "cascade")

    def test_configure_owner_keeps_poller_writes(self) -> None:
        updates = [lock_tests.message_update("555", 41, "/start")]
        with self.patches(updates=updates), \
            mock.patch.object(telegram_setup, "choose_private_chat", return_value={"id": "555", "label": "@new-owner"}), \
            mock.patch.object(telegram_setup, "configure_bot_commands"):
            telegram_setup.configure_owner(send_test=False)

        stored = self.assert_poller_writes_kept(geoip_reset=True)
        self.assertEqual(stored["chatId"], "555")
        self.assertEqual(stored["chatLabel"], "@new-owner")
        self.assertEqual(stored["botUsername"], "ExampleVpnBot")
        self.assertTrue(stored["enabled"])
        self.assertEqual(stored["clientSubscriptionState"]["userUpdateOffset"], 42)

    def test_setup_keeps_poller_writes(self) -> None:
        answers = {"BOT_TOKEN: ": "654321:new-token", "BOT_NAME [Vireika]: ": "Helper", "Route mode [1-direct]: ": "1"}
        with self.patches(), \
            mock.patch("builtins.input", side_effect=lambda prompt: answers[prompt]), \
            mock.patch.object(telegram_setup, "set_route_mode") as set_route_mode, \
            mock.patch.object(telegram_setup, "configure_owner") as configure_owner:
            telegram_setup.setup()

        set_route_mode.assert_called_once_with("direct")
        configure_owner.assert_called_once_with(send_test=True)
        stored = self.assert_poller_writes_kept()
        self.assertEqual(stored["token"], "654321:new-token")
        self.assertEqual(stored["botName"], "Helper")


def full_save_calls(path: Path) -> list:
    """Вызовы полной записи настроек Telegram: settings.save_db(...) и обёртка save_db(...) в commands/telegram.py."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "save_db":
            found.append(f"save_db:{node.lineno}")
        elif isinstance(func, ast.Attribute) and func.attr == "save_db" and not (
            isinstance(func.value, ast.Name) and func.value.id == "client_repository"
        ):
            found.append(f"{ast.unparse(func)}:{node.lineno}")
    return found


class TelegramFullSaveSourceTests(unittest.TestCase):
    def test_setup_and_commands_do_not_use_full_settings_save(self) -> None:
        for path in (PACKAGE_DIR / "telegram" / "setup.py", PACKAGE_DIR / "commands" / "telegram.py"):
            with self.subTest(path.name):
                self.assertEqual(full_save_calls(path), [])

    def test_no_full_settings_save_in_package(self) -> None:
        """Аналог `grep "settings.save_db("`: полный снимок настроек Telegram в пакете никто не пишет."""
        found = []
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "settings.save_db(" in line:
                    found.append(f"{path.relative_to(PACKAGE_DIR)}:{number}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
