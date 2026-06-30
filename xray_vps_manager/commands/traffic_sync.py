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

from xray_vps_manager.core.paths import MANAGER_DB_PATH
from xray_vps_manager.core.server_env import read_server_env
from xray_vps_manager.core.time import parse_time, xray_access_time_to_iso
from xray_vps_manager.clients import credentials as client_credentials
from xray_vps_manager.clients.models import split_email as split_client_email
from xray_vps_manager.clients import repository as client_repository
from xray_vps_manager.traffic import consistency as traffic_consistency
from xray_vps_manager.traffic import repository as traffic_repository

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
SERVER_ENV_PATH = Path("/usr/local/etc/xray/server.env")
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
    return read_server_env(SERVER_ENV_PATH)


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


def access_time_to_iso(value, source_timezone=None):
    return xray_access_time_to_iso(value, source_timezone)


def max_time(left, right):
    if not left:
        return right
    if not right:
        return left
    left_time = parse_time(left)
    right_time = parse_time(right)
    if left_time is not None and right_time is not None:
        return right if right_time > left_time else left
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
    traffic_repository.save_traffic_db(db)


def split_email(email):
    return split_client_email(email)[0]


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


def managed_inbounds(config):
    return [
        inbound
        for inbound in config.get("inbounds", [])
        if inbound.get("protocol") in ("vless", "trojan")
    ]


def known_credentials():
    credentials = {}
    config = load_json(CONFIG_PATH, {})
    inbounds = managed_inbounds(config)
    if not inbounds:
        inbound = find_reality_inbound(config)
        inbounds = [inbound] if inbound else []
    for inbound in inbounds:
        tag = inbound.get("tag") or INBOUND_TAG
        items = inbound.get("settings", {}).get("clients", [])
        if not items and inbound.get("protocol") == "trojan":
            items = inbound.get("settings", {}).get("users", [])
        for item in items:
            email = item.get("email", "")
            if not email:
                continue
            credentials[(split_email(email), tag)] = email

    db = client_repository.load_db_sql()
    for name, entry in client_repository.db_clients(db).items():
        entry_credentials = client_credentials.sorted_credentials(entry)
        if not entry_credentials:
            email = entry.get("client", {}).get("email") or name
            tag = entry.get("connection") or INBOUND_TAG
            credentials.setdefault((name, tag), email)
            continue
        for credential in entry_credentials:
            tag = str(credential.get("connection") or entry.get("connection") or INBOUND_TAG)
            email = client_credentials.credential_email(name, entry, credential)
            credentials.setdefault((name, tag), email)

    return credentials


def known_clients():
    clients = {}
    for (name, _tag), email in known_credentials().items():
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


def counter_delta(current, last, direction):
    if direction not in last or last.get(direction) is None:
        return 0
    return positive_delta(current, last.get(direction))


def set_last_online(entry, stamp, source, reference_stamp=None):
    current = entry.get("lastOnline", "")
    current_time = parse_time(current)
    stamp_time = parse_time(stamp)
    reference_time = parse_time(reference_stamp)
    if (
        current_time is not None
        and stamp_time is not None
        and reference_time is not None
        and current_time > reference_time
        and stamp_time <= reference_time
    ):
        entry["lastOnline"] = stamp
        entry["lastOnlineSource"] = source
        return
    entry["lastOnline"] = max_time(entry.get("lastOnline", ""), stamp)
    entry["lastOnlineSource"] = source


def normalize_future_last_online(entry, stamp):
    last_online = parse_time(entry.get("lastOnline", ""))
    current = parse_time(stamp)
    if last_online is None or current is None or last_online <= current:
        return False

    updated = entry.get("updated", "")
    updated_time = parse_time(updated)
    if updated_time is None or updated_time > current:
        return False

    entry["lastOnline"] = updated
    entry["lastOnlineSource"] = "traffic"
    return True


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


def copy_entry_state(source):
    if not isinstance(source, dict):
        return {}
    copied = {
        "email": source.get("email", ""),
        "incoming": int(source.get("incoming", 0) or 0),
        "outgoing": int(source.get("outgoing", 0) or 0),
        "last": dict(source.get("last") or {}),
        "history": json.loads(json.dumps(source.get("history") if isinstance(source.get("history"), dict) else {})),
    }
    for key in ("lastOnline", "lastOnlineSource", "lastAccepted", "updated"):
        if source.get(key):
            copied[key] = source[key]
    return copied


def ensure_credential_entry(db, name, connection_tag, email):
    credential_entries = db.setdefault("credentials", {})
    entry = traffic_repository.ensure_credential_entry(credential_entries, name, connection_tag, email)
    aggregate = db.setdefault("clients", {}).get(name, {})
    credential_count = len(db.get("credentials", {}).get(name, {}))
    if (
        not entry.get("incoming")
        and not entry.get("outgoing")
        and not entry.get("last")
        and isinstance(aggregate, dict)
        and credential_count == 1
    ):
        entry.update(copy_entry_state(aggregate))
        entry["email"] = email
    return entry


def prune_history(db, bucket_time):
    cutoff = subtract_months(bucket_time.date(), HISTORY_RETENTION_MONTHS)
    entries = list(db.setdefault("clients", {}).values())
    for credentials in db.setdefault("credentials", {}).values():
        if isinstance(credentials, dict):
            entries.extend(credentials.values())
    for entry in entries:
        if not isinstance(entry, dict):
            continue
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


def sync_access_log(db, credentials, stamp):
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
    email_to_credential = {email: (name, tag) for (name, tag), email in credentials.items()}
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
        match_key = email_to_credential.get(email)
        if match_key is None:
            name = split_email(email)
            fallback = [(candidate_name, tag) for (candidate_name, tag), value in credentials.items() if candidate_name == name and value == email]
            match_key = fallback[0] if fallback else None
        if match_key is None:
            continue
        name, connection_tag = match_key
        event_time = access_time_to_iso(match.group(1))
        entry = ensure_entry(entries, name, credentials[match_key])
        credential_entry = ensure_credential_entry(db, name, connection_tag, credentials[match_key])
        set_last_online(entry, event_time, "access-log", stamp)
        set_last_online(credential_entry, event_time, "access-log", stamp)
        entry["lastAccepted"] = max_time(entry.get("lastAccepted", ""), event_time)
        credential_entry["lastAccepted"] = max_time(credential_entry.get("lastAccepted", ""), event_time)
        seen.add(match_key)

    access_state.update({
        "path": str(ACCESS_LOG_PATH),
        "inode": stat.st_ino,
        "offset": new_offset,
        "updated": stamp,
    })
    return seen


def sync_locked():
    credentials = known_credentials()
    if not credentials:
        log("No clients found.")
        return 0

    try:
        runtime = query_runtime_stats()
    except Exception as exc:
        log(f"Traffic sync skipped: {exc}")
        return 1

    stamp = now()
    bucket_time = local_bucket_time()
    db = traffic_repository.load_traffic_db_for_read()
    db["version"] = 2
    db["historyRetentionMonths"] = HISTORY_RETENTION_MONTHS
    entries = db.setdefault("clients", {})
    db.setdefault("credentials", {})
    access_seen = sync_access_log(db, credentials, stamp)

    for (name, connection_tag), email in credentials.items():
        entry = ensure_entry(entries, name, email)
        credential_entry = ensure_credential_entry(db, name, connection_tag, email)
        last = credential_entry.setdefault("last", {})
        current_up = counter(runtime, email, "uplink")
        current_down = counter(runtime, email, "downlink")
        delta_up = counter_delta(current_up, last, "uplink")
        delta_down = counter_delta(current_down, last, "downlink")
        credential_entry["incoming"] = int(credential_entry.get("incoming", 0)) + delta_up
        credential_entry["outgoing"] = int(credential_entry.get("outgoing", 0)) + delta_down
        add_history_delta(credential_entry, bucket_time, delta_up, delta_down)
        entry["incoming"] = int(entry.get("incoming", 0)) + delta_up
        entry["outgoing"] = int(entry.get("outgoing", 0)) + delta_down
        add_history_delta(entry, bucket_time, delta_up, delta_down)
        traffic_changed = delta_up > 0 or delta_down > 0
        if traffic_changed and (name, connection_tag) not in access_seen:
            set_last_online(credential_entry, stamp, "traffic", stamp)
            set_last_online(entry, stamp, "traffic", stamp)
        if traffic_changed:
            credential_entry["updated"] = stamp
            entry["updated"] = stamp
        credential_entry["last"] = {
            "uplink": current_up,
            "downlink": current_down,
        }
        entry["last"] = {
            "uplink": sum(
                int(item.get("last", {}).get("uplink", 0) or 0)
                for item in db.get("credentials", {}).get(name, {}).values()
                if isinstance(item, dict)
            ),
            "downlink": sum(
                int(item.get("last", {}).get("downlink", 0) or 0)
                for item in db.get("credentials", {}).get(name, {}).values()
                if isinstance(item, dict)
            ),
        }
        normalize_future_last_online(credential_entry, stamp)
        normalize_future_last_online(entry, stamp)

    prune_history(db, bucket_time)
    try:
        client_db = client_repository.load_db_sql()
    except Exception as exc:
        log(f"Traffic history repair skipped: {exc}")
    else:
        repaired = traffic_consistency.repair_retained_history_gaps(
            db,
            client_repository.db_clients(client_db),
            subtract_months(bucket_time.date(), HISTORY_RETENTION_MONTHS),
            bucket_time,
        )
        if repaired:
            log(f"Repaired retained traffic history gaps for clients: {', '.join(gap.name for gap in repaired)}")
    db["updated"] = stamp
    save_traffic(db)
    log(f"Traffic stats saved: {MANAGER_DB_PATH}")
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
