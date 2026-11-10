"""Общее представление результатов применения конфигурации в адаптерах."""

from contextlib import contextmanager

from xray_vps_manager.core.errors import ApplyConfigError, ManagerError


def apply_error_text(error: ApplyConfigError) -> str:
    text = f"New config failed: {error.detail or error.message}."
    if error.restored:
        text += f" Restored backup: {error.backup}"
    elif error.backup:
        text += f" Config backup: {error.backup}"
    return text


def backup_text(backup) -> str:
    return str(backup) if backup is not None else "not created; config is unchanged."


def exception_text(error: BaseException) -> str:
    message = str(error) or type(error).__name__
    return "\n".join(part for part in (message, getattr(error, "detail", "")) if part)


@contextmanager
def menu_error_boundary():
    """Передать ошибку сценария и отмену штатному обработчику действия меню."""
    try:
        yield
    except ManagerError as error:
        if error.exit_code == 130:
            raise KeyboardInterrupt from error
        text = apply_error_text(error) if isinstance(error, ApplyConfigError) else exception_text(error)
        if error.hint:
            text += "\n" + error.hint
        raise SystemExit(text) from error
