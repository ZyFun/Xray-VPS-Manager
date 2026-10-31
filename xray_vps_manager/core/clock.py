"""Канон времени и единая политика локальной зоны менеджера."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os
import struct
from datetime import date, datetime, time, timezone, tzinfo
from typing import Iterator, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.core.paths import paths
from xray_vps_manager.core.settings import read_env

_LOCAL_TIMEZONE_NAMES = ('', 'server', 'local', 'default', 'system', 'сервер', 'локально', 'по умолчанию')

_moment: ContextVar[datetime | None] = ContextVar('clock_moment', default=None)
_local_zone: ContextVar[tzinfo | None] = ContextVar('clock_local_zone', default=None)


def now() -> datetime:
    moment = _moment.get()
    return moment if moment is not None else datetime.now(timezone.utc)


@contextmanager
def frozen(moment: datetime, *, local_timezone: tzinfo | None = None) -> Iterator[None]:
    """Обратимая подмена часов; local_timezone — серверная зона Sandbox."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError('frozen requires an aware datetime')
    token = _moment.set(moment.astimezone(timezone.utc))
    local_token = _local_zone.set(local_timezone) if local_timezone is not None else None
    try:
        yield
    finally:
        if local_token is not None:
            _local_zone.reset(local_token)
        _moment.reset(token)


def utc_stamp() -> str:
    return now().strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time(value: str | None, *, strip_whitespace: bool = True, normalize_utc: bool = True) -> datetime | None:
    """ISO-время; параметры сохраняют отдельный контракт clients.runtime."""
    if not value:
        return None
    raw = str(value)
    if strip_whitespace:
        raw = raw.strip()
    if raw.endswith('Z'):
        raw = raw[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) if normalize_utc else parsed


def timezone_named(name: str) -> tzinfo:
    """Единственная точка создания IANA-зоны; ошибки остаются у адаптеров."""
    return ZoneInfo(name)


def normalize_timezone_name(value: str | None) -> str:
    raw = (value or '').strip()
    if raw.lower() in _LOCAL_TIMEZONE_NAMES:
        return ''
    try:
        timezone_named(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        # Строгая проверка нового значения для set-timezone.
        raise ValueError('MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time.') from exc
    return raw


@lru_cache(maxsize=8)
def _timezone_from_file(path: Path, identity: tuple[int, ...]) -> tzinfo:
    """Кэш TZif ограничен; метаданные в ключе обновляют зону при смене файла."""
    with path.open('rb') as source:
        return ZoneInfo.from_file(source)


def server_local_timezone() -> tzinfo:
    """Зона сервера с DST: инъекция теста, localtime, TZ, текущее смещение."""
    zone = _local_zone.get()
    if zone is not None:
        return zone
    try:
        path = paths().localtime
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        return _timezone_from_file(path, identity)
    except (OSError, ValueError, EOFError, struct.error):
        pass
    name = os.environ.get('TZ', '').strip().removeprefix(':')
    if name:
        try:
            return timezone_named(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return now().astimezone().tzinfo or timezone.utc


@dataclass(frozen=True)
class ManagerTimezone:
    tz: tzinfo
    name: str
    label: str
    valid: bool


def manager_timezone(env: Mapping[str, str] | None = None) -> ManagerTimezone:
    """Неверная настройка сохраняет имя и предупреждение, используя зону сервера."""
    values = read_env() if env is None else env
    name = (values.get('MANAGER_TIMEZONE') or '').strip()
    if name.lower() in _LOCAL_TIMEZONE_NAMES:
        name = ''
    if name:
        try:
            return ManagerTimezone(timezone_named(name), name, name, True)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return ManagerTimezone(
                server_local_timezone(), name,
                f'MANAGER_TIMEZONE invalid: {name} (server local time)', False,
            )
    return ManagerTimezone(server_local_timezone(), '', 'server local time', True)


def today(tz: tzinfo) -> date:
    return now().astimezone(tz).date()


def local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def xray_access_time_to_iso(value: str, source_timezone: tzinfo | None = None) -> str:
    parsed = datetime.strptime(value, '%Y/%m/%d %H:%M:%S')
    local = parsed.replace(tzinfo=source_timezone or server_local_timezone())
    return local.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
