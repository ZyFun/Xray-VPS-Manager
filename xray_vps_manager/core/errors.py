"""Manager error hierarchy (target architecture, section 3.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence


class ManagerError(Exception):
    exit_code: int = 1

    def __init__(self, message: str, *, hint: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail


class ValidationError(ManagerError, ValueError):
    """Invalid user input; also a ValueError so existing `except ValueError` keeps working."""


class NotFoundError(ManagerError):
    """Client, connection, backup or route not found."""


class ConflictError(ManagerError):
    """Name taken, port busy, connection used by clients."""


class StateError(ManagerError):
    """Missing manager.db/config.json, not root, schema newer than code."""


class LockTimeout(ManagerError):
    """Another manager operation holds the lock longer than the timeout."""


class ExternalCommandError(ManagerError):
    """systemctl/xray/curl/caddy/sshd failed."""

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str] = (),
        returncode: Optional[int] = None,
        output: str = "",
        hint: str = "",
        detail: str = "",
    ) -> None:
        super().__init__(message, hint=hint, detail=detail)
        self.command = list(command)
        self.returncode = returncode
        self.output = output


class ApplyConfigError(ManagerError):
    """config.json could not be applied (section 3.2)."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        backup: Optional[Path] = None,
        restored: bool = False,
        hint: str = "",
        detail: str = "",
    ) -> None:
        super().__init__(message, hint=hint, detail=detail)
        self.stage = stage
        self.backup = backup
        self.restored = restored


class ConfigInvariantError(ApplyConfigError):
    """Candidate config.json violates manager invariants."""


class RestoreError(ManagerError):
    """Backup restore failed."""


class Cancelled(ManagerError):
    """Cancelled by the user."""

    exit_code = 130
