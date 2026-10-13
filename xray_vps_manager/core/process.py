"""Subprocess helpers."""

from __future__ import annotations

import socket
import subprocess
import time
from collections.abc import Iterable, Sequence

DEFAULT_VERIFY_TIMEOUT = 3.0
DEFAULT_POLL_INTERVAL = 0.5
SHOW_TIMEOUT_FLOOR = 2.0
UNIT_FAILED_SUBSTATES = ("failed", "auto-restart", "stop-sigterm", "stop-sigkill", "final-sigterm", "final-sigkill")


class SystemdUnitStartError(subprocess.CalledProcessError):
    """Unit восстановился после `systemctl restart`, но не остался активным или не открыл порты.

    Наследует CalledProcessError, чтобы существующие `except subprocess.CalledProcessError`
    вокруг применения config.json откатывали конфиг как при неудачном restart.
    """

    def __init__(self, unit: str, detail: str, output: str = "") -> None:
        super().__init__(1, ["systemctl", "restart", unit], output, detail)
        self.unit = unit
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.unit} did not start after restart: {self.detail}"


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


def restart_systemd_unit(
    unit: str,
    timeout: int = 30,
    retry_delay: float = 1.0,
    *,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
    ports: Iterable[tuple[str, int]] = (),
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> subprocess.CompletedProcess[str]:
    """Перезапускает unit и проверяет фактический старт (XR-REV-010).

    `systemctl restart` для Type=simple возвращает 0 до того, как сервис прочитает конфиг и займёт порты,
    поэтому после успешного restart unit опрашивается через `systemctl show` всё окно `verify_timeout` секунд:
    состояние failed/auto-restart или ненулевой NRestarts уже с первого опроса — ошибка сразу; к концу окна unit
    должен быть активен, а все переданные TCP-`ports` — принимать соединение. Окно не сокращается при открытых
    портах: порт может занимать другой процесс (например, Caddy), а Xray падает уже после первого опроса.
    `verify_timeout <= 0` отключает проверку. Ошибка проверки — SystemdUnitStartError
    (подкласс CalledProcessError), чтобы вызывающий код откатывал конфиг как при неудачном restart;
    зависание любого вызова `systemctl` внутри helper'а (TimeoutExpired) тоже становится этой ошибкой.
    Перед проверяемой попыткой unit останавливается, затем сбрасывается NRestarts: старый systemd может
    сохранять счётчик при ручном restart из auto-restart (70-R07). Stop исключает гонку со старым процессом
    между reset-failed и новым запуском. При отключённой проверке сохраняется обычный restart.
    """
    if verify_timeout > 0:
        _prepare_verified_restart(unit, timeout)
    result = _run_systemctl(unit, ["restart", unit], timeout=timeout)
    if result.returncode != 0:
        if verify_timeout > 0:
            _prepare_verified_restart(unit, timeout)
        else:
            _run_systemctl(unit, ["reset-failed", unit], timeout=10)
        if retry_delay > 0:
            time.sleep(retry_delay)

        retry = _run_systemctl(unit, ["restart", unit], timeout=timeout)
        if retry.returncode != 0:
            stdout = "\n".join(part for part in (result.stdout, retry.stdout) if part).strip()
            stderr = "\n".join(part for part in (result.stderr, retry.stderr) if part).strip()
            raise subprocess.CalledProcessError(retry.returncode, retry.args, stdout, stderr)
        result = retry

    if verify_timeout > 0:
        verify_systemd_unit_started(unit, verify_timeout=verify_timeout, ports=ports, poll_interval=poll_interval)
    return result


def _prepare_verified_restart(unit: str, timeout: int) -> None:
    """Останавливает unit и сбрасывает старый счётчик до нового запуска (70-R07)."""
    for action, action_timeout in (("stop", timeout), ("reset-failed", 10)):
        result = _run_systemctl(unit, [action, unit], timeout=action_timeout)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)


def _run_systemctl(unit: str, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """`systemctl ARGS`; таймаут превращается в SystemdUnitStartError, чтобы вызывающий код откатил конфиг (70-R02)."""
    try:
        return run_capture(["systemctl", *args], timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SystemdUnitStartError(unit, f"systemctl {' '.join(args)} timed out after {timeout:g}s") from exc


def systemd_unit_state(unit: str, timeout: float = 10) -> dict[str, str]:
    """ActiveState, SubState и NRestarts unit через `systemctl show`."""
    result = _run_systemctl(unit, ["show", "--property=ActiveState,SubState,NRestarts", unit], timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        raise SystemdUnitStartError(unit, f"systemctl show failed: {detail}", result.stdout or "")
    state: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            state[key.strip()] = value.strip()
    return state


def tcp_endpoint_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def verify_systemd_unit_started(
    unit: str,
    *,
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
    ports: Iterable[tuple[str, int]] = (),
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> dict[str, str]:
    """Опрашивает unit всё окно после restart; возвращает последнее состояние или поднимает SystemdUnitStartError.

    Вызывается после stop/reset-failed/restart с известным нулевым счётчиком: ненулевое значение уже
    в первом опросе означает новый автоперезапуск (70-R06/70-R07). Успех определяется только в конце окна,
    даже если порты открыты раньше (XR-REV-010, 70-R01).
    """
    endpoints = [(str(host), int(port)) for host, port in ports]
    deadline = time.monotonic() + verify_timeout
    state: dict[str, str] = {}
    while True:
        # `show` ограничен оставшимся окном, но не меньше порога, чтобы последний опрос не обрывался ложно.
        state = systemd_unit_state(unit, timeout=max(SHOW_TIMEOUT_FLOOR, deadline - time.monotonic()))
        active = state.get("ActiveState", "")
        sub = state.get("SubState", "")
        restarts = _int_or_zero(state.get("NRestarts"))
        summary = f"{unit} ActiveState={active or '?'} SubState={sub or '?'} NRestarts={restarts}"
        if active in ("failed", "inactive") or sub in UNIT_FAILED_SUBSTATES:
            raise SystemdUnitStartError(unit, summary)
        if restarts > 0:
            raise SystemdUnitStartError(unit, f"{summary} (unit was restarted automatically after start)")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining) if poll_interval > 0 else remaining)

    if state.get("ActiveState") != "active":
        raise SystemdUnitStartError(unit, f"{summary} after {verify_timeout:g}s")
    closed = [(host, port) for host, port in endpoints if not tcp_endpoint_open(host, port)]
    if closed:
        ports_text = ", ".join(f"{host}:{port}" for host, port in closed)
        raise SystemdUnitStartError(unit, f"ports not open after {verify_timeout:g}s: {ports_text} ({summary})")
    return state


def _int_or_zero(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0
