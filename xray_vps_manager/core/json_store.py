"""JSON file helpers with Xray-friendly ownership support."""

from __future__ import annotations

import json
import os
import pwd
import shutil
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def chown_xray(path: Path) -> None:
    try:
        user = pwd.getpwnam("xray")
    except KeyError:
        return
    try:
        os.chown(path, 0, user.pw_gid)
    except PermissionError:
        return


def save_json(path: Path, data: Any, mode: int = 0o640, group_xray: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, mode)
    if group_xray:
        chown_xray(tmp)
    shutil.move(str(tmp), str(path))
