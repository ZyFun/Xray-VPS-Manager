"""Subprocess helpers."""

from __future__ import annotations

import subprocess
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
