"""Правила сортировки обычных резервных копий менеджера."""

import re
from calendar import timegm
from os import stat_result
from pathlib import Path

from xray_vps_manager.core.clock import parse_time


_ARCHIVE_NAME = re.compile(r"xray-backup-([0-9]{8})-([0-9]{6})([0-9]{6})?Z\.tar\.gz")


def _archive_sort_time_ns(name: str, mtime_ns: int) -> int:
    match = _ARCHIVE_NAME.fullmatch(name)
    if match is None:
        return mtime_ns

    day, time, microseconds = match.groups()
    stamp = parse_time(f"{day}T{time}.{microseconds or '000000'}Z")
    if stamp is None:
        return mtime_ns
    return timegm(stamp.utctimetuple()) * 10**9 + stamp.microsecond * 1000


def list_archives(backup_dir: Path) -> list[tuple[Path, stat_result]]:
    archives = []
    # При совпадении времени сохраняем прежний порядок по убыванию имени.
    for path in sorted(backup_dir.glob("*.tar.gz"), reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        archives.append((path, stat))
    archives.sort(key=lambda entry: _archive_sort_time_ns(entry[0].name, entry[1].st_mtime_ns), reverse=True)
    return archives
