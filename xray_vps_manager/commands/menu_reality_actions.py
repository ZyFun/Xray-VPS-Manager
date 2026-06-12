"""Reality connection actions used by the interactive menu."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from xray_vps_manager.clients import connections as connection_store
from xray_vps_manager.clients.repository import db_connections, load_db, load_db_for_read, save_db
from xray_vps_manager.clients.settings import (
    fingerprint as server_fingerprint,
    save_server_env_values,
    server_env_values,
)
from xray_vps_manager.core.paths import CONFIG_PATH, XRAY_BIN
from xray_vps_manager.core.terminal import table_border, table_row
from xray_vps_manager.xray.config import (
    INBOUND_TAG,
    connection_name_from_tag,
    connection_settings_from_inbound,
    find_inbound,
    find_inbound_by_tag,
    inbound_tag,
    load_config as load_xray_config,
    reality_dest,
    reality_inbounds,
    save_config,
)

CLIENT_LINK_PATH = Path("/root/xray-reality-client.txt")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
FINGERPRINTS = [
    "chrome",
    "firefox",
    "safari",
    "ios",
    "android",
    "edge",
    "360",
    "qq",
    "random",
    "randomized",
]

CommandRunner = Callable[[list[str]], None]
ConfirmCallback = Callable[[str], bool]


def die(message: str) -> None:
    raise SystemExit(message)


def run(command: list[str], **kwargs) -> None:
    subprocess.run(command, check=True, **kwargs)


def load_config() -> dict:
    try:
        return load_xray_config()
    except FileNotFoundError as exc:
        die(str(exc))


def validate_port(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value or ""):
        die("PORT must be a number from 1 to 65535.")
    port = int(value, 10)
    if port < 1 or port > 65535:
        die("PORT must be a number from 1 to 65535.")
    return port


def validate_host(value: str, label: str = "SNI") -> str:
    if not value or "/" in value or ":" in value or not HOST_RE.fullmatch(value):
        die(f"{label} must be a domain without https://, path, or port.")
    return value


def validate_fingerprint(value: str) -> str:
    value = (value or "").strip().lower()
    if value not in FINGERPRINTS:
        die("FINGERPRINT must be one of: " + ", ".join(FINGERPRINTS))
    return value


def current_fingerprint() -> str:
    try:
        value = server_fingerprint().strip().lower()
    except ValueError:
        return "chrome"
    return value if value in FINGERPRINTS else "chrome"


def connection_rows() -> list[dict[str, str | int]]:
    config = load_config()
    db = load_db_for_read()
    connection_store.ensure_connections(config, db)
    rows = []
    for inbound in reality_inbounds(config):
        settings = connection_settings_from_inbound(inbound)
        entry = db_connections(db).get(settings["tag"], {})
        rows.append({
            "tag": settings["tag"],
            "name": entry.get("name") or connection_name_from_tag(settings["tag"]),
            "port": entry.get("port") or settings["port"],
            "sni": entry.get("sni") or settings["sni"],
            "fingerprint": entry.get("fingerprint") or current_fingerprint(),
        })
    return rows


def print_connection_selection_table(rows: list[dict[str, str | int]]) -> None:
    headers = ("№", "NAME", "TAG", "PORT", "SNI", "FINGERPRINT")
    values = [
        (
            str(index),
            row["name"],
            row["tag"],
            row["port"],
            row["sni"],
            row["fingerprint"],
        )
        for index, row in enumerate(rows, start=1)
    ]
    values.append(("0", "Назад", "", "", "", ""))
    widths = [
        max(len(headers[column]), *(len(str(row[column])) for row in values))
        for column in range(len(headers))
    ]
    border = table_border(widths)
    print(border)
    print(table_row(headers, widths))
    print(border)
    for row in values:
        print(table_row(row, widths))
    print(border)


def choose_connection(action: str, auto_single: bool = True) -> str:
    rows = connection_rows()
    if not rows:
        die("No Reality connections found.")
    if len(rows) == 1 and auto_single:
        return str(rows[0]["tag"])
    print(f"Выбери подключение для действия: {action}.")
    print_connection_selection_table(rows)
    while True:
        choice = input("Подключение: ").strip()
        if choice == "0":
            return ""
        if re.fullmatch(r"[0-9]+", choice):
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return str(rows[index - 1]["tag"])
        print("Неизвестное подключение. Выбери номер из списка или 0 для возврата.")


def port_used_by_other_inbound(config: dict, port: int, current_tag: str) -> bool:
    for inbound in config.get("inbounds", []):
        if inbound.get("port") == port and inbound_tag(inbound) != current_tag:
            return True
    return False


def current_settings(config: dict, connection_tag: str | None = None) -> tuple[dict, dict, int, str, str]:
    if connection_tag:
        inbound = find_inbound_by_tag(config, connection_tag)
    else:
        inbound = find_inbound(config)
    reality = inbound.get("streamSettings", {}).get("realitySettings", {})
    server_names = reality.get("serverNames") or [""]
    port = int(inbound.get("port", 443))
    sni = server_names[0]
    dest = reality.get("dest", reality_dest(sni))
    return inbound, reality, port, sni, dest


def update_connection_db(tag: str, port=None, sni=None, dest=None, fingerprint=None) -> None:
    db = load_db()
    connections = db_connections(db)
    entry = connections.setdefault(tag, {"tag": tag, "name": connection_name_from_tag(tag)})
    if port is not None:
        entry["port"] = port
    if sni is not None:
        entry["sni"] = sni
    if dest is not None:
        entry["dest"] = dest
    if fingerprint is not None:
        entry["fingerprint"] = fingerprint
    save_db(db)


def write_server_env(port: int, sni: str, dest: str, fingerprint: str | None = None) -> None:
    values = server_env_values()
    values["PORT"] = str(port)
    values["REALITY_SNI"] = sni
    values["REALITY_DEST"] = dest
    values["FINGERPRINT"] = validate_fingerprint(fingerprint) if fingerprint is not None else current_fingerprint()
    values.setdefault("SERVER_ADDR", "")
    save_server_env_values(values)


def apply_config(config: dict) -> Path:
    backup = save_config(config)
    try:
        run([str(XRAY_BIN), "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed. Restored backup: {backup}")
    return backup


def refresh_initial_link() -> None:
    config = load_config()
    inbound = find_inbound(config)
    clients = inbound.get("settings", {}).get("clients", [])
    if not clients:
        return
    name = clients[0].get("email", "starter").split("|created=", 1)[0]
    result = subprocess.run(
        ["xray-client", "link", name],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return

    _, _, port, sni, dest = current_settings(config)
    fp = current_fingerprint()
    link = result.stdout.strip()
    content = (
        f"CLIENT_URI={link}\n"
        f"PORT={port}\n"
        "PROTOCOL=VLESS\n"
        "SECURITY=REALITY\n"
        "FLOW=xtls-rprx-vision\n"
        f"SNI={sni}\n"
        f"DEST={dest}\n"
        f"FINGERPRINT={fp}\n"
    )
    CLIENT_LINK_PATH.write_text(content)
    os.chmod(CLIENT_LINK_PATH, 0o600)


def prompt(default, message: str) -> str:
    value = input(f"{message} [{default}]: ").strip()
    return value or str(default)


def show_settings(call: CommandRunner) -> None:
    call(["xray-client", "connection-list"])


def create_connection(call: CommandRunner) -> None:
    print("Новое подключение создаёт отдельный VLESS Reality inbound с собственным портом и SNI.")
    name = input("Имя подключения: ").strip()
    print("PORT: публичный TCP-порт для подключения клиентов. Он не должен совпадать с уже занятыми портами.")
    port = str(validate_port(input("PORT: ").strip()))
    print("REALITY_SNI: реальный HTTPS-домен без https:// и без порта.")
    sni = validate_host(input("REALITY_SNI: ").strip(), "REALITY_SNI")
    fp = choose_fingerprint(current_fingerprint())
    call(["xray-client", "add-connection", name, port, sni, fp])


def update_port() -> None:
    config = load_config()
    tag = choose_connection("обновления PORT")
    if not tag:
        print("Действие отменено.")
        return
    inbound, reality, current_port, sni, _ = current_settings(config, tag)
    print()
    print("Новый PORT: публичный TCP-порт для подключения клиентов.")
    print("DEST будет автоматически пересчитан как REALITY_SNI:443.")
    port = validate_port(prompt(current_port, "PORT"))
    if port_used_by_other_inbound(config, port, tag):
        die(f"PORT is already used by another inbound: {port}")
    dest = reality_dest(sni)
    inbound["port"] = port
    reality["dest"] = dest
    backup = apply_config(config)
    update_connection_db(tag, port=port, sni=sni, dest=dest)
    if tag == INBOUND_TAG:
        write_server_env(port, sni, dest)
    refresh_initial_link()
    print(f"PORT обновлён: {port}")
    print(f"REALITY_DEST обновлён: {dest}")
    print(f"Backup: {backup}")


def update_sni() -> None:
    config = load_config()
    tag = choose_connection("обновления REALITY_SNI")
    if not tag:
        print("Действие отменено.")
        return
    _, reality, port, current_sni, _ = current_settings(config, tag)
    print()
    print("Новый REALITY_SNI: реальный HTTPS-домен без https:// и без порта.")
    print("DEST будет автоматически пересчитан как REALITY_SNI:443.")
    sni = validate_host(prompt(current_sni, "REALITY_SNI"), "REALITY_SNI")
    dest = reality_dest(sni)
    reality["serverNames"] = [sni]
    reality["dest"] = dest
    backup = apply_config(config)
    update_connection_db(tag, port=port, sni=sni, dest=dest)
    if tag == INBOUND_TAG:
        write_server_env(port, sni, dest)
    refresh_initial_link()
    print(f"REALITY_SNI обновлён: {sni}")
    print(f"REALITY_DEST обновлён: {dest}")
    print(f"Backup: {backup}")


def update_port_and_sni() -> None:
    config = load_config()
    tag = choose_connection("обновления PORT и REALITY_SNI")
    if not tag:
        print("Действие отменено.")
        return
    inbound, reality, current_port, current_sni, _ = current_settings(config, tag)
    print()
    print("Обновление PORT и REALITY_SNI.")
    print("DEST будет автоматически пересчитан как REALITY_SNI:443.")
    port = validate_port(prompt(current_port, "PORT"))
    if port_used_by_other_inbound(config, port, tag):
        die(f"PORT is already used by another inbound: {port}")
    sni = validate_host(prompt(current_sni, "REALITY_SNI"), "REALITY_SNI")
    dest = reality_dest(sni)
    inbound["port"] = port
    reality["serverNames"] = [sni]
    reality["dest"] = dest
    backup = apply_config(config)
    update_connection_db(tag, port=port, sni=sni, dest=dest)
    if tag == INBOUND_TAG:
        write_server_env(port, sni, dest)
    refresh_initial_link()
    print(f"PORT обновлён: {port}")
    print(f"REALITY_SNI обновлён: {sni}")
    print(f"REALITY_DEST обновлён: {dest}")
    print(f"Backup: {backup}")


def choose_fingerprint(default: str) -> str:
    print()
    print("Новый FINGERPRINT: маскировка браузера/uTLS в клиентской VLESS-ссылке.")
    print("Обычно оставляют chrome. Если нужно, выбери другой профиль из списка.")
    for index, value in enumerate(FINGERPRINTS, start=1):
        print(f"  {index}) {value}")
    value = input(f"FINGERPRINT [{default}] (номер или значение): ").strip().lower()
    value = value or default
    if value.isdigit():
        index = int(value, 10)
        if 1 <= index <= len(FINGERPRINTS):
            return FINGERPRINTS[index - 1]
    return validate_fingerprint(value)


def update_fingerprint() -> None:
    config = load_config()
    tag = choose_connection("обновления FINGERPRINT")
    if not tag:
        print("Действие отменено.")
        return
    _, _, port, sni, dest = current_settings(config, tag)
    fp = choose_fingerprint(current_fingerprint())
    update_connection_db(tag, port=port, sni=sni, dest=dest, fingerprint=fp)
    if tag == INBOUND_TAG:
        write_server_env(port, sni, dest, fp)
    refresh_initial_link()
    print(f"FINGERPRINT обновлён: {fp}")
    print("Xray перезапускать не нужно. Выведи клиенту новую ссылку через xray-client link ИМЯ.")


def delete_connection(call: CommandRunner, confirm: ConfirmCallback) -> None:
    rows = connection_rows()
    if len(rows) <= 1:
        print("Нельзя удалить последнее Reality-подключение. Сначала создай другое подключение.")
        return
    tag = choose_connection("удаления подключения", auto_single=False)
    if not tag:
        print("Действие отменено.")
        return
    row = next((item for item in rows if item["tag"] == tag), None)
    name = row["name"] if row else tag
    print()
    print(f"Будет удалено подключение: {name} ({tag})")
    print("Все клиенты этого подключения также будут удалены вместе с их историей трафика.")
    if not confirm("Продолжить удаление"):
        print("Удаление отменено.")
        return
    call(["xray-client", "remove-connection", tag])
    refresh_initial_link()
