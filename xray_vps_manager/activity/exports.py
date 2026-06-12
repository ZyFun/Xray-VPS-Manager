"""Activity export archive helpers."""

from __future__ import annotations

import json
import os
import re
import shlex
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from xray_vps_manager.activity import repository
from xray_vps_manager.activity.constants import EXPORT_DIR
from xray_vps_manager.activity.time import utc_stamp


def create_client_export(name: str, start, end, events: list[dict], aggregate: dict) -> Path:
    repository.ensure_dirs()
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
    return archive


def resolve_export_archive(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.exists():
        path = EXPORT_DIR / value
    if not path.exists():
        raise FileNotFoundError(value)
    export_root = EXPORT_DIR.resolve()
    archive = path.resolve()
    if archive.parent != export_root:
        raise PermissionError(str(path))
    if not archive.name.endswith(".tar.gz"):
        raise ValueError("not-tar-gz")
    return archive


def export_archive_rows(format_size: Callable[[int], str]) -> list[dict]:
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


def delete_export(value: str) -> tuple[Path, int]:
    archive = resolve_export_archive(value)
    size = archive.stat().st_size
    archive.unlink()
    return archive, size


def export_archives() -> list[Path]:
    if not EXPORT_DIR.exists():
        return []
    archives = []
    for path in sorted(EXPORT_DIR.glob("*.tar.gz")):
        try:
            archive = path.resolve()
            if archive.parent == EXPORT_DIR.resolve() and archive.is_file():
                archives.append(archive)
        except OSError:
            continue
    return archives


def delete_all_exports() -> tuple[int, int, list[str]]:
    total_size = 0
    removed = 0
    warnings = []
    for archive in export_archives():
        try:
            size = archive.stat().st_size
            archive.unlink()
        except OSError as exc:
            warnings.append(f"WARN: failed to delete {archive}: {exc}")
            continue
        total_size += size
        removed += 1
    return removed, total_size, warnings


def default_ssh_target(server_addr: str) -> str:
    server_addr = (server_addr or "").strip()
    if server_addr:
        return server_addr if "@" in server_addr else f"root@{server_addr}"
    return "root@SERVER_HOST"


def quote_local_path(value: str) -> str:
    if value == "~":
        return "~"
    if value.startswith("~/"):
        rest = value[2:]
        return "~/" + (shlex.quote(rest) if rest else "")
    return shlex.quote(value)


def download_command(value: str, ssh_target: str, local_path: str) -> str:
    archive = resolve_export_archive(value)
    target = local_path.rstrip("/") + "/"
    return f"scp {shlex.quote(ssh_target + ':' + str(archive))} {quote_local_path(target)}"

