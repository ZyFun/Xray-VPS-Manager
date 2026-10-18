"""Inter-process manager locks (target architecture, section 3.1)."""

from __future__ import annotations

import fcntl
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from xray_vps_manager.core import paths
from xray_vps_manager.core.errors import LockTimeout
from xray_vps_manager.core.time import utc_stamp

DEFAULT_TIMEOUT = 60.0
POLL_INTERVAL = 0.2
LOCK_FILE_MODE = 0o600
LOCK_TIMEOUT_MESSAGE = "Другая операция менеджера ещё выполняется"
LOCK_TIMEOUT_HINT = "Дождись её завершения и повтори команду."
MANAGER_UPDATE_LOCK_MESSAGE = "Другое обновление или откат менеджера уже выполняется"

_manager_lock_depth = 0


def lock_holder_line(purpose: str = "") -> str:
    command = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "python"
    cmd = " ".join(part for part in (command, purpose.strip()) if part)
    return f"pid={os.getpid()} cmd={cmd} since={utc_stamp()}"


def read_lock_holder(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 4096).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def write_lock_holder(fd: int, line: str) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, (line + "\n").encode("utf-8"))


def acquire_flock(fd: int, timeout: float) -> None:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                holder = read_lock_holder(fd)
                message = f"{LOCK_TIMEOUT_MESSAGE}: {holder}" if holder else LOCK_TIMEOUT_MESSAGE
                raise LockTimeout(message, hint=LOCK_TIMEOUT_HINT) from None
            time.sleep(min(POLL_INTERVAL, remaining))


@contextmanager
def hold_file_lock(path: Path, timeout: float, purpose: str = "", message: str = LOCK_TIMEOUT_MESSAGE) -> Iterator[None]:
    """Hold an exclusive flock on ``path`` and record the holder in it while the body runs."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, LOCK_FILE_MODE)
    try:
        try:
            acquire_flock(fd, timeout)
        except LockTimeout as exc:
            if message == LOCK_TIMEOUT_MESSAGE:
                raise
            text = exc.message.replace(LOCK_TIMEOUT_MESSAGE, message, 1)
            raise LockTimeout(text, hint=exc.hint) from None
        write_lock_holder(fd, lock_holder_line(purpose))
        try:
            yield
        finally:
            try:
                os.ftruncate(fd, 0)
            except OSError:
                pass
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def manager_lock(timeout: float = DEFAULT_TIMEOUT, *, purpose: str = "") -> Iterator[None]:
    """Hold manager.lock around a state-changing operation.

    Reentrant within the process: a nested call does not take flock again.
    Never use it in traffic sync: it runs from ExecStop of xray.service while
    the Xray restart itself happens under this lock.
    """
    global _manager_lock_depth
    if _manager_lock_depth:
        _manager_lock_depth += 1
        try:
            yield
        finally:
            _manager_lock_depth -= 1
        return

    with hold_file_lock(paths.MANAGER_LOCK_PATH, timeout, purpose):
        _manager_lock_depth = 1
        try:
            yield
        finally:
            _manager_lock_depth = 0


@contextmanager
def manager_update_lock(timeout: float = 0.0, *, purpose: str = "") -> Iterator[None]:
    """Serialize ``xray-manager-update --update/--rollback`` (manager-update.lock).

    Separate from manager.lock: an update holds it for minutes (xray-test), and
    timer writers taking manager.lock must not wait for it. By default a second
    update fails at once instead of waiting.
    """
    paths.MANAGER_UPDATE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with hold_file_lock(paths.MANAGER_UPDATE_LOCK_PATH, timeout, purpose, MANAGER_UPDATE_LOCK_MESSAGE):
        yield
