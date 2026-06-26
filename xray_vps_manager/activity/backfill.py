"""Manual detailed activity backfill from raw Xray access logs."""

from __future__ import annotations

import gzip
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from xray_vps_manager.activity import settings
from xray_vps_manager.activity import time as activity_time
from xray_vps_manager.activity import bypass as activity_bypass
from xray_vps_manager.activity import parser
from xray_vps_manager.activity.constants import ACCESS_LOG_PATH
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import activity as sqlite_activity
from xray_vps_manager.db.repositories import clients as sqlite_clients
from xray_vps_manager.db.storage import sqlite_read_ready


def access_log_files(path: Path = ACCESS_LOG_PATH) -> list[Path]:
    files = []
    if path.exists():
        files.append(path)
    if path.parent.exists():
        files.extend(sorted(item for item in path.parent.glob(f"{path.name}.*") if item.is_file()))
    return files


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def _event_in_range(event: dict[str, Any], start: date, end: date) -> bool:
    event_day = str(event.get("time") or "")[:10]
    try:
        parsed = date.fromisoformat(event_day)
    except ValueError:
        return False
    return start <= parsed <= end


def _retention_start(today: date | None = None) -> date:
    from datetime import timedelta

    current = today or activity_time.today_utc_date()
    return current - timedelta(days=settings.retention_days() - 1)


def _event_before_retention(event: dict[str, Any], retention_start: date) -> bool:
    event_day = str(event.get("time") or "")[:10]
    try:
        parsed = date.fromisoformat(event_day)
    except ValueError:
        return True
    return parsed < retention_start


def iter_backfill_events(
    files: Iterable[Path],
    clients: dict,
    *,
    client_name: str,
    start: date,
    end: date,
    scan_stats: list[dict[str, Any]] | None = None,
):
    for path in files:
        file_stats: dict[str, Any] = {
            "file": str(path),
            "rawLines": 0,
            "parsedEvents": 0,
            "matchedEvents": 0,
        }
        if scan_stats is not None:
            scan_stats.append(file_stats)
        try:
            with _open_text(path) as handle:
                for line in handle:
                    file_stats["rawLines"] += 1
                    event = parser.parse_access_line(line.rstrip("\n"), clients)
                    if not event:
                        continue
                    file_stats["parsedEvents"] += 1
                    if client_name != "all" and event.get("client") != client_name:
                        continue
                    if not _event_in_range(event, start, end):
                        continue
                    file_stats["matchedEvents"] += 1
                    yield path, event
        except OSError as exc:
            file_stats["error"] = str(exc)
            continue


def run_backfill(
    clients: dict,
    *,
    client_name: str,
    start: date,
    end: date,
    apply: bool = False,
) -> dict[str, Any]:
    files = access_log_files()
    stats: dict[str, Any] = {
        "target": client_name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "files": [str(path) for path in files],
        "rawLines": 0,
        "parsedEvents": 0,
        "matched": 0,
        "inserted": 0,
        "duplicates": 0,
        "unknownClients": 0,
        "retentionSkipped": 0,
        "risks": {},
        "clients": {},
        "fileStats": [],
    }
    if not files:
        return stats
    if not database.database_file_exists():
        raise RuntimeError("SQLite manager database is missing")

    connection = database.open_database()
    try:
        if not sqlite_read_ready(connection):
            raise RuntimeError("SQLite database is not marked ready")
        known_client_names = set(sqlite_clients.list_clients(connection))
        retention_start = _retention_start()
        file_stats: list[dict[str, Any]] = []
        config = activity_bypass.load_config()
        with database.transaction(connection):
            for _path, event in iter_backfill_events(
                files,
                clients,
                client_name=client_name,
                start=start,
                end=end,
                scan_stats=file_stats,
            ):
                activity_bypass.append_bypass_risk(event, config=config)
                stats["matched"] += 1
                stats["clients"][event.get("client") or ""] = stats["clients"].get(event.get("client") or "", 0) + 1
                for risk in event.get("risks", []):
                    stats["risks"][risk] = stats["risks"].get(risk, 0) + 1
                if _event_before_retention(event, retention_start):
                    stats["retentionSkipped"] += 1
                    continue
                if event.get("client") not in known_client_names:
                    stats["unknownClients"] += 1
                    continue
                if sqlite_activity.event_exists(connection, event):
                    stats["duplicates"] += 1
                    continue
                if apply:
                    sqlite_activity.add_event(connection, event)
                    stats["inserted"] += 1
        stats["fileStats"] = file_stats
        stats["rawLines"] = sum(int(item.get("rawLines") or 0) for item in file_stats)
        stats["parsedEvents"] = sum(int(item.get("parsedEvents") or 0) for item in file_stats)
    finally:
        connection.close()
    return stats
