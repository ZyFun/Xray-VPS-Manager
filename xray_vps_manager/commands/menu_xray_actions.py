"""Xray service actions used by the interactive menu."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from xray_vps_manager.core.paths import CONFIG_PATH, paths
from xray_vps_manager.core.systemd import MANAGER_UNITS
from xray_vps_manager.core.terminal import green, red
from xray_vps_manager.commands.presenters import apply_error_text, backup_text
from xray_vps_manager.core.errors import ApplyConfigError
from xray_vps_manager.routing import torrent as torrent_service
from xray_vps_manager.xray import apply as xray_apply
from xray_vps_manager.xray import caddy
from xray_vps_manager.xray.torrent import torrent_block_enabled

CLIENT_LINK_PATH = Path("/root/xray-reality-client.txt")

CommandRunner = Callable[[list[str]], None]
ConfirmCallback = Callable[[str], bool]


def die(message: str) -> None:
    raise SystemExit(message)


def run(command: list[str], **kwargs) -> None:
    subprocess.run(command, check=True, **kwargs)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        die(f"Config not found: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text())


def apply_config(config: dict) -> Path | None:
    try:
        return xray_apply.apply_config(config).backup
    except ApplyConfigError as exc:
        if exc.exit_code == 130:
            raise KeyboardInterrupt from exc
        die(apply_error_text(exc))


def print_torrent_status() -> None:
    config = load_config()
    if torrent_block_enabled(config):
        print(f"Торренты: {green('запрещены')}")
    else:
        print(f"Торренты: {red('разрешены')}")
        print("Рекомендуемое состояние для сервера: запрещены.")


def set_torrent_block(blocked: bool) -> None:
    try:
        result = torrent_service.set_blocked(blocked)
    except ApplyConfigError as exc:
        if exc.exit_code == 130:
            raise KeyboardInterrupt from exc
        die(apply_error_text(exc))
    print_torrent_status()
    if not result.changed:
        print("Изменения не требуются.")
    else:
        print(f"Backup: {backup_text(result.backup)}")


def show_xray_status(call: CommandRunner) -> None:
    call(["systemctl", "status", "xray", "--no-pager"])


def restart_xray(call: CommandRunner) -> None:
    call(["systemctl", "restart", "xray"])
    call(["systemctl", "is-active", "xray"])


def check_config(call: CommandRunner) -> None:
    call(xray_apply.config_test_command(CONFIG_PATH))


def check_timers(call: CommandRunner) -> None:
    configs = caddy.list_random_tls_configs(paths().caddy_random_tls_dir)
    instances = dict.fromkeys(caddy.random_tls_timer_instance(config.domain) for config in configs)
    call(["systemctl", "status", *MANAGER_UNITS, *instances, "--no-pager"])


def run_all_tests(call: CommandRunner) -> None:
    call(["xray-test"])


def print_initial_link() -> None:
    if CLIENT_LINK_PATH.exists():
        print(CLIENT_LINK_PATH.read_text())
    else:
        print("Файл /root/xray-reality-client.txt не найден. Можно вывести ссылку через xray-client link NAME.")


def show_cascades(call: CommandRunner) -> None:
    call(["xray-set-cascade", "list"])


def add_or_replace_cascade(call: CommandRunner) -> None:
    call(["xray-set-cascade", "add"])


def select_cascade(call: CommandRunner) -> None:
    call(["xray-set-cascade", "use"])


def test_cascade(call: CommandRunner) -> None:
    call(["xray-set-cascade", "--test"])


def test_selected_cascade(call: CommandRunner) -> None:
    call(["xray-set-cascade", "test-select"])


def remove_cascade(call: CommandRunner) -> None:
    call(["xray-set-cascade", "remove"])


def disable_cascade(call: CommandRunner) -> None:
    call(["xray-set-cascade", "--disable"])


def check_update(call: CommandRunner) -> None:
    call(["xray-update", "--check"])


def test_latest(call: CommandRunner) -> None:
    call(["xray-update", "--test-latest"])


def update_xray(call: CommandRunner) -> None:
    call(["xray-update", "--update"])


def show_update_backups(call: CommandRunner) -> None:
    call(["xray-update", "--backups"])


def rollback_xray(call: CommandRunner, confirm: ConfirmCallback) -> None:
    if confirm("Откатить Xray к последней сохранённой предыдущей версии?"):
        call(["xray-update", "--rollback"])
    else:
        print("Откат отменён.")


def update_assets(call: CommandRunner, source: str) -> None:
    call(["xray-update", "--update-assets", source])


def sqlite_status(call: CommandRunner) -> None:
    call(["xray-vps-manager", "sqlite", "status"])


def check_manager_update(call: CommandRunner) -> None:
    call(["xray-manager-update", "--check"])


def update_manager(call: CommandRunner) -> None:
    call(["xray-manager-update", "--update"])


def update_manager_tag(call: CommandRunner) -> None:
    tag = input("Тег релиза, например v1.0.1: ").strip()
    if not tag:
        print("Тег не указан.")
        return
    call(["xray-manager-update", "--update", tag])


def show_manager_update_backups(call: CommandRunner) -> None:
    call(["xray-manager-update", "--backups"])


def rollback_manager(call: CommandRunner, confirm: ConfirmCallback) -> None:
    if confirm("Откатить Xray VPS Manager к последнему backup?"):
        call(["xray-manager-update", "--rollback"])
    else:
        print("Откат отменён.")


def warp_status(call: CommandRunner) -> None:
    call(["xray-warp", "status"])


def create_warp_outbound(call: CommandRunner) -> None:
    call(["xray-warp", "create"])


def recreate_warp_profile(call: CommandRunner, confirm: ConfirmCallback) -> None:
    print("Будет создан новый WARP-аккаунт и новый WireGuard profile.")
    print("Старые файлы wgcf-account.toml и wgcf-profile.conf будут заменены.")
    if not confirm("Пересоздать WARP профиль"):
        print("Действие отменено.")
        return
    call(["xray-warp", "create", "--force"])


def enable_warp(call: CommandRunner) -> None:
    call(["xray-warp", "enable"])


def disable_warp(call: CommandRunner) -> None:
    call(["xray-warp", "disable"])


def test_warp(call: CommandRunner) -> None:
    call(["xray-warp", "test"])


def remove_warp(call: CommandRunner) -> None:
    call(["xray-warp", "remove"])


def verify_warp_disabled(call: CommandRunner) -> None:
    call(["xray-warp", "verify-disabled"])


def block_torrents() -> None:
    set_torrent_block(True)


def allow_torrents() -> None:
    set_torrent_block(False)
