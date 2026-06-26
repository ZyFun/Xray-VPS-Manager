#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from xray_vps_manager.activity import parser as activity_parser
from xray_vps_manager.core.paths import CONFIG_PATH, XRAY_BIN
from xray_vps_manager.core.server_env import ORDERED_ENV_KEYS, read_server_env, write_server_env
from xray_vps_manager.core.terminal import print_table
from xray_vps_manager.db import database as sqlite_database
from xray_vps_manager.db.repositories import bypass as sqlite_bypass
from xray_vps_manager.telegram import bypass_notifications
from xray_vps_manager.xray import bypass as bypass_config
from xray_vps_manager.xray import config as xray_config
from xray_vps_manager.xray import outbound_links

TEST_INBOUND_TAG = "bypass-test-socks"
TEST_SOCKS_HOST = "127.0.0.1"
TEST_SOCKS_PORT = 10809
GEOIP_REGION_PRESETS = [
    ("RU", "Россия"),
    ("US", "США"),
    ("CN", "Китай"),
    ("KZ", "Казахстан"),
    ("BY", "Беларусь"),
    ("UA", "Украина"),
    ("TR", "Турция"),
    ("DE", "Германия"),
    ("NL", "Нидерланды"),
    ("FI", "Финляндия"),
    ("EE", "Эстония"),
    ("GB", "Великобритания"),
]


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config() -> dict:
    try:
        return xray_config.load_config(CONFIG_PATH)
    except FileNotFoundError as exc:
        die(str(exc))


def write_server_env_values(values: dict[str, str]) -> None:
    write_server_env(values, ordered_keys=ORDERED_ENV_KEYS)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def run_capture(command: list[str], timeout: int = 20):
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def apply_config(config: dict, before: dict) -> Path:
    backup = xray_config.save_config(config, CONFIG_PATH)
    try:
        run([str(XRAY_BIN), "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
    except subprocess.CalledProcessError:
        CONFIG_PATH.write_text(json.dumps(before, indent=2, ensure_ascii=False) + "\n")
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed validation. Restored previous config. Backup: {backup}")
    return backup


def open_db():
    return sqlite_database.open_database()


def region_label_for_code(code: str) -> str:
    normalized = bypass_config.normalize_region_code(code)
    labels = {item_code: label for item_code, label in GEOIP_REGION_PRESETS}
    return labels.get(normalized, "geoip.dat")


def print_geoip_region_table(rows: list[tuple[str, str]], include_search: bool = False) -> None:
    values = [(index, code, label) for index, (code, label) in enumerate(rows, start=1)]
    print_table(["#", "CODE", "DESCRIPTION"], values + ([("S", "Search", "find another code in geoip.dat")] if include_search else []))
    print("0\tCancel")


def search_geoip_region() -> tuple[str, str]:
    while True:
        query = input("Region filter or GeoIP code, e.g. Россия, RU or U (Enter - cancel): ").strip()
        if not query:
            return "", ""
        query_code = query.upper()
        query_text = query.lower()
        rows: list[tuple[str, str]] = []
        seen = set()
        for code, label in GEOIP_REGION_PRESETS:
            if query_code in code or query_text in label.lower():
                rows.append((code, label))
                seen.add(code)
        for code in activity_parser.available_geoip_codes():
            if query_code in code and code not in seen:
                rows.append((code, "geoip.dat"))
                seen.add(code)
        if not rows:
            print("No GeoIP regions found for this filter.")
            continue
        displayed = rows[:30]
        print_geoip_region_table(displayed)
        selected = choose_geoip_region_from_rows(displayed, "GeoIP region: ")
        if selected:
            return selected, region_label_for_code(selected)
        return "", ""


def choose_geoip_region_from_rows(rows: list[tuple[str, str]], prompt: str) -> str:
    while True:
        choice = input(prompt).strip().lower()
        if choice in ("", "0"):
            return ""
        if choice.isdigit():
            index = int(choice, 10)
            if 1 <= index <= len(rows):
                return rows[index - 1][0]
        print("Unknown region. Choose a number from the list or 0 to cancel.")


def choose_geoip_region() -> tuple[str, str]:
    while True:
        print("Choose GeoIP region for this bypass route.")
        print_geoip_region_table(GEOIP_REGION_PRESETS, include_search=True)
        choice = input("GeoIP region: ").strip().lower()
        if choice in ("", "0"):
            return "", ""
        if choice in ("s", "search", "поиск"):
            selected, label = search_geoip_region()
            if selected:
                return selected, label
            continue
        if choice.isdigit():
            index = int(choice, 10)
            if 1 <= index <= len(GEOIP_REGION_PRESETS):
                code, label = GEOIP_REGION_PRESETS[index - 1]
                return code, label
        print("Unknown region. Choose a number, S for search, or 0 to cancel.")


def read_region(args_region: str = "") -> tuple[str, str]:
    if args_region:
        code = bypass_config.normalize_region_code(args_region)
        return code, region_label_for_code(code)
    if not sys.stdin.isatty():
        die("--region CODE is required in non-interactive mode.")
    code, label = choose_geoip_region()
    if not code:
        die("GeoIP region is required.")
    return bypass_config.normalize_region_code(code), label


def read_vless_link() -> str:
    if sys.stdin.isatty():
        print("Paste bypass VLESS link and press Enter:")
    uri = sys.stdin.readline().strip()
    if not uri:
        die("Empty link.")
    return uri


def outbound_address(outbound: dict | None) -> str:
    vnext = (outbound or {}).get("settings", {}).get("vnext", [])
    if not vnext:
        return "-"
    address = vnext[0].get("address", "")
    port = vnext[0].get("port", "")
    return f"{address}:{port}" if address and port else address or "-"


def route_status(config: dict, record: dict) -> str:
    region = record.get("regionCode", "")
    if not record.get("enabled"):
        return "disabled"
    if bypass_config.configured_bypass_for_warning(config, region) == record.get("tag"):
        return "enabled"
    return "drift"


def print_bypass_table(config: dict, routes: dict[str, dict]) -> None:
    rows = []
    for index, tag in enumerate(sorted(set(routes) | {item.get("tag", "") for item in bypass_config.bypass_outbounds(config)}), start=1):
        if not tag:
            continue
        record = routes.get(tag, {})
        outbound = bypass_config.outbound_by_tag(config, tag)
        rows.append(
            [
                index,
                bypass_config.bypass_name_from_tag(tag) or record.get("name", ""),
                record.get("regionCode", "-") or "-",
                record.get("regionLabel", "") or "-",
                tag,
                route_status(config, record) if record else "outbound-only",
                outbound_address(outbound),
            ]
        )
    print_table(
        ["#", "Name", "Region", "Label", "Tag", "Route", "Address"],
        rows,
        empty_message="GeoIP bypass routes are not configured.",
    )


def normalize_name_or_die(value: str) -> str:
    try:
        return bypass_config.normalize_bypass_name(value)
    except ValueError as exc:
        die(str(exc))


def tag_from_name_or_die(value: str) -> str:
    return bypass_config.bypass_tag(normalize_name_or_die(value))


def choose_bypass_tag(config: dict, routes: dict[str, dict], prompt: str = "Bypass") -> str:
    tags = sorted(set(routes) | {str(item.get("tag") or "") for item in bypass_config.bypass_outbounds(config)})
    tags = [tag for tag in tags if tag]
    if not tags:
        die("GeoIP bypass routes are not configured. Add a bypass first.")
    while True:
        print_bypass_table(config, routes)
        default = "1" if len(tags) == 1 else ""
        suffix = f" [{default}]" if default else ""
        choice = input(f"{prompt}{suffix}: ").strip()
        if not choice and default:
            choice = default
        if choice.isdigit():
            index = int(choice, 10)
            if 1 <= index <= len(tags):
                return tags[index - 1]
        try:
            tag = bypass_config.bypass_tag(choice)
        except ValueError:
            tag = choice
        if tag in tags:
            return tag
        print("Unknown bypass. Choose a number, name, or tag from the table.")


def parse_common_options(args: list[str]) -> tuple[list[str], dict[str, str | bool]]:
    options: dict[str, str | bool] = {"region": "", "replace": False}
    rest: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--region":
            if index + 1 >= len(args):
                die("--region requires value.")
            options["region"] = args[index + 1]
            index += 2
            continue
        if item == "--replace":
            options["replace"] = True
            index += 1
            continue
        rest.append(item)
        index += 1
    return rest, options


def confirm_replace(conflict: dict, region_code: str) -> bool:
    if not sys.stdin.isatty():
        return False
    print(f"GeoIP region {region_code} is already enabled on {conflict.get('tag')}.")
    answer = input("Replace the active bypass route for this region? [y/N]: ").strip().lower()
    return answer in ("y", "yes", "д", "да")


def disable_conflicting_region_route(
    connection,
    config: dict,
    tag: str,
    region_code: str,
    *,
    replace: bool,
    env_values: dict[str, str],
) -> dict | None:
    conflict = sqlite_bypass.active_route_for_region(connection, region_code)
    if not conflict or conflict.get("tag") == tag:
        return None
    if not replace and not confirm_replace(conflict, region_code):
        die(f"GeoIP region {region_code} is already enabled on {conflict.get('tag')}. Use --replace to replace it.")
    bypass_config.disable_bypass_route(
        config,
        str(conflict.get("tag") or ""),
        str(conflict.get("regionCode") or region_code),
        env_values=env_values,
    )
    return conflict


def apply_enable(
    connection,
    config: dict,
    record: dict,
    *,
    replace: bool = False,
    env_values: dict[str, str],
) -> dict | None:
    region = bypass_config.normalize_region_code(str(record.get("regionCode") or ""))
    conflict = disable_conflicting_region_route(
        connection,
        config,
        str(record.get("tag") or ""),
        region,
        replace=replace,
        env_values=env_values,
    )
    bypass_config.ensure_geoip_domain_strategy(config, env_values)
    bypass_config.apply_bypass_route(config, str(record.get("tag") or ""), region)
    return conflict


def save_route(connection, record: dict) -> None:
    sqlite_bypass.upsert_route(connection, str(record["tag"]), record)


def notify_config_event(event: str, record: dict) -> None:
    try:
        bypass_notifications.notify_config_event(event, record)
    except Exception as exc:
        print(f"WARN: Telegram bypass config notification skipped: {exc}", file=sys.stderr)


def cmd_status() -> None:
    config = load_config()
    connection = open_db()
    try:
        routes = sqlite_bypass.list_routes(connection)
    finally:
        connection.close()
    print_bypass_table(config, routes)


def cmd_add(args: list[str]) -> None:
    rest, options = parse_common_options(args)
    name = normalize_name_or_die(rest[0]) if rest else None
    if not name:
        if not sys.stdin.isatty():
            die("Usage: xray-set-bypass add NAME --region CODE")
        name = normalize_name_or_die(input("Bypass name: ").strip())
    tag = bypass_config.bypass_tag(name)
    if not sys.stdin.isatty() and not options["region"]:
        die("--region CODE is required in non-interactive mode.")
    uri = read_vless_link()
    try:
        outbound, label = outbound_links.parse_vless_outbound(uri, tag)
    except ValueError as exc:
        die(str(exc))
    region, region_label = read_region(str(options["region"] or ""))

    config = load_config()
    before = copy.deepcopy(config)
    env_values = read_server_env()
    connection = open_db()
    try:
        bypass_config.upsert_bypass_outbound(config, outbound)
        stamp = utc_stamp()
        existing = sqlite_bypass.get_route(connection, tag) or {}
        record = {
            "tag": tag,
            "name": name,
            "regionCode": region,
            "regionLabel": region_label,
            "label": label,
            "enabled": True,
            "created": existing.get("created") or stamp,
            "updated": stamp,
        }
        conflict = apply_enable(
            connection,
            config,
            record,
            replace=bool(options["replace"]),
            env_values=env_values,
        )
        backup = apply_config(config, before)
        if conflict:
            sqlite_bypass.set_enabled(connection, str(conflict.get("tag") or ""), False, utc_stamp())
        save_route(connection, record)
        write_server_env_values(env_values)
    finally:
        connection.close()

    print(f"GeoIP bypass configured: {name} ({tag})")
    print(f"Region: {region} / {region_label}")
    print(f"Upstream: {label}")
    if conflict:
        print(f"Replaced active route: {conflict.get('tag')}")
    print(f"Route: geoip:{region.lower()} -> {bypass_config.geoip_warning_tag(region)} -> {tag}")
    print(f"Backup: {backup}")
    notify_config_event("enabled", record)


def route_record_or_die(connection, tag: str) -> dict:
    record = sqlite_bypass.get_route(connection, tag)
    if not record:
        die(f"GeoIP bypass route metadata not found: {tag}")
    return record


def cmd_enable(args: list[str]) -> None:
    rest, options = parse_common_options(args)
    config = load_config()
    connection = open_db()
    try:
        routes = sqlite_bypass.list_routes(connection)
        tag = tag_from_name_or_die(rest[0]) if rest else choose_bypass_tag(config, routes, "Enable bypass")
        record = route_record_or_die(connection, tag)
        if not bypass_config.bypass_outbound(config, tag):
            die(f"Bypass outbound is not configured in Xray config: {tag}")
        before = copy.deepcopy(config)
        env_values = read_server_env()
        conflict = apply_enable(
            connection,
            config,
            record,
            replace=bool(options["replace"]),
            env_values=env_values,
        )
        backup = apply_config(config, before)
        if conflict:
            sqlite_bypass.set_enabled(connection, str(conflict.get("tag") or ""), False, utc_stamp())
        record["enabled"] = True
        record["updated"] = utc_stamp()
        save_route(connection, record)
        write_server_env_values(env_values)
    finally:
        connection.close()
    print(f"GeoIP bypass enabled: {tag}")
    print(f"Region: {record.get('regionCode')} / {record.get('regionLabel') or '-'}")
    if conflict:
        print(f"Replaced active route: {conflict.get('tag')}")
    print(f"Backup: {backup}")
    notify_config_event("enabled", record)


def cmd_region(args: list[str]) -> None:
    rest, options = parse_common_options(args)
    if len(rest) < 1 and not sys.stdin.isatty():
        die("Usage: xray-set-bypass region NAME [CODE] [--replace]")
    config = load_config()
    connection = open_db()
    try:
        routes = sqlite_bypass.list_routes(connection)
        tag = tag_from_name_or_die(rest[0]) if rest else choose_bypass_tag(config, routes, "Bypass")
        record = route_record_or_die(connection, tag)
        old_region = str(record.get("regionCode") or "")
        region_arg = rest[1] if len(rest) >= 2 else str(options["region"] or "")
        region, region_label = read_region(region_arg)
        before = copy.deepcopy(config)
        env_values = read_server_env()
        if record.get("enabled"):
            if old_region:
                bypass_config.disable_bypass_route(config, tag, old_region, env_values=env_values)
            record["regionCode"] = region
            record["regionLabel"] = region_label
            conflict = apply_enable(connection, config, record, replace=bool(options["replace"]), env_values=env_values)
            backup = apply_config(config, before)
            if conflict:
                sqlite_bypass.set_enabled(connection, str(conflict.get("tag") or ""), False, utc_stamp())
        else:
            record["regionCode"] = region
            record["regionLabel"] = region_label
            backup = None
        record["updated"] = utc_stamp()
        save_route(connection, record)
        write_server_env_values(env_values)
    finally:
        connection.close()
    print(f"GeoIP bypass region updated: {tag}")
    print(f"Region: {region} / {region_label}")
    if backup:
        print(f"Backup: {backup}")
    notify_config_event("region-changed", record)


def cmd_disable(args: list[str]) -> None:
    config = load_config()
    connection = open_db()
    try:
        routes = sqlite_bypass.list_routes(connection)
        tag = tag_from_name_or_die(args[0]) if args else choose_bypass_tag(config, routes, "Disable bypass")
        record = route_record_or_die(connection, tag)
        before = copy.deepcopy(config)
        env_values = read_server_env()
        bypass_config.disable_bypass_route(config, tag, str(record.get("regionCode") or ""), env_values=env_values)
        backup = apply_config(config, before)
        record["enabled"] = False
        record["updated"] = utc_stamp()
        save_route(connection, record)
        write_server_env_values(env_values)
    finally:
        connection.close()
    print(f"GeoIP bypass disabled: {tag}")
    print(f"Backup: {backup}")
    notify_config_event("disabled", record)


def confirm_remove(tag: str) -> bool:
    if not sys.stdin.isatty():
        return True
    answer = input(f"Remove GeoIP bypass {tag}? [y/N]: ").strip().lower()
    return answer in ("y", "yes", "д", "да")


def cmd_remove(args: list[str]) -> None:
    config = load_config()
    connection = open_db()
    try:
        routes = sqlite_bypass.list_routes(connection)
        tag = tag_from_name_or_die(args[0]) if args else choose_bypass_tag(config, routes, "Remove bypass")
        record = route_record_or_die(connection, tag)
        if not confirm_remove(tag):
            print("Remove cancelled.")
            return
        before = copy.deepcopy(config)
        env_values = read_server_env()
        bypass_config.disable_bypass_route(
            config,
            tag,
            str(record.get("regionCode") or ""),
            remove_outbound=True,
            env_values=env_values,
        )
        backup = apply_config(config, before)
        sqlite_bypass.delete_route(connection, tag)
        write_server_env_values(env_values)
    finally:
        connection.close()
    print(f"GeoIP bypass removed: {tag}")
    print(f"Backup: {backup}")
    notify_config_event("removed", record)


def wait_for_tcp(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
    if last_error:
        print(f"ERROR: {host}:{port} did not open in time: {last_error}", file=sys.stderr)
    return False


def remove_test_routes(config: dict) -> None:
    rules = bypass_config.routing_rules(config)
    config["routing"]["rules"] = [
        rule
        for rule in rules
        if TEST_INBOUND_TAG not in bypass_config.rule_values(rule, "inboundTag")
    ]


def add_test_inbound(config: dict, outbound_tag: str) -> None:
    inbounds = [
        item
        for item in config.setdefault("inbounds", [])
        if item.get("tag") != TEST_INBOUND_TAG
    ]
    inbounds.append(
        {
            "tag": TEST_INBOUND_TAG,
            "listen": TEST_SOCKS_HOST,
            "port": TEST_SOCKS_PORT,
            "protocol": "socks",
            "settings": {
                "auth": "noauth",
                "udp": True,
            },
            "sniffing": {
                "enabled": True,
                "destOverride": ["http", "tls"],
            },
        }
    )
    config["inbounds"] = inbounds
    remove_test_routes(config)
    bypass_config.routing_rules(config).insert(
        0,
        {
            "type": "field",
            "inboundTag": [TEST_INBOUND_TAG],
            "outboundTag": outbound_tag,
        },
    )


def install_config_without_backup(config: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(CONFIG_PATH)


def test_bypass(tag: str) -> None:
    original_text = CONFIG_PATH.read_text()
    config = json.loads(original_text)
    if not bypass_config.bypass_outbound(config, tag):
        die(f"Bypass outbound is not configured: {tag}")
    test_config = copy.deepcopy(config)
    add_test_inbound(test_config, tag)
    try:
        install_config_without_backup(test_config)
        run([str(XRAY_BIN), "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])

        proxy = f"socks5h://{TEST_SOCKS_HOST}:{TEST_SOCKS_PORT}"
        urls = [
            "https://ifconfig.me/ip",
            "https://icanhazip.com",
            "https://checkip.amazonaws.com",
        ]
        print(f"Temporary SOCKS inbound: {TEST_SOCKS_HOST}:{TEST_SOCKS_PORT}")
        if not wait_for_tcp(TEST_SOCKS_HOST, TEST_SOCKS_PORT):
            die("Temporary SOCKS inbound did not become ready.")
        time.sleep(0.5)
        print(f"Testing outbound through Xray bypass: {tag}")
        ok = False
        for url in urls:
            result = run_capture(
                [
                    "curl",
                    "-4",
                    "--proxy",
                    proxy,
                    "--connect-timeout",
                    "8",
                    "--max-time",
                    "20",
                    "-sS",
                    url,
                ],
                timeout=25,
            )
            output = result.stdout.strip()
            error = result.stderr.strip()
            if result.returncode == 0 and output:
                ok = True
                print(f"OK {url} -> {output}")
            else:
                detail = error or output or f"curl exited with {result.returncode}"
                print(f"FAIL {url} -> {detail}")
        if not ok:
            die("No test endpoint succeeded through bypass.")
    finally:
        CONFIG_PATH.write_text(original_text)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        try:
            run([str(XRAY_BIN), "run", "-test", "-config", str(CONFIG_PATH)])
            run(["systemctl", "restart", "xray"])
        except subprocess.CalledProcessError as exc:
            print(f"ERROR: Failed to restore Xray after test: {exc}", file=sys.stderr)
            raise
    print("Test finished. Original config restored.")


def cmd_test(args: list[str]) -> None:
    config = load_config()
    connection = open_db()
    try:
        routes = sqlite_bypass.list_routes(connection)
    finally:
        connection.close()
    tag = tag_from_name_or_die(args[0]) if args else choose_bypass_tag(config, routes, "Test bypass")
    test_bypass(tag)


def print_usage() -> None:
    print(
        """Usage:
  xray-set-bypass list
  xray-set-bypass add NAME --region CODE [--replace]
  xray-set-bypass enable NAME [--replace]
  xray-set-bypass region NAME CODE [--replace]
  xray-set-bypass disable NAME
  xray-set-bypass test NAME
  xray-set-bypass remove NAME
  xray-set-bypass status

If --region is omitted in an interactive add/region command, a numbered GeoIP list with search is shown.
"""
    )


def main() -> None:
    if os.geteuid() != 0:
        die("Run this script as root.")
    if not CONFIG_PATH.exists():
        die(f"Config not found: {CONFIG_PATH}")

    args = sys.argv[1:]
    if not args:
        cmd_add([])
        return

    command = args[0]
    rest = args[1:]
    if command in ("-h", "--help", "help"):
        print_usage()
    elif command in ("list", "status", "--list", "--status"):
        cmd_status()
    elif command in ("add", "set", "replace"):
        cmd_add(rest)
    elif command == "enable":
        cmd_enable(rest)
    elif command in ("region", "set-region"):
        cmd_region(rest)
    elif command == "disable":
        cmd_disable(rest)
    elif command in ("remove", "delete", "rm"):
        cmd_remove(rest)
    elif command in ("test", "--test"):
        cmd_test(rest)
    else:
        cmd_add(args)


if __name__ == "__main__":
    main()
