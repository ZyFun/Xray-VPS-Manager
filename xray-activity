#!/usr/bin/env python3
import fcntl
import fnmatch
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
CLIENT_DB_PATH = Path("/usr/local/etc/xray/clients.json")
SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
ACTIVITY_DB_PATH = Path("/usr/local/etc/xray/activity.json")
ACTIVITY_EXCEPTIONS_PATH = Path("/usr/local/etc/xray/activity-exceptions.json")
ACTIVITY_DIR = Path("/usr/local/etc/xray/activity")
CLIENT_LOG_DIR = ACTIVITY_DIR / "clients"
EXPORT_DIR = Path("/root/xray_activity_exports")
LOCK_PATH = Path("/usr/local/etc/xray/activity.lock")
ACCESS_LOG_PATH = Path("/var/log/xray/access.log")
XRAY_BIN = Path("/usr/local/bin/xray")
GEOIP_PATHS = [
    Path("/usr/local/share/xray/geoip.dat"),
    Path("/usr/share/xray/geoip.dat"),
    Path("/usr/local/share/v2ray/geoip.dat"),
    Path("/usr/share/v2ray/geoip.dat"),
]
INBOUND_TAG = "vless-reality"
XRAY_GEOIP_OUTBOUND_PREFIX = "geoip-warning-"
DEFAULT_RETENTION_DAYS = 365
SMTP_PORTS = {"25", "465", "587", "2525"}
ADMIN_PORTS = {"22", "23", "135", "139", "445", "3389", "5900"}
DEFAULT_RISK_BURST_EVENTS = 1000
DEFAULT_RISK_BURST_WINDOW_MINUTES = 15
DEFAULT_RISK_UNIQUE_HOSTS = 500
DEFAULT_RISK_UNIQUE_PORTS = 20
ACCESS_RE = re.compile(
    r"^(?P<time>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\s+(?P<body>.*?)\s+email:\s+(?P<email>.+)$"
)
ROUTE_RE = re.compile(r"\[([^\]]+)\]")
TARGET_RE = re.compile(r"\b(?P<status>accepted|rejected)\s+(?P<target>(?P<network>tcp|udp):\S+)")
NETWORK_TARGET_RE = re.compile(r"\b(?P<target>(?P<network>tcp|udp):\S+)")
GEOIP_CODES_CACHE = None
EXCEPTION_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:*?/-]+$")

if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def log(message):
    if "--quiet" not in sys.argv:
        print(message)


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def run_capture(command, timeout=10):
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def require_root():
    if os.geteuid() != 0:
        die("Run this script as root.")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_stamp():
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_time(value):
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def access_time_to_iso(value):
    parsed = datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def parse_date(value, label="DATE"):
    try:
        return date.fromisoformat(value)
    except ValueError:
        die(f"{label} must be in YYYY-MM-DD format.")


def today_utc_date():
    return utc_now().date()


def date_range_from_days(days):
    end = today_utc_date()
    start = end - timedelta(days=max(1, int(days)) - 1)
    return start, end


def iter_dates(start, end):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def server_env_values():
    values = {}
    if SERVER_ENV_PATH.exists():
        for line in SERVER_ENV_PATH.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"').strip("'")
    return values


def write_server_env(values):
    values.pop("ACTIVITY_GEOIP_WARNING_CODE", None)
    ordered = [
        "SERVER_ADDR",
        "SERVER_NAME",
        "PORT",
        "REALITY_SNI",
        "REALITY_DEST",
        "FINGERPRINT",
        "MANAGER_TIMEZONE",
        "SECURITY_AUDIT_LAST_RUN",
        "ACTIVITY_LOGGING_ENABLED",
        "ACTIVITY_RETENTION_DAYS",
        "ACTIVITY_RISK_BURST_EVENTS",
        "ACTIVITY_RISK_BURST_WINDOW_MINUTES",
        "ACTIVITY_RISK_UNIQUE_HOSTS",
        "ACTIVITY_RISK_UNIQUE_PORTS",
        "ACTIVITY_XRAY_GEOIP_WARNING_CODE",
        "ACTIVITY_XRAY_GEOIP_PREVIOUS_DOMAIN_STRATEGY",
    ]
    lines = [f"{key}={values.get(key, '')}" for key in ordered if key in values]
    for key in sorted(values):
        if key not in ordered:
            lines.append(f"{key}={values[key]}")
    tmp = SERVER_ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    try:
        shutil.chown(tmp, user="root", group="xray")
    except LookupError:
        shutil.chown(tmp, user="root")
    os.chmod(tmp, 0o640)
    tmp.replace(SERVER_ENV_PATH)


def activity_enabled():
    return (server_env_values().get("ACTIVITY_LOGGING_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "y")


def xray_geoip_warning_code():
    return (server_env_values().get("ACTIVITY_XRAY_GEOIP_WARNING_CODE") or "").strip().upper()


def retention_days():
    raw = (server_env_values().get("ACTIVITY_RETENTION_DAYS") or str(DEFAULT_RETENTION_DAYS)).strip()
    try:
        value = int(raw, 10)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return max(1, value)


def parse_retention_days(value):
    raw = str(value or "").strip()
    if not re.fullmatch(r"[0-9]+", raw):
        die("Retention days must be a number from 1 to 3650.")
    days = int(raw, 10)
    if days < 1 or days > 3650:
        die("Retention days must be a number from 1 to 3650.")
    return days


def env_int(name, default, minimum=1, maximum=1000000):
    raw = (server_env_values().get(name) or str(default)).strip()
    try:
        value = int(raw, 10)
    except ValueError:
        return default
    if value < minimum or value > maximum:
        return default
    return value


def risk_limits():
    return {
        "burstEvents": env_int("ACTIVITY_RISK_BURST_EVENTS", DEFAULT_RISK_BURST_EVENTS, 1, 1000000),
        "burstWindowMinutes": env_int("ACTIVITY_RISK_BURST_WINDOW_MINUTES", DEFAULT_RISK_BURST_WINDOW_MINUTES, 1, 1440),
        "uniqueHosts": env_int("ACTIVITY_RISK_UNIQUE_HOSTS", DEFAULT_RISK_UNIQUE_HOSTS, 1, 1000000),
        "uniquePorts": env_int("ACTIVITY_RISK_UNIQUE_PORTS", DEFAULT_RISK_UNIQUE_PORTS, 1, 65535),
    }


def with_activity_defaults(env):
    env.setdefault("ACTIVITY_LOGGING_ENABLED", "false")
    env.setdefault("ACTIVITY_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    env.setdefault("ACTIVITY_RISK_BURST_EVENTS", str(DEFAULT_RISK_BURST_EVENTS))
    env.setdefault("ACTIVITY_RISK_BURST_WINDOW_MINUTES", str(DEFAULT_RISK_BURST_WINDOW_MINUTES))
    env.setdefault("ACTIVITY_RISK_UNIQUE_HOSTS", str(DEFAULT_RISK_UNIQUE_HOSTS))
    env.setdefault("ACTIVITY_RISK_UNIQUE_PORTS", str(DEFAULT_RISK_UNIQUE_PORTS))
    env.setdefault("ACTIVITY_XRAY_GEOIP_WARNING_CODE", "")
    return env


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def chown_xray(path):
    try:
        shutil.chown(path, user="root", group="xray")
    except LookupError:
        shutil.chown(path, user="root")


def ensure_dirs():
    ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    chown_xray(ACTIVITY_DIR)
    chown_xray(CLIENT_LOG_DIR)
    shutil.chown(EXPORT_DIR, user="root")
    os.chmod(ACTIVITY_DIR, 0o750)
    os.chmod(CLIENT_LOG_DIR, 0o750)
    os.chmod(EXPORT_DIR, 0o700)


def save_activity_db(db):
    ensure_dirs()
    tmp = ACTIVITY_DB_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n")
    chown_xray(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(ACTIVITY_DB_PATH)


def load_activity_db():
    db = load_json(ACTIVITY_DB_PATH, {})
    if not isinstance(db, dict):
        db = {}
    db.setdefault("version", 1)
    db.setdefault("clients", {})
    db.setdefault("accessLog", {})
    db["retentionDays"] = retention_days()
    db["enabled"] = activity_enabled()
    return db


def normalize_exception_value(value, fatal=True):
    def fail(message):
        if fatal:
            die(message)
        raise ValueError(message)

    raw = str(value or "").strip()
    if not raw:
        fail("Exception value must not be empty.")

    if raw.startswith(("tcp:", "udp:")):
        _network, host, _port = parse_target(raw)
        raw = host or raw

    if "://" in raw:
        parsed = urlparse(raw)
        raw = parsed.hostname or raw

    raw = raw.strip().strip("[]").strip().lower()
    if not raw:
        fail("Exception value must contain a domain, IP, CIDR, or wildcard mask.")

    if "/" not in raw and raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if port.isdigit():
            raw = host.strip("[]")

    if not EXCEPTION_VALUE_RE.fullmatch(raw):
        fail("Exception may contain only letters, digits, dot, dash, underscore, *, ?, /, and :.")
    return raw


def classify_exception_value(value, fatal=True):
    normalized = normalize_exception_value(value, fatal=fatal)
    if "/" in normalized:
        try:
            ipaddress.ip_network(normalized, strict=False)
            return normalized, "cidr"
        except ValueError:
            if fatal:
                die("CIDR exception must be a valid IP network, for example 203.0.113.0/24.")
            raise
    try:
        ipaddress.ip_address(normalized)
        return normalized, "ip"
    except ValueError:
        pass
    if "*" in normalized or "?" in normalized:
        return normalized, "mask"
    return normalized, "domain"


def load_activity_exceptions():
    db = load_json(ACTIVITY_EXCEPTIONS_PATH, {})
    if not isinstance(db, dict):
        db = {}
    items = []
    seen = set()
    for item in db.get("items", []):
        if isinstance(item, str):
            item = {"value": item, "source": "legacy"}
        if not isinstance(item, dict):
            continue
        try:
            value, kind = classify_exception_value(item.get("value", ""), fatal=False)
        except ValueError:
            continue
        if value in seen:
            continue
        seen.add(value)
        items.append({
            "value": value,
            "kind": kind,
            "createdAt": item.get("createdAt") or utc_stamp(),
            "source": item.get("source") or "manual",
        })
    return {"version": 1, "items": items}


def save_activity_exceptions(db):
    ensure_dirs()
    tmp = ACTIVITY_EXCEPTIONS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n")
    chown_xray(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(ACTIVITY_EXCEPTIONS_PATH)


def exception_items():
    return load_activity_exceptions().get("items", [])


def host_for_exception_match(host):
    value = str(host or "").strip().strip("[]").lower()
    if not value:
        return ""
    if "/" not in value and value.count(":") == 1:
        candidate, port = value.rsplit(":", 1)
        if port.isdigit():
            value = candidate.strip("[]")
    return value


def exception_matches_host(item, host):
    value = item.get("value", "")
    kind = item.get("kind", "")
    host_value = host_for_exception_match(host)
    if not value or not host_value:
        return False
    if kind == "cidr":
        try:
            return ipaddress.ip_address(host_value) in ipaddress.ip_network(value, strict=False)
        except ValueError:
            return False
    if kind == "ip":
        return host_value == value
    if kind == "mask":
        return fnmatch.fnmatchcase(host_value, value)
    return host_value == value


def event_exception(event, exceptions=None):
    exceptions = exceptions if exceptions is not None else exception_items()
    host = event.get("host") or ""
    for item in exceptions:
        if exception_matches_host(item, host):
            return item
    return None


def safe_client_file(name):
    safe = re.sub(r"[^A-Za-z0-9_.@-]+", "_", name).strip("._")
    return CLIENT_LOG_DIR / f"{safe or 'client'}.jsonl"


def split_email(email):
    if "|created=" in email:
        name, _created = email.split("|created=", 1)
        return name
    return email


def reality_inbounds(config):
    return [
        inbound
        for inbound in config.get("inbounds", [])
        if inbound.get("protocol") == "vless"
        and inbound.get("streamSettings", {}).get("security") == "reality"
    ]


def known_clients():
    clients = {}
    config = load_json(CONFIG_PATH, {})
    for inbound in reality_inbounds(config):
        tag = inbound.get("tag") or INBOUND_TAG
        for item in inbound.get("settings", {}).get("clients", []):
            email = item.get("email", "")
            if not email:
                continue
            name = split_email(email)
            clients[name] = {"email": email, "connection": tag}

    db = load_json(CLIENT_DB_PATH, {"clients": {}})
    for name, entry in db.get("clients", {}).items():
        clients.setdefault(
            name,
            {
                "email": entry.get("client", {}).get("email") or name,
                "connection": entry.get("connection") or INBOUND_TAG,
            },
        )
    return clients


def parse_target(value):
    if not value or ":" not in value:
        return "", "", ""
    network, rest = value.split(":", 1)
    host = rest
    port = ""
    if rest.startswith("[") and "]:" in rest:
        host, port = rest[1:].split("]:", 1)
    elif ":" in rest:
        host, port = rest.rsplit(":", 1)
    return network, host, port


def read_varint(data, index):
    shift = 0
    value = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
    raise ValueError("truncated varint")


def parse_proto_fields(data):
    index = 0
    fields = []
    while index < len(data):
        key, index = read_varint(data, index)
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, index = read_varint(data, index)
            fields.append((field_number, wire_type, value))
        elif wire_type == 1:
            value = data[index:index + 8]
            index += 8
            fields.append((field_number, wire_type, value))
        elif wire_type == 2:
            length, index = read_varint(data, index)
            value = data[index:index + length]
            index += length
            fields.append((field_number, wire_type, value))
        elif wire_type == 5:
            value = data[index:index + 4]
            index += 4
            fields.append((field_number, wire_type, value))
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
    return fields


def geoip_path():
    for path in GEOIP_PATHS:
        if path.exists():
            return path
    return None


def iter_geoip_entries():
    path = geoip_path()
    if not path:
        return
    data = path.read_bytes()
    for field_number, wire_type, geoip_blob in parse_proto_fields(data):
        if field_number != 1 or wire_type != 2:
            continue
        country_code = ""
        for inner_number, inner_type, inner_value in parse_proto_fields(geoip_blob):
            if inner_number == 1 and inner_type == 2:
                country_code = inner_value.decode("utf-8", errors="ignore").upper()
        if country_code:
            yield country_code


def available_geoip_codes():
    global GEOIP_CODES_CACHE
    if GEOIP_CODES_CACHE is None:
        try:
            GEOIP_CODES_CACHE = sorted(set(iter_geoip_entries()))
        except Exception:
            GEOIP_CODES_CACHE = []
    return GEOIP_CODES_CACHE


def parse_route(body):
    match = ROUTE_RE.search(body)
    if not match:
        return "", ""
    parts = [part.strip() for part in match.group(1).split("->")]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return parts[0], ""


def parse_source(body):
    first = body.split(" ", 1)[0].strip()
    if first and ":" in first and not first.startswith(("accepted", "rejected", "tcp:", "udp:")):
        return first
    return ""


def event_risks(event):
    risks = []
    port = str(event.get("port") or "")
    outbound = (event.get("outbound") or "").lower()
    target = (event.get("target") or "").lower()
    if port in SMTP_PORTS:
        risks.append("smtp")
    if port in ADMIN_PORTS:
        risks.append("admin-port")
    if "block" in outbound or "blocked" in outbound or "blackhole" in outbound:
        risks.append("blocked")
    if "bittorrent" in target or "torrent" in outbound:
        risks.append("torrent")
    if outbound.startswith(XRAY_GEOIP_OUTBOUND_PREFIX):
        code = outbound[len(XRAY_GEOIP_OUTBOUND_PREFIX):].upper()
        if code:
            risks.append(f"xray-geoip:{code}")
    return risks


def parse_access_line(line, clients):
    match = ACCESS_RE.match(line)
    if not match:
        return None
    email = match.group("email").strip()
    name = split_email(email)
    if name not in clients:
        return None

    body = match.group("body")
    target_match = TARGET_RE.search(body)
    status = ""
    target = ""
    network = ""
    if target_match:
        status = target_match.group("status")
        target = target_match.group("target")
        network = target_match.group("network")
    else:
        target_match = NETWORK_TARGET_RE.search(body)
        if target_match:
            target = target_match.group("target")
            network = target_match.group("network")

    if target:
        network, host, port = parse_target(target)
    else:
        host = ""
        port = ""

    inbound, outbound = parse_route(body)
    event = {
        "time": access_time_to_iso(match.group("time")),
        "client": name,
        "email": clients[name]["email"],
        "connection": clients[name].get("connection", ""),
        "source": parse_source(body),
        "status": status,
        "network": network,
        "target": target,
        "host": host,
        "port": port,
        "inbound": inbound,
        "outbound": outbound,
    }
    risks = event_risks(event)
    if risks:
        event["risks"] = risks
    return event


def append_event(event):
    ensure_dirs()
    path = safe_client_file(event["client"])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    chown_xray(path)
    os.chmod(path, 0o640)


def update_summary(db, event):
    clients = db.setdefault("clients", {})
    entry = clients.setdefault(event["client"], {"days": {}, "totalEvents": 0})
    entry["email"] = event.get("email", "")
    entry["connection"] = event.get("connection", "")
    entry["totalEvents"] = int(entry.get("totalEvents", 0)) + 1
    entry.setdefault("firstSeen", event["time"])
    entry["lastSeen"] = event["time"]

    day_key = event["time"][:10]
    day = entry.setdefault("days", {}).setdefault(
        day_key,
        {
            "events": 0,
            "hosts": {},
            "ports": {},
            "outbounds": {},
            "risks": {},
        },
    )
    day["events"] = int(day.get("events", 0)) + 1
    if event.get("host"):
        day.setdefault("hosts", {})[event["host"]] = int(day.setdefault("hosts", {}).get(event["host"], 0)) + 1
    if event.get("port"):
        day.setdefault("ports", {})[str(event["port"])] = int(day.setdefault("ports", {}).get(str(event["port"]), 0)) + 1
    if event.get("outbound"):
        day.setdefault("outbounds", {})[event["outbound"]] = int(day.setdefault("outbounds", {}).get(event["outbound"], 0)) + 1
    for risk in event.get("risks", []):
        day.setdefault("risks", {})[risk] = int(day.setdefault("risks", {}).get(risk, 0)) + 1


def prune_db_summary(db, cutoff):
    for entry in db.setdefault("clients", {}).values():
        days = entry.get("days", {})
        if not isinstance(days, dict):
            entry["days"] = {}
            continue
        for key in list(days):
            try:
                if date.fromisoformat(key) < cutoff:
                    del days[key]
            except ValueError:
                del days[key]


def prune_client_log(path, cutoff_dt):
    if not path.exists():
        return 0
    kept = []
    removed = 0
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            removed += 1
            continue
        event_time = parse_time(event.get("time"))
        if event_time and event_time >= cutoff_dt:
            kept.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        else:
            removed += 1
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(kept) + ("\n" if kept else ""))
    chown_xray(tmp)
    os.chmod(tmp, 0o640)
    tmp.replace(path)
    return removed


def prune_activity(db, force=False):
    last_prune = parse_time(db.get("lastPrune", ""))
    if not force and last_prune and utc_now() - last_prune < timedelta(hours=20):
        return 0
    cutoff_date = today_utc_date() - timedelta(days=retention_days() - 1)
    cutoff_dt = datetime.combine(cutoff_date, datetime.min.time(), tzinfo=timezone.utc)
    prune_db_summary(db, cutoff_date)
    removed = 0
    if CLIENT_LOG_DIR.exists():
        for path in CLIENT_LOG_DIR.glob("*.jsonl"):
            removed += prune_client_log(path, cutoff_dt)
    db["lastPrune"] = utc_stamp()
    return removed


def initialize_access_offset(db):
    state = db.setdefault("accessLog", {})
    if ACCESS_LOG_PATH.exists():
        stat = ACCESS_LOG_PATH.stat()
        state.update({
            "path": str(ACCESS_LOG_PATH),
            "inode": stat.st_ino,
            "offset": stat.st_size,
            "updated": utc_stamp(),
        })
    else:
        state.update({
            "path": str(ACCESS_LOG_PATH),
            "inode": None,
            "offset": 0,
            "updated": utc_stamp(),
        })


def sync_activity():
    if not activity_enabled():
        log("Activity logging disabled.")
        return 0
    ensure_dirs()
    clients = known_clients()
    if not clients:
        log("No clients found.")
        return 0
    if not ACCESS_LOG_PATH.exists():
        log(f"Access log not found: {ACCESS_LOG_PATH}")
        return 0

    try:
        stat = ACCESS_LOG_PATH.stat()
    except OSError as exc:
        log(f"Cannot stat access log: {exc}")
        return 1

    db = load_activity_db()
    state = db.setdefault("accessLog", {})
    previous_inode = state.get("inode")
    previous_offset = int(state.get("offset", 0) or 0)
    offset = previous_offset if previous_inode == stat.st_ino and stat.st_size >= previous_offset else 0
    processed = 0
    skipped = 0

    try:
        with ACCESS_LOG_PATH.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
            new_offset = handle.tell()
    except OSError as exc:
        log(f"Cannot read access log: {exc}")
        return 1

    for raw_line in data.decode("utf-8", errors="replace").splitlines():
        event = parse_access_line(raw_line, clients)
        if not event:
            skipped += 1
            continue
        append_event(event)
        update_summary(db, event)
        processed += 1

    removed = prune_activity(db)
    state.update({
        "path": str(ACCESS_LOG_PATH),
        "inode": stat.st_ino,
        "offset": new_offset,
        "updated": utc_stamp(),
    })
    db["enabled"] = True
    db["retentionDays"] = retention_days()
    db["lastSync"] = utc_stamp()
    save_activity_db(db)
    log(f"Activity sync saved: {processed} events, {skipped} skipped, {removed} pruned.")
    return 0


def access_log_setting():
    config = load_json(CONFIG_PATH, {})
    return config.get("log", {}).get("access", "")


def access_log_available_for_parsing():
    setting = access_log_setting()
    return setting and setting != "none"


def set_enabled(value):
    env = with_activity_defaults(server_env_values())
    env["ACTIVITY_LOGGING_ENABLED"] = "true" if value else "false"
    write_server_env(env)


def set_retention_days(value):
    days = parse_retention_days(value)
    env = with_activity_defaults(server_env_values())
    env["ACTIVITY_RETENTION_DAYS"] = str(days)
    write_server_env(env)
    db = load_activity_db()
    db["retentionDays"] = days
    removed = prune_activity(db, force=True)
    save_activity_db(db)
    print(f"Activity retention set to {days} days.")
    print(f"Pruned old activity events: {removed}")


def parse_limit_value(label, value, minimum, maximum):
    raw = str(value or "").strip()
    if not re.fullmatch(r"[0-9]+", raw):
        die(f"{label} must be a number from {minimum} to {maximum}.")
    parsed = int(raw, 10)
    if parsed < minimum or parsed > maximum:
        die(f"{label} must be a number from {minimum} to {maximum}.")
    return parsed


def set_risk_limits(burst_events, burst_window_minutes, unique_hosts, unique_ports):
    values = {
        "ACTIVITY_RISK_BURST_EVENTS": parse_limit_value("BURST_EVENTS", burst_events, 1, 1000000),
        "ACTIVITY_RISK_BURST_WINDOW_MINUTES": parse_limit_value("BURST_WINDOW_MINUTES", burst_window_minutes, 1, 1440),
        "ACTIVITY_RISK_UNIQUE_HOSTS": parse_limit_value("UNIQUE_HOSTS", unique_hosts, 1, 1000000),
        "ACTIVITY_RISK_UNIQUE_PORTS": parse_limit_value("UNIQUE_PORTS", unique_ports, 1, 65535),
    }
    env = with_activity_defaults(server_env_values())
    for key, value in values.items():
        env[key] = str(value)
    write_server_env(env)
    print("Activity suspicious limits updated.")
    print_risk_limits()


def print_risk_limits():
    limits = risk_limits()
    rows = [
        ["Burst events", limits["burstEvents"]],
        ["Burst window", f"{limits['burstWindowMinutes']} minutes"],
        ["Unique hosts", limits["uniqueHosts"]],
        ["Unique ports", limits["uniquePorts"]],
    ]
    print_table(["LIMIT", "VALUE"], rows)


def enable_activity():
    ensure_dirs()
    set_enabled(True)
    db = load_activity_db()
    db["enabled"] = True
    db["retentionDays"] = retention_days()
    initialize_access_offset(db)
    save_activity_db(db)
    print("Activity log parsing enabled.")
    print("Collection starts from the current access.log position; older access log lines are not imported.")
    if not access_log_available_for_parsing():
        print("WARN: Xray access log is not configured. Parser is enabled, but no events will be collected until access log exists.")


def disable_activity():
    set_enabled(False)
    db = load_activity_db()
    db["enabled"] = False
    save_activity_db(db)
    print("Activity log parsing disabled.")
    print("Xray access log config was not changed. Existing activity logs were kept.")


def top_items(counter, limit=3):
    if not isinstance(counter, dict) or not counter:
        return "-"
    items = sorted(counter.items(), key=lambda item: int(item[1]), reverse=True)[:limit]
    return ", ".join(f"{key}({value})" for key, value in items)


def table_border(widths):
    return "+" + "+".join("-" * (width + 2) for width in widths) + "+"


def table_row(values, widths):
    return "|" + "|".join(f" {str(values[index]).ljust(widths[index])} " for index in range(len(widths))) + "|"


def print_table(headers, rows):
    if not rows:
        print("No rows.")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))
    border = table_border(widths)
    print(border)
    print(table_row(headers, widths))
    print(border)
    for row in rows:
        print(table_row(row, widths))
    print(border)


def format_size(value):
    value = int(value or 0)
    if value < 1024:
        return f"{value}B"
    for suffix, size in (("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3)):
        next_size = size * 1024
        if value < next_size or suffix == "GB":
            return f"{value / size:.2f}{suffix}"


def iter_events(name, start, end):
    path = safe_client_file(name)
    if not path.exists():
        return
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_time = parse_time(event.get("time"))
        if event_time and start_dt <= event_time < end_dt:
            yield event


def aggregate_events(events, skip_exceptions=False, exceptions=None):
    exceptions = exceptions if exceptions is not None else exception_items()
    result = {
        "events": 0,
        "hosts": {},
        "ports": {},
        "outbounds": {},
        "risks": {},
        "exceptions": {},
        "hours": {},
        "times": [],
        "sources": {},
    }
    for event in events:
        matched_exception = event_exception(event, exceptions)
        if matched_exception:
            value = matched_exception.get("value", "")
            if value:
                result["exceptions"][value] = result["exceptions"].get(value, 0) + 1
            if skip_exceptions:
                continue
        result["events"] += 1
        if event.get("host"):
            result["hosts"][event["host"]] = result["hosts"].get(event["host"], 0) + 1
        if event.get("port"):
            port = str(event["port"])
            result["ports"][port] = result["ports"].get(port, 0) + 1
        if event.get("outbound"):
            result["outbounds"][event["outbound"]] = result["outbounds"].get(event["outbound"], 0) + 1
        if event.get("source"):
            result["sources"][event["source"]] = result["sources"].get(event["source"], 0) + 1
        if not matched_exception:
            for risk in event.get("risks", []):
                result["risks"][risk] = result["risks"].get(risk, 0) + 1
        event_time = parse_time(event.get("time"))
        if event_time:
            result["times"].append(event_time)
            hour_key = event_time.strftime("%Y-%m-%d %H:00")
            result["hours"][hour_key] = result["hours"].get(hour_key, 0) + 1
    return result


def rolling_burst(times, window_minutes):
    ordered = sorted(time for time in times if time)
    if not ordered:
        return 0, None
    best_count = 0
    best_start = None
    end_index = 0
    window = timedelta(minutes=window_minutes)
    for start_index, start_time in enumerate(ordered):
        while end_index < len(ordered) and ordered[end_index] < start_time + window:
            end_index += 1
        count = end_index - start_index
        if count > best_count:
            best_count = count
            best_start = start_time
    return best_count, best_start


def report_client(name, days_value="7"):
    days = int(days_value or "7", 10)
    start, end = date_range_from_days(days)
    exceptions = exception_items()
    rows = []
    total_events = 0
    for day in iter_dates(start, end):
        aggregate = aggregate_events(iter_events(name, day, day), exceptions=exceptions)
        total_events += aggregate["events"]
        rows.append(
            [
                day.isoformat(),
                aggregate["events"],
                len(aggregate["hosts"]),
                top_items(aggregate["ports"]),
                top_items(aggregate["outbounds"]),
                top_items(aggregate["risks"]),
                top_items(aggregate["exceptions"]),
                top_items(aggregate["hosts"]),
            ]
        )
    print(f"Activity report for client: {name}")
    print(f"Period: {start.isoformat()} - {end.isoformat()} UTC")
    print_table(["DATE", "EVENTS", "HOSTS", "PORTS", "OUTBOUNDS", "RISKS", "EXCEPTIONS", "TOP HOSTS"], rows)
    print(f"Total events: {total_events}")


def risk_findings(name, aggregate):
    findings = []
    risks = aggregate["risks"]
    limits = risk_limits()
    if risks.get("smtp", 0) > 0:
        findings.append(("smtp", f"SMTP-like ports used: {risks['smtp']}", "Уточнить назначение трафика; при необходимости временно отключить клиента."))
    if risks.get("blocked", 0) > 0 or risks.get("torrent", 0) > 0:
        count = risks.get("blocked", 0) + risks.get("torrent", 0)
        findings.append(("blocked", f"Blocked/torrent events: {count}", "Проверить отчёт клиента и оставить запрет торрентов включённым."))
    for risk, count in sorted(risks.items()):
        if risk.startswith("xray-geoip:"):
            code = risk.split(":", 1)[1]
            findings.append((risk, f"Xray routed destination events in geoip:{code}: {count}", f"Xray routing зафиксировал трафик в регион {code}; проверить раздельное туннелирование клиента."))
    burst_count, burst_start = rolling_burst(aggregate.get("times", []), limits["burstWindowMinutes"])
    if burst_count >= limits["burstEvents"]:
        start = burst_start.strftime("%Y-%m-%d %H:%M UTC") if burst_start else "unknown time"
        findings.append(("burst", f"{burst_count} events during {limits['burstWindowMinutes']} min from {start}", "Похоже на автоматизацию/парсинг; проверить клиента и лимиты."))
    if len(aggregate["hosts"]) >= limits["uniqueHosts"]:
        findings.append(("many-hosts", f"Unique hosts: {len(aggregate['hosts'])}", "Похоже на парсинг или сканирование; запросить объяснение у клиента."))
    if len(aggregate["ports"]) >= limits["uniquePorts"]:
        findings.append(("many-ports", f"Unique ports: {len(aggregate['ports'])}", "Похоже на сканирование; временно отключить клиента при повторении."))
    return findings


def risk_names_for_event(event):
    risks = set(event.get("risks") or [])
    risks.update(event_risks(event))
    return sorted(str(risk) for risk in risks if risk)


def suspicious(days_value="7"):
    days = int(days_value or "7", 10)
    start, end = date_range_from_days(days)
    clients = known_clients()
    exceptions = exception_items()
    rows = []
    for name in sorted(clients):
        aggregate = aggregate_events(iter_events(name, start, end), skip_exceptions=True, exceptions=exceptions)
        findings = risk_findings(name, aggregate)
        if not findings:
            continue
        risk_names = ", ".join(item[0] for item in findings)
        details = "; ".join(item[1] for item in findings[:3])
        recommendation = findings[0][2]
        rows.append([name, risk_names, aggregate["events"], len(aggregate["hosts"]), top_items(aggregate["ports"]), details, recommendation])

    print(f"Suspicious activity report: {start.isoformat()} - {end.isoformat()} UTC")
    if not rows:
        print("No suspicious activity found by current rules.")
        return
    print_table(["CLIENT", "RISKS", "EVENTS", "HOSTS", "PORTS", "DETAILS", "RECOMMENDATION"], rows)


def geoip_risks_for_event(event):
    return sorted(risk for risk in risk_names_for_event(event) if str(risk).startswith("xray-geoip:"))


def activity_display_timezone():
    configured = (server_env_values().get("MANAGER_TIMEZONE") or "").strip()
    if configured:
        try:
            return ZoneInfo(configured), configured
        except ZoneInfoNotFoundError:
            return timezone.utc, f"UTC (invalid MANAGER_TIMEZONE: {configured})"
    local = datetime.now().astimezone().tzinfo or timezone.utc
    local_name = datetime.now(local).tzname()
    label = "server local time"
    if local_name:
        label += f" ({local_name})"
    return local, label


def format_event_time(value, tzinfo):
    moment = parse_time(value)
    if not moment:
        return value or "-"
    return moment.astimezone(tzinfo).strftime("%Y-%m-%d %H:%M:%S")


def split_ip_or_domain(host):
    value = (host or "").strip().strip("[]")
    if not value:
        return "-", "-"
    try:
        ipaddress.ip_address(value)
        return value, "-"
    except ValueError:
        return "-", value


def geoip_risk_details(days_value="7"):
    days = int(days_value or "7", 10)
    start, end = date_range_from_days(days)
    clients = known_clients()
    found = False
    display_tz, display_tz_label = activity_display_timezone()
    exceptions = exception_items()
    print(f"GeoIP risk details: {start.isoformat()} - {end.isoformat()} UTC")
    print(f"Timezone: {display_tz_label}")
    for name in sorted(clients):
        rows = []
        for event in iter_events(name, start, end):
            if event_exception(event, exceptions):
                continue
            risks = geoip_risks_for_event(event)
            if not risks:
                continue
            ip_value, domain_value = split_ip_or_domain(event.get("host", ""))
            rows.append(
                [
                    format_event_time(event.get("time"), display_tz),
                    ip_value,
                    domain_value,
                    event.get("port") or "-",
                    ", ".join(risk.split(":", 1)[1] for risk in risks),
                    event.get("outbound") or "-",
                ]
            )
        if not rows:
            continue
        found = True
        print()
        print(f"Client: {name}")
        print_table(["TIME", "IP", "DOMAIN", "PORT", "REGION", "OUTBOUND"], rows)
    if not found:
        print("No GeoIP risk events found by current rules.")


def add_exception(value, source="manual"):
    normalized, kind = classify_exception_value(value)
    source = re.sub(r"[^A-Za-z0-9_.@:-]+", "_", str(source or "manual")).strip("_") or "manual"
    db = load_activity_exceptions()
    for item in db.get("items", []):
        if item.get("value") == normalized:
            print(f"Exception already exists: {normalized}")
            return
    db.setdefault("items", []).append({
        "value": normalized,
        "kind": kind,
        "createdAt": utc_stamp(),
        "source": source,
    })
    save_activity_exceptions(db)
    print(f"Added activity exception: {normalized}")
    print(f"Kind: {kind}")


def delete_exception(value):
    normalized, _kind = classify_exception_value(value)
    db = load_activity_exceptions()
    before = len(db.get("items", []))
    db["items"] = [item for item in db.get("items", []) if item.get("value") != normalized]
    if len(db["items"]) == before:
        die(f"Activity exception not found: {normalized}")
    save_activity_exceptions(db)
    print(f"Deleted activity exception: {normalized}")


def delete_all_exceptions(confirmed=False):
    if not confirmed:
        die("Refusing to delete all activity exceptions without --yes.")
    db = load_activity_exceptions()
    count = len(db.get("items", []))
    db["items"] = []
    save_activity_exceptions(db)
    print(f"Deleted activity exceptions: {count}")


def list_exceptions(plain=False):
    db = load_activity_exceptions()
    save_activity_exceptions(db)
    rows = sorted(db.get("items", []), key=lambda item: item.get("value", ""))
    if plain:
        for item in rows:
            print("\t".join([
                item.get("value", ""),
                item.get("kind", ""),
                item.get("createdAt", ""),
                item.get("source", ""),
            ]))
        return
    if not rows:
        print("No activity exceptions configured.")
        return
    print_table(
        ["VALUE", "KIND", "CREATED", "SOURCE"],
        [[item.get("value", ""), item.get("kind", ""), item.get("createdAt", ""), item.get("source", "")] for item in rows],
    )


def exception_candidate_rows(days_value="7"):
    days = int(days_value or "7", 10)
    start, end = date_range_from_days(days)
    clients = known_clients()
    exceptions = exception_items()
    candidates = {}
    for name in sorted(clients):
        for event in iter_events(name, start, end):
            if event_exception(event, exceptions):
                continue
            risks = risk_names_for_event(event)
            if not risks:
                continue
            host = event.get("host") or ""
            if not host:
                continue
            try:
                value, kind = classify_exception_value(host, fatal=False)
            except ValueError:
                continue
            row = candidates.setdefault(
                value,
                {
                    "value": value,
                    "kind": kind,
                    "events": 0,
                    "clients": {},
                    "risks": {},
                    "ports": {},
                    "lastSeen": "",
                    "sampleTarget": event.get("target") or host,
                },
            )
            row["events"] += 1
            row["clients"][name] = row["clients"].get(name, 0) + 1
            for risk in risks:
                row["risks"][risk] = row["risks"].get(risk, 0) + 1
            if event.get("port"):
                port = str(event.get("port"))
                row["ports"][port] = row["ports"].get(port, 0) + 1
            if event.get("time", "") > row["lastSeen"]:
                row["lastSeen"] = event.get("time", "")
                row["sampleTarget"] = event.get("target") or host
    return sorted(candidates.values(), key=lambda row: (row["events"], row["value"]), reverse=True)


def print_exception_candidates(days_value="7", plain=False):
    rows = exception_candidate_rows(days_value)
    if plain:
        for row in rows:
            print("\t".join([
                row["value"],
                row["kind"],
                str(row["events"]),
                top_items(row["clients"], limit=5),
                top_items(row["risks"], limit=5),
                top_items(row["ports"], limit=5),
                row["lastSeen"],
                row["sampleTarget"],
            ]))
        return
    if not rows:
        print("No suspicious activity candidates found for exceptions.")
        return
    print_table(
        ["VALUE", "KIND", "EVENTS", "CLIENTS", "RISKS", "PORTS", "LAST SEEN"],
        [[row["value"], row["kind"], row["events"], top_items(row["clients"], 3), top_items(row["risks"], 3), top_items(row["ports"], 3), row["lastSeen"]] for row in rows],
    )


def export_client(name, start_value, end_value, path_only=False):
    start = parse_date(start_value, "START_DATE")
    end = parse_date(end_value, "END_DATE")
    if end < start:
        die("END_DATE must not be earlier than START_DATE.")
    ensure_dirs()
    events = list(iter_events(name, start, end))
    aggregate = aggregate_events(events)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(EXPORT_DIR, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    safe_name = re.sub(r"[^A-Za-z0-9_.@-]+", "_", name)
    archive = EXPORT_DIR / f"xray-activity-{safe_name}-{start.isoformat()}-{end.isoformat()}-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="xray-activity-export-") as temp_name:
        temp_dir = Path(temp_name)
        events_path = temp_dir / "events.jsonl"
        summary_path = temp_dir / "summary.json"
        readme_path = temp_dir / "README.txt"
        events_path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + ("\n" if events else ""))
        summary_path.write_text(
            json.dumps(
                {
                    "client": name,
                    "period": {"start": start.isoformat(), "end": end.isoformat(), "timezone": "UTC"},
                    "generatedAt": utc_stamp(),
                    "eventCount": aggregate["events"],
                    "uniqueHosts": len(aggregate["hosts"]),
                    "topPorts": aggregate["ports"],
                    "topOutbounds": aggregate["outbounds"],
                    "risks": aggregate["risks"],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        readme_path.write_text(
            "Xray VPS Manager activity export.\n"
            "This archive contains connection metadata only, not decrypted HTTPS contents.\n"
            "Treat it as sensitive personal data.\n"
        )
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(events_path, arcname="events.jsonl")
            tar.add(summary_path, arcname="summary.json")
            tar.add(readme_path, arcname="README.txt")

    os.chmod(archive, 0o600)
    if path_only:
        print(archive)
    else:
        print(f"Export created: {archive}")
        print(f"Events: {len(events)}")
        print(f"Size: {format_size(archive.stat().st_size)}")


def resolve_export_archive(value):
    path = Path(value).expanduser()
    if not path.exists():
        path = EXPORT_DIR / value
    if not path.exists():
        die(f"Activity export archive not found: {value}")
    try:
        export_root = EXPORT_DIR.resolve()
        archive = path.resolve()
    except OSError:
        die(f"Activity export archive not found: {value}")
    if archive.parent != export_root:
        die(f"Refusing to use an archive outside {EXPORT_DIR}: {path}")
    if not archive.name.endswith(".tar.gz"):
        die("Refusing to use a file that does not look like a .tar.gz activity export.")
    return archive


def export_archive_rows():
    rows = []
    if not EXPORT_DIR.exists():
        return rows
    for path in sorted(EXPORT_DIR.glob("*.tar.gz"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        client = "-"
        period = "-"
        events = "-"
        try:
            with tarfile.open(path, "r:gz") as tar:
                member = tar.getmember("summary.json")
                handle = tar.extractfile(member)
                if handle:
                    summary = json.loads(handle.read().decode("utf-8"))
                    client = str(summary.get("client") or "-")
                    period_data = summary.get("period") or {}
                    start = period_data.get("start") or "-"
                    end = period_data.get("end") or "-"
                    period = f"{start}..{end}"
                    events = str(summary.get("eventCount", "-"))
        except Exception:
            pass
        rows.append({
            "path": str(path),
            "file": path.name,
            "created": datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "size": format_size(stat.st_size),
            "client": client,
            "period": period,
            "events": events,
        })
    return rows


def list_exports(plain=False):
    rows = export_archive_rows()
    if plain:
        for row in rows:
            print("\t".join([row["path"], row["created"], row["size"], row["client"], row["period"], row["events"]]))
        return
    if not rows:
        print("No activity export archives found.")
        return
    print_table(
        ["FILE", "CREATED", "SIZE", "CLIENT", "PERIOD", "EVENTS"],
        [[row["file"], row["created"], row["size"], row["client"], row["period"], row["events"]] for row in rows],
    )


def delete_export(value):
    archive = resolve_export_archive(value)
    size = archive.stat().st_size
    archive.unlink()
    print(f"Deleted activity export: {archive}")
    print(f"Freed: {format_size(size)}")


def delete_all_exports(confirmed=False):
    if not confirmed:
        die("Refusing to delete all activity exports without --yes.")
    if not EXPORT_DIR.exists():
        print("No activity export archives found.")
        return

    archives = []
    for path in sorted(EXPORT_DIR.glob("*.tar.gz")):
        try:
            archive = path.resolve()
            if archive.parent == EXPORT_DIR.resolve() and archive.is_file():
                archives.append(archive)
        except OSError:
            continue

    if not archives:
        print("No activity export archives found.")
        return

    total_size = 0
    removed = 0
    for archive in archives:
        try:
            size = archive.stat().st_size
            archive.unlink()
        except OSError as exc:
            print(f"WARN: failed to delete {archive}: {exc}")
            continue
        total_size += size
        removed += 1

    print(f"Deleted activity exports: {removed}")
    print(f"Freed: {format_size(total_size)}")
    print(f"Directory: {EXPORT_DIR}")


def default_ssh_target():
    server_addr = (server_env_values().get("SERVER_ADDR") or os.environ.get("SERVER_ADDR", "")).strip()
    if server_addr:
        return server_addr if "@" in server_addr else f"root@{server_addr}"
    return "root@SERVER_HOST"


def quote_local_path(value):
    if value == "~":
        return "~"
    if value.startswith("~/"):
        rest = value[2:]
        return "~/" + (shlex.quote(rest) if rest else "")
    return shlex.quote(value)


def download_command(value, ssh_target=None, local_path="~/Downloads"):
    archive = resolve_export_archive(value)
    ssh_target = ssh_target or default_ssh_target()
    target = local_path.rstrip("/") + "/"
    print("Run this command on your local computer:")
    print(f"scp {shlex.quote(ssh_target + ':' + str(archive))} {quote_local_path(target)}")


def status():
    db = load_activity_db()
    config = load_json(CONFIG_PATH, {})
    access = config.get("log", {}).get("access", "")
    client_files = list(CLIENT_LOG_DIR.glob("*.jsonl")) if CLIENT_LOG_DIR.exists() else []
    size = sum(path.stat().st_size for path in client_files if path.exists())
    exceptions = exception_items()
    rows = [
        ["Parser enabled", "yes" if activity_enabled() else "no"],
        ["Retention", f"{retention_days()} days"],
        ["Suspicious burst", f"{risk_limits()['burstEvents']} events / {risk_limits()['burstWindowMinutes']} min"],
        ["Suspicious hosts", risk_limits()["uniqueHosts"]],
        ["Suspicious ports", risk_limits()["uniquePorts"]],
        ["Suspicious exceptions", len(exceptions)],
        ["Xray route GeoIP warnings", xray_geoip_warning_code() or "disabled"],
        ["Access log", access or "not configured"],
        ["GeoIP data", str(geoip_path()) if geoip_path() else "geoip.dat not available"],
        ["Activity DB", str(ACTIVITY_DB_PATH)],
        ["Exception DB", str(ACTIVITY_EXCEPTIONS_PATH)],
        ["Client logs", f"{len(client_files)} files, {format_size(size)}"],
        ["Last sync", db.get("lastSync", "never")],
    ]
    print_table(["SETTING", "VALUE"], rows)
    if not activity_enabled():
        print("Activity log parser is disabled. Enable it from menu or run: xray-activity enable")
    if xray_geoip_warning_code() and xray_geoip_warning_code() not in available_geoip_codes():
        print(f"WARN: Xray route GeoIP warnings are set to {xray_geoip_warning_code()}, but this region was not found in geoip.dat.")


def usage():
    print(
        """Usage:
  xray-activity status
  xray-activity enable
  xray-activity disable
  xray-activity sync [--quiet]
  xray-activity client NAME [DAYS]
  xray-activity suspicious [DAYS]
  xray-activity geoip-risks [DAYS]
  xray-activity exception-candidates [DAYS] [--plain]
  xray-activity exceptions [--plain]
  xray-activity exception-add VALUE [SOURCE]
  xray-activity exception-delete VALUE
  xray-activity exception-delete-all --yes
  xray-activity export NAME START_DATE END_DATE [--path-only]
  xray-activity export-list [--plain]
  xray-activity export-delete ARCHIVE_PATH_OR_NAME
  xray-activity export-delete-all --yes
  xray-activity download-command ARCHIVE_PATH_OR_NAME [SSH_TARGET_OR_USER_HOST] [LOCAL_DIR]
  xray-activity retention [DAYS]
  xray-activity risk-limits
  xray-activity risk-limits set BURST_EVENTS BURST_WINDOW_MINUTES UNIQUE_HOSTS UNIQUE_PORTS
  xray-activity geo-list [FILTER]
"""
    )


def main():
    require_root()
    args = [arg for arg in sys.argv[1:] if arg != "--quiet"]
    command = args[0] if args else "status"
    with LOCK_PATH.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if command == "status":
            status()
        elif command == "enable":
            enable_activity()
        elif command == "disable":
            disable_activity()
        elif command == "sync":
            sys.exit(sync_activity())
        elif command == "client" and len(args) in (2, 3):
            report_client(args[1], args[2] if len(args) == 3 else "7")
        elif command == "suspicious" and len(args) in (1, 2):
            suspicious(args[1] if len(args) == 2 else "7")
        elif command == "geoip-risks" and len(args) in (1, 2):
            geoip_risk_details(args[1] if len(args) == 2 else "7")
        elif command == "exception-candidates" and len(args) in (1, 2, 3):
            plain = "--plain" in args
            values = [arg for arg in args[1:] if arg != "--plain"]
            print_exception_candidates(values[0] if values else "7", plain=plain)
        elif command == "exceptions" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--plain":
                usage()
                sys.exit(1)
            list_exceptions(plain=len(args) == 2)
        elif command == "exception-add" and len(args) in (2, 3):
            add_exception(args[1], args[2] if len(args) == 3 else "manual")
        elif command == "exception-delete" and len(args) == 2:
            delete_exception(args[1])
        elif command == "exception-delete-all" and len(args) in (1, 2):
            delete_all_exceptions(confirmed=len(args) == 2 and args[1] == "--yes")
        elif command == "export" and len(args) in (4, 5):
            if len(args) == 5 and args[4] != "--path-only":
                usage()
                sys.exit(1)
            export_client(args[1], args[2], args[3], path_only=len(args) == 5)
        elif command == "export-list" and len(args) in (1, 2):
            if len(args) == 2 and args[1] != "--plain":
                usage()
                sys.exit(1)
            list_exports(plain=len(args) == 2)
        elif command in ("export-delete", "delete-export") and len(args) == 2:
            delete_export(args[1])
        elif command in ("export-delete-all", "delete-all-exports") and len(args) in (1, 2):
            delete_all_exports(confirmed=len(args) == 2 and args[1] == "--yes")
        elif command == "download-command" and len(args) in (2, 3, 4):
            download_command(args[1], args[2] if len(args) >= 3 else None, args[3] if len(args) >= 4 else "~/Downloads")
        elif command == "retention" and len(args) in (1, 2):
            if len(args) == 1:
                print(f"Activity retention: {retention_days()} days")
            else:
                set_retention_days(args[1])
        elif command == "risk-limits" and len(args) in (1, 6):
            if len(args) == 1:
                print_risk_limits()
            elif args[1] == "set":
                set_risk_limits(args[2], args[3], args[4], args[5])
            else:
                usage()
                sys.exit(1)
        elif command == "geo-list" and len(args) in (1, 2):
            query = args[1].upper() if len(args) == 2 else ""
            for code in available_geoip_codes():
                if not query or query in code:
                    print(code)
        else:
            usage()
            sys.exit(1)


if __name__ == "__main__":
    main()
