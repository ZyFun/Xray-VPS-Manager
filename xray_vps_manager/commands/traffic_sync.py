#!/usr/bin/env python3
import fcntl
import json
import os
import re
import subprocess
import sys
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.traffic import repository as traffic_repository

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
CLIENT_DB_PATH = Path("/usr/local/etc/xray/clients.json")
SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
TRAFFIC_PATH = Path("/usr/local/etc/xray/traffic.json")
LOCK_PATH = Path("/usr/local/etc/xray/traffic.lock")
ACCESS_LOG_PATH = Path("/var/log/xray/access.log")
INBOUND_TAG = "vless-reality"
STATS_SERVER = "127.0.0.1:10085"
HISTORY_RETENTION_MONTHS = 6
ACCESS_RE = re.compile(r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)? .* email: (.+)$')


def log(message):
    if "--quiet" not in sys.argv:
        print(message)


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def server_env_values():
    values = {}
    if SERVER_ENV_PATH.exists():
        for line in SERVER_ENV_PATH.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"').strip("'")
    return values


def manager_timezone():
    value = (server_env_values().get("MANAGER_TIMEZONE") or "").strip()
    if not value:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        log(f"Invalid MANAGER_TIMEZONE={value!r}. Falling back to server local time.")
        return datetime.now().astimezone().tzinfo


def local_bucket_time():
    return datetime.now(manager_timezone()).replace(microsecond=0)


def subtract_months(day, months):
    month = day.month - months
    year = day.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def access_time_to_iso(value):
    parsed = datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def max_time(left, right):
    if not left:
        return right
    if not right:
        return left
    return max(left, right)


def run_capture(command, timeout=5):
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def save_traffic(db):
    traffic_repository.save_traffic_db(db, TRAFFIC_PATH)


def split_email(email):
    if "|created=" in email:
        name, _ = email.split("|created=", 1)
        return name
    return email


def find_reality_inbound(config):
    for inbound in config.get("inbounds", []):
        if inbound.get("tag") == INBOUND_TAG:
            return inbound
    for inbound in config.get("inbounds", []):
        if inbound.get("protocol") == "vless" and inbound.get("streamSettings", {}).get("security") == "reality":
            return inbound
    return None


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
    inbounds = reality_inbounds(config)
    if not inbounds:
        inbound = find_reality_inbound(config)
        inbounds = [inbound] if inbound else []
    for inbound in inbounds:
        for item in inbound.get("settings", {}).get("clients", []):
            email = item.get("email", "")
            if not email:
                continue
            clients[split_email(email)] = email

    db = load_json(CLIENT_DB_PATH, {"clients": {}})
    for name, entry in db.get("clients", {}).items():
        email = entry.get("client", {}).get("email") or name
        clients.setdefault(name, email)

    return clients


def parse_stats_output(text):
    stats = {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        items = parsed.get("stat") or parsed.get("stats") or []
        for item in items:
            name = item.get("name")
            value = item.get("value")
            if name is not None and value is not None:
                stats[str(name)] = int(value)
        if stats:
            return stats

    for name, value in re.findall(r'name:\s*"([^"]+)".*?value:\s*([0-9]+)', text, re.S):
        stats[name] = int(value)
    return stats


def query_runtime_stats():
    result = run_capture(
        [
            "/usr/local/bin/xray",
            "api",
            "statsquery",
            f"--server={STATS_SERVER}",
            "-pattern",
            "user>>>",
        ],
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "xray stats API is unavailable").strip())
    return parse_stats_output(result.stdout)


def counter(stats, email, direction):
    return int(stats.get(f"user>>>{email}>>>traffic>>>{direction}", 0))


def positive_delta(current, previous):
    if previous is None:
        return current
    if current >= previous:
        return current - previous
    return current


def set_last_online(entry, stamp, source):
    entry["lastOnline"] = max_time(entry.get("lastOnline", ""), stamp)
    entry["lastOnlineSource"] = source


def ensure_entry(entries, name, email):
    return traffic_repository.ensure_entry(entries, name, email)


def add_history_delta(entry, bucket_time, incoming, outgoing):
    if incoming <= 0 and outgoing <= 0:
        return
    history = entry.setdefault("history", {})
    date_key = bucket_time.strftime("%Y-%m-%d")
    hour_key = bucket_time.strftime("%H")
    hour = history.setdefault(date_key, {}).setdefault(hour_key, {"incoming": 0, "outgoing": 0})
    hour["incoming"] = int(hour.get("incoming", 0)) + incoming
    hour["outgoing"] = int(hour.get("outgoing", 0)) + outgoing


def prune_history(db, bucket_time):
    cutoff = subtract_months(bucket_time.date(), HISTORY_RETENTION_MONTHS)
    for entry in db.setdefault("clients", {}).values():
        history = entry.get("history")
        if not isinstance(history, dict):
            entry["history"] = {}
            continue
        for date_key in list(history):
            try:
                history_date = date.fromisoformat(date_key)
            except ValueError:
                del history[date_key]
                continue
            if history_date < cutoff:
                del history[date_key]


def sync_access_log(db, clients, stamp):
    if not ACCESS_LOG_PATH.exists():
        return set()

    try:
        stat = ACCESS_LOG_PATH.stat()
    except OSError:
        return set()

    access_state = db.setdefault("accessLog", {})
    previous_inode = access_state.get("inode")
    previous_offset = int(access_state.get("offset", 0) or 0)
    offset = previous_offset if previous_inode == stat.st_ino and stat.st_size >= previous_offset else 0

    seen = set()
    entries = db.setdefault("clients", {})
    try:
        with ACCESS_LOG_PATH.open("rb") as log_file:
            log_file.seek(offset)
            data = log_file.read()
            new_offset = log_file.tell()
    except OSError:
        return seen

    for raw_line in data.decode("utf-8", errors="replace").splitlines():
        match = ACCESS_RE.match(raw_line)
        if not match:
            continue
        email = match.group(2).strip()
        name = split_email(email)
        if name not in clients:
            continue
        event_time = access_time_to_iso(match.group(1))
        entry = ensure_entry(entries, name, clients[name])
        set_last_online(entry, event_time, "access-log")
        entry["lastAccepted"] = max_time(entry.get("lastAccepted", ""), event_time)
        seen.add(name)

    access_state.update({
        "path": str(ACCESS_LOG_PATH),
        "inode": stat.st_ino,
        "offset": new_offset,
        "updated": stamp,
    })
    return seen


def sync_locked():
    clients = known_clients()
    if not clients:
        log("No clients found.")
        return 0

    try:
        runtime = query_runtime_stats()
    except Exception as exc:
        log(f"Traffic sync skipped: {exc}")
        return 1

    stamp = now()
    bucket_time = local_bucket_time()
    db = traffic_repository.load_traffic_db(TRAFFIC_PATH)
    db["version"] = 2
    db["historyRetentionMonths"] = HISTORY_RETENTION_MONTHS
    entries = db.setdefault("clients", {})
    access_seen = sync_access_log(db, clients, stamp)

    for name, email in clients.items():
        entry = ensure_entry(entries, name, email)
        last = entry.setdefault("last", {})
        current_up = counter(runtime, email, "uplink")
        current_down = counter(runtime, email, "downlink")
        delta_up = positive_delta(current_up, last.get("uplink"))
        delta_down = positive_delta(current_down, last.get("downlink"))
        entry["incoming"] = int(entry.get("incoming", 0)) + delta_up
        entry["outgoing"] = int(entry.get("outgoing", 0)) + delta_down
        add_history_delta(entry, bucket_time, delta_up, delta_down)
        traffic_changed = delta_up > 0 or delta_down > 0
        if traffic_changed and name not in access_seen:
            set_last_online(entry, stamp, "traffic")
        if traffic_changed:
            entry["updated"] = stamp
        entry["last"] = {
            "uplink": current_up,
            "downlink": current_down,
        }

    prune_history(db, bucket_time)
    db["updated"] = stamp
    save_traffic(db)
    log(f"Traffic stats saved: {TRAFFIC_PATH}")
    return 0


def sync():
    with LOCK_PATH.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return sync_locked()


def main():
    if os.geteuid() != 0:
        print("ERROR: Run this script as root.", file=sys.stderr)
        sys.exit(1)
    sys.exit(sync())


if __name__ == "__main__":
    main()
