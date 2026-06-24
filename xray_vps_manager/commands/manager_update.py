#!/usr/bin/env python3
"""Update Xray VPS Manager from GitHub Releases."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from xray_vps_manager.commands.menu import MENU_VERSION
from xray_vps_manager.core.terminal import green, print_table, red, yellow

REPO_OWNER = "ZyFun"
REPO_NAME = "Xray-VPS-Manager"
REPO_SLUG = f"{REPO_OWNER}/{REPO_NAME}"
GITHUB_API_LATEST = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
GITHUB_TAG_ARCHIVE = f"https://github.com/{REPO_SLUG}/archive/refs/tags/{{tag}}.tar.gz"

SOURCE_DIR = Path("/root/xray_server")
SBIN_DIR = Path("/usr/local/sbin")
MANAGER_LIB_DIR = Path("/usr/local/lib/xray-vps-manager")
MANAGER_PACKAGE_DIR = MANAGER_LIB_DIR / "xray_vps_manager"
BACKUP_DIR = Path("/usr/local/lib/xray-vps-manager-backups")
MANAGER_SYSTEMD_UNITS = (
    "xray-traffic-sync.timer",
    "xray-raw-log-rotate.timer",
    "xray-client-expire.timer",
    "xray-traffic-sync.service",
    "xray-raw-log-rotate.service",
    "xray-client-expire.service",
    "xray-telegram-poller.service",
)

MANAGER_WRAPPERS = (
    "xray-client",
    "xray-set-cascade",
    "xray-menu",
    "xray-activity",
    "xray-traffic-sync",
    "xray-update",
    "xray-backup",
    "xray-test",
    "xray-warp",
    "xray-telegram",
    "xray-vps-manager",
    "xray-manager-update",
)

SOURCE_ITEMS = (
    *MANAGER_WRAPPERS,
    "bootstrap.sh",
    "install.sh",
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "tests",
    "xray_vps_manager",
)

REQUIRED_RELEASE_ITEMS = (
    *MANAGER_WRAPPERS,
    "bootstrap.sh",
    "install.sh",
    "xray_vps_manager",
)


class ManagerUpdateError(RuntimeError):
    """Raised for expected manager update failures."""


def ok(message: str) -> None:
    print(green(f"OK: {message}"))


def warn(message: str) -> None:
    print(yellow(f"WARN: {message}"))


def fail(message: str) -> None:
    print(red(f"FAIL: {message}"))


def run(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def compact_output(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
    return "\n".join(detail.splitlines()[:12])


def require_root() -> None:
    if os.geteuid() != 0:
        raise ManagerUpdateError("Запусти команду от root.")


def current_manager_version() -> str:
    return MENU_VERSION


def parse_version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"v?(\d+(?:\.\d+){1,3})", value or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def normalize_tag(tag: str) -> str:
    value = tag.strip()
    if re.fullmatch(r"\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9._-]+)?", value):
        return f"v{value}"
    return value


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "unknown"


def release_archive_url(tag: str) -> str:
    return GITHUB_TAG_ARCHIVE.format(tag=tag)


def http_json(url: str, timeout: int = 20) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "xray-manager-update"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_release() -> dict:
    try:
        data = http_json(GITHUB_API_LATEST)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ManagerUpdateError(
                f"В репозитории {REPO_SLUG} пока нет опубликованных релизов."
            ) from exc
        raise ManagerUpdateError(f"Не удалось получить latest release: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ManagerUpdateError(f"Не удалось подключиться к GitHub: {exc.reason}") from exc

    tag = data.get("tag_name")
    if not tag:
        raise ManagerUpdateError("GitHub latest release не содержит tag_name.")
    return data


def print_update_check() -> int:
    latest = latest_release()
    current = current_manager_version()
    tag = latest["tag_name"]
    current_tuple = parse_version_tuple(current)
    latest_tuple = parse_version_tuple(tag)

    print(f"Current manager version: {current}")
    print(f"Latest GitHub release: {tag}")
    if latest.get("html_url"):
        print(f"Release page: {latest['html_url']}")

    if current_tuple and latest_tuple:
        if latest_tuple > current_tuple:
            ok(f"Доступно обновление менеджера: {current} -> {tag}")
            return 0
        if latest_tuple == current_tuple:
            ok("Установлена версия latest release.")
            return 0
        warn(f"Локальная версия новее latest release: {current} > {tag}")
        return 0

    warn("Не удалось сравнить версии как semver; сравни теги вручную.")
    return 0


def safe_extract_archive(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        destination_root = destination.resolve()
        members = tar.getmembers()
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise ManagerUpdateError(f"Неподдерживаемый тип файла в архиве: {member.name}")
            member_target = (destination / member.name).resolve()
            try:
                member_target.relative_to(destination_root)
            except ValueError as exc:
                raise ManagerUpdateError(f"Небезопасный путь в архиве: {member.name}") from exc
        tar.extractall(destination)

    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise ManagerUpdateError("Архив релиза должен содержать одну корневую папку.")
    return roots[0]


def download_release_archive(tag: str, destination: Path) -> Path:
    url = release_archive_url(tag)
    archive = destination / f"{safe_name(tag)}.tar.gz"
    request = urllib.request.Request(url, headers={"User-Agent": "xray-manager-update"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            with archive.open("wb") as output:
                shutil.copyfileobj(response, output)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ManagerUpdateError(f"Релизный архив не найден для тега {tag}.") from exc
        raise ManagerUpdateError(f"Не удалось скачать архив релиза: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ManagerUpdateError(f"Не удалось скачать архив релиза: {exc.reason}") from exc
    return archive


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def chown_root(path: Path) -> None:
    if os.geteuid() != 0:
        return
    shutil.chown(path, user="root", group="root")


def chown_tree(path: Path) -> None:
    if not path.exists():
        return
    chown_root(path)
    if path.is_dir():
        for child in path.rglob("*"):
            chown_root(child)


def set_tree_permissions(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        os.chmod(path, 0o755)
        for child in path.rglob("*"):
            if child.is_dir():
                os.chmod(child, 0o755)
            elif child.is_file():
                os.chmod(child, 0o644)
    elif path.is_file():
        os.chmod(path, 0o644)


def cleanup_appledouble(path: Path) -> None:
    if not path.is_dir():
        return
    for child in path.rglob("._*"):
        if child.is_file() or child.is_symlink():
            child.unlink()


def copy_path(source: Path, destination: Path) -> None:
    remove_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=False)
    else:
        shutil.copy2(source, destination)


def copy_known_items(source_root: Path, target_root: Path, item_names: tuple[str, ...], remove_missing: bool) -> None:
    target_root.mkdir(parents=True, exist_ok=True)
    for item_name in item_names:
        source = source_root / item_name
        target = target_root / item_name
        if source.exists():
            copy_path(source, target)
        elif remove_missing:
            remove_path(target)
    cleanup_appledouble(target_root)
    set_tree_permissions(target_root)
    for wrapper in MANAGER_WRAPPERS:
        wrapper_path = target_root / wrapper
        if wrapper_path.exists():
            os.chmod(wrapper_path, 0o755)
    install_path = target_root / "install.sh"
    if install_path.exists():
        os.chmod(install_path, 0o755)
    bootstrap_path = target_root / "bootstrap.sh"
    if bootstrap_path.exists():
        os.chmod(bootstrap_path, 0o755)
    chown_tree(target_root)


def validate_release_source(source_root: Path) -> None:
    missing = [item for item in REQUIRED_RELEASE_ITEMS if not (source_root / item).exists()]
    if missing:
        raise ManagerUpdateError("В архиве релиза не хватает файлов: " + ", ".join(missing))
    package = source_root / "xray_vps_manager"
    if not (package / "runner.py").exists():
        raise ManagerUpdateError("В архиве релиза нет xray_vps_manager/runner.py.")


def create_backup(target_tag: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = BACKUP_DIR / f"{timestamp}-{safe_name(current_manager_version())}-to-{safe_name(target_tag)}"
    if backup.exists():
        raise ManagerUpdateError(f"Backup уже существует: {backup}")
    backup.mkdir(parents=True)

    copy_known_items(SOURCE_DIR, backup / "source", SOURCE_ITEMS, remove_missing=False)
    copy_known_items(SBIN_DIR, backup / "sbin", MANAGER_WRAPPERS, remove_missing=False)
    if MANAGER_PACKAGE_DIR.exists():
        copy_path(MANAGER_PACKAGE_DIR, backup / "package" / "xray_vps_manager")
        cleanup_appledouble(backup / "package")
        set_tree_permissions(backup / "package")
        chown_tree(backup / "package")

    manifest = {
        "createdUtc": timestamp,
        "currentVersion": current_manager_version(),
        "targetTag": target_tag,
        "sourceDir": str(SOURCE_DIR),
        "managerLibDir": str(MANAGER_LIB_DIR),
    }
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    chown_tree(backup)
    return backup


def install_wrappers(source_root: Path) -> None:
    SBIN_DIR.mkdir(parents=True, exist_ok=True)
    for wrapper in MANAGER_WRAPPERS:
        source = source_root / wrapper
        target = SBIN_DIR / wrapper
        copy_path(source, target)
        os.chmod(target, 0o755)
        chown_root(target)


def restore_wrappers(source_root: Path) -> None:
    SBIN_DIR.mkdir(parents=True, exist_ok=True)
    for wrapper in MANAGER_WRAPPERS:
        source = source_root / wrapper
        target = SBIN_DIR / wrapper
        if source.exists():
            copy_path(source, target)
            os.chmod(target, 0o755)
            chown_root(target)
        else:
            remove_path(target)


def install_python_package(source_root: Path) -> None:
    MANAGER_LIB_DIR.mkdir(parents=True, exist_ok=True)
    copy_path(source_root / "xray_vps_manager", MANAGER_PACKAGE_DIR)
    cleanup_appledouble(MANAGER_PACKAGE_DIR)
    set_tree_permissions(MANAGER_PACKAGE_DIR)
    chown_tree(MANAGER_PACKAGE_DIR)


def install_release_source(source_root: Path) -> None:
    validate_release_source(source_root)
    copy_known_items(source_root, SOURCE_DIR, SOURCE_ITEMS, remove_missing=True)
    install_wrappers(source_root)
    install_python_package(source_root)


def sync_raw_log_units() -> None:
    xray_activity = SBIN_DIR / "xray-activity"
    if not xray_activity.exists():
        warn("xray-activity не найден; raw-log rotation units не синхронизированы.")
        return
    result = run([str(xray_activity), "raw-log-timer-sync"], timeout=30)
    if result.returncode == 0:
        ok("raw-log rotation systemd units синхронизированы.")
    else:
        warn(f"raw-log rotation units не удалось синхронизировать: {compact_output(result)}")


def restart_manager_services() -> None:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        warn("systemctl не найден; перезапуск фоновых сервисов пропущен.")
        return

    daemon_reload = run([systemctl, "daemon-reload"], timeout=30)
    if daemon_reload.returncode == 0:
        ok("systemd daemon-reload выполнен.")
    else:
        warn(f"systemd daemon-reload не прошёл: {compact_output(daemon_reload)}")

    for unit in MANAGER_SYSTEMD_UNITS:
        result = run([systemctl, "try-restart", unit], timeout=30)
        if result.returncode == 0:
            ok(f"{unit} обновлён через try-restart.")
        else:
            warn(f"Не удалось обновить {unit}: {compact_output(result)}")

    reset_failed = run([systemctl, "reset-failed", *MANAGER_SYSTEMD_UNITS], timeout=30)
    if reset_failed.returncode != 0:
        warn(f"systemctl reset-failed не прошёл: {compact_output(reset_failed)}")


def validate_installed_manager(run_xray_test: bool) -> None:
    checks = [
        [str(SBIN_DIR / "xray-vps-manager"), "--help"],
        [str(SBIN_DIR / "xray-manager-update"), "--help"],
    ]
    for command in checks:
        result = run(command, timeout=20)
        if result.returncode != 0:
            raise ManagerUpdateError(f"Проверка {' '.join(command)} не прошла: {compact_output(result)}")

    if not run_xray_test:
        warn("xray-test пропущен по параметру --no-test.")
        return

    xray_test = SBIN_DIR / "xray-test"
    if not xray_test.exists():
        warn("xray-test не найден; диагностика сервера пропущена.")
        return

    result = run([str(xray_test)], timeout=180)
    if result.returncode != 0:
        raise ManagerUpdateError(f"xray-test не прошёл после обновления:\n{compact_output(result)}")
    ok("xray-test прошёл после обновления.")


def backup_rows() -> list[list[str]]:
    if not BACKUP_DIR.exists():
        return []
    rows = []
    for backup in sorted(path for path in BACKUP_DIR.iterdir() if path.is_dir()):
        manifest_path = backup / "manifest.json"
        manifest = {}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
        rows.append(
            [
                backup.name,
                manifest.get("currentVersion", ""),
                manifest.get("targetTag", ""),
                manifest.get("createdUtc", ""),
            ]
        )
    return rows


def list_backups() -> None:
    print_table(["Backup", "From", "Target", "Created UTC"], backup_rows(), empty_message="Бэкапы менеджера не найдены.")


def choose_backup(name: str | None) -> Path:
    if not BACKUP_DIR.exists():
        raise ManagerUpdateError("Бэкапы менеджера не найдены.")
    if name:
        backup = BACKUP_DIR / name
        if not backup.is_dir():
            raise ManagerUpdateError(f"Backup не найден: {name}")
        return backup
    backups = sorted(path for path in BACKUP_DIR.iterdir() if path.is_dir())
    if not backups:
        raise ManagerUpdateError("Бэкапы менеджера не найдены.")
    return backups[-1]


def restore_backup(backup: Path) -> None:
    source_backup = backup / "source"
    sbin_backup = backup / "sbin"
    package_backup = backup / "package" / "xray_vps_manager"

    if source_backup.exists():
        copy_known_items(source_backup, SOURCE_DIR, SOURCE_ITEMS, remove_missing=True)
    if sbin_backup.exists():
        restore_wrappers(sbin_backup)
    if package_backup.exists():
        copy_path(package_backup, MANAGER_PACKAGE_DIR)
        cleanup_appledouble(MANAGER_PACKAGE_DIR)
        set_tree_permissions(MANAGER_PACKAGE_DIR)
        chown_tree(MANAGER_PACKAGE_DIR)
    else:
        raise ManagerUpdateError(f"В backup нет Python-пакета: {backup}")


def update_to_tag(tag: str, force: bool, run_xray_test: bool) -> None:
    require_root()
    target_tag = normalize_tag(tag)
    current = current_manager_version()
    current_tuple = parse_version_tuple(current)
    target_tuple = parse_version_tuple(target_tag)

    if not force and current_tuple and target_tuple and target_tuple <= current_tuple:
        if target_tuple == current_tuple:
            ok(f"Версия {current} уже установлена. Для переустановки используй --force.")
        else:
            warn(f"Целевой тег {target_tag} старее текущей версии {current}. Для установки используй --force.")
        return

    with tempfile.TemporaryDirectory(prefix="xray-manager-update-") as tmp:
        tmp_path = Path(tmp)
        archive = download_release_archive(target_tag, tmp_path)
        release_source = safe_extract_archive(archive, tmp_path / "extract")
        validate_release_source(release_source)

        backup = create_backup(target_tag)
        print(f"Backup: {backup}")
        try:
            install_release_source(release_source)
            sync_raw_log_units()
            restart_manager_services()
            validate_installed_manager(run_xray_test)
        except Exception:
            warn("Обновление менеджера не прошло; восстанавливаю предыдущую версию.")
            restore_backup(backup)
            sync_raw_log_units()
            restart_manager_services()
            raise

    ok(f"Xray VPS Manager обновлён до {target_tag}.")


def update_latest(force: bool, run_xray_test: bool) -> None:
    release = latest_release()
    update_to_tag(release["tag_name"], force=force, run_xray_test=run_xray_test)


def rollback(name: str | None, run_xray_test: bool) -> None:
    require_root()
    backup = choose_backup(name)
    restore_backup(backup)
    sync_raw_log_units()
    restart_manager_services()
    validate_installed_manager(run_xray_test)
    ok(f"Xray VPS Manager восстановлен из backup: {backup.name}")


def usage() -> str:
    return """Usage:
  xray-manager-update --check
  xray-manager-update --update [TAG] [--force] [--no-test]
  xray-manager-update --backups
  xray-manager-update --rollback [BACKUP_NAME] [--no-test]

Examples:
  xray-manager-update --check
  xray-manager-update --update
  xray-manager-update --update v1.0.1
  xray-manager-update --rollback
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, usage=argparse.SUPPRESS)
    parser.add_argument("--help", "-h", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--update", nargs="?", const="latest")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-test", action="store_true")
    parser.add_argument("--backups", action="store_true")
    parser.add_argument("--rollback", nargs="?", const="")
    return parser


def selected_actions(args: argparse.Namespace) -> int:
    return sum(
        [
            bool(args.help),
            bool(args.check),
            args.update is not None,
            bool(args.backups),
            args.rollback is not None,
        ]
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.help or selected_actions(args) == 0:
        print(usage())
        return
    if selected_actions(args) > 1:
        fail("Выбери только одно действие.")
        print(usage())
        sys.exit(1)

    try:
        if args.check:
            sys.exit(print_update_check())
        if args.backups:
            list_backups()
            return
        if args.rollback is not None:
            rollback(args.rollback or None, run_xray_test=not args.no_test)
            return
        if args.update is not None:
            if args.update == "latest":
                update_latest(force=args.force, run_xray_test=not args.no_test)
            else:
                update_to_tag(args.update, force=args.force, run_xray_test=not args.no_test)
            return
    except ManagerUpdateError as exc:
        fail(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
