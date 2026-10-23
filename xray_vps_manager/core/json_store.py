"""JSON file helpers with Xray-friendly ownership support."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Переходные алиасы: реализация записи и владения существует только в fs.
from xray_vps_manager.core.fs import chown_xray, save_json


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default
