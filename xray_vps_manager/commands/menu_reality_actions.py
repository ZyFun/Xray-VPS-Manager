"""Reality connection actions used by the interactive menu."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from xray_vps_manager.clients import connections as connection_store
from xray_vps_manager.clients.repository import db_connections, load_db_sql, save_db
from xray_vps_manager.clients.settings import (
    fingerprint as server_fingerprint,
    save_server_env_values,
    server_env_values,
)
from xray_vps_manager.core.paths import CONFIG_PATH, XRAY_BIN
from xray_vps_manager.core.terminal import table_border, table_row
from xray_vps_manager.xray.config import (
    DEFAULT_XHTTP_TLS_PUBLIC_PORT,
    INBOUND_TAG,
    connection_name_from_tag,
    connection_settings_from_inbound,
    find_inbound,
    find_inbound_by_tag,
    inbound_tag,
    load_config as load_xray_config,
    normalize_grpc_service_name,
    normalize_reality_transport,
    normalize_xhttp_mode,
    normalize_xhttp_path,
    reality_dest,
    reality_inbounds,
    vless_connection_inbounds,
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
TRANSPORTS = ["tcp", "grpc", "xhttp"]

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


def validate_transport(value: str) -> str:
    try:
        return normalize_reality_transport(value)
    except ValueError as exc:
        die(str(exc))


def validate_grpc_service_name(value: str) -> str:
    try:
        return normalize_grpc_service_name(value)
    except ValueError as exc:
        die(str(exc))


def validate_xhttp_path(value: str) -> str:
    try:
        return normalize_xhttp_path(value)
    except ValueError as exc:
        die(str(exc))


def validate_xhttp_mode(value: str) -> str:
    try:
        return normalize_xhttp_mode(value)
    except ValueError as exc:
        die(str(exc))


def current_fingerprint() -> str:
    try:
        value = server_fingerprint().strip().lower()
    except ValueError:
        return "chrome"
    return value if value in FINGERPRINTS else "chrome"


def connection_rows(security_filter: str | None = None) -> list[dict[str, str | int]]:
    config = load_config()
    db = load_db_sql()
    connection_store.ensure_connections(config, db)
    rows = []
    for inbound in vless_connection_inbounds(config):
        settings = connection_settings_from_inbound(inbound)
        entry = db_connections(db).get(settings["tag"], {})
        security = entry.get("security") or ("reality" if settings.get("security") == "reality" else "tls")
        if security_filter and security != security_filter:
            continue
        rows.append({
            "tag": settings["tag"],
            "name": entry.get("name") or connection_name_from_tag(settings["tag"]),
            "security": security,
            "port": entry.get("port") or settings["port"],
            "sni": entry.get("sni") or settings["sni"],
            "transport": entry.get("transport") or settings["transport"],
            "fingerprint": (entry.get("fingerprint") or current_fingerprint()) if security == "reality" else "-",
        })
    return rows


def print_connection_selection_table(rows: list[dict[str, str | int]]) -> None:
    headers = ("№", "NAME", "TAG", "SECURITY", "PORT", "SNI", "TRANSPORT", "FINGERPRINT")
    values = [
        (
            str(index),
            row["name"],
            row["tag"],
            row["security"],
            row["port"],
            row["sni"],
            row["transport"],
            row["fingerprint"],
        )
        for index, row in enumerate(rows, start=1)
    ]
    values.append(("0", "Назад", "", "", "", "", "", ""))
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


def choose_connection(action: str, auto_single: bool = True, security_filter: str | None = None) -> str:
    rows = connection_rows(security_filter=security_filter)
    if not rows:
        die("No matching VLESS connections found.")
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


def update_connection_db(
    tag: str,
    port=None,
    sni=None,
    dest=None,
    fingerprint=None,
    transport=None,
    grpc_service_name=None,
    xhttp_path=None,
    xhttp_mode=None,
) -> None:
    db = load_db_sql()
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
    for key in ("transport", "grpcServiceName", "xhttpPath", "xhttpMode"):
        if key in entry and (
            transport is not None or grpc_service_name is not None or xhttp_path is not None or xhttp_mode is not None
        ):
            entry.pop(key, None)
    if transport is not None:
        entry["transport"] = validate_transport(transport)
        if entry["transport"] == "grpc":
            entry["grpcServiceName"] = validate_grpc_service_name(grpc_service_name or "")
        elif entry["transport"] == "xhttp":
            entry["xhttpPath"] = validate_xhttp_path(xhttp_path or "")
            entry["xhttpMode"] = validate_xhttp_mode(xhttp_mode or "")
    save_db(db)


def write_server_env(
    port: int,
    sni: str,
    dest: str,
    fingerprint: str | None = None,
    transport: str | None = None,
    grpc_service_name: str | None = None,
    xhttp_path: str | None = None,
    xhttp_mode: str | None = None,
) -> None:
    values = server_env_values()
    values["PORT"] = str(port)
    values["REALITY_SNI"] = sni
    values["REALITY_DEST"] = dest
    values["FINGERPRINT"] = validate_fingerprint(fingerprint) if fingerprint is not None else current_fingerprint()
    if transport is not None:
        values["REALITY_TRANSPORT"] = validate_transport(transport)
        values.pop("GRPC_SERVICE_NAME", None)
        values.pop("XHTTP_PATH", None)
        values.pop("XHTTP_MODE", None)
        if values["REALITY_TRANSPORT"] == "grpc":
            values["GRPC_SERVICE_NAME"] = validate_grpc_service_name(grpc_service_name or "")
        elif values["REALITY_TRANSPORT"] == "xhttp":
            values["XHTTP_PATH"] = validate_xhttp_path(xhttp_path or "")
            values["XHTTP_MODE"] = validate_xhttp_mode(xhttp_mode or "")
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

    inbound, _, port, sni, dest = current_settings(config)
    settings = connection_settings_from_inbound(inbound)
    fp = current_fingerprint()
    link = result.stdout.strip()
    flow = "xtls-rprx-vision" if settings["transport"] == "tcp" else ""
    content = (
        f"CLIENT_URI={link}\n"
        f"PORT={port}\n"
        "PROTOCOL=VLESS\n"
        "SECURITY=REALITY\n"
        f"TRANSPORT={settings['transport']}\n"
        f"FLOW={flow}\n"
        f"SNI={sni}\n"
        f"DEST={dest}\n"
        f"FINGERPRINT={fp}\n"
    )
    CLIENT_LINK_PATH.write_text(content)
    os.chmod(CLIENT_LINK_PATH, 0o600)


def prompt(default, message: str) -> str:
    value = input(f"{message} [{default}]: ").strip()
    return value or str(default)


def ask_yes_no(message: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{message} [{suffix}]: ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes", "д", "да")


def choose_security(default: str = "reality") -> str:
    print()
    print("SECURITY: тип входящего подключения.")
    print("  1) reality - текущая схема без своего домена и сертификата")
    print("  2) tls     - XHTTP через Caddy с доменом и автоматическим сертификатом")
    raw = input(f"SECURITY [{default}] (номер или значение): ").strip().lower() or default
    if raw == "1":
        return "reality"
    if raw == "2":
        return "tls"
    if raw in ("reality", "tls"):
        return raw
    die("SECURITY must be reality or tls.")


def choose_transport(default: str = "tcp") -> dict[str, str]:
    print()
    print("TRANSPORT: транспорт VLESS Reality для этого подключения.")
    print("  tcp   - TCP transport с Vision flow")
    print("  grpc  - gRPC поверх HTTP/2")
    print("  xhttp - XHTTP/XMUX")
    for index, value in enumerate(TRANSPORTS, start=1):
        print(f"  {index}) {value}")
    raw = input(f"TRANSPORT [{default}] (номер или значение): ").strip().lower()
    raw = raw or default
    if raw.isdigit():
        index = int(raw, 10)
        if 1 <= index <= len(TRANSPORTS):
            raw = TRANSPORTS[index - 1]
    transport = validate_transport(raw)
    settings = {"transport": transport}
    if transport == "grpc":
        service_name = prompt("vless-grpc", "GRPC serviceName")
        settings["grpc_service_name"] = validate_grpc_service_name(service_name)
    elif transport == "xhttp":
        path = prompt("/vless-xhttp", "XHTTP path")
        mode = prompt("auto", "XHTTP mode")
        settings["xhttp_path"] = validate_xhttp_path(path)
        settings["xhttp_mode"] = validate_xhttp_mode(mode)
    return settings


def show_settings(call: CommandRunner) -> None:
    call(["xray-client", "connection-list"])


def create_connection(call: CommandRunner) -> None:
    security = choose_security("reality")
    name = input("Имя подключения: ").strip()
    if security == "tls":
        create_tls_xhttp_connection(call, name)
        return
    print("Новое подключение создаёт отдельный VLESS Reality inbound с собственным портом и SNI.")
    print("PORT: публичный TCP-порт для подключения клиентов. Он не должен совпадать с уже занятыми портами.")
    port = str(validate_port(input("PORT: ").strip()))
    print("REALITY_SNI: реальный HTTPS-домен без https:// и без порта.")
    sni = validate_host(input("REALITY_SNI: ").strip(), "REALITY_SNI")
    fp = choose_fingerprint(current_fingerprint())
    transport = choose_transport("tcp")
    command = ["xray-client", "add-connection", name, port, sni, fp, "--transport", transport["transport"]]
    if transport["transport"] == "grpc":
        command.extend(["--grpc-service-name", transport["grpc_service_name"]])
    elif transport["transport"] == "xhttp":
        command.extend(["--xhttp-path", transport["xhttp_path"], "--xhttp-mode", transport["xhttp_mode"]])
    call(command)


def create_tls_xhttp_connection(call: CommandRunner, name: str) -> None:
    config = load_config()
    print("TLS-XHTTP подключение работает через Caddy: публично api.domain:443, внутри Xray слушает 127.0.0.1:LOCAL_PORT.")
    domain = validate_host(input("TLS domain/SNI: ").strip(), "TLS_DOMAIN")
    default_local_port = connection_store.next_local_port(config)
    local_port = str(validate_port(prompt(default_local_port, "LOCAL_PORT для Xray")))
    public_port = str(validate_port(prompt(DEFAULT_XHTTP_TLS_PUBLIC_PORT, "PUBLIC_PORT для Caddy")))
    path = validate_xhttp_path(prompt("/vless-xhttp", "XHTTP path"))
    mode = validate_xhttp_mode(prompt("auto", "XHTTP mode"))
    tls_min = prompt("tls1.2", "TLS min version (tls1.2/tls1.3/default)")
    tls_max = prompt("tls1.2", "TLS max version (tls1.2/tls1.3/default)")
    install_caddy = ask_yes_no("Установить и настроить Caddy сейчас", True)
    if install_caddy:
        conflicts = connection_store.public_port_conflicts(config, int(public_port))
        if conflicts:
            tags = ", ".join(inbound.get("tag") or "(no-tag)" for inbound in conflicts)
            print(f"Caddy не сможет занять публичный порт {public_port}: сейчас его слушает Xray inbound: {tags}.")
            print("Сначала перенеси существующее Reality-подключение на другой публичный порт, затем повтори настройку Caddy.")
            return
    command = [
        "xray-client",
        "add-connection",
        name,
        local_port,
        domain,
        "--security",
        "tls",
        "--transport",
        "xhttp",
        "--xhttp-path",
        path,
        "--xhttp-mode",
        mode,
        "--public-port",
        public_port,
        "--tls-min-version",
        tls_min,
        "--tls-max-version",
        tls_max,
    ]
    if install_caddy:
        command.append("--install-caddy")
    call(command)


def update_port() -> None:
    config = load_config()
    tag = choose_connection("обновления PORT", security_filter="reality")
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
    tag = choose_connection("обновления REALITY_SNI", security_filter="reality")
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
    tag = choose_connection("обновления PORT и REALITY_SNI", security_filter="reality")
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
    tag = choose_connection("обновления FINGERPRINT", security_filter="reality")
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


def update_transport(call: CommandRunner) -> None:
    config = load_config()
    tag = choose_connection("обновления TRANSPORT")
    if not tag:
        print("Действие отменено.")
        return
    inbound = find_inbound_by_tag(config, tag)
    current = connection_settings_from_inbound(inbound)
    settings = choose_transport(str(current.get("transport") or "tcp"))
    command = ["xray-client", "connection-transport", tag, settings["transport"]]
    if settings["transport"] == "grpc":
        command.extend(["--grpc-service-name", settings["grpc_service_name"]])
    elif settings["transport"] == "xhttp":
        command.extend(["--xhttp-path", settings["xhttp_path"], "--xhttp-mode", settings["xhttp_mode"]])
    call(command)
    refresh_initial_link()


def delete_connection(call: CommandRunner, confirm: ConfirmCallback) -> None:
    rows = connection_rows()
    if len(rows) <= 1:
        print("Нельзя удалить последнее VLESS-подключение. Сначала создай другое подключение.")
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


def rename_connection(call: CommandRunner) -> None:
    tag = choose_connection("переименования подключения", auto_single=False)
    if not tag:
        print("Действие отменено.")
        return
    rows = connection_rows()
    row = next((item for item in rows if item["tag"] == tag), None)
    current_name = row["name"] if row else tag
    new_name = input(f"Новое имя подключения [{current_name}]: ").strip()
    if not new_name or new_name == current_name:
        print("Имя не изменено.")
        return
    call(["xray-client", "connection-rename", tag, new_name])
