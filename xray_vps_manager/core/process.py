"""Subprocess helpers."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence


def run(command: Sequence[str], timeout: int = 30, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        **kwargs,
    )


def run_capture(
    command: Sequence[str],
    timeout: int = 30,
    input_text: str | None = None,
    **kwargs,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        **kwargs,
    )


def restart_systemd_unit(unit: str, timeout: int = 30, retry_delay: float = 1.0) -> subprocess.CompletedProcess[str]:
    result = run_capture(["systemctl", "restart", unit], timeout=timeout)
    if result.returncode == 0:
        return result

    run_capture(["systemctl", "reset-failed", unit], timeout=10)
    if retry_delay > 0:
        time.sleep(retry_delay)

    retry = run_capture(["systemctl", "restart", unit], timeout=timeout)
    if retry.returncode == 0:
        return retry

    stdout = "\n".join(part for part in (result.stdout, retry.stdout) if part).strip()
    stderr = "\n".join(part for part in (result.stderr, retry.stderr) if part).strip()
    raise subprocess.CalledProcessError(retry.returncode, retry.args, stdout, stderr)
