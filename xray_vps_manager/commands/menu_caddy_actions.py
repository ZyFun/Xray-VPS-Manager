"""Caddy management actions used by the interactive menu."""

from __future__ import annotations

import subprocess
import time
import re
from collections.abc import Callable
from pathlib import Path

from xray_vps_manager.clients import connections as connection_store
from xray_vps_manager.clients.repository import db_connections, load_db_sql
from xray_vps_manager.core.terminal import print_table
from xray_vps_manager.xray import caddy
from xray_vps_manager.xray.config import find_inbound_by_tag, load_config as load_xray_config

ConfirmCallback = Callable[[str], bool]


def die(message: str) -> None:
    raise SystemExit(message)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, text=True, **kwargs)


def run_no_check(command: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def compact_output(result: subprocess.CompletedProcess) -> str:
    return (result.stdout or result.stderr or f"exit {result.returncode}").strip()


def installed() -> bool:
    result = run_no_check(["sh", "-c", "command -v caddy"], timeout=5)
    return result.returncode == 0


def caddy_status() -> None:
    run(["systemctl", "--no-pager", "--full", "status", "caddy.service"])


def install_caddy() -> None:
    installed_now = caddy.install_caddy_if_needed(subprocess.run)
    caddy.ensure_caddyfile_import()
    caddy.validate_and_reload_caddy(subprocess.run)
    if installed_now:
        print("Caddy installed and started.")
    else:
        print("Caddy is already installed. Config import is present.")


def validate_config() -> None:
    run(["caddy", "validate", "--config", str(caddy.CADDYFILE_PATH)])


def reload_caddy() -> None:
    caddy.validate_and_reload_caddy(subprocess.run)
    print("Caddy reloaded.")


def restart_caddy() -> None:
    validate_config()
    run(["systemctl", "restart", "caddy"])
    print("Caddy restarted.")


def show_caddyfile() -> None:
    path = caddy.CADDYFILE_PATH
    if not path.exists():
        print(f"Caddyfile not found: {path}")
        return
    print(path.read_text())


def site_rows() -> list[caddy.SiteConfig]:
    return caddy.list_site_configs()


def show_sites() -> None:
    rows = [
        [
            item.domain,
            str(item.path),
            item.local_port or "-",
            item.tls_min_version,
            item.tls_max_version,
        ]
        for item in site_rows()
    ]
    print_table(["DOMAIN", "FILE", "UPSTREAM", "TLS MIN", "TLS MAX"], rows, empty_message="Caddy site configs not found.")


def list_config_backups() -> None:
    print_table(["PATH", "CREATED", "SIZE"], caddy.config_backup_rows(), empty_message="Caddy backups not found.")


def choose_config_backup(action: str) -> str | None:
    rows = caddy.config_backup_rows()
    if not rows:
        print("Caddy backups not found.")
        return None
    table_rows = [[str(index), *row] for index, row in enumerate(rows, start=1)]
    table_rows.append(["0", "Назад", "", ""])
    print(f"Выбери Caddy backup для действия: {action}.")
    print_table(["№", "PATH", "CREATED", "SIZE"], table_rows, empty_message=None)
    while True:
        choice = input("Backup: ").strip()
        if choice == "0":
            return None
        if choice.isdigit():
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1][0]
        print("Неизвестный backup. Выбери номер из списка или 0 для возврата.")


def create_config_backup() -> None:
    try:
        caddy.create_config_backup()
    except FileNotFoundError as exc:
        die(str(exc))


def restore_config_backup(confirm: ConfirmCallback) -> None:
    archive = choose_config_backup("восстановления")
    if not archive:
        return
    print("Будут восстановлены настройки Caddy из backup.")
    print("Это заменит /etc/caddy/Caddyfile и /etc/caddy/conf.d, если conf.d есть в архиве.")
    print("Перед восстановлением будет создан pre-restore backup текущих настроек Caddy.")
    if not confirm("Восстановить Caddy config"):
        print("Восстановление отменено.")
        return
    try:
        restored_from, pre_backup, restored = caddy.restore_config_backup(archive)
    except Exception as exc:
        die(f"Caddy restore failed. Previous config was restored from pre-restore backup if possible. Detail: {exc}")
    print(f"Restored from: {restored_from}")
    print(f"Pre-restore backup: {pre_backup}")
    print("Restored paths:")
    for path in restored:
        print(f"  {path}")
    print("Caddy config validated and reloaded.")


def delete_config_backup(confirm: ConfirmCallback) -> None:
    archive = choose_config_backup("удаления")
    if not archive:
        return
    if not confirm(f"Удалить Caddy backup {archive}"):
        print("Удаление отменено.")
        return
    try:
        removed = caddy.delete_config_backup(archive)
    except (FileNotFoundError, ValueError) as exc:
        die(str(exc))
    print(f"Deleted Caddy backup: {removed}")


def choose_site_root(action: str, default: str = "") -> str:
    candidates = caddy.site_root_candidates()
    print(f"Выбери папку сайта для действия: {action}.")
    if candidates:
        rows = [[str(index), str(path), caddy.format_size(caddy.tree_size(path))] for index, path in enumerate(candidates, start=1)]
        rows.append(["M", "Ввести путь вручную", ""])
        rows.append(["0", "Назад", ""])
        print_table(["№", "SITE ROOT", "SIZE"], rows, empty_message=None)
    else:
        print("Автоматически найденных site root нет. Можно ввести путь вручную.")
    while True:
        prompt_text = "Site root"
        if default:
            prompt_text += f" [{default}]"
        prompt_text += ": "
        choice = input(prompt_text).strip()
        if not choice and default:
            return default
        if choice == "0":
            return ""
        if choice.lower() == "m" or (choice and not choice.isdigit()):
            manual = input("Абсолютный путь к папке сайта: ").strip() if choice.lower() == "m" else choice
            if not manual:
                print("Путь не указан.")
                continue
            return manual
        if choice.isdigit() and candidates:
            index = int(choice, 10)
            if 1 <= index <= len(candidates):
                return str(candidates[index - 1])
        print("Неизвестный выбор. Выбери номер, M для ручного ввода или 0 для возврата.")


def create_site_backup() -> None:
    site_root = choose_site_root("backup")
    if not site_root:
        print("Действие отменено.")
        return
    try:
        caddy.create_site_backup(site_root)
    except (FileNotFoundError, ValueError) as exc:
        die(str(exc))


def list_site_backups() -> None:
    print_table(["PATH", "SITE ROOT", "CREATED", "SIZE"], caddy.site_backup_rows(), empty_message="Caddy site backups not found.")


def choose_site_backup(action: str) -> str | None:
    rows = caddy.site_backup_rows()
    if not rows:
        print("Caddy site backups not found.")
        return None
    table_rows = [[str(index), *row] for index, row in enumerate(rows, start=1)]
    table_rows.append(["0", "Назад", "", "", ""])
    print(f"Выбери Caddy site backup для действия: {action}.")
    print_table(["№", "PATH", "SITE ROOT", "CREATED", "SIZE"], table_rows, empty_message=None)
    while True:
        choice = input("Backup: ").strip()
        if choice == "0":
            return None
        if choice.isdigit():
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1][0]
        print("Неизвестный backup. Выбери номер из списка или 0 для возврата.")


def restore_site_backup(confirm: ConfirmCallback) -> None:
    archive = choose_site_backup("восстановления")
    if not archive:
        return
    manifest = caddy.backup_manifest(caddy.resolve_site_backup(archive))
    default_root = str(manifest.get("siteRoot") or "")
    target_root = choose_site_root("restore", default=default_root)
    if not target_root:
        print("Восстановление отменено.")
        return
    print("Будут восстановлены файлы сайта из backup.")
    print(f"Target root: {target_root}")
    print("Перед заменой существующей папки сайта будет создан pre-restore backup, если папка уже существует.")
    if not confirm("Восстановить сайт Caddy"):
        print("Восстановление отменено.")
        return
    try:
        restored_from, pre_backup, restored_root = caddy.restore_site_backup(archive, target_root=target_root)
    except (FileNotFoundError, ValueError, OSError) as exc:
        die(f"Caddy site restore failed. Previous site files were restored from pre-restore backup if possible. Detail: {exc}")
    print(f"Restored from: {restored_from}")
    if pre_backup:
        print(f"Pre-restore site backup: {pre_backup}")
    else:
        print("Pre-restore site backup: none, target root did not exist")
    print(f"Restored site root: {restored_root}")


def delete_site_backup(confirm: ConfirmCallback) -> None:
    archive = choose_site_backup("удаления")
    if not archive:
        return
    if not confirm(f"Удалить Caddy site backup {archive}"):
        print("Удаление отменено.")
        return
    try:
        removed = caddy.delete_site_backup(archive)
    except (FileNotFoundError, ValueError) as exc:
        die(str(exc))
    print(f"Deleted Caddy site backup: {removed}")


def choose_site(action: str, auto_single: bool = False) -> caddy.SiteConfig | None:
    sites = site_rows()
    if not sites:
        print("Caddy site configs not found.")
        return None
    if len(sites) == 1 and auto_single:
        return sites[0]
    rows = [
        [
            str(index),
            item.domain,
            str(item.path),
            item.local_port or "-",
            item.tls_min_version,
            item.tls_max_version,
        ]
        for index, item in enumerate(sites, start=1)
    ]
    rows.append(["0", "Назад", "", "", "", ""])
    print(f"Выбери Caddy site для действия: {action}.")
    print_table(["№", "DOMAIN", "FILE", "UPSTREAM", "TLS MIN", "TLS MAX"], rows, empty_message=None)
    while True:
        choice = input("Site: ").strip()
        if choice == "0":
            return None
        if choice.isdigit():
            index = int(choice, 10)
            if 1 <= index <= len(sites):
                return sites[index - 1]
        print("Неизвестный site. Выбери номер из списка или 0 для возврата.")


def show_site_config() -> None:
    site = choose_site("просмотра config", auto_single=True)
    if not site:
        return
    print(site.path.read_text())


def prompt(default: str | int, message: str) -> str:
    value = input(f"{message} [{default}]: ").strip()
    return value or str(default)


def validate_port(value: str) -> int:
    try:
        port = int(value, 10)
    except ValueError as exc:
        raise SystemExit("PORT must be a number from 1 to 65535.") from exc
    if port < 1 or port > 65535:
        raise SystemExit("PORT must be a number from 1 to 65535.")
    return port


def prompt_tls_versions(current_min: str = "tls1.2", current_max: str = "tls1.2") -> tuple[str, str]:
    try:
        current_key = caddy.tls_version_choice_key(current_min, current_max)
        current_label = caddy.tls_version_label(current_min, current_max)
    except ValueError as exc:
        die(str(exc))
    print()
    print("TLS: выбери версию протокола для Caddy site.")
    for index, choice in enumerate(caddy.TLS_VERSION_CHOICES, start=1):
        marker = " (текущий)" if choice.key == current_key else ""
        print(f"  {index}) {choice.label}{marker}")
    while True:
        value = input(f"TLS [{current_label}] (номер из списка): ").strip()
        if not value:
            choice = caddy.tls_version_choice(current_key or "tls12")
            return choice.tls_min_version, choice.tls_max_version
        if value.isdigit():
            index = int(value, 10)
            if 1 <= index <= len(caddy.TLS_VERSION_CHOICES):
                choice = caddy.TLS_VERSION_CHOICES[index - 1]
                return choice.tls_min_version, choice.tls_max_version
        print(f"Выбери номер 1-{len(caddy.TLS_VERSION_CHOICES)} или нажми Enter для {current_label}.")


def apply_site_write(domain: str, local_port: int, tls_min: str, tls_max: str) -> None:
    try:
        result = caddy.update_site_config(domain, local_port, tls_min_version=tls_min, tls_max_version=tls_max)
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        die(f"Caddy config failed. Previous site config was restored. Detail: {exc}")
    print(f"Caddy site updated: {result.path}")


def tls_connection_options() -> list[dict]:
    config = load_xray_config()
    db = load_db_sql()
    connection_store.ensure_connections(config, db)
    options = []
    for tag, entry in db_connections(db).items():
        if (entry.get("security") or "reality") != "tls":
            continue
        inbound = find_inbound_by_tag(config, tag)
        options.append(
            {
                "tag": tag,
                "name": entry.get("name") or tag,
                "domain": entry.get("publicHost") or entry.get("sni") or "",
                "publicPort": int(entry.get("publicPort") or entry.get("port") or 443),
                "localPort": int(entry.get("localPort") or inbound.get("port") or 0),
                "tlsMin": entry.get("tlsMinVersion") or "tls1.2",
                "tlsMax": entry.get("tlsMaxVersion") or "tls1.2",
            }
        )
    return options


def choose_tls_connection() -> dict | None:
    options = tls_connection_options()
    if not options:
        print("TLS/XHTTP connections not found.")
        return None
    rows = [
        [str(index), item["name"], item["tag"], item["domain"], item["publicPort"], item["localPort"]]
        for index, item in enumerate(options, start=1)
    ]
    rows.append(["0", "Назад", "", "", "", ""])
    print("Выбери TLS/XHTTP подключение.")
    print_table(["№", "NAME", "TAG", "DOMAIN", "PUBLIC", "LOCAL"], rows, empty_message=None)
    while True:
        choice = input("Подключение: ").strip()
        if choice == "0":
            return None
        if choice.isdigit():
            index = int(choice, 10)
            if 1 <= index <= len(options):
                return options[index - 1]
        print("Неизвестное подключение. Выбери номер из списка или 0 для возврата.")


def create_site_from_tls_connection() -> None:
    item = choose_tls_connection()
    if not item:
        return
    domain = caddy.validate_domain(item["domain"])
    tls_min, tls_max = prompt_tls_versions(item["tlsMin"], item["tlsMax"])
    apply_site_write(domain, item["localPort"], tls_min, tls_max)


def create_site_manual() -> None:
    try:
        domain = caddy.validate_domain(input("DOMAIN: ").strip())
    except ValueError as exc:
        die(str(exc))
    local_port = validate_port(prompt("10300", "LOCAL_PORT upstream"))
    tls_min, tls_max = prompt_tls_versions("tls1.2", "tls1.2")
    apply_site_write(domain, local_port, tls_min, tls_max)


def update_site_tls() -> None:
    site = choose_site("изменения TLS version", auto_single=True)
    if not site:
        return
    if site.local_port is None:
        print("Не удалось определить upstream local port в site config.")
        return
    tls_min, tls_max = prompt_tls_versions(site.tls_min_version, site.tls_max_version)
    apply_site_write(site.domain, site.local_port, tls_min, tls_max)


def update_site_upstream() -> None:
    site = choose_site("изменения upstream local port", auto_single=True)
    if not site:
        return
    current_port = site.local_port or 10300
    local_port = validate_port(prompt(current_port, "LOCAL_PORT upstream"))
    apply_site_write(site.domain, local_port, site.tls_min_version, site.tls_max_version)


def update_site_domain() -> None:
    site = choose_site("изменения домена site", auto_single=True)
    if not site:
        return
    if site.local_port is None:
        print("Не удалось определить upstream local port в site config.")
        return
    try:
        new_domain = caddy.validate_domain(prompt(site.domain, "NEW_DOMAIN"))
    except ValueError as exc:
        die(str(exc))
    if new_domain == site.domain:
        print("Домен не изменён.")
        return

    old_path = site.path
    new_path = caddy.site_config_path(new_domain)
    old_backup = caddy.backup_file(old_path)
    new_backup = caddy.backup_file(new_path)
    try:
        caddy.write_site_config(new_domain, site.local_port, tls_min_version=site.tls_min_version, tls_max_version=site.tls_max_version)
        if old_path.exists():
            old_path.unlink()
        caddy.validate_and_reload_caddy(subprocess.run)
    except (OSError, subprocess.CalledProcessError) as exc:
        caddy.restore_file(old_backup, old_path)
        caddy.restore_file(new_backup, new_path)
        try:
            caddy.validate_and_reload_caddy(subprocess.run)
        except Exception:
            pass
        die(f"Caddy config failed. Restored backups. Detail: {exc}")
    print(f"Caddy site domain changed: {site.domain} -> {new_domain}")
    print("Важно: это не меняет VLESS-подключение в manager.db и не перевыпускает клиентские ссылки.")


def delete_site(confirm: ConfirmCallback) -> None:
    site = choose_site("удаления site config")
    if not site:
        return
    print(f"Будет удалён Caddy site: {site.domain}")
    print(f"Файл: {site.path}")
    if not confirm("Удалить Caddy site"):
        print("Удаление отменено.")
        return
    backup = caddy.backup_file(site.path)
    try:
        if site.path.exists():
            site.path.unlink()
        caddy.validate_and_reload_caddy(subprocess.run)
    except (OSError, subprocess.CalledProcessError) as exc:
        caddy.restore_file(backup, site.path)
        try:
            caddy.validate_and_reload_caddy(subprocess.run)
        except Exception:
            pass
        die(f"Caddy config failed. Restored backup: {backup}. Detail: {exc}")
    print(f"Caddy site removed. Backup: {backup}")


def remove_default_http_site(confirm: ConfirmCallback) -> None:
    print("Действие удалит дефолтный блок ':80 { file_server }' из /etc/caddy/Caddyfile.")
    print("ACME HTTP challenge и HTTPS redirect для доменных site configs останутся у Caddy.")
    if not confirm("Удалить дефолтный :80 site"):
        print("Действие отменено.")
        return
    backup = caddy.backup_file(caddy.CADDYFILE_PATH)
    try:
        changed = caddy.remove_site_block_from_caddyfile(":80")
        if not changed:
            print("Блок :80 не найден.")
            return
        caddy.validate_and_reload_caddy(subprocess.run)
    except (OSError, subprocess.CalledProcessError) as exc:
        caddy.restore_file(backup, caddy.CADDYFILE_PATH)
        try:
            caddy.validate_and_reload_caddy(subprocess.run)
        except Exception:
            pass
        die(f"Caddy config failed. Restored backup: {backup}. Detail: {exc}")
    print(f"Default :80 site removed. Backup: {backup}")


def show_logs() -> None:
    run(["journalctl", "-u", "caddy", "-n", "120", "--no-pager"])


def tls_handshake_check() -> None:
    site = choose_site("проверки TLS handshake", auto_single=True)
    domain = site.domain if site else input("DOMAIN: ").strip()
    try:
        domain = caddy.validate_domain(domain)
    except ValueError as exc:
        die(str(exc))
    for version in ("tls1_2", "tls1_3"):
        print()
        print(f"Проверка {version.replace('_', '.')}:")
        result = run_no_check(
            [
                "timeout",
                "10",
                "openssl",
                "s_client",
                "-connect",
                f"{domain}:443",
                "-servername",
                domain,
                f"-{version}",
                "-brief",
            ],
            timeout=15,
        )
        output = (result.stdout or "") + (result.stderr or "")
        print(output.strip() or f"exit {result.returncode}")


def systemctl_value(*args: str) -> str:
    try:
        result = run_no_check(["systemctl", *args], timeout=10)
    except FileNotFoundError:
        return "systemctl not found"
    value = compact_output(result)
    if result.returncode != 0 and value:
        return f"{value} (exit {result.returncode})"
    return value or "-"


def random_tls_current_label(domain: str) -> str:
    try:
        site = caddy.site_config_for_domain(domain)
    except (FileNotFoundError, ValueError):
        return "-"
    return caddy.tls_version_label(site.tls_min_version, site.tls_max_version)


def random_tls_next_label(domain: str) -> str:
    try:
        site = caddy.site_config_for_domain(domain)
    except (FileNotFoundError, ValueError):
        return "-"
    return caddy.next_random_tls_label(site.tls_min_version, site.tls_max_version)


def format_timer_eta(seconds: int) -> str:
    if seconds <= 0:
        return "сейчас"
    minutes = max(1, int(round(seconds / 60)))
    if minutes < 60:
        return f"через {minutes} мин"
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"через {hours} ч"
    return f"через {hours} ч {rest} мин"


def format_systemd_left(value: str) -> str:
    text = (value or "").strip()
    if not text or text.lower() == "n/a":
        return "-"
    units = {
        "ms": "мс",
        "s": "с",
        "sec": "с",
        "min": "мин",
        "h": "ч",
        "day": "д",
        "days": "д",
        "month": "мес",
        "months": "мес",
    }
    parts = []
    for amount, unit in re.findall(r"(\d+)\s*([A-Za-z]+)", text):
        parts.append(f"{amount} {units.get(unit, unit)}")
    if parts:
        return "через " + " ".join(parts)
    return f"через {text}"


def timer_line_next_and_left(line: str) -> tuple[str, str]:
    tokens = line.split()
    if not tokens:
        return "-", "-"
    if tokens[0].lower() == "n/a":
        next_run = "n/a"
        left_start = 1
    elif len(tokens) >= 4:
        next_run = " ".join(tokens[:4])
        left_start = 4
    else:
        return line.strip(), "-"

    weekdays = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
    last_start = None
    for index in range(left_start, len(tokens)):
        token = tokens[index]
        if token.lower() == "n/a":
            last_start = index
            break
        if token in weekdays and index + 2 < len(tokens):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tokens[index + 1]) and re.fullmatch(r"\d{2}:\d{2}:\d{2}", tokens[index + 2]):
                last_start = index
                break
    if last_start is None:
        return next_run, "-"
    return next_run, " ".join(tokens[left_start:last_start]) or "-"


def random_tls_timer_left_rows() -> list[list[str]]:
    try:
        result = run_no_check(["systemctl", "list-timers", "--all", "--no-legend", "--no-pager", caddy.RANDOM_TLS_TIMER], timeout=10)
    except FileNotFoundError:
        return [["NEXT RUN", "systemctl not found"], ["NEXT RUN IN", "-"]]
    if result.returncode != 0:
        return [["NEXT RUN", compact_output(result)], ["NEXT RUN IN", "-"]]
    line = (result.stdout or "").strip().splitlines()
    if not line:
        return [["NEXT RUN", "-"], ["NEXT RUN IN", "-"]]
    next_run, left = timer_line_next_and_left(line[0])
    return [["NEXT RUN", next_run], ["NEXT RUN IN", format_systemd_left(left)]]


def random_tls_next_timer_rows() -> list[list[str]]:
    try:
        result = run_no_check(
            ["systemctl", "show", caddy.RANDOM_TLS_TIMER, "--property=NextElapseUSecRealtime", "--value"],
            timeout=10,
        )
    except FileNotFoundError:
        return [["NEXT RUN", "systemctl not found"], ["NEXT RUN IN", "-"]]
    if result.returncode != 0:
        return [["NEXT RUN", compact_output(result)], ["NEXT RUN IN", "-"]]

    next_run = (result.stdout or "").strip()
    if not next_run or next_run.lower() in {"n/a", "never"}:
        return random_tls_timer_left_rows()

    try:
        parsed = run_no_check(["date", "-d", next_run, "+%s"], timeout=5)
    except FileNotFoundError:
        return [["NEXT RUN", next_run], ["NEXT RUN IN", "-"]]
    if parsed.returncode != 0:
        return [["NEXT RUN", next_run], ["NEXT RUN IN", "-"]]
    try:
        delta_seconds = int(parsed.stdout.strip()) - int(time.time())
    except ValueError:
        return [["NEXT RUN", next_run], ["NEXT RUN IN", "-"]]
    return [["NEXT RUN", next_run], ["NEXT RUN IN", format_timer_eta(delta_seconds)]]


def print_random_tls_result(result: caddy.RandomTlsApplyResult) -> None:
    previous = caddy.tls_version_label(result.previous_tls_min_version, result.previous_tls_max_version)
    current = caddy.tls_version_label(result.tls_min_version, result.tls_max_version)
    print(f"Caddy TLS randomized for {result.domain}: {previous} -> {current}")
    print(f"Site config: {result.path}")
    if result.backup:
        print(f"Backup: {result.backup}")


def random_tls_status() -> None:
    rows = [
        ["ENV", str(caddy.CADDY_RANDOM_TLS_ENV_PATH if caddy.CADDY_RANDOM_TLS_ENV_PATH.exists() else "missing")],
        ["TIMER ACTIVE", systemctl_value("is-active", caddy.RANDOM_TLS_TIMER)],
        ["TIMER ENABLED", systemctl_value("is-enabled", caddy.RANDOM_TLS_TIMER)],
        ["SERVICE ACTIVE", systemctl_value("is-active", caddy.RANDOM_TLS_SERVICE)],
    ]
    try:
        config = caddy.read_random_tls_config()
    except (RuntimeError, ValueError) as exc:
        rows.append(["CONFIG", str(exc)])
    else:
        rows.extend(
            [
                ["DOMAIN", config.domain],
                ["LOCAL PORT", str(config.local_port)],
                ["CURRENT TLS", random_tls_current_label(config.domain)],
                ["NEXT TLS", random_tls_next_label(config.domain)],
            ]
        )
        rows.extend(random_tls_next_timer_rows())
    print_table(["ITEM", "VALUE"], rows)
    print()
    print("Next timer event:")
    print(systemctl_value("list-timers", "--all", "--no-pager", caddy.RANDOM_TLS_TIMER))


def enable_random_tls(confirm: ConfirmCallback) -> None:
    site = choose_site("включения TLS randomizer", auto_single=True)
    if not site:
        return
    if site.local_port is None:
        print("Не удалось определить upstream local port в site config.")
        return
    print("TLS randomizer будет менять Caddy site между строгими TLS 1.2 и TLS 1.3.")
    print("Интервал задаёт systemd timer: 15 минут + случайная задержка до 45 минут.")
    print("Если клиент или сеть не принимает одну из версий TLS, этот site может временно стать недоступным.")
    if not confirm(f"Включить TLS randomizer для {site.domain}"):
        print("Действие отменено.")
        return
    try:
        config = caddy.write_random_tls_config(site.domain, site.local_port)
        units = caddy.write_random_tls_systemd_units()
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "--now", caddy.RANDOM_TLS_TIMER])
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        die(f"Не удалось включить TLS randomizer: {exc}")
    print(f"TLS randomizer enabled for {config.domain}:{config.local_port}.")
    print(f"Service unit: {units['service']}")
    print(f"Timer unit: {units['timer']}")
    print("Первое автоматическое переключение будет через случайные 15-60 минут.")


def disable_random_tls(confirm: ConfirmCallback) -> None:
    print("TLS randomizer будет остановлен. Текущая TLS-версия site config останется как есть.")
    if not confirm("Отключить TLS randomizer"):
        print("Действие отменено.")
        return
    for command in (
        ["systemctl", "disable", "--now", caddy.RANDOM_TLS_TIMER],
        ["systemctl", "stop", caddy.RANDOM_TLS_SERVICE],
    ):
        try:
            result = run_no_check(command, timeout=20)
        except FileNotFoundError:
            print("systemctl not found.")
            return
        if result.returncode != 0:
            print(f"{' '.join(command)}: {compact_output(result)}")
    print("TLS randomizer disabled.")
    print(f"Config left in place: {caddy.CADDY_RANDOM_TLS_ENV_PATH}")


def random_tls_run_now() -> None:
    try:
        result = caddy.apply_random_tls_switch()
    except (FileNotFoundError, RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        die(f"TLS randomizer failed: {exc}")
    print_random_tls_result(result)
