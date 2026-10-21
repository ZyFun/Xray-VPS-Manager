"""Manager update scenarios (target architecture, section 5: ``system/manager_update.py``).

For now only the wrappers' interpreter of migration step 0a lives here: which Python the commands in
/usr/local/sbin start with and MANAGER_PYTHON in server.env. Downloading releases, backups and rollback
stay in ``commands/manager_update.py`` until migration step 8 moves them. The scenario does not print:
the command shows WrappersResult.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from xray_vps_manager.core import interpreter as manager_interpreter
from xray_vps_manager.core.errors import LockTimeout
from xray_vps_manager.core.fs import remove_path, replace_file, set_root_executable
from xray_vps_manager.core.server_env import read_server_env, update_server_env

MANAGER_PYTHON_LOCK_TIMEOUT = 60.0


@dataclass(frozen=True)
class WrappersResult:
    # None: no usable Python >= 3.12, the wrappers keep `#!/usr/bin/env python3`.
    interpreter: Optional[manager_interpreter.Interpreter]
    # MANAGER_PYTHON read from server.env ('' when absent or unreadable).
    configured: str = ""
    # (path, reason) of the rejected candidates, in selection order.
    skipped: Tuple[Tuple[str, str], ...] = ()
    # server.env could not be read, MANAGER_PYTHON was not taken into account.
    server_env_error: str = ""
    # MANAGER_PYTHON was written to server.env.
    recorded: bool = False
    # MANAGER_PYTHON was not written: manager.lock busy or an I/O error.
    record_error: str = ""


def write_wrappers(
    source_root: Path,
    target_dir: Path,
    names: Sequence[str],
    *,
    server_env_path: Path,
    remove_missing: bool,
    lock_timeout: float = MANAGER_PYTHON_LOCK_TIMEOUT,
) -> WrappersResult:
    """Write the wrappers ``names`` from ``source_root`` to ``target_dir`` with the shebang of the selected Python.

    The interpreter comes from ``core.interpreter.select_interpreter`` with MANAGER_PYTHON from
    ``server_env_path``; update and rollback use the same choice (user decision 2026-10-21), so a backup
    with `#!/usr/bin/env python3` or with a removed interpreter does not bring them back. Each wrapper is
    replaced with one rename, mode 0755, owner root; ``remove_missing`` (rollback) removes the wrappers
    absent from ``source_root``. Then MANAGER_PYTHON follows the absolute shebang: written under
    manager_lock only when it changed, a missing server.env is not created. An error writing a wrapper
    propagates; server.env problems are reported in the result and do not stop the update.
    """
    configured, server_env_error = configured_python(server_env_path)
    selection = manager_interpreter.select_interpreter(configured)
    interpreter = selection.interpreter
    shebang = manager_interpreter.shebang_line(interpreter.path if interpreter else "")
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = source_root / name
        target = target_dir / name
        if remove_missing and not source.exists():
            remove_path(target)
            continue
        content = manager_interpreter.with_shebang(source.read_bytes(), shebang)
        replace_file(target, content, prepare=set_root_executable)
    recorded, record_error = record_python(server_env_path, interpreter, lock_timeout)
    return WrappersResult(interpreter, configured, selection.skipped, server_env_error, recorded, record_error)


def configured_python(server_env_path: Path) -> Tuple[str, str]:
    """(MANAGER_PYTHON, "") from server.env, or ("", error) when the file cannot be read."""
    # Автооткат неудачного обновления тоже выбирает интерпретатор: нечитаемый server.env его не прерывает.
    try:
        values = read_server_env(server_env_path)
    except (OSError, UnicodeError) as exc:
        return "", str(exc)
    return values.get(manager_interpreter.MANAGER_PYTHON_KEY, "").strip(), ""


def record_python(
    server_env_path: Path,
    interpreter: Optional[manager_interpreter.Interpreter],
    lock_timeout: float = MANAGER_PYTHON_LOCK_TIMEOUT,
) -> Tuple[bool, str]:
    """(written, error) of recording ``interpreter`` as MANAGER_PYTHON; nothing is written without an interpreter."""
    if interpreter is None or not server_env_path.exists():
        return False, ""
    try:
        if read_server_env(server_env_path).get(manager_interpreter.MANAGER_PYTHON_KEY) == interpreter.path:
            return False, ""
        # Под manager_lock (81-R02); занятая блокировка не прерывает обновление: ключ допишет следующее.
        update_server_env(
            {manager_interpreter.MANAGER_PYTHON_KEY: interpreter.path},
            path=server_env_path,
            timeout=lock_timeout,
        )
    except (OSError, UnicodeError, LockTimeout) as exc:
        return False, str(exc)
    return True, ""
