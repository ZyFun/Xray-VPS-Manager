"""Счётчики старых credentials между клиентской записью и остановкой Xray."""

from xray_vps_manager.core.clock import parse_time
from xray_vps_manager.db.repositories import clients, settings, traffic
from xray_vps_manager.db.repositories.base import decode_json, encode_json


KEY = "clients.trafficHandoff"
DB_FIELD = "clientTrafficHandoff"


def load(conn):
    return decode_json(settings.get_metadata(conn, KEY))


def save(conn, handoff):
    settings.set_metadata(conn, KEY, encode_json(handoff))


def start(conn, *, owner, name, retired, target):
    """Сохранить только базовые runtime-счётчики, без накопленной истории."""
    if not retired:
        return False
    aggregate = traffic.get_traffic_totals(conn, name)
    stored = {tag: traffic.get_credential_traffic_totals(conn, name, tag) for tag in retired}
    first = min(retired)
    seeded = bool(aggregate) and not any(stored.values())
    if seeded:
        # traffic-sync инициализирует первую credential из общего итога старой БД.
        stored[first] = aggregate
    credentials = {
        tag: {"email": email, "last": stored[tag].get("last", {}), "history": {}}
        for tag, email in retired.items()
    }
    if seeded:
        credentials[first]["seeded"] = True
    save(conn, {"owner": owner, "name": name, "target": target, "credentials": credentials})
    return True


def _is_newer(value, previous):
    if not value:
        return False
    if not previous:
        return True
    left, right = parse_time(value), parse_time(previous)
    return left > right if left is not None and right is not None else value > previous


def _merge_totals(entry, delta):
    for direction in ("incoming", "outgoing"):
        entry[direction] = entry.get(direction, 0) + delta.get(direction, 0)
    if _is_newer(delta.get("lastOnline"), entry.get("lastOnline")):
        entry["lastOnline"] = delta["lastOnline"]
        entry["lastOnlineSource"] = delta["lastOnlineSource"]
    for key in ("lastAccepted", "updated"):
        if _is_newer(delta.get(key), entry.get(key)):
            entry[key] = delta[key]


def _restore(conn, handoff):
    name = handoff["name"]
    credentials = handoff["credentials"]
    aggregate = traffic.get_traffic_totals(conn, name)
    removed = not handoff.get("target")
    for tag, delta in credentials.items():
        if not delta.get("sampled") and not delta.get("lastAccepted"):
            continue
        aggregate.setdefault("email", delta["email"])
        if removed:
            _merge_totals(aggregate, delta)
        restore_credential = clients.get_client_credential(conn, name, tag) is not None
        copy_move_history = False
        if restore_credential:
            entry = traffic.get_credential_traffic_totals(conn, name, tag)
            seeded = not entry and delta.get("seeded")
            if seeded:
                entry = traffic.get_traffic_entry(conn, name)
                copy_move_history = not removed  # Общий трафик переноса уже записан синхронизацией.
            if not copy_move_history:
                _merge_totals(entry, delta)
            entry["email"] = delta["email"]
            entry["last"] = delta["last"]
            if seeded:
                traffic.upsert_credential_traffic_entry(conn, name, tag, entry)
            else:
                traffic.upsert_credential_traffic_totals(conn, name, tag, entry)
        for day, buckets in delta["history"].items():
            for hour, values in buckets.items():
                incoming, outgoing = values["incoming"], values["outgoing"]
                if removed:
                    traffic.add_history_delta(conn, name, day, int(hour), incoming, outgoing)
                if restore_credential and not copy_move_history:
                    traffic.add_credential_history_delta(conn, name, tag, day, int(hour), incoming, outgoing)
    if any(delta.get("sampled") or delta.get("lastAccepted") for delta in credentials.values()):
        aggregate["last"] = {
            direction: sum(delta["last"].get(direction, 0) for delta in credentials.values())
            for direction in ("uplink", "downlink")
        }
        traffic.upsert_traffic_totals(conn, name, aggregate)


def finish(conn, *, owner, restore_credentials):
    handoff = load(conn)
    if handoff.get("owner") != owner:
        return
    if restore_credentials:
        _restore(conn, handoff)
    settings.delete_metadata(conn, KEY)
