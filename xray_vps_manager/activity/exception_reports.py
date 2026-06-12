"""Activity exception management and exception candidate reports."""

from __future__ import annotations

from xray_vps_manager.activity import exceptions as activity_exceptions
from xray_vps_manager.activity import repository as activity_repository
from xray_vps_manager.activity import reports as activity_reports
from xray_vps_manager.activity import sync as activity_sync
from xray_vps_manager.activity import time as activity_time


def iter_events(name, start, end):
    yield from activity_repository.iter_events(name, start, end, activity_time.parse_time)


def add_exception(value: str, source: str = "manual") -> dict:
    normalized, kind = activity_exceptions.classify_exception_value(value)
    source = activity_exceptions.normalize_source(source)
    db = activity_exceptions.load_activity_exceptions()
    for item in db.get("items", []):
        if item.get("value") == normalized:
            return {"added": False, "value": normalized, "kind": kind}
    db.setdefault("items", []).append(
        {
            "value": normalized,
            "kind": kind,
            "createdAt": activity_time.utc_stamp(),
            "source": source,
        }
    )
    activity_exceptions.save_activity_exceptions(db)
    return {"added": True, "value": normalized, "kind": kind}


def delete_exception(value: str) -> str:
    normalized, _kind = activity_exceptions.classify_exception_value(value)
    db = activity_exceptions.load_activity_exceptions()
    before = len(db.get("items", []))
    db["items"] = [item for item in db.get("items", []) if item.get("value") != normalized]
    if len(db["items"]) == before:
        raise KeyError(normalized)
    activity_exceptions.save_activity_exceptions(db)
    return normalized


def delete_all_exceptions() -> int:
    db = activity_exceptions.load_activity_exceptions()
    count = len(db.get("items", []))
    db["items"] = []
    activity_exceptions.save_activity_exceptions(db)
    return count


def list_exception_rows() -> list[dict]:
    db = activity_exceptions.load_activity_exceptions()
    activity_exceptions.save_activity_exceptions(db)
    return sorted(db.get("items", []), key=lambda item: item.get("value", ""))


def exception_candidate_rows(days_value: str = "7") -> list[dict]:
    days = int(days_value or "7", 10)
    start, end = activity_time.date_range_from_days(days)
    clients = activity_sync.known_clients()
    exceptions = activity_exceptions.exception_items()
    candidates = {}
    for name in sorted(clients):
        for event in iter_events(name, start, end):
            if activity_exceptions.event_exception(event, exceptions):
                continue
            risks = activity_reports.risk_names_for_event(event)
            if not risks:
                continue
            host = event.get("host") or ""
            if not host:
                continue
            try:
                value, kind = activity_exceptions.classify_exception_value(host, fatal=False)
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
