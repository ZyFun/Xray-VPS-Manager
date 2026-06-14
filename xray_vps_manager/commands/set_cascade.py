#!/usr/bin/env python3
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
from urllib.parse import parse_qs, unquote, urlparse

from xray_vps_manager.core.terminal import print_table
from xray_vps_manager.xray import cascade as cascade_config

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
GEOIP_WARNING_OUTBOUND_PREFIX = "geoip-warning-"
TEST_INBOUND_TAG = "cascade-test-socks"
TEST_SOCKS_HOST = "127.0.0.1"
TEST_SOCKS_PORT = 10808


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def one(params, key, default=""):
    value = params.get(key, [default])
    return value[0] if value else default


def parse_vless(uri, tag=None):
    parsed = urlparse(uri.strip())
    if parsed.scheme.lower() != "vless":
        die("Only vless:// links are supported.")
    if not parsed.username or not parsed.hostname:
        die("The VLESS link must include UUID and host.")

    params = parse_qs(parsed.query, keep_blank_values=True)
    port = parsed.port or 443
    network = one(params, "type", "tcp") or "tcp"
    security = one(params, "security", "none") or "none"
    encryption = one(params, "encryption", "none") or "none"
    flow = one(params, "flow", "")

    outbound = {
        "tag": tag or cascade_config.cascade_tag(),
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": parsed.hostname,
                    "port": port,
                    "users": [
                        {
                            "id": unquote(parsed.username),
                            "encryption": encryption,
                        }
                    ],
                }
            ]
        },
        "streamSettings": {
            "network": network,
            "security": security,
        },
    }

    user = outbound["settings"]["vnext"][0]["users"][0]
    if flow:
        user["flow"] = flow

    stream = outbound["streamSettings"]
    if security == "reality":
        public_key = one(params, "pbk")
        sni = one(params, "sni")
        short_id = one(params, "sid")
        if not public_key or not sni:
            die("Reality VLESS requires pbk and sni parameters.")
        reality = {
            "serverName": sni,
            "publicKey": public_key,
            "fingerprint": one(params, "fp", "chrome") or "chrome",
        }
        if short_id:
            reality["shortId"] = short_id
        spider_x = one(params, "spx", "")
        if spider_x:
            reality["spiderX"] = unquote(spider_x)
        stream["realitySettings"] = reality
    elif security == "tls":
        tls = {}
        sni = one(params, "sni")
        if sni:
            tls["serverName"] = sni
        fp = one(params, "fp")
        if fp:
            tls["fingerprint"] = fp
        stream["tlsSettings"] = tls
    elif security in ("none", ""):
        stream["security"] = "none"
    else:
        die(f"Unsupported security={security!r}; supported: reality, tls, none.")

    if network == "tcp":
        header_type = one(params, "headerType", "none") or "none"
        stream["tcpSettings"] = {"header": {"type": header_type}}
    elif network == "ws":
        ws = {}
        path = one(params, "path")
        host = one(params, "host")
        if path:
            ws["path"] = unquote(path)
        if host:
            ws["headers"] = {"Host": host}
        stream["wsSettings"] = ws
    elif network == "grpc":
        service_name = one(params, "serviceName")
        grpc = {}
        if service_name:
            grpc["serviceName"] = unquote(service_name)
        stream["grpcSettings"] = grpc
    else:
        die(f"Unsupported network type={network!r}; supported: tcp, ws, grpc.")

    label = unquote(parsed.fragment) if parsed.fragment else f"{parsed.hostname}:{port}"
    return outbound, label


def load_config():
    return json.loads(CONFIG_PATH.read_text())


def remove_tag(items, tag):
    return [item for item in items if item.get("tag") != tag]


def is_geoip_warning_tag(tag):
    return str(tag or "").startswith(GEOIP_WARNING_OUTBOUND_PREFIX)


def geoip_warning_tags(config):
    tags = {item.get("tag") for item in config.get("outbounds", []) if is_geoip_warning_tag(item.get("tag"))}
    tags.update(rule.get("outboundTag") for rule in config.get("routing", {}).get("rules", []) if is_geoip_warning_tag(rule.get("outboundTag")))
    return sorted(tag for tag in tags if tag)


def outbound_by_tag(config, tag):
    for outbound in config.get("outbounds", []):
        if outbound.get("tag") == tag:
            return outbound
    return None


def route_source_outbound(config, fallback=None):
    catchall = cascade_config.current_catchall_tag(config)
    if catchall:
        outbound = outbound_by_tag(config, catchall)
        if outbound:
            return outbound
    if fallback:
        return fallback
    active = cascade_config.active_cascade_outbound(config)
    if active:
        return active
    return cascade_config.ensure_direct_outbound(config)


def sync_geoip_warning_outbounds(config, source_outbound=None):
    tags = geoip_warning_tags(config)
    if not tags:
        return
    source = route_source_outbound(config, fallback=source_outbound)
    config["outbounds"] = [item for item in config.get("outbounds", []) if not is_geoip_warning_tag(item.get("tag"))]
    for tag in tags:
        outbound = copy.deepcopy(source)
        outbound["tag"] = tag
        config.setdefault("outbounds", []).append(outbound)


def is_api_rule(rule):
    return rule.get("outboundTag") == cascade_config.API_TAG or cascade_config.API_TAG in cascade_config.rule_values(rule, "inboundTag")


def ensure_private_block_rule(config):
    rules = cascade_config.routing_rules(config)
    for rule in rules:
        if (
            rule.get("type") == "field"
            and rule.get("outboundTag") == cascade_config.BLOCKED_TAG
            and "geoip:private" in cascade_config.rule_values(rule, "ip")
        ):
            return
    insert_index = 0
    while insert_index < len(rules) and is_api_rule(rules[insert_index]):
        insert_index += 1
    rules.insert(
        insert_index,
        {
            "type": "field",
            "ip": ["geoip:private"],
            "outboundTag": cascade_config.BLOCKED_TAG,
        },
    )


def configure_cascade(config, outbound):
    tag = outbound["tag"]
    cascade_config.ensure_base_outbounds(config)
    config["outbounds"] = remove_tag(config.get("outbounds", []), tag)
    config["outbounds"].insert(0, outbound)
    ensure_private_block_rule(config)
    cascade_config.activate_cascade_route(config, tag)
    cascade_config.sync_telegram_cascade_rules(config, tag)
    sync_geoip_warning_outbounds(config, outbound)


def set_active_cascade(config, tag):
    if not cascade_config.cascade_outbound(config, tag):
        raise ValueError(f"Cascade outbound is not configured: {tag}")
    cascade_config.move_outbound_to_front(config, tag)
    cascade_config.activate_cascade_route(config, tag)
    cascade_config.sync_telegram_cascade_rules(config, tag)
    sync_geoip_warning_outbounds(config, cascade_config.cascade_outbound(config, tag))


def disable_cascade(config):
    rules = cascade_config.routing_rules(config)
    config["routing"]["rules"] = [
        rule
        for rule in rules
        if not (cascade_config.is_cascade_tag(rule.get("outboundTag")) and cascade_config.is_catchall_rule(rule))
    ]
    cascade_config.ensure_base_outbounds(config)
    sync_geoip_warning_outbounds(config)


def remove_cascade(config, tag):
    if not cascade_config.cascade_outbound(config, tag):
        raise ValueError(f"Cascade outbound is not configured: {tag}")
    was_active = cascade_config.active_cascade_tag(config) == tag
    config["outbounds"] = remove_tag(config.get("outbounds", []), tag)
    replacement = cascade_config.first_cascade_tag(config)
    kept_rules = []
    for rule in cascade_config.routing_rules(config):
        if rule.get("outboundTag") != tag:
            kept_rules.append(rule)
            continue
        if replacement and cascade_config.TELEGRAM_SOCKS_TAG in cascade_config.rule_values(rule, "inboundTag"):
            rule["outboundTag"] = replacement
            kept_rules.append(rule)
    config["routing"]["rules"] = kept_rules
    if was_active and replacement:
        set_active_cascade(config, replacement)
    else:
        sync_geoip_warning_outbounds(config)


def write_config(config):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    backup = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.bak.{timestamp}")
    shutil.copy2(CONFIG_PATH, backup)

    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(CONFIG_PATH)
    return backup


def restore_config(before):
    CONFIG_PATH.write_text(json.dumps(before, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(CONFIG_PATH, user="root", group="xray")
    os.chmod(CONFIG_PATH, 0o640)


def apply_config(config, before):
    backup = write_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        run(["systemctl", "restart", "xray"])
    except subprocess.CalledProcessError:
        restore_config(before)
        run(["systemctl", "restart", "xray"])
        die(f"New config failed validation. Restored previous config. Backup: {backup}")
    return backup


def install_config_without_backup(config):
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(CONFIG_PATH)


def run(command):
    subprocess.run(command, check=True)


def run_capture(command, timeout=20):
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def wait_for_tcp(host, port, timeout=10.0):
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


def remove_test_routes(config):
    rules = cascade_config.routing_rules(config)
    config["routing"]["rules"] = [
        rule
        for rule in rules
        if TEST_INBOUND_TAG not in cascade_config.rule_values(rule, "inboundTag")
    ]


def add_test_inbound(config, outbound_tag):
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
    cascade_config.routing_rules(config).insert(
        0,
        {
            "type": "field",
            "inboundTag": [TEST_INBOUND_TAG],
            "outboundTag": outbound_tag,
        },
    )


def outbound_address(outbound):
    vnext = outbound.get("settings", {}).get("vnext", [])
    if not vnext:
        return "-"
    address = vnext[0].get("address", "")
    port = vnext[0].get("port", "")
    return f"{address}:{port}" if address and port else address or "-"


def current_route_label(config):
    tag = cascade_config.current_catchall_tag(config)
    if not tag:
        return "default direct"
    if tag == cascade_config.WARP_OUTBOUND_TAG:
        return "warp-out"
    if tag == cascade_config.DIRECT_TAG:
        return "direct"
    if cascade_config.is_cascade_tag(tag):
        return tag
    return tag


def cascade_rows(config):
    active = cascade_config.active_cascade_tag(config)
    rows = []
    for index, outbound in enumerate(cascade_config.cascade_outbounds(config), start=1):
        tag = outbound.get("tag", "")
        rows.append(
            [
                index,
                cascade_config.cascade_name_from_tag(tag),
                tag,
                "active" if tag == active else "-",
                outbound_address(outbound),
            ]
        )
    return rows


def print_cascade_table(config):
    print_table(
        ["#", "Name", "Tag", "Route", "Address"],
        cascade_rows(config),
        empty_message="Cascade outbounds are not configured.",
    )
    print(f"Current catch-all route: {current_route_label(config)}")


def normalize_name_or_die(value):
    try:
        return cascade_config.normalize_cascade_name(value)
    except ValueError as exc:
        die(str(exc))


def tag_from_name_or_die(value):
    return cascade_config.cascade_tag(normalize_name_or_die(value))


def choose_cascade_tag(config, prompt="Cascade", allow_empty=False):
    outbounds = cascade_config.cascade_outbounds(config)
    if not outbounds:
        die("Cascade outbounds are not configured. Add a cascade first.")
    while True:
        print_cascade_table(config)
        default = "1" if len(outbounds) == 1 and not allow_empty else ""
        suffix = f" [{default}]" if default else ""
        choice = input(f"{prompt}{suffix}: ").strip()
        if not choice and default:
            choice = default
        if not choice and allow_empty:
            return ""
        if choice.isdigit():
            index = int(choice, 10)
            if 1 <= index <= len(outbounds):
                return str(outbounds[index - 1].get("tag"))
        try:
            tag = cascade_config.cascade_tag(choice)
        except ValueError:
            tag = choice
        if cascade_config.cascade_outbound(config, tag):
            return tag
        print("Unknown cascade. Choose a number, name, or tag from the table.")


def ask_cascade_name(default=cascade_config.DEFAULT_CASCADE_NAME):
    while True:
        value = input(f"Cascade name [{default}]: ").strip() or default
        try:
            return cascade_config.normalize_cascade_name(value)
        except ValueError as exc:
            print(f"ERROR: {exc}")


def read_cascade_name(args):
    if args:
        return normalize_name_or_die(args[0])
    if sys.stdin.isatty():
        print("Cascade name is used in the outbound tag: cascade-{name}.")
        print("Allowed chars: a-z, 0-9, dash, underscore.")
        return ask_cascade_name()
    return cascade_config.DEFAULT_CASCADE_NAME


def read_vless_link():
    if sys.stdin.isatty():
        print("Paste upstream VLESS link and press Enter:")
    uri = sys.stdin.readline().strip()
    if not uri:
        die("Empty link.")
    return uri


def cmd_status():
    print_cascade_table(load_config())


def cmd_add(args):
    name = read_cascade_name(args)
    tag = cascade_config.cascade_tag(name)
    uri = read_vless_link()
    outbound, label = parse_vless(uri, tag=tag)
    config = load_config()
    before = copy.deepcopy(config)
    configure_cascade(config, outbound)
    backup = apply_config(config, before)

    print(f"Cascade enabled: {name} ({tag})")
    print(f"Upstream: {label}")
    print(f"Backup: {backup}")
    print(f"All non-private outbound traffic now goes through tag: {tag}")
    print("To switch cascade: xray-set-cascade use NAME")
    print("To disable cascade routing: xray-set-cascade --disable")


def cmd_use(args):
    config = load_config()
    tag = tag_from_name_or_die(args[0]) if args else choose_cascade_tag(config, "Active cascade")
    before = copy.deepcopy(config)
    try:
        set_active_cascade(config, tag)
    except ValueError as exc:
        die(str(exc))
    backup = apply_config(config, before)
    print(f"Active cascade: {cascade_config.cascade_name_from_tag(tag)} ({tag})")
    print(f"Backup: {backup}")


def confirm_remove(tag):
    answer = input(f"Remove cascade {cascade_config.cascade_name_from_tag(tag)} ({tag})? [y/N]: ").strip().lower()
    return answer in ("y", "yes", "д", "да")


def cmd_remove(args):
    config = load_config()
    tag = tag_from_name_or_die(args[0]) if args else choose_cascade_tag(config, "Remove cascade")
    if sys.stdin.isatty() and not confirm_remove(tag):
        print("Remove cancelled.")
        return
    before = copy.deepcopy(config)
    try:
        remove_cascade(config, tag)
    except ValueError as exc:
        die(str(exc))
    backup = apply_config(config, before)
    print(f"Cascade removed: {cascade_config.cascade_name_from_tag(tag)} ({tag})")
    print(f"Current catch-all route: {current_route_label(config)}")
    print(f"Backup: {backup}")


def cmd_disable():
    config = load_config()
    before = copy.deepcopy(config)
    disable_cascade(config)
    backup = apply_config(config, before)
    print("Cascade routing disabled. Configured cascade outbounds were kept.")
    print(f"Current catch-all route: {current_route_label(config)}")
    print(f"Backup: {backup}")


def test_cascade(tag=None):
    original_text = CONFIG_PATH.read_text()
    config = json.loads(original_text)
    selected_tag = tag or cascade_config.active_cascade_tag(config)
    if not selected_tag:
        cascades = cascade_config.cascade_outbounds(config)
        if len(cascades) == 1:
            selected_tag = str(cascades[0].get("tag"))
        elif cascades:
            die("No active cascade route. Run xray-set-cascade use NAME or xray-set-cascade test-select.")
        else:
            die("Cascade outbound is not configured. Run xray-set-cascade add NAME first.")
    if not cascade_config.cascade_outbound(config, selected_tag):
        die(f"Cascade outbound is not configured: {selected_tag}")

    test_config = copy.deepcopy(config)
    add_test_inbound(test_config, selected_tag)

    try:
        install_config_without_backup(test_config)
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
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
        print(f"Testing outbound through Xray cascade: {selected_tag}")
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
            die("No test endpoint succeeded through cascade.")
    finally:
        CONFIG_PATH.write_text(original_text)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        try:
            run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
            run(["systemctl", "restart", "xray"])
        except subprocess.CalledProcessError as exc:
            print(f"ERROR: Failed to restore Xray after test: {exc}", file=sys.stderr)
            raise

    print("Test finished. Original config restored.")


def cmd_test(args, choose=False):
    config = load_config()
    if choose:
        tag = choose_cascade_tag(config, "Test cascade")
    elif args:
        tag = tag_from_name_or_die(args[0])
    else:
        tag = None
    test_cascade(tag)


def print_usage():
    print(
        """Usage:
  xray-set-cascade                 Add/replace cascade interactively
  xray-set-cascade add [NAME]      Add/replace named cascade and make it active
  xray-set-cascade list            Show configured cascades
  xray-set-cascade use [NAME]      Select active cascade
  xray-set-cascade test [NAME]     Test active or named cascade
  xray-set-cascade test-select     Select cascade from table and test it
  xray-set-cascade remove [NAME]   Remove named cascade
  xray-set-cascade --disable       Disable cascade routing, keep outbounds
"""
    )


def main():
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
    elif command in ("use", "switch", "select"):
        cmd_use(rest)
    elif command in ("remove", "delete", "rm"):
        cmd_remove(rest)
    elif command in ("--disable", "--direct", "disable", "direct"):
        cmd_disable()
    elif command in ("--test", "test"):
        cmd_test(rest)
    elif command in ("test-select", "select-test"):
        cmd_test([], choose=True)
    else:
        cmd_add(args)


if __name__ == "__main__":
    main()
