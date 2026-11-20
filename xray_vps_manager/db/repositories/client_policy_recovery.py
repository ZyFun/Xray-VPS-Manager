"""Ожидающее подтверждения восстановление доступа после сброса лимита."""

from xray_vps_manager.core.errors import StateError
from xray_vps_manager.db.repositories import settings
from xray_vps_manager.db.repositories.base import decode_json, encode_json


_KEY = "clients.pendingLimitReactivation"
type PendingReactivations = dict[str, dict[str, str]]


def load(conn) -> PendingReactivations:
    raw = settings.get_metadata(conn, _KEY)
    pending = decode_json(raw, default=False) if raw else {}
    if not isinstance(pending, dict) or any(
        not isinstance(items, dict) or any(not isinstance(value, str) for value in items.values())
        for items in pending.values()
    ):
        raise StateError("Не удалось прочитать записи восстановления доступа после сброса лимита")
    return pending


def save(conn, pending: PendingReactivations) -> None:
    if pending:
        settings.set_metadata(conn, _KEY, encode_json(pending))
    else:
        settings.delete_metadata(conn, _KEY)
