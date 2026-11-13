"""Выполнение всех точечных компенсаций с сохранением причин ошибок."""

from xray_vps_manager.core.errors import ManagerError, StateError


def compensate(error: BaseException, actions) -> None:
    """Выполнить именованные действия возврата и поднять исходную/составную ошибку."""
    failures = []
    for name, action in actions:
        try:
            action()
        except BaseException as rollback_error:
            reason = str(rollback_error) or type(rollback_error).__name__
            failures.append(f"{name}: {reason}\n{getattr(rollback_error, 'detail', '')}".strip())
    if failures:
        original = str(error) or type(error).__name__
        raise StateError(
            "Операция и её компенсация завершились с ошибкой; состояние не подтверждено",
            detail=f"Операция: {original}\n{getattr(error, 'detail', '')}\n" + "\n".join(failures),
        ) from error
    if isinstance(error, ManagerError):
        raise error
    failure = StateError("Операция не завершена; затронутые изменения отменены", detail=str(error))
    if isinstance(error, KeyboardInterrupt):
        failure.exit_code = 130
    raise failure from error
