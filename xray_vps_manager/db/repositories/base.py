"""Shared helpers for SQLite repository modules."""

from __future__ import annotations

import json
from typing import Any, Iterable


def encode_json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def decode_json(value: str | None, default: Any = None) -> Any:
    fallback = {} if default is None else default
    if not value:
        return fallback
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return fallback
    return decoded


def row_dict(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def without_keys(source: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    excluded = set(keys)
    return {key: value for key, value in source.items() if key not in excluded}
