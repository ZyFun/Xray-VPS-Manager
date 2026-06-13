"""Xray service actions used by the interactive menu."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from xray_vps_manager.core.paths import CONFIG_PATH
from xray_vps_manager.core.terminal import green, red

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


def write_config(config: dict) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    backup = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.bak.{timestamp}")
    shutil.copy2(CONFIG_PATH, backup)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(CONFIG_PATH)
    return backup


def apply_config(config: dict) -> Path:
    backup = write_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")
    return backup


def ensure_blocked_outbound(config: dict) -> bool:
    outbounds = config.setdefault("outbounds", [])
    if not any(outbound.get("tag") == "blocked" for outbound in outbounds):
        outbounds.append({"tag": "blocked", "protocol": "blackhole"})
        return True
    return False


def routing_rules(config: dict) -> list[dict]:
    routing = config.setdefault("routing", {})
    routing.setdefault("domainStrategy", "IPIfNonMatch")
    return routing.setdefault("rules", [])


def rule_values(rule: dict, key: str) -> list:
    value = rule.get(key, [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return []


def is_api_rule(rule: dict) -> bool:
    return rule.get("outboundTag") == "api" or "api" in rule_values(rule, "inboundTag")


def is_bittorrent_rule(rule: dict) -> bool:
    return "bittorrent" in rule_values(rule, "protocol")


def torrent_block_rule() -> dict:
    return {
        "type": "field",
        "protocol": ["bittorrent"],
        "outboundTag": "blocked",
    }


def torrent_block_enabled(config: dict) -> bool:
    return any(rule.get("outboundTag") == "blocked" and is_bittorrent_rule(rule) for rule in routing_rules(config))


def print_torrent_status() -> None:
    config = load_config()
    if torrent_block_enabled(config):
        print(f"Торренты: {green('запрещены')}")
    else:
        print(f"Торренты: {red('разрешены')}")
        print("Рекомендуемое состояние для сервера: запрещены.")


def insert_torrent_rule(rules: list[dict]) -> None:
    insert_index = 0
    while insert_index < len(rules) and is_api_rule(rules[insert_index]):
        insert_index += 1
    rules.insert(insert_index, torrent_block_rule())


def set_torrent_block(blocked: bool) -> None:
    config = load_config()
    changed = ensure_blocked_outbound(config)
    rules = routing_rules(config)
    rules_without_torrent = [rule for rule in rules if not is_bittorrent_rule(rule)]

    if blocked:
        insert_torrent_rule(rules_without_torrent)

    if rules_without_torrent != rules:
        changed = True

    if not changed:
        print_torrent_status()
        print("Изменения не требуются.")
        return

    config["routing"]["rules"] = rules_without_torrent
    backup = apply_config(config)
    print_torrent_status()
    print(f"Backup: {backup}")


def show_xray_status(call: CommandRunner) -> None:
    call(["systemctl", "status", "xray", "--no-pager"])


def restart_xray(call: CommandRunner) -> None:
    call(["systemctl", "restart", "xray"])
    call(["systemctl", "is-active", "xray"])


def check_config(call: CommandRunner) -> None:
    call(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])


def check_timers(call: CommandRunner) -> None:
    call(["systemctl", "status", "xray-traffic-sync.timer", "xray-client-expire.timer", "xray-telegram-poller.service", "--no-pager"])


def run_all_tests(call: CommandRunner) -> None:
    call(["xray-test"])


def print_initial_link() -> None:
    if CLIENT_LINK_PATH.exists():
        print(CLIENT_LINK_PATH.read_text())
    else:
        print("Файл /root/xray-reality-client.txt не найден. Можно вывести ссылку через xray-client link NAME.")


def add_or_replace_cascade(call: CommandRunner) -> None:
    call(["xray-set-cascade"])


def test_cascade(call: CommandRunner) -> None:
    call(["xray-set-cascade", "--test"])


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


def sqlite_preflight(call: CommandRunner) -> None:
    call(["xray-vps-manager", "sqlite", "preflight"])


def sqlite_validate_cutover(call: CommandRunner) -> None:
    call(["xray-vps-manager", "sqlite", "validate-cutover"])


def sqlite_cutover(call: CommandRunner, confirm: ConfirmCallback) -> None:
    print("Cutover остановит manager-сервисы записи, создаст бэкап, импортирует JSON/JSONL в SQLite")
    print("и включит SQLite как основной источник чтения и записи.")
    print("Если SQLite уже включён, команда только проверит текущую базу без повторного импорта JSON.")
    if confirm("Выполнить финальное переключение на SQLite?"):
        call(["xray-vps-manager", "sqlite", "cutover", "--yes"])
    else:
        print("Cutover SQLite отменён.")


def sqlite_cleanup_legacy(call: CommandRunner, confirm: ConfirmCallback) -> None:
    print("Будет выполнена проверка SQLite, создан свежий бэкап и удалены legacy JSON/JSONL-файлы.")
    print("Безопасный dry-run можно выполнить командой: xray-vps-manager sqlite cleanup-legacy")
    if confirm("Удалить legacy JSON/JSONL после бэкапа"):
        call(["xray-vps-manager", "sqlite", "cleanup-legacy", "--yes"])
    else:
        print("Очистка legacy SQLite отменена.")


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
