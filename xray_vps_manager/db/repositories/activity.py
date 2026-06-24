"""SQLite repository for activity events and suspicious exceptions."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta
from typing import Any, Iterable

from xray_vps_manager.activity.constants import (
    DETAIL_MODE_ALL,
    DETAIL_MODE_OFF,
    DETAIL_MODE_SELECTED,
    DETAIL_MODES,
)
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories.base import decode_json, encode_json

ACTIVITY_ACCESS_LOG_OFFSET = "activity-access-log"
ACTIVITY_SOURCE_METADATA_KEY = "activity.sourceMetadata"
ACTIVITY_SUMMARY_KEY = "activity.summary"
ACTIVITY_DETAIL_MODE_KEY = "activity.detailMode"
ACTIVITY_ALERT_WINDOW_STATE_KEY = "activity.alertWindows"


def normalized_detail_mode(value: str | None, default: str = DETAIL_MODE_OFF) -> str:
    mode = str(value or "").strip().lower()
    if mode in DETAIL_MODES:
        return mode
    return default


def get_detail_mode(connection: sqlite3.Connection, *, legacy_enabled: bool | None = None) -> str:
    row = connection.execute(
        "SELECT value FROM manager_metadata WHERE key = ?",
        (ACTIVITY_DETAIL_MODE_KEY,),
    ).fetchone()
    if row and str(row["value"] or "").strip():
        return normalized_detail_mode(row["value"])
    if legacy_enabled is None:
        return DETAIL_MODE_OFF
    return DETAIL_MODE_ALL if legacy_enabled else DETAIL_MODE_OFF


def set_detail_mode(connection: sqlite3.Connection, mode: str) -> str:
    normalized = normalized_detail_mode(mode)
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO manager_metadata(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (ACTIVITY_DETAIL_MODE_KEY, normalized),
        )
    return normalized


def list_detail_clients(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT client_name FROM activity_capture_clients ORDER BY client_name"
    ).fetchall()
    return [str(row["client_name"]) for row in rows]


def set_detail_clients(connection: sqlite3.Connection, names: Iterable[str]) -> None:
    normalized = sorted({str(name or "").strip() for name in names if str(name or "").strip()})
    with database.transaction(connection):
        connection.execute("DELETE FROM activity_capture_clients")
        for name in normalized:
            connection.execute(
                "INSERT OR IGNORE INTO activity_capture_clients(client_name) VALUES (?)",
                (name,),
            )


def add_detail_client(connection: sqlite3.Connection, name: str) -> bool:
    normalized = str(name or "").strip()
    if not normalized:
        return False
    with database.transaction(connection):
        result = connection.execute(
            "INSERT OR IGNORE INTO activity_capture_clients(client_name) VALUES (?)",
            (normalized,),
        )
    return int(result.rowcount or 0) > 0


def remove_detail_client(connection: sqlite3.Connection, name: str) -> bool:
    with database.transaction(connection):
        result = connection.execute(
            "DELETE FROM activity_capture_clients WHERE client_name = ?",
            (str(name or "").strip(),),
        )
    return int(result.rowcount or 0) > 0


def detail_capture_status(connection: sqlite3.Connection, *, legacy_enabled: bool | None = None) -> dict[str, Any]:
    return {
        "mode": get_detail_mode(connection, legacy_enabled=legacy_enabled),
        "selectedClients": list_detail_clients(connection),
    }


def add_event(connection: sqlite3.Connection, event: dict[str, Any]) -> int:
    risks = event.get("risks") if isinstance(event.get("risks"), list) else []
    with database.transaction(connection):
        cursor = connection.execute(
            """
            INSERT INTO activity_events(
                event_time, client_name, email, connection_tag, source, status, network,
                target, host, port, inbound, outbound, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("time") or event.get("event_time") or "",
                event.get("client") or event.get("client_name") or "",
                event.get("email") or "",
                event.get("connection") or event.get("connection_tag") or "",
                event.get("source") or "",
                event.get("status") or "",
                event.get("network") or "",
                event.get("target") or "",
                event.get("host") or "",
                int(event.get("port") or 0) if str(event.get("port") or "").isdigit() else None,
                event.get("inbound") or "",
                event.get("outbound") or "",
                encode_json(event),
            ),
        )
        event_id = int(cursor.lastrowid)
        for risk in risks:
            connection.execute(
                "INSERT OR IGNORE INTO activity_event_risks(event_id, risk) VALUES (?, ?)",
                (event_id, str(risk)),
            )
    return event_id


def event_exists(connection: sqlite3.Connection, event: dict[str, Any]) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM activity_events
        WHERE event_time = ?
          AND client_name = ?
          AND COALESCE(source, '') = ?
          AND COALESCE(target, '') = ?
          AND COALESCE(outbound, '') = ?
        LIMIT 1
        """,
        (
            event.get("time") or event.get("event_time") or "",
            event.get("client") or event.get("client_name") or "",
            event.get("source") or "",
            event.get("target") or "",
            event.get("outbound") or "",
        ),
    ).fetchone()
    return row is not None


def _event_from_row(connection: sqlite3.Connection, row) -> dict[str, Any]:
    raw = decode_json(row["raw_json"])
    event = dict(raw) if isinstance(raw, dict) else {}
    event.update(
        {
            "id": int(row["id"]),
            "time": row["event_time"],
            "client": row["client_name"],
            "email": row["email"] or "",
            "connection": row["connection_tag"] or "",
            "source": row["source"] or "",
            "status": row["status"] or "",
            "network": row["network"] or "",
            "target": row["target"] or "",
            "host": row["host"] or "",
            "port": str(row["port"] or ""),
            "inbound": row["inbound"] or "",
            "outbound": row["outbound"] or "",
            "risks": event_risks(connection, int(row["id"])),
        }
    )
    return event


def _event_time(event: dict[str, Any]) -> str:
    return str(event.get("time") or event.get("event_time") or "")


def _event_client(event: dict[str, Any]) -> str:
    return str(event.get("client") or event.get("client_name") or "")


def _event_connection(event: dict[str, Any]) -> str:
    return str(event.get("connection") or event.get("connection_tag") or "")


def _event_port_value(event: dict[str, Any]) -> int | None:
    value = str(event.get("port") or "").strip()
    return int(value) if value.isdigit() else None


def _event_count(event: dict[str, Any]) -> int:
    try:
        return max(1, int(event.get("event_count") or event.get("count") or 1))
    except (TypeError, ValueError):
        return 1


def _event_risks_value(event: dict[str, Any]) -> list[str]:
    risks = event.get("risks")
    if isinstance(risks, list):
        return [str(risk) for risk in risks if str(risk or "").strip()]
    risk = event.get("risk")
    if risk:
        return [str(risk)]
    return []


def _risk_severity(risk: str) -> str:
    if risk in {"blocked", "torrent", "admin-port", "smtp"}:
        return "warning"
    if str(risk).startswith("xray-geoip:"):
        return "warning"
    return "info"


def _alert_dedupe_key(event: dict[str, Any], risk: str) -> str:
    event_time = _event_time(event)
    hour = event_time[:13] if len(event_time) >= 13 else event_time
    payload = "|".join(
        [
            hour,
            _event_client(event),
            _event_connection(event),
            str(event.get("host") or ""),
            str(event.get("port") or ""),
            str(event.get("outbound") or ""),
            str(risk),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def add_alert_event(
    connection: sqlite3.Connection,
    event: dict[str, Any],
    risk: str,
    *,
    severity: str | None = None,
    raw_ref_event_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> int:
    event_time = _event_time(event)
    dedupe_key = _alert_dedupe_key(event, risk)
    normalized_severity = severity if severity in {"info", "warning", "critical"} else _risk_severity(risk)
    count = _event_count(event)
    extra_json = dict(extra or {})
    if event.get("event_count") is not None:
        extra_json["eventCount"] = count
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO activity_alert_events(
                event_time, client_name, email, connection_tag, source, status, network,
                target, host, port, inbound, outbound, risk, severity, dedupe_key,
                event_count, first_seen_at, last_seen_at, raw_ref_event_id, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
                event_count = activity_alert_events.event_count + excluded.event_count,
                event_time = CASE
                    WHEN excluded.event_time > activity_alert_events.event_time THEN excluded.event_time
                    ELSE activity_alert_events.event_time
                END,
                last_seen_at = CASE
                    WHEN excluded.last_seen_at > activity_alert_events.last_seen_at THEN excluded.last_seen_at
                    ELSE activity_alert_events.last_seen_at
                END,
                email = excluded.email,
                source = excluded.source,
                status = excluded.status,
                network = excluded.network,
                target = excluded.target,
                inbound = excluded.inbound,
                outbound = excluded.outbound,
                severity = excluded.severity,
                raw_ref_event_id = COALESCE(activity_alert_events.raw_ref_event_id, excluded.raw_ref_event_id),
                extra_json = excluded.extra_json
            """,
            (
                event_time,
                _event_client(event),
                event.get("email") or "",
                _event_connection(event),
                event.get("source") or "",
                event.get("status") or "",
                event.get("network") or "",
                event.get("target") or "",
                event.get("host") or "",
                _event_port_value(event),
                event.get("inbound") or "",
                event.get("outbound") or "",
                str(risk),
                normalized_severity,
                dedupe_key,
                count,
                event_time,
                event_time,
                raw_ref_event_id,
                encode_json(extra_json),
            ),
        )
        row = connection.execute(
            "SELECT id FROM activity_alert_events WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
    return int(row["id"])


def add_alerts_for_event(
    connection: sqlite3.Connection,
    event: dict[str, Any],
    *,
    raw_ref_event_id: int | None = None,
) -> list[int]:
    ids = []
    for risk in _event_risks_value(event):
        ids.append(add_alert_event(connection, event, risk, raw_ref_event_id=raw_ref_event_id))
    return ids


def _window_event(event: dict[str, Any], risk: str, count: int) -> dict[str, Any]:
    item = dict(event)
    item["risk"] = risk
    item["risks"] = [risk]
    item["event_count"] = max(1, int(count or 1))
    item["target"] = ""
    item["host"] = ""
    item["port"] = ""
    item["outbound"] = ""
    return item


def _parse_event_time(value: str):
    from xray_vps_manager.activity.time import parse_time

    return parse_time(value)


def _utc_stamp(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _state_times_after(values: Iterable[str], cutoff) -> list[str]:
    kept = []
    for value in values:
        parsed = _parse_event_time(str(value or ""))
        if parsed is not None and parsed >= cutoff:
            kept.append(_utc_stamp(parsed))
    return kept


def add_window_alerts_for_event(
    connection: sqlite3.Connection,
    event: dict[str, Any],
    limits: dict[str, Any],
    display_tz,
) -> list[int]:
    moment = _parse_event_time(_event_time(event))
    client_name = _event_client(event)
    if moment is None or not client_name:
        return []

    try:
        burst_events = max(1, int(limits.get("burstEvents") or 0))
        burst_window_minutes = max(1, int(limits.get("burstWindowMinutes") or 1))
        unique_hosts_limit = max(1, int(limits.get("uniqueHosts") or 0))
        unique_ports_limit = max(1, int(limits.get("uniquePorts") or 0))
    except (TypeError, ValueError):
        return []

    state = get_metadata_json(connection, ACTIVITY_ALERT_WINDOW_STATE_KEY)
    clients = state.setdefault("clients", {})
    client_state = clients.setdefault(client_name, {})
    last_alerts = client_state.setdefault("lastAlerts", {})
    ids: list[int] = []

    event_count = _event_count(event)
    window_start = moment - timedelta(minutes=burst_window_minutes)
    event_times = _state_times_after(client_state.get("eventTimes", []), window_start)
    event_times.extend([_utc_stamp(moment)] * event_count)
    event_times = event_times[-max(burst_events * 2, 1000):]
    client_state["eventTimes"] = event_times

    local = moment.astimezone(display_tz)
    hour_bucket = local.replace(minute=0, second=0, microsecond=0).isoformat()
    if len(event_times) >= burst_events and last_alerts.get("burst") != hour_bucket:
        ids.append(
            add_alert_event(
                connection,
                _window_event(event, "burst", len(event_times)),
                "burst",
                severity="warning",
                extra={"windowMinutes": burst_window_minutes, "eventsInWindow": len(event_times)},
            )
        )
        last_alerts["burst"] = hour_bucket

    day_bucket = local.date().isoformat()
    if client_state.get("uniqueDay") != day_bucket:
        client_state["uniqueDay"] = day_bucket
        client_state["hosts"] = []
        client_state["ports"] = []
        for key in ("unique-hosts", "unique-ports"):
            if str(last_alerts.get(key) or "") < day_bucket:
                last_alerts.pop(key, None)

    hosts = set(str(value) for value in client_state.get("hosts", []) if str(value or ""))
    ports = set(str(value) for value in client_state.get("ports", []) if str(value or ""))
    host_value = str(event.get("host") or "").strip()
    port_value = str(event.get("port") or "").strip()
    if host_value:
        hosts.add(_counter_unique_hash("host", host_value))
    if port_value:
        ports.add(_counter_unique_hash("port", port_value))
    client_state["hosts"] = sorted(hosts)
    client_state["ports"] = sorted(ports)

    if len(hosts) >= unique_hosts_limit and last_alerts.get("unique-hosts") != day_bucket:
        ids.append(
            add_alert_event(
                connection,
                _window_event(event, "unique-hosts", len(hosts)),
                "unique-hosts",
                severity="warning",
                extra={"uniqueHosts": len(hosts), "bucketStart": day_bucket},
            )
        )
        last_alerts["unique-hosts"] = day_bucket
    if len(ports) >= unique_ports_limit and last_alerts.get("unique-ports") != day_bucket:
        ids.append(
            add_alert_event(
                connection,
                _window_event(event, "unique-ports", len(ports)),
                "unique-ports",
                severity="warning",
                extra={"uniquePorts": len(ports), "bucketStart": day_bucket},
            )
        )
        last_alerts["unique-ports"] = day_bucket

    set_metadata_json(connection, ACTIVITY_ALERT_WINDOW_STATE_KEY, state)
    return ids


def _alert_from_row(row) -> dict[str, Any]:
    extra = decode_json(row["extra_json"], {})
    event: dict[str, Any] = dict(extra) if isinstance(extra, dict) else {}
    risk = str(row["risk"] or "")
    event.update(
        {
            "id": int(row["id"]),
            "alertId": int(row["id"]),
            "time": row["event_time"],
            "client": row["client_name"],
            "email": row["email"] or "",
            "connection": row["connection_tag"] or "",
            "source": row["source"] or "",
            "status": row["status"] or "",
            "network": row["network"] or "",
            "target": row["target"] or "",
            "host": row["host"] or "",
            "port": str(row["port"] or ""),
            "inbound": row["inbound"] or "",
            "outbound": row["outbound"] or "",
            "risk": risk,
            "risks": [risk] if risk else [],
            "severity": row["severity"] or "warning",
            "event_count": int(row["event_count"] or 1),
            "first_seen_at": row["first_seen_at"] or "",
            "last_seen_at": row["last_seen_at"] or "",
            "notified_admin_at": row["notified_admin_at"] or "",
        }
    )
    return event


def max_alert_id(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(id), 0) AS value FROM activity_alert_events").fetchone()
    return int(row["value"] or 0)


def iter_geoip_alerts_after(
    connection: sqlite3.Connection,
    *,
    after_id: int = 0,
    after_time: str | None = None,
    limit: int = 1000,
) -> Iterable[dict[str, Any]]:
    clauses = ["risk LIKE ?"]
    params: list[Any] = ["xray-geoip:%"]
    if after_id > 0:
        clauses.append("(id > ? OR (notified_admin_at IS NOT NULL AND last_seen_at > notified_admin_at))")
        params.append(after_id)
    elif after_time:
        clauses.append("event_time > ?")
        params.append(after_time)
    params.append(max(1, int(limit or 1000)))
    rows = connection.execute(
        f"""
        SELECT *
        FROM activity_alert_events
        WHERE {" AND ".join(clauses)}
        ORDER BY id
        LIMIT ?
        """,
        params,
    ).fetchall()
    for row in rows:
        yield _alert_from_row(row)


def list_alert_events(
    connection: sqlite3.Connection,
    *,
    risk_prefix: str | None = None,
    client_name: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if risk_prefix:
        clauses.append("risk LIKE ?")
        params.append(f"{risk_prefix}%")
    if client_name:
        clauses.append("client_name = ?")
        params.append(client_name)
    if start:
        clauses.append("event_time >= ?")
        params.append(start)
    if end:
        clauses.append("event_time < ?")
        params.append(end)
    params.append(max(1, int(limit or 100)))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM activity_alert_events{where} ORDER BY event_time DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    return [_alert_from_row(row) for row in rows]


def delete_alert_events_before(connection: sqlite3.Connection, cutoff: str) -> int:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM activity_alert_events WHERE event_time < ?", (cutoff,))
    return int(result.rowcount or 0)


def mark_alerts_admin_notified(connection: sqlite3.Connection, alert_ids: Iterable[int], stamp: str) -> int:
    ids = set()
    for alert_id in alert_ids:
        try:
            normalized = int(alert_id or 0)
        except (TypeError, ValueError):
            continue
        if normalized > 0:
            ids.add(normalized)
    ids = sorted(ids)
    if not ids:
        return 0
    with database.transaction(connection):
        result = connection.executemany(
            """
            UPDATE activity_alert_events
            SET notified_admin_at = ?
            WHERE id = ?
            """,
            [(stamp, alert_id) for alert_id in ids],
        )
    return int(result.rowcount or 0)


def _counter_bucket_key(event: dict[str, Any], bucket_type: str, display_tz) -> str:
    from xray_vps_manager.activity.time import parse_time

    parsed = parse_time(_event_time(event))
    if parsed is None:
        return _event_time(event)[:13] if bucket_type == "hour" else _event_time(event)[:10]
    local = parsed.astimezone(display_tz)
    if bucket_type == "hour":
        return local.replace(minute=0, second=0, microsecond=0).isoformat()
    return local.date().isoformat()


def _counter_unique_hash(kind: str, value: str) -> str:
    normalized = f"{kind}:{value.strip().lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def upsert_client_counter(
    connection: sqlite3.Connection,
    event: dict[str, Any],
    *,
    bucket_type: str,
    bucket_start: str,
) -> None:
    if bucket_type not in {"hour", "day"}:
        raise ValueError("bucket_type must be hour or day")
    client_name = _event_client(event)
    connection_tag = _event_connection(event)
    event_time = _event_time(event)
    risks = _event_risks_value(event)
    count = _event_count(event)
    geoip_count = count if any(risk.startswith("xray-geoip:") for risk in risks) else 0
    suspicious_count = count if risks else 0
    blocked_count = count if any(risk == "blocked" for risk in risks) else 0
    with database.transaction(connection):
        row = connection.execute(
            """
            SELECT risk_counts_json
            FROM activity_client_counters
            WHERE client_name = ? AND connection_tag = ? AND bucket_type = ? AND bucket_start = ?
            """,
            (client_name, connection_tag, bucket_type, bucket_start),
        ).fetchone()
        risk_counts = decode_json(row["risk_counts_json"] if row else "{}", {})
        if not isinstance(risk_counts, dict):
            risk_counts = {}
        for risk in risks:
            risk_counts[risk] = int(risk_counts.get(risk, 0) or 0) + count
        connection.execute(
            """
            INSERT INTO activity_client_counters(
                client_name, connection_tag, bucket_type, bucket_start,
                total_events, geoip_events, suspicious_events, blocked_events,
                first_seen_at, last_seen_at, risk_counts_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_name, connection_tag, bucket_type, bucket_start)
            DO UPDATE SET
                total_events = activity_client_counters.total_events + excluded.total_events,
                geoip_events = activity_client_counters.geoip_events + excluded.geoip_events,
                suspicious_events = activity_client_counters.suspicious_events + excluded.suspicious_events,
                blocked_events = activity_client_counters.blocked_events + excluded.blocked_events,
                first_seen_at = CASE
                    WHEN activity_client_counters.first_seen_at IS NULL
                      OR excluded.first_seen_at < activity_client_counters.first_seen_at
                    THEN excluded.first_seen_at
                    ELSE activity_client_counters.first_seen_at
                END,
                last_seen_at = CASE
                    WHEN activity_client_counters.last_seen_at IS NULL
                      OR excluded.last_seen_at > activity_client_counters.last_seen_at
                    THEN excluded.last_seen_at
                    ELSE activity_client_counters.last_seen_at
                END,
                risk_counts_json = excluded.risk_counts_json
            """,
            (
                client_name,
                connection_tag,
                bucket_type,
                bucket_start,
                count,
                geoip_count,
                suspicious_count,
                blocked_count,
                event_time,
                event_time,
                encode_json(risk_counts),
            ),
        )
        for kind, value in (("host", event.get("host")), ("port", event.get("port"))):
            normalized = str(value or "").strip()
            if not normalized:
                continue
            result = connection.execute(
                """
                INSERT OR IGNORE INTO activity_client_counter_uniques(
                    client_name, connection_tag, bucket_type, bucket_start, unique_kind, value_hash
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    client_name,
                    connection_tag,
                    bucket_type,
                    bucket_start,
                    kind,
                    _counter_unique_hash(kind, normalized),
                ),
            )
            if int(result.rowcount or 0) > 0:
                column = "unique_hosts" if kind == "host" else "unique_ports"
                connection.execute(
                    f"""
                    UPDATE activity_client_counters
                    SET {column} = {column} + 1
                    WHERE client_name = ? AND connection_tag = ? AND bucket_type = ? AND bucket_start = ?
                    """,
                    (client_name, connection_tag, bucket_type, bucket_start),
                )


def upsert_client_counters(connection: sqlite3.Connection, event: dict[str, Any], display_tz) -> None:
    for bucket_type in ("hour", "day"):
        upsert_client_counter(
            connection,
            event,
            bucket_type=bucket_type,
            bucket_start=_counter_bucket_key(event, bucket_type, display_tz),
        )


def list_client_counters(
    connection: sqlite3.Connection,
    *,
    bucket_type: str,
    start: str | None = None,
    end: str | None = None,
    client_name: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = ["bucket_type = ?"]
    params: list[Any] = [bucket_type]
    if start:
        clauses.append("bucket_start >= ?")
        params.append(start)
    if end:
        clauses.append("bucket_start < ?")
        params.append(end)
    if client_name:
        clauses.append("client_name = ?")
        params.append(client_name)
    rows = connection.execute(
        f"""
        SELECT
            client_name,
            bucket_type,
            bucket_start,
            SUM(total_events) AS total_events,
            SUM(geoip_events) AS geoip_events,
            SUM(suspicious_events) AS suspicious_events,
            SUM(blocked_events) AS blocked_events,
            MIN(first_seen_at) AS first_seen_at,
            MAX(last_seen_at) AS last_seen_at
        FROM activity_client_counters
        WHERE {" AND ".join(clauses)}
        GROUP BY client_name, bucket_type, bucket_start
        ORDER BY bucket_start DESC, total_events DESC
        LIMIT ?
        """,
        [*params, max(1, int(limit or 100))],
    ).fetchall()
    return [
        {
            "client": row["client_name"],
            "connection": "",
            "bucketType": row["bucket_type"],
            "bucketStart": row["bucket_start"],
            "totalEvents": int(row["total_events"] or 0),
            "geoipEvents": int(row["geoip_events"] or 0),
            "suspiciousEvents": int(row["suspicious_events"] or 0),
            "blockedEvents": int(row["blocked_events"] or 0),
            "uniqueHosts": _client_counter_unique_count(connection, row, "host"),
            "uniquePorts": _client_counter_unique_count(connection, row, "port"),
            "firstSeen": row["first_seen_at"] or "",
            "lastSeen": row["last_seen_at"] or "",
            "riskCounts": _client_counter_risk_counts(connection, row),
        }
        for row in rows
    ]


def _client_counter_unique_count(connection: sqlite3.Connection, row, unique_kind: str) -> int:
    result = connection.execute(
        """
        SELECT COUNT(DISTINCT value_hash) AS count
        FROM activity_client_counter_uniques
        WHERE client_name = ? AND bucket_type = ? AND bucket_start = ? AND unique_kind = ?
        """,
        (row["client_name"], row["bucket_type"], row["bucket_start"], unique_kind),
    ).fetchone()
    return int(result["count"] or 0) if result else 0


def _client_counter_risk_counts(connection: sqlite3.Connection, row) -> dict[str, int]:
    counts: dict[str, int] = {}
    risk_rows = connection.execute(
        """
        SELECT risk_counts_json
        FROM activity_client_counters
        WHERE client_name = ? AND bucket_type = ? AND bucket_start = ?
        """,
        (row["client_name"], row["bucket_type"], row["bucket_start"]),
    ).fetchall()
    for risk_row in risk_rows:
        risk_counts = decode_json(risk_row["risk_counts_json"], {})
        if not isinstance(risk_counts, dict):
            continue
        for risk, count in risk_counts.items():
            risk_name = str(risk or "").strip()
            if not risk_name:
                continue
            try:
                value = int(count or 0)
            except (TypeError, ValueError):
                value = 0
            counts[risk_name] = int(counts.get(risk_name, 0)) + max(0, value)
    return counts


def delete_client_counters_before(connection: sqlite3.Connection, cutoff: str) -> int:
    with database.transaction(connection):
        connection.execute(
            "DELETE FROM activity_client_counter_uniques WHERE bucket_start < ?",
            (cutoff,),
        )
        result = connection.execute(
            "DELETE FROM activity_client_counters WHERE bucket_start < ?",
            (cutoff,),
        )
    return int(result.rowcount or 0)


def upsert_xray_error_event(connection: sqlite3.Connection, item: dict[str, Any]) -> int:
    event_time = str(item.get("event_time") or item.get("time") or "")
    level = str(item.get("level") or "error").strip().lower() or "error"
    source = str(item.get("source") or "xray-error-log").strip() or "xray-error-log"
    component = str(item.get("component") or "").strip()
    message = str(item.get("message") or "").strip()
    raw_line = str(item.get("raw_line") or item.get("rawLine") or message)
    payload = "|".join([level, source, component, message])
    dedupe_key = str(item.get("dedupe_key") or hashlib.sha256(payload.encode("utf-8")).hexdigest())
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    count = _event_count(item)
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO xray_error_events(
                event_time, level, source, component, message, dedupe_key,
                event_count, first_seen_at, last_seen_at, raw_line, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
                event_count = xray_error_events.event_count + excluded.event_count,
                event_time = CASE
                    WHEN excluded.event_time > xray_error_events.event_time THEN excluded.event_time
                    ELSE xray_error_events.event_time
                END,
                last_seen_at = CASE
                    WHEN excluded.last_seen_at > xray_error_events.last_seen_at THEN excluded.last_seen_at
                    ELSE xray_error_events.last_seen_at
                END,
                level = excluded.level,
                source = excluded.source,
                component = excluded.component,
                message = excluded.message,
                raw_line = excluded.raw_line,
                extra_json = excluded.extra_json
            """,
            (
                event_time,
                level,
                source,
                component,
                message,
                dedupe_key,
                count,
                event_time,
                event_time,
                raw_line,
                encode_json(extra),
            ),
        )
        row = connection.execute(
            "SELECT id FROM xray_error_events WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
    return int(row["id"])


def list_xray_error_events(
    connection: sqlite3.Connection,
    *,
    level: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if level:
        levels = [item.strip().lower() for item in str(level).split(",") if item.strip()]
        if len(levels) == 1:
            clauses.append("LOWER(level) = ?")
            params.append(levels[0])
        elif levels:
            clauses.append(f"LOWER(level) IN ({','.join('?' for _item in levels)})")
            params.extend(levels)
    if start:
        clauses.append("event_time >= ?")
        params.append(start)
    if end:
        clauses.append("event_time < ?")
        params.append(end)
    params.append(max(1, int(limit or 100)))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM xray_error_events{where} ORDER BY event_time DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "time": row["event_time"],
            "level": row["level"],
            "source": row["source"],
            "component": row["component"] or "",
            "message": row["message"],
            "eventCount": int(row["event_count"] or 0),
            "firstSeen": row["first_seen_at"],
            "lastSeen": row["last_seen_at"],
            "notifiedAdminAt": row["notified_admin_at"] or "",
            "rawLine": row["raw_line"] or "",
            "extra": decode_json(row["extra_json"], {}),
        }
        for row in rows
    ]


def get_xray_error_event(connection: sqlite3.Connection, event_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM xray_error_events WHERE id = ?",
        (int(event_id),),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "time": row["event_time"],
        "level": row["level"],
        "source": row["source"],
        "component": row["component"] or "",
        "message": row["message"],
        "eventCount": int(row["event_count"] or 0),
        "firstSeen": row["first_seen_at"],
        "lastSeen": row["last_seen_at"],
        "notifiedAdminAt": row["notified_admin_at"] or "",
        "rawLine": row["raw_line"] or "",
        "extra": decode_json(row["extra_json"], {}),
    }


def delete_xray_error_events_before(connection: sqlite3.Connection, cutoff: str) -> int:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM xray_error_events WHERE event_time < ?", (cutoff,))
    return int(result.rowcount or 0)


def event_risks(connection: sqlite3.Connection, event_id: int) -> list[str]:
    rows = connection.execute(
        "SELECT risk FROM activity_event_risks WHERE event_id = ? ORDER BY risk",
        (event_id,),
    ).fetchall()
    return [row["risk"] for row in rows]


def iter_events(
    connection: sqlite3.Connection,
    *,
    client_name: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> Iterable[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if client_name:
        clauses.append("client_name = ?")
        params.append(client_name)
    if start:
        clauses.append("event_time >= ?")
        params.append(start)
    if end:
        clauses.append("event_time < ?")
        params.append(end)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM activity_events{where} ORDER BY event_time, id",
        params,
    ).fetchall()
    for row in rows:
        yield _event_from_row(connection, row)


def max_event_id(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(id), 0) AS value FROM activity_events").fetchone()
    return int(row["value"] or 0)


def first_event_time(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT MIN(event_time) AS value FROM activity_events WHERE event_time != ''"
    ).fetchone()
    value = row["value"] if row else None
    return str(value) if value else None


def iter_geoip_events_after(
    connection: sqlite3.Connection,
    *,
    after_id: int = 0,
    after_time: str | None = None,
    limit: int = 1000,
) -> Iterable[dict[str, Any]]:
    clauses = [
        """
        (
            outbound LIKE ?
            OR EXISTS (
                SELECT 1
                FROM activity_event_risks
                WHERE activity_event_risks.event_id = activity_events.id
                  AND activity_event_risks.risk LIKE ?
            )
        )
        """
    ]
    params: list[Any] = ["geoip-warning-%", "xray-geoip:%"]
    if after_id > 0:
        clauses.append("id > ?")
        params.append(after_id)
    elif after_time:
        clauses.append("event_time > ?")
        params.append(after_time)
    params.append(max(1, int(limit or 1000)))
    rows = connection.execute(
        f"""
        SELECT *
        FROM activity_events
        WHERE {" AND ".join(clauses)}
        ORDER BY id
        LIMIT ?
        """,
        params,
    ).fetchall()
    for row in rows:
        yield _event_from_row(connection, row)


def list_event_clients(
    connection: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[str]:
    clauses = ["client_name != ''"]
    params: list[Any] = []
    if start:
        clauses.append("event_time >= ?")
        params.append(start)
    if end:
        clauses.append("event_time < ?")
        params.append(end)
    rows = connection.execute(
        f"""
        SELECT DISTINCT client_name
        FROM activity_events
        WHERE {" AND ".join(clauses)}
        ORDER BY client_name
        """,
        params,
    ).fetchall()
    return [row["client_name"] for row in rows]


def delete_events_before(connection: sqlite3.Connection, cutoff: str) -> int:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM activity_events WHERE event_time < ?", (cutoff,))
    return int(result.rowcount or 0)


def upsert_access_log_state(connection: sqlite3.Connection, state: dict[str, Any] | None) -> None:
    if not isinstance(state, dict):
        return
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO file_offsets(name, path, inode, offset, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                path = excluded.path,
                inode = excluded.inode,
                offset = excluded.offset,
                updated_at = excluded.updated_at
            """,
            (
                ACTIVITY_ACCESS_LOG_OFFSET,
                state.get("path") or "",
                state.get("inode"),
                int(state.get("offset") or 0),
                state.get("updated") or state.get("updated_at") or "",
            ),
        )


def get_access_log_state(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        "SELECT path, inode, offset, updated_at FROM file_offsets WHERE name = ?",
        (ACTIVITY_ACCESS_LOG_OFFSET,),
    ).fetchone()
    if row is None:
        return {}
    state: dict[str, Any] = {
        "path": row["path"] or "",
        "offset": int(row["offset"] or 0),
    }
    if row["inode"] is not None:
        state["inode"] = int(row["inode"])
    if row["updated_at"]:
        state["updated"] = row["updated_at"]
    return state


def set_source_metadata(connection: sqlite3.Connection, value: dict[str, Any]) -> None:
    set_metadata_json(connection, ACTIVITY_SOURCE_METADATA_KEY, value)


def get_source_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    return get_metadata_json(connection, ACTIVITY_SOURCE_METADATA_KEY)


def set_summary(connection: sqlite3.Connection, value: dict[str, Any]) -> None:
    set_metadata_json(connection, ACTIVITY_SUMMARY_KEY, value)


def get_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    return get_metadata_json(connection, ACTIVITY_SUMMARY_KEY)


def set_metadata_json(connection: sqlite3.Connection, key: str, value: dict[str, Any]) -> None:
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO manager_metadata(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (key, encode_json(value)),
        )


def get_metadata_json(connection: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = connection.execute("SELECT value FROM manager_metadata WHERE key = ?", (key,)).fetchone()
    decoded = decode_json(row["value"] if row else "", {})
    return decoded if isinstance(decoded, dict) else {}


def upsert_exception(connection: sqlite3.Connection, item: dict[str, Any]) -> None:
    with database.transaction(connection):
        connection.execute(
            """
            INSERT INTO activity_exceptions(value, kind, source, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(value) DO UPDATE SET
                kind = excluded.kind,
                source = excluded.source,
                created_at = excluded.created_at
            """,
            (
                item.get("value") or "",
                item.get("kind") or "domain",
                item.get("source") or "manual",
                item.get("createdAt") or item.get("created_at") or "",
            ),
        )


def list_exceptions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT value, kind, source, created_at FROM activity_exceptions ORDER BY value"
    ).fetchall()
    return [
        {
            "value": row["value"],
            "kind": row["kind"],
            "source": row["source"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def delete_exception(connection: sqlite3.Connection, value: str) -> bool:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM activity_exceptions WHERE value = ?", (value,))
    return result.rowcount > 0


def clear_exceptions(connection: sqlite3.Connection) -> int:
    with database.transaction(connection):
        result = connection.execute("DELETE FROM activity_exceptions")
    return int(result.rowcount or 0)
