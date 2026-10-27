"""Канон времени. Переходные политики R1.2 сохраняют поведение до B-задач."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time, timezone, tzinfo
from typing import Iterator, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xray_vps_manager.core.settings import read_env

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
    if raw.lower() in ('', 'server', 'local', 'default', 'system', 'сервер', 'локально', 'по умолчанию'):
        return ''
    try:
        timezone_named(raw)
    except ZoneInfoNotFoundError as exc:
        # ValueError — текущий клиентский контракт, до поведенческой B-задачи.
        raise ValueError('MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time.') from exc
    return raw


def server_local_timezone() -> tzinfo:
    """Снимок текущего смещения, включая XR-REV-065; исправление DST — отдельно."""
    zone = _local_zone.get()
    local = now().astimezone(zone)
    if zone is not None:
        return timezone(local.utcoffset(), local.tzname())
    return local.tzinfo or timezone.utc


@dataclass(frozen=True)
class ManagerTimezone:
    tz: tzinfo
    name: str
    label: str
    valid: bool


def manager_timezone(env: Mapping[str, str] | None = None, *, invalid: str = 'local') -> ManagerTimezone:
    """До B1.1 адаптер явно выбирает прежнюю политику local/utc/raise."""
    if invalid not in ('local', 'utc', 'raise'):
        raise ValueError('invalid timezone policy')
    values = read_env() if env is None else env
    name = (values.get('MANAGER_TIMEZONE') or '').strip()
    if name:
        try:
            return ManagerTimezone(timezone_named(name), name, name, True)
        except ZoneInfoNotFoundError:
            if invalid == 'raise':
                raise
            if invalid == 'utc':
                return ManagerTimezone(timezone.utc, name, f'UTC (invalid MANAGER_TIMEZONE: {name})', False)
    return ManagerTimezone(server_local_timezone(), name, 'server local time', not bool(name))


def today(tz: tzinfo) -> date:
    return now().astimezone(tz).date()


def local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def xray_access_time_to_iso(value: str, source_timezone: tzinfo | None = None) -> str:
    parsed = datetime.strptime(value, '%Y/%m/%d %H:%M:%S')
    local = parsed.replace(tzinfo=source_timezone or server_local_timezone())
    return local.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
