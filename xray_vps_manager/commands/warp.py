#!/usr/bin/env python3
import configparser
import copy
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
WARP_DIR = Path("/usr/local/etc/xray/warp")
WGCF_BIN = Path("/usr/local/bin/wgcf")
WGCF_ACCOUNT = WARP_DIR / "wgcf-account.toml"
WGCF_PROFILE = WARP_DIR / "wgcf-profile.conf"
WARP_OUTBOUND_TAG = "warp-out"
CASCADE_UPSTREAM_TAG = "cascade-upstream"
DIRECT_TAG = "direct"
BLOCKED_TAG = "blocked"
API_TAG = "api"
TEST_INBOUND_TAG = "warp-test-socks"
TEST_SOCKS_HOST = "127.0.0.1"
TEST_SOCKS_PORT = 10809
VERIFY_INBOUND_TAG = "warp-verify-disabled-socks"
VERIFY_SOCKS_PORT = 10819
API_HOST = "api.cloudflareclient.com"
HOSTS_PATH = Path("/etc/hosts")
HOSTS_PIN_COMMENT = "# xray-vps-manager WARP API pin"
FALLBACK_API_IPS = ("8.47.69.1", "8.6.112.1")
GITHUB_API_LATEST = "https://api.github.com/repos/ViRb3/wgcf/releases/latest"
GREEN = "\033[32m"
RESET = "\033[0m"


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def color(text, code):
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{RESET}"


def green(text):
    return color(text, GREEN)


def run(command, **kwargs):
    subprocess.run(command, check=True, **kwargs)


def run_capture(command, timeout=10, **kwargs):
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        **kwargs,
    )


def restart_xray():
    result = run_capture(["systemctl", "restart", "xray"], timeout=30)
    if result.returncode == 0:
        return

    run_capture(["systemctl", "reset-failed", "xray"], timeout=10)
    time.sleep(1.0)
    retry = run_capture(["systemctl", "restart", "xray"], timeout=30)
    if retry.returncode == 0:
        return

    stdout = "\n".join(part for part in (result.stdout, retry.stdout) if part).strip()
    stderr = "\n".join(part for part in (result.stderr, retry.stderr) if part).strip()
    raise subprocess.CalledProcessError(retry.returncode, retry.args, stdout, stderr)


def require_root():
    if os.geteuid() != 0:
        die("Run this script as root.")


def load_config():
    if not CONFIG_PATH.exists():
        die(f"Config not found: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text())


def save_config(config):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    backup = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.bak.{timestamp}")
    shutil.copy2(CONFIG_PATH, backup)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(CONFIG_PATH)
    return backup


def install_config_without_backup(config):
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(CONFIG_PATH)


def apply_config(config):
    backup = save_config(config)
    try:
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        restart_xray()
    except subprocess.CalledProcessError:
        shutil.copy2(backup, CONFIG_PATH)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        restart_xray()
        die(f"New config failed. Restored backup: {backup}")
    return backup


def ensure_warp_dir():
    WARP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.chown(WARP_DIR, user="root", group="xray")
    os.chmod(WARP_DIR, 0o750)


def chmod_warp_files():
    for path in (WGCF_ACCOUNT, WGCF_PROFILE):
        if path.exists():
            shutil.chown(path, user="root", group="xray")
            os.chmod(path, 0o640)


def tls_handshake_ok(host, server_name=API_HOST, timeout=6):
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=server_name):
                return True
    except OSError:
        return False


def api_candidate_ips():
    values = []
    try:
        for item in socket.getaddrinfo(API_HOST, 443, family=socket.AF_INET, type=socket.SOCK_STREAM):
            ip = item[4][0]
            if ip not in values:
                values.append(ip)
    except OSError:
        pass
    for ip in FALLBACK_API_IPS:
        if ip not in values:
            values.append(ip)
    return values


def hosts_line_contains_name(line, name):
    body = line.split("#", 1)[0]
    parts = body.split()
    return name in parts[1:]


def pin_api_host(ip):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = HOSTS_PATH.with_name(f"{HOSTS_PATH.name}.bak.xray-warp.{timestamp}")
    shutil.copy2(HOSTS_PATH, backup)
    lines = HOSTS_PATH.read_text().splitlines()
    lines = [line for line in lines if not hosts_line_contains_name(line, API_HOST)]
    lines.append(f"{ip} {API_HOST} {HOSTS_PIN_COMMENT}")
    tmp = HOSTS_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    shutil.chown(tmp, user="root", group="root")
    os.chmod(tmp, 0o644)
    tmp.replace(HOSTS_PATH)
    print(f"Pinned {API_HOST} to {ip} in {HOSTS_PATH}. Backup: {backup}")


def ensure_cloudflare_api_tls():
    if tls_handshake_ok(API_HOST):
        return
    print(f"WARN: TLS handshake to {API_HOST} through current DNS failed. Trying known A records...")
    for ip in api_candidate_ips():
        if tls_handshake_ok(ip):
            pin_api_host(ip)
            return
    print(f"WARN: Could not find a working IPv4 for {API_HOST}. Continuing; wgcf may fail.")


def http_json(url):
    request = Request(url, headers={"User-Agent": "xray-vps-manager"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def machine_arch():
    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        return "amd64"
    if arch in ("aarch64", "arm64"):
        return "arm64"
    if arch.startswith("armv7"):
        return "armv7"
    die(f"Unsupported CPU architecture for wgcf auto-install: {arch}")


def install_wgcf():
    if WGCF_BIN.exists() and os.access(WGCF_BIN, os.X_OK):
        print(f"wgcf already installed: {WGCF_BIN}")
        return

    arch = machine_arch()
    print("Downloading latest wgcf release from GitHub...")
    release = http_json(GITHUB_API_LATEST)
    assets = release.get("assets", [])
    candidates = []
    for asset in assets:
        name = str(asset.get("name", "")).lower()
        url = asset.get("browser_download_url", "")
        if "linux" in name and arch in name and url:
            candidates.append((name, url))
    if not candidates:
        die(f"Could not find linux_{arch} wgcf asset in latest release.")

    name, url = sorted(candidates, key=lambda item: len(item[0]))[0]
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = Path(tmp.name)
        request = Request(url, headers={"User-Agent": "xray-vps-manager"})
        with urlopen(request, timeout=120) as response:
            shutil.copyfileobj(response, tmp)

    os.chmod(tmp_path, 0o755)
    shutil.move(str(tmp_path), WGCF_BIN)
    shutil.chown(WGCF_BIN, user="root", group="root")
    os.chmod(WGCF_BIN, 0o755)
    print(f"Installed wgcf: {WGCF_BIN} ({name})")


def run_wgcf(command, allow_tos_retry=False):
    result = run_capture([str(WGCF_BIN), *command], timeout=120, cwd=str(WARP_DIR))
    if result.returncode == 0:
        output = (result.stdout + result.stderr).strip()
        if output:
            print(output)
        return
    if allow_tos_retry:
        retry = subprocess.run(
            [str(WGCF_BIN), *command],
            input="yes\n",
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            cwd=str(WARP_DIR),
        )
        if retry.returncode == 0:
            output = (retry.stdout + retry.stderr).strip()
            if output:
                print(output)
            return
        result = retry
    raise subprocess.CalledProcessError(result.returncode, [str(WGCF_BIN), *command], result.stdout, result.stderr)


def ensure_warp_profile(force=False):
    ensure_warp_dir()
    install_wgcf()
    ensure_cloudflare_api_tls()
    if force:
        for path in (WGCF_ACCOUNT, WGCF_PROFILE):
            path.unlink(missing_ok=True)

    if not WGCF_ACCOUNT.exists():
        print("Registering a new WARP account...")
        try:
            run_wgcf(["register", "--accept-tos"], allow_tos_retry=False)
        except subprocess.CalledProcessError:
            run_wgcf(["register"], allow_tos_retry=True)
        chmod_warp_files()
    else:
        print(f"WARP account already exists: {WGCF_ACCOUNT}")

    print("Generating WireGuard profile...")
    try:
        run_wgcf(["generate"])
        if not WGCF_PROFILE.exists():
            die(f"wgcf did not create profile: {WGCF_PROFILE}")
    finally:
        chmod_warp_files()


def split_csv(value):
    items = []
    for part in value.split(","):
        item = part.strip()
        if item:
            items.append(item)
    return items


def parse_reserved(value):
    raw = value.strip().strip("[]")
    if not raw:
        return []
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not re.fullmatch(r"\d+", part):
            return []
        result.append(int(part, 10))
    return result


def ipv4_endpoint_candidates(value):
    endpoint = value.strip()
    if not endpoint or endpoint.startswith("["):
        return []
    if endpoint.count(":") != 1:
        return []
    host, port = endpoint.rsplit(":", 1)
    try:
        socket.inet_aton(host)
        return []
    except OSError:
        pass
    candidates = []
    try:
        for item in socket.getaddrinfo(host, int(port), family=socket.AF_INET, type=socket.SOCK_DGRAM):
            ip = item[4][0]
            candidate = f"{ip}:{port}"
            if ip and candidate not in candidates:
                candidates.append(candidate)
    except OSError:
        pass
    if host == "engage.cloudflareclient.com":
        for ip in ("162.159.192.1", "162.159.193.1"):
            candidate = f"{ip}:{port}"
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def parse_wgcf_profile(path=WGCF_PROFILE, endpoint_override=None):
    if not path.exists():
        die("WARP profile is missing. Run: xray-warp create")

    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str
    parser.read(path)
    if "Interface" not in parser or "Peer" not in parser:
        die(f"Invalid WireGuard profile: {path}")

    interface = parser["Interface"]
    peer = parser["Peer"]
    private_key = interface.get("PrivateKey", "").strip()
    public_key = peer.get("PublicKey", "").strip()
    endpoint = peer.get("Endpoint", "").strip()
    if not private_key or not public_key or not endpoint:
        die("WireGuard profile must contain PrivateKey, PublicKey and Endpoint.")

    addresses = split_csv(interface.get("Address", ""))
    if not addresses:
        die("WireGuard profile must contain Address.")
    allowed_ips = split_csv(peer.get("AllowedIPs", "0.0.0.0/0"))
    if not allowed_ips:
        allowed_ips = ["0.0.0.0/0"]

    settings = {
        "secretKey": private_key,
        "address": addresses,
        "peers": [
            {
                "publicKey": public_key,
                "endpoint": endpoint_override or endpoint,
                "keepAlive": 25,
                "allowedIPs": allowed_ips,
            }
        ],
        "mtu": int(interface.get("MTU", "1280").strip() or "1280"),
        "noKernelTun": True,
        "domainStrategy": "ForceIPv4",
    }
    reserved = parse_reserved(peer.get("Reserved", ""))
    if reserved:
        settings["reserved"] = reserved
    return settings


def warp_outbound_from_profile(endpoint_override=None):
    return {
        "tag": WARP_OUTBOUND_TAG,
        "protocol": "wireguard",
        "settings": parse_wgcf_profile(WGCF_PROFILE, endpoint_override=endpoint_override),
    }


def ensure_base_outbounds(config):
    outbounds = config.setdefault("outbounds", [])
    if not any(item.get("tag") == DIRECT_TAG for item in outbounds):
        outbounds.append({"tag": DIRECT_TAG, "protocol": "freedom"})
    if not any(item.get("tag") == BLOCKED_TAG for item in outbounds):
        outbounds.append({"tag": BLOCKED_TAG, "protocol": "blackhole"})
    return outbounds


def routing_rules(config):
    routing = config.setdefault("routing", {})
    routing.setdefault("domainStrategy", "IPIfNonMatch")
    return routing.setdefault("rules", [])


def rule_values(rule, key):
    value = rule.get(key, [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return []


def is_api_rule(rule):
    return rule.get("outboundTag") == API_TAG or API_TAG in rule_values(rule, "inboundTag")


def is_catchall_rule(rule, tag=None):
    if rule.get("type") != "field":
        return False
    if tag and rule.get("outboundTag") != tag:
        return False
    if rule.get("network") != "tcp,udp":
        return False
    for key in ("domain", "ip", "protocol", "inboundTag", "port", "source", "sourcePort", "attrs"):
        if key in rule:
            return False
    return True


def current_catchall_tag(config):
    for rule in reversed(routing_rules(config)):
        if is_catchall_rule(rule):
            return rule.get("outboundTag", "")
    return ""


def remove_outbound(config, tag):
    before = config.setdefault("outbounds", [])
    after = [item for item in before if item.get("tag") != tag]
    config["outbounds"] = after
    return len(after) != len(before)


def upsert_warp_outbound(config, endpoint_override=None, replace=True):
    outbound = warp_outbound_from_profile(endpoint_override=endpoint_override)
    outbounds = ensure_base_outbounds(config)
    replaced = False
    for index, item in enumerate(outbounds):
        if item.get("tag") == WARP_OUTBOUND_TAG:
            if replace:
                outbounds[index] = outbound
            replaced = True
            break
    if not replaced:
        outbounds.append(outbound)
    return not replaced


def remove_managed_catchall_routes(config):
    rules = routing_rules(config)
    managed = {WARP_OUTBOUND_TAG, CASCADE_UPSTREAM_TAG, DIRECT_TAG}
    kept = [rule for rule in rules if not (rule.get("outboundTag") in managed and is_catchall_rule(rule))]
    config["routing"]["rules"] = kept
    return len(kept) != len(rules)


def append_catchall_route(config, tag):
    routing_rules(config).append(
        {
            "type": "field",
            "network": "tcp,udp",
            "outboundTag": tag,
        }
    )


def configure_warp_outbound(config, endpoint_override=None):
    upsert_warp_outbound(config, endpoint_override=endpoint_override)


def enable_warp_route(config, endpoint_override=None):
    upsert_warp_outbound(config, endpoint_override=endpoint_override)
    remove_managed_catchall_routes(config)
    append_catchall_route(config, WARP_OUTBOUND_TAG)


def disable_warp_route(config):
    changed = remove_managed_catchall_routes(config)
    if any(item.get("tag") == CASCADE_UPSTREAM_TAG for item in config.get("outbounds", [])):
        append_catchall_route(config, CASCADE_UPSTREAM_TAG)
        changed = True
    return changed


def remove_warp(config):
    changed = disable_warp_route(config)
    if remove_outbound(config, WARP_OUTBOUND_TAG):
        changed = True
    return changed


def print_status():
    config = load_config()
    outbound = next((item for item in config.get("outbounds", []) if item.get("tag") == WARP_OUTBOUND_TAG), None)
    catchall = current_catchall_tag(config)
    profile_endpoint = ""
    if WGCF_PROFILE.exists():
        try:
            settings = parse_wgcf_profile(WGCF_PROFILE)
            profile_endpoint = settings.get("peers", [{}])[0].get("endpoint", "")
        except SystemExit:
            profile_endpoint = "invalid profile"
    config_endpoint = ""
    if outbound:
        config_endpoint = outbound.get("settings", {}).get("peers", [{}])[0].get("endpoint", "")
    rows = [
        ("wgcf", str(WGCF_BIN) if WGCF_BIN.exists() else "not installed"),
        ("account", str(WGCF_ACCOUNT) if WGCF_ACCOUNT.exists() else "not created"),
        ("profile", str(WGCF_PROFILE) if WGCF_PROFILE.exists() else "not created"),
        ("profile endpoint", profile_endpoint or "-"),
        ("config endpoint", config_endpoint or "-"),
        ("outbound", "configured" if outbound else "not configured"),
        ("route", "enabled" if catchall == WARP_OUTBOUND_TAG else f"disabled (catch-all: {catchall or 'default direct'})"),
    ]
    width = max(len(key) for key, _ in rows)
    for key, value in rows:
        print(f"{key.ljust(width)} : {value}")


def cmd_create(force=False):
    ensure_warp_profile(force=force)
    endpoint = choose_working_endpoint(required=False)
    config = load_config()
    configure_warp_outbound(config, endpoint_override=endpoint)
    backup = apply_config(config)
    print("WARP outbound configured but not enabled.")
    print(f"WARP endpoint: {endpoint}")
    print(f"Backup: {backup}")


def cmd_enable():
    config = load_config()
    if not WGCF_PROFILE.exists():
        print("WARP profile is missing. Creating it first...")
        ensure_warp_profile(force=False)
    endpoint = choose_working_endpoint(required=True)
    enable_warp_route(config, endpoint_override=endpoint)
    backup = apply_config(config)
    print("WARP is enabled for Xray outbound traffic.")
    print("All managed catch-all tcp/udp traffic now goes through tag: warp-out")
    print(f"WARP endpoint: {endpoint}")
    print(f"Backup: {backup}")


def cmd_disable():
    config = load_config()
    changed = disable_warp_route(config)
    if not changed:
        print("WARP route is already disabled.")
        cmd_verify_disabled()
        return
    backup = apply_config(config)
    print("WARP route is disabled.")
    if any(item.get("tag") == CASCADE_UPSTREAM_TAG for item in config.get("outbounds", [])):
        print("Catch-all traffic restored to cascade-upstream.")
    else:
        print("Catch-all traffic restored to default direct outbound.")
    print(f"Backup: {backup}")
    cmd_verify_disabled()


def cmd_remove():
    config = load_config()
    changed = remove_warp(config)
    if not changed:
        print("WARP config is already removed from Xray.")
        return
    backup = apply_config(config)
    print("WARP outbound removed from Xray config. Local wgcf profile files were kept.")
    print(f"Backup: {backup}")


def wait_for_tcp(host, port, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def remove_inbound_routes(config, tag):
    rules = routing_rules(config)
    kept = []
    for rule in rules:
        if tag in rule_values(rule, "inboundTag"):
            continue
        kept.append(rule)
    config["routing"]["rules"] = kept


def add_socks_inbound(config, tag, port):
    inbounds = [
        inbound
        for inbound in config.get("inbounds", [])
        if inbound.get("tag") != tag
    ]
    inbounds.append(
        {
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
    )
    config["inbounds"] = inbounds


def add_test_inbound(config):
    add_socks_inbound(config, TEST_INBOUND_TAG, TEST_SOCKS_PORT)
    remove_inbound_routes(config, TEST_INBOUND_TAG)
    rules = routing_rules(config)
    rules.insert(
        0,
        {
            "type": "field",
            "inboundTag": [TEST_INBOUND_TAG],
            "outboundTag": WARP_OUTBOUND_TAG,
        },
    )


def add_verify_disabled_inbound(config):
    add_socks_inbound(config, VERIFY_INBOUND_TAG, VERIFY_SOCKS_PORT)
    remove_inbound_routes(config, VERIFY_INBOUND_TAG)


def cloudflare_trace_via_proxy(port):
    proxy = f"socks5h://{TEST_SOCKS_HOST}:{port}"
    return run_capture(
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
            "https://www.cloudflare.com/cdn-cgi/trace",
        ],
        timeout=25,
    )


def trace_field(output, name):
    prefix = f"{name}="
    return next((line for line in output.splitlines() if line.startswith(prefix)), "")


def trace_detail(output):
    ip_line = trace_field(output, "ip") or "ip=?"
    warp_line = trace_field(output, "warp") or "warp=?"
    return f"{ip_line} {warp_line}"


def probe_warp_disabled():
    original_text = CONFIG_PATH.read_text()
    config = json.loads(original_text)
    test_config = copy.deepcopy(config)
    add_verify_disabled_inbound(test_config)

    try:
        install_config_without_backup(test_config)
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        restart_xray()
        if not wait_for_tcp(TEST_SOCKS_HOST, VERIFY_SOCKS_PORT):
            return False, "Temporary SOCKS inbound did not become ready.", False
        time.sleep(0.5)

        result = cloudflare_trace_via_proxy(VERIFY_SOCKS_PORT)
        output = result.stdout.strip()
        error = result.stderr.strip()
        if result.returncode != 0 or not output:
            return False, error or output or f"curl exited with {result.returncode}", False
        warp_line = trace_field(output, "warp")
        if not warp_line:
            return False, f"Cloudflare trace did not include warp status: {trace_detail(output)}", False
        return True, trace_detail(output), warp_line in ("warp=on", "warp=plus")
    finally:
        CONFIG_PATH.write_text(original_text)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        try:
            run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
            restart_xray()
        except subprocess.CalledProcessError as exc:
            print(f"ERROR: Failed to restore Xray after WARP disable verification: {exc}", file=sys.stderr)
            raise


def cmd_verify_disabled():
    print("Verifying normal Xray route without forcing warp-out...")
    ok, detail, warp_active = probe_warp_disabled()
    if not ok:
        die(f"Could not verify that WARP is disabled: {detail}")
    if warp_active:
        die(f"Normal Xray route still uses WARP: {detail}")
    print(green(f"OK normal Xray route does not use WARP -> {detail}"))


def probe_warp_endpoint(endpoint):
    original_text = CONFIG_PATH.read_text()
    config = json.loads(original_text)
    test_config = copy.deepcopy(config)
    upsert_warp_outbound(test_config, endpoint_override=endpoint)
    add_test_inbound(test_config)

    try:
        install_config_without_backup(test_config)
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        restart_xray()
        if not wait_for_tcp(TEST_SOCKS_HOST, TEST_SOCKS_PORT):
            return False, "Temporary SOCKS inbound did not become ready."
        time.sleep(1.5)

        last_detail = ""
        for attempt in range(2):
            result = cloudflare_trace_via_proxy(TEST_SOCKS_PORT)
            output = result.stdout.strip()
            error = result.stderr.strip()
            if result.returncode == 0 and output:
                warp_line = trace_field(output, "warp")
                if warp_line in ("warp=on", "warp=plus"):
                    return True, trace_detail(output)
                last_detail = trace_detail(output)
            else:
                last_detail = error or output or f"curl exited with {result.returncode}"
            if attempt == 0:
                time.sleep(1.0)
        return False, last_detail
    finally:
        CONFIG_PATH.write_text(original_text)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        try:
            run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
            restart_xray()
        except subprocess.CalledProcessError as exc:
            print(f"ERROR: Failed to restore Xray after endpoint probe: {exc}", file=sys.stderr)
            raise


def profile_endpoint():
    settings = parse_wgcf_profile(WGCF_PROFILE)
    return settings.get("peers", [{}])[0].get("endpoint", "")


def choose_working_endpoint(required=False):
    endpoint = profile_endpoint()
    print(f"Testing WARP endpoint from profile: {endpoint}")
    ok, detail = probe_warp_endpoint(endpoint)
    if ok:
        print(f"OK {endpoint} -> {detail}")
        return endpoint
    print(f"FAIL {endpoint} -> {detail}")

    for candidate in ipv4_endpoint_candidates(endpoint):
        print(f"Testing WARP IPv4 fallback endpoint: {candidate}")
        ok, detail = probe_warp_endpoint(candidate)
        if ok:
            print(f"OK {candidate} -> {detail}")
            return candidate
        print(f"FAIL {candidate} -> {detail}")

    message = "No working WARP endpoint found."
    if required:
        die(message)
    print(f"WARN: {message} Keeping profile endpoint in config; do not enable WARP until test passes.")
    return endpoint


def cmd_test():
    if not WGCF_PROFILE.exists():
        die("WARP profile is missing. Run: xray-warp create")

    original_text = CONFIG_PATH.read_text()
    config = json.loads(original_text)
    test_config = copy.deepcopy(config)
    upsert_warp_outbound(test_config, replace=False)
    add_test_inbound(test_config)

    try:
        install_config_without_backup(test_config)
        run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
        restart_xray()
        proxy = f"socks5h://{TEST_SOCKS_HOST}:{TEST_SOCKS_PORT}"
        print(f"Temporary SOCKS inbound: {TEST_SOCKS_HOST}:{TEST_SOCKS_PORT}")
        if not wait_for_tcp(TEST_SOCKS_HOST, TEST_SOCKS_PORT):
            die("Temporary SOCKS inbound did not become ready.")
        time.sleep(0.5)

        tests = [
            ("Cloudflare trace", "https://www.cloudflare.com/cdn-cgi/trace"),
            ("IPv4", "https://checkip.amazonaws.com"),
            ("IPv4", "https://icanhazip.com"),
        ]
        ok = False
        for label, url in tests:
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
                if "cdn-cgi/trace" in url:
                    warp_line = next((line for line in output.splitlines() if line.startswith("warp=")), "")
                    ip_line = next((line for line in output.splitlines() if line.startswith("ip=")), "")
                    print(f"OK {label} -> {ip_line or 'ip=?'} {warp_line or 'warp=?'}")
                else:
                    print(f"OK {label} {url} -> {output}")
            else:
                detail = error or output or f"curl exited with {result.returncode}"
                print(f"FAIL {label} {url} -> {detail}")
        if not ok:
            die("No test endpoint succeeded through WARP.")
    finally:
        CONFIG_PATH.write_text(original_text)
        shutil.chown(CONFIG_PATH, user="root", group="xray")
        os.chmod(CONFIG_PATH, 0o640)
        try:
            run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
            restart_xray()
        except subprocess.CalledProcessError as exc:
            print(f"ERROR: Failed to restore Xray after test: {exc}", file=sys.stderr)
            raise

    print("Test finished. Original config restored.")


def usage():
    print(
        """Usage:
  xray-warp status
  xray-warp create [--force]
  xray-warp enable
  xray-warp disable
  xray-warp verify-disabled
  xray-warp remove
  xray-warp test
"""
    )


def main():
    require_root()
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if command == "status":
            print_status()
        elif command == "create" and len(sys.argv) in (2, 3):
            force = len(sys.argv) == 3 and sys.argv[2] == "--force"
            if len(sys.argv) == 3 and not force:
                usage()
                sys.exit(1)
            cmd_create(force=force)
        elif command == "enable" and len(sys.argv) == 2:
            cmd_enable()
        elif command == "disable" and len(sys.argv) == 2:
            cmd_disable()
        elif command == "verify-disabled" and len(sys.argv) == 2:
            cmd_verify_disabled()
        elif command == "remove" and len(sys.argv) == 2:
            cmd_remove()
        elif command == "test" and len(sys.argv) == 2:
            cmd_test()
        else:
            usage()
            sys.exit(1)
    except subprocess.CalledProcessError as exc:
        stdout = (exc.stdout or "").strip()
        stderr = (exc.stderr or "").strip()
        if stdout:
            print(stdout)
        if stderr:
            print(stderr, file=sys.stderr)
        die(f"Command failed: {' '.join(str(item) for item in exc.cmd)}")


if __name__ == "__main__":
    main()
