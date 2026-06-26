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
from urllib.parse import urlsplit

from xray_vps_manager.activity.constants import ACCESS_LOG_PATH
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
ROUTE_TEST_INBOUND_TAG = "bypass-route-test-socks"
TEST_SOCKS_HOST = "127.0.0.1"
TEST_SOCKS_PORT = 10809
ROUTE_TEST_SOCKS_PORT = 18109
FORCED_TEST_URLS = [
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
]
DEFAULT_FOREIGN_ROUTE_TEST_URL = "https://example.com/"
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


def normalize_test_target(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if any(char.isspace() for char in text):
        raise ValueError("Bypass test target must be a domain, IP address, or http/https URL without spaces.")
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Bypass test target must use http or https.")
    if not parts.hostname:
        raise ValueError("Bypass test target must include a domain or IP address.")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("Bypass test target port must be a number between 1 and 65535.") from exc
    return parts._replace(path=parts.path or "/").geturl()


def is_clear_test_target(value: str) -> bool:
    return (value or "").strip().lower() in ("-", "none", "clear", "off", "нет")


def record_test_target(record: dict | None) -> str:
    return str((record or {}).get("testTarget") or "")


def set_record_test_target(record: dict, target: str) -> None:
    if target:
        record["testTarget"] = target
    else:
        record.pop("testTarget", None)


def read_test_target(args_target: str = "", default: str = "") -> str:
    if args_target:
        if is_clear_test_target(args_target):
            return ""
        try:
            return normalize_test_target(args_target)
        except ValueError as exc:
            die(str(exc))
    if not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default else " (Enter - skip)"
    value = input(f"Expected bypass test target, domain/IP/URL{suffix}: ").strip()
    if not value:
        return default
    if is_clear_test_target(value):
        return ""
    try:
        return normalize_test_target(value)
    except ValueError as exc:
        die(str(exc))


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
    if not region:
        return "needs-region"
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
                route_status(config, record) if record else "needs-region",
                record_test_target(record) or "-",
                outbound_address(outbound),
            ]
        )
    print_table(
        ["#", "Name", "Region", "Label", "Tag", "Route", "Test target", "Address"],
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
    options: dict[str, str | bool] = {"region": "", "replace": False, "test_target": ""}
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
        if item in ("--test-target", "--target"):
            if index + 1 >= len(args):
                die(f"{item} requires value.")
            options["test_target"] = args[index + 1]
            index += 2
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
        test_target = read_test_target(str(options["test_target"] or ""), record_test_target(existing))
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
        set_record_test_target(record, test_target)
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
    if record_test_target(record):
        print(f"Test target: {record_test_target(record)}")
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


def route_record_for_outbound(
    record: dict | None,
    tag: str,
    outbound: dict | None,
    *,
    region_arg: str = "",
    test_target_arg: str = "",
    enabled: bool = False,
) -> dict:
    created = record is None
    if record is None and not outbound:
        die(f"GeoIP bypass route metadata and Xray outbound not found: {tag}")
    if record is None:
        print(f"GeoIP bypass metadata is missing for {tag}; choose region to repair it.")
        region, region_label = read_region(region_arg)
        stamp = utc_stamp()
        record = {
            "tag": tag,
            "name": bypass_config.bypass_name_from_tag(tag),
            "regionCode": region,
            "regionLabel": region_label,
            "label": outbound_address(outbound),
            "enabled": enabled,
            "created": stamp,
            "updated": stamp,
        }
    if not record.get("regionCode"):
        region, region_label = read_region(region_arg)
        record["regionCode"] = region
        record["regionLabel"] = region_label
    if test_target_arg or created:
        set_record_test_target(record, read_test_target(test_target_arg, record_test_target(record)))
    return record


def cmd_enable(args: list[str]) -> None:
    rest, options = parse_common_options(args)
    config = load_config()
    connection = open_db()
    try:
        routes = sqlite_bypass.list_routes(connection)
        tag = tag_from_name_or_die(rest[0]) if rest else choose_bypass_tag(config, routes, "Enable bypass")
        outbound = bypass_config.bypass_outbound(config, tag)
        if not outbound:
            die(f"Bypass outbound is not configured in Xray config: {tag}")
        record = route_record_for_outbound(
            sqlite_bypass.get_route(connection, tag),
            tag,
            outbound,
            region_arg=str(options["region"] or ""),
            test_target_arg=str(options["test_target"] or ""),
            enabled=True,
        )
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
    if record_test_target(record):
        print(f"Test target: {record_test_target(record)}")
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
        region_arg = rest[1] if len(rest) >= 2 else str(options["region"] or "")
        region, region_label = read_region(region_arg)
        record = sqlite_bypass.get_route(connection, tag)
        old_region = str(record.get("regionCode") or "") if record else ""
        if record is None:
            outbound = bypass_config.bypass_outbound(config, tag)
            if not outbound:
                die(f"GeoIP bypass route metadata and Xray outbound not found: {tag}")
            stamp = utc_stamp()
            record = {
                "tag": tag,
                "name": bypass_config.bypass_name_from_tag(tag),
                "regionCode": region,
                "regionLabel": region_label,
                "label": outbound_address(outbound),
                "enabled": False,
                "created": stamp,
                "updated": stamp,
            }
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
        if options["test_target"]:
            set_record_test_target(record, read_test_target(str(options["test_target"] or ""), record_test_target(record)))
        record["updated"] = utc_stamp()
        save_route(connection, record)
        write_server_env_values(env_values)
    finally:
        connection.close()
    print(f"GeoIP bypass region updated: {tag}")
    print(f"Region: {region} / {region_label}")
    if record_test_target(record):
        print(f"Test target: {record_test_target(record)}")
    if backup:
        print(f"Backup: {backup}")
    notify_config_event("region-changed", record)


def cmd_target(args: list[str]) -> None:
    rest, options = parse_common_options(args)
    if len(rest) < 1 and not sys.stdin.isatty():
        die("Usage: xray-set-bypass target NAME TARGET")
    if len(rest) < 2 and not options["test_target"] and not sys.stdin.isatty():
        die("Usage: xray-set-bypass target NAME TARGET")
    config = load_config()
    connection = open_db()
    try:
        routes = sqlite_bypass.list_routes(connection)
        tag = tag_from_name_or_die(rest[0]) if rest else choose_bypass_tag(config, routes, "Bypass")
        outbound = bypass_config.bypass_outbound(config, tag)
        record = route_record_for_outbound(
            sqlite_bypass.get_route(connection, tag),
            tag,
            outbound,
            region_arg=str(options["region"] or ""),
        )
        target_arg = rest[1] if len(rest) >= 2 else str(options["test_target"] or "")
        set_record_test_target(record, read_test_target(target_arg, record_test_target(record)))
        record["updated"] = utc_stamp()
        save_route(connection, record)
    finally:
        connection.close()
    print(f"GeoIP bypass test target updated: {tag}")
    print(f"Test target: {record_test_target(record) or '-'}")


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


def test_inbound_tags() -> set[str]:
    return {TEST_INBOUND_TAG, ROUTE_TEST_INBOUND_TAG}


def remove_test_artifacts(config: dict) -> None:
    tags = test_inbound_tags()
    config["inbounds"] = [
        inbound
        for inbound in config.setdefault("inbounds", [])
        if inbound.get("tag") not in tags
    ]
    rules = bypass_config.routing_rules(config)
    config["routing"]["rules"] = [
        rule
        for rule in rules
        if not tags.intersection(bypass_config.rule_values(rule, "inboundTag"))
    ]


def ensure_test_ports_available(config: dict) -> None:
    ports = {TEST_SOCKS_PORT, ROUTE_TEST_SOCKS_PORT}
    tags = test_inbound_tags()
    conflicts = []
    for inbound in config.get("inbounds", []):
        tag = str(inbound.get("tag") or "")
        if tag in tags:
            continue
        if inbound.get("port") in ports:
            conflicts.append(f"{tag or '-'}:{inbound.get('port')}")
    if conflicts:
        die(f"Temporary SOCKS port conflict: {', '.join(conflicts)}")


def socks_test_inbound(tag: str, port: int) -> dict:
    return {
        "tag": tag,
        "listen": TEST_SOCKS_HOST,
        "port": port,
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


def add_test_inbounds(config: dict, outbound_tag: str) -> None:
    remove_test_artifacts(config)
    ensure_test_ports_available(config)
    config.setdefault("inbounds", []).extend(
        [
            socks_test_inbound(TEST_INBOUND_TAG, TEST_SOCKS_PORT),
            socks_test_inbound(ROUTE_TEST_INBOUND_TAG, ROUTE_TEST_SOCKS_PORT),
        ]
    )
    bypass_config.routing_rules(config).insert(
        0,
        {
            "type": "field",
            "inboundTag": [TEST_INBOUND_TAG],
            "outboundTag": outbound_tag,
        },
    )


def access_log_offset() -> int:
    try:
        return ACCESS_LOG_PATH.stat().st_size
    except FileNotFoundError:
        return 0


def access_log_lines_since(offset: int, marker: str) -> list[str]:
    try:
        with ACCESS_LOG_PATH.open("rb") as handle:
            handle.seek(offset)
            return [
                line
                for line in handle.read().decode("utf-8", errors="replace").splitlines()
                if marker in line
            ]
    except FileNotFoundError:
        return []


def route_outbound_from_access_line(line: str) -> str:
    marker = f"[{ROUTE_TEST_INBOUND_TAG} -> "
    if marker not in line:
        return ""
    return line.split(marker, 1)[1].split("]", 1)[0].strip()


def access_log_line_for_host(offset: int, host: str, port: int, timeout: float = 2.0) -> str:
    deadline = time.monotonic() + timeout
    pattern = f"tcp:{host}:{port}".lower()
    while True:
        for line in access_log_lines_since(offset, ROUTE_TEST_INBOUND_TAG):
            if pattern in line.lower():
                return line
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.2)


def route_test_destination(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return host, port


def active_region_for_tag(config: dict, routes: dict[str, dict], tag: str) -> str:
    for record in routes.values():
        if record.get("tag") == tag and record.get("enabled") and record.get("regionCode"):
            return bypass_config.normalize_region_code(str(record.get("regionCode")))
    for warning_tag in bypass_config.warning_tags(config):
        region = bypass_config.region_from_geoip_warning_tag(warning_tag)
        if bypass_config.configured_bypass_for_warning(config, region) == tag:
            return region
    return ""


def install_config_without_backup(config: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(CONFIG_PATH)


def curl_probe(proxy: str, url: str, *, capture_body: bool = False):
    command = [
        "curl",
        "-4",
        "--proxy",
        proxy,
        "--noproxy",
        "",
        "--connect-timeout",
        "8",
        "--max-time",
        "20",
        "-k",
        "-sS",
    ]
    if capture_body:
        command.append(url)
    else:
        command.extend(["-o", "/dev/null", "-w", "%{http_code}", url])
    return run_capture(command, timeout=25)


def test_forced_bypass_outbound(tag: str) -> bool:
    proxy = f"socks5h://{TEST_SOCKS_HOST}:{TEST_SOCKS_PORT}"
    print(f"Testing forced outbound through Xray bypass: {tag}")
    ok = False
    for url in FORCED_TEST_URLS:
        result = curl_probe(proxy, url, capture_body=True)
        output = result.stdout.strip()
        error = result.stderr.strip()
        if result.returncode == 0 and output:
            ok = True
            print(f"OK forced {url} -> {output}")
        else:
            detail = error or output or f"curl exited with {result.returncode}"
            print(f"FAIL forced {url} -> {detail}")
    return ok


def test_route_split(region: str, target_url: str) -> bool:
    if not region:
        print("WARN: Route split test skipped: bypass region is not configured.")
        return True
    if not target_url:
        print("WARN: Route split test skipped: expected bypass test target is not configured.")
        return True
    proxy = f"socks5h://{TEST_SOCKS_HOST}:{ROUTE_TEST_SOCKS_PORT}"
    expected_region_outbound = bypass_config.geoip_warning_tag(region)
    print(
        "Testing route split through normal Xray rules: "
        f"{target_url} should use {expected_region_outbound}; "
        f"{DEFAULT_FOREIGN_ROUTE_TEST_URL} should not."
    )
    results = []
    for label, url in (("TARGET", target_url), ("FOREIGN", DEFAULT_FOREIGN_ROUTE_TEST_URL)):
        offset = access_log_offset()
        result = curl_probe(proxy, url)
        status = result.stdout.strip()
        error = result.stderr.strip()
        detail = status if result.returncode == 0 else error or status or f"curl exited with {result.returncode}"
        host, port = route_test_destination(url)
        line = access_log_line_for_host(offset, host, port)
        results.append((label, url, result.returncode, detail, line))
    ok = True
    for label, url, returncode, detail, line in results:
        outbound = route_outbound_from_access_line(line)
        if label == "TARGET":
            route_ok = outbound == expected_region_outbound
        else:
            route_ok = outbound and outbound != expected_region_outbound and not bypass_config.is_bypass_tag(outbound)
        curl_ok = returncode == 0 and detail not in ("000", "")
        if route_ok and curl_ok:
            print(f"OK route {label} {url} -> {outbound}; HTTP {detail}")
        else:
            ok = False
            route_detail = outbound or "no access.log route"
            status_detail = detail or f"curl exited with {returncode}"
            print(f"FAIL route {label} {url} -> {route_detail}; {status_detail}")
    return ok


def test_bypass(tag: str, region: str = "", test_target: str = "") -> None:
    original_text = CONFIG_PATH.read_text()
    config = json.loads(original_text)
    clean_config = copy.deepcopy(config)
    remove_test_artifacts(clean_config)
    restore_text = (
        json.dumps(clean_config, indent=2, ensure_ascii=False) + "\n"
        if clean_config != config
        else original_text
    )
    if not bypass_config.bypass_outbound(config, tag):
        die(f"Bypass outbound is not configured: {tag}")
    test_config = copy.deepcopy(clean_config)
    add_test_inbounds(test_config, tag)
    try:
        install_config_without_backup(test_config)
        run([str(XRAY_BIN), "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])

        print(f"Temporary SOCKS inbound: {TEST_SOCKS_HOST}:{TEST_SOCKS_PORT}")
        if not wait_for_tcp(TEST_SOCKS_HOST, TEST_SOCKS_PORT):
            die("Temporary SOCKS inbound did not become ready.")
        print(f"Temporary route-test SOCKS inbound: {TEST_SOCKS_HOST}:{ROUTE_TEST_SOCKS_PORT}")
        if not wait_for_tcp(TEST_SOCKS_HOST, ROUTE_TEST_SOCKS_PORT):
            die("Temporary route-test SOCKS inbound did not become ready.")
        time.sleep(0.5)
        forced_ok = test_forced_bypass_outbound(tag)
        route_ok = test_route_split(region, test_target)
        if not forced_ok or not route_ok:
            die("GeoIP bypass test failed.")
    finally:
        CONFIG_PATH.write_text(restore_text)
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
    rest, options = parse_common_options(args)
    config = load_config()
    connection = open_db()
    try:
        routes = sqlite_bypass.list_routes(connection)
    finally:
        connection.close()
    tag = tag_from_name_or_die(rest[0]) if rest else choose_bypass_tag(config, routes, "Test bypass")
    record = routes.get(tag, {})
    test_target = record_test_target(record)
    if options["test_target"]:
        test_target = read_test_target(str(options["test_target"] or ""), test_target)
    elif not test_target and sys.stdin.isatty():
        test_target = read_test_target("", "")
    test_bypass(tag, active_region_for_tag(config, routes, tag), test_target)


def print_usage() -> None:
    print(
        """Usage:
  xray-set-bypass list
  xray-set-bypass add NAME --region CODE [--test-target TARGET] [--replace]
  xray-set-bypass enable NAME [--test-target TARGET] [--replace]
  xray-set-bypass region NAME CODE [--test-target TARGET] [--replace]
  xray-set-bypass target NAME TARGET
  xray-set-bypass disable NAME
  xray-set-bypass test NAME [--test-target TARGET]
  xray-set-bypass remove NAME
  xray-set-bypass status

If --region is omitted in an interactive add/region command, a numbered GeoIP list with search is shown.
TARGET can be a domain, IP address, or http/https URL. Use "-" to clear the stored test target.
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
    elif command in ("target", "set-target", "test-target"):
        cmd_target(rest)
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
