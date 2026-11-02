"""Единые операции systemd и совместимые контракты остановки писателей."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Callable

from xray_vps_manager.core.errors import ExternalCommandError
from xray_vps_manager.core import clock, process

DEFAULT_VERIFY_TIMEOUT = 3.0
DEFAULT_POLL_INTERVAL = 0.5
SHOW_TIMEOUT_FLOOR = 2.0
UNIT_FAILED_SUBSTATES = ("failed", "auto-restart", "stop-sigterm", "stop-sigkill", "final-sigterm", "final-sigkill")

# Manager-units, которые пишут manager.db/config.json: останавливаются на время cleanup и restore (XR-REV-012).
MANAGER_UNITS = (
    "xray-traffic-sync.timer",
    "xray-raw-log-rotate.timer",
    "xray-client-expire.timer",
    "xray-traffic-sync.service",
    "xray-raw-log-rotate.service",
    "xray-client-expire.service",
    "xray-telegram-poller.service",
)
WRITER_UNITS = MANAGER_UNITS
WRITER_STOP_UNITS = MANAGER_UNITS  # Совместимость restore/cleanup: stop всех писателей.
# Минутные oneshot service не запускаем повторно после restore/cleanup.
WRITER_RESTART_UNITS = tuple(unit for unit in MANAGER_UNITS
                             if unit.endswith('.timer') or unit == 'xray-telegram-poller.service')

Runner = Callable[..., subprocess.CompletedProcess]


def calendar_timezone_suffix(env: dict[str, str], on_warning: Callable[[str], None] | None = None) -> str:
    """Суффикс OnCalendar для зоны менеджера. Пустой суффикс означает время сервера."""
    zone = clock.manager_timezone(env)
    if not zone.valid:
        if on_warning is not None:
            on_warning(zone.label)
        return ""
    return f" {zone.name}" if zone.name else ""


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


def _run_systemctl_units(args: list[str], *, runner: Runner | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = (runner or process._invoke)(
        ["systemctl", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and int(result.returncode or 0) != 0:
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        raise RuntimeError(output or f"systemctl {' '.join(args)} failed")
    return result


def active_units(units: Iterable[str], *, runner: Runner | None = None) -> tuple[str, ...]:
    active = []
    for unit in units:
        result = _run_systemctl_units(["is-active", "--quiet", unit], runner=runner, check=False)
        if int(result.returncode or 0) == 0:
            active.append(unit)
    return tuple(active)


def stop_writer_units(*, runner: Runner | None = None) -> tuple[str, ...]:
    _run_systemctl_units(["stop", *WRITER_STOP_UNITS], runner=runner)
    return WRITER_STOP_UNITS


def restart_units(units: Iterable[str], *, runner: Runner | None = None) -> tuple[str, ...]:
    selected = tuple(units)
    if selected:
        _run_systemctl_units(["start", *selected], runner=runner)
    return selected


def stop_systemd_unit(unit: str, timeout: int = 30) -> None:
    """Дожидается остановки unit и его ExecStop; ошибка запрещает замену рабочих файлов."""
    result = _run_systemctl(unit, ["stop", unit], timeout=timeout)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)


def restart_unit(
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
    между reset-failed и новым запуском. Выгруженный после stop сервис допускается только после
    подтверждения его загрузки, покоя и нулевого счётчика через show (VIREI-251).
    При отключённой проверке сохраняется обычный restart.
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
    stop_systemd_unit(unit, timeout=timeout)
    # Текст ошибки — только предварительный фильтр; безопасность подтверждает show ниже.
    result = _run_systemctl(unit, ["reset-failed", unit], timeout=10, env={**os.environ, "LC_ALL": "C"})
    if result.returncode != 0:
        if _unloaded_service_is_ready(unit, result):
            return
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)


def _unloaded_service_is_ready(unit: str, result: subprocess.CompletedProcess[str]) -> bool:
    """VIREI-251: отсутствие загруженного unit не равно отсутствию его файла.

    reset-failed не загружает unit; show загружает существующее определение. После выгрузки
    счётчик уже нулевой. Нельзя принимать за baseline первое значение после нового старта:
    оно может включать раннее падение. Неполный ответ, старый процесс или новое задание
    не подтверждают безопасное состояние; ошибка содержит отвергнутые поля (251-R01).
    """
    name = unit if unit.endswith(".service") else unit + ".service"
    expected = f"Failed to reset failed state of unit {name}: Unit {name} not loaded."
    if result.returncode != 1 or (result.stderr or "").strip() != expected:
        return False
    state = _systemd_unit_properties(
        unit, "LoadState,ActiveState,SubState,NRestarts,MainPID,ControlPID,Job", timeout=10, show_empty=True,
    )
    # systemctl печатает отсутствие Job как пустое значение, а не 0; --all сохраняет поле.
    expected_state = {
        "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
        "NRestarts": "0", "MainPID": "0", "ControlPID": "0", "Job": "",
    }
    if not all(state.get(key) == value for key, value in expected_state.items()):
        summary = " ".join(f"{key}={state.get(key, '<missing>')!r}" for key in expected_state)
        raise SystemdUnitStartError(
            unit, f"reset-failed: {(result.stderr or '').strip()}; state after stop not confirmed: {summary}",
            result.stdout or "",
        )
    return True


def _run_systemctl(unit: str, args: list[str], timeout: float, *, env=None) -> subprocess.CompletedProcess[str]:
    """`systemctl ARGS`; таймаут превращается в SystemdUnitStartError, чтобы вызывающий код откатил конфиг (70-R02)."""
    try:
        kwargs = {"env": env} if env is not None else {}
        return process.run_capture(["systemctl", *args], timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise SystemdUnitStartError(unit, f"systemctl {' '.join(args)} timed out after {timeout:g}s") from exc


def systemd_unit_state(unit: str, timeout: float = 10) -> dict[str, str]:
    """ActiveState, SubState и NRestarts unit через `systemctl show`."""
    return _systemd_unit_properties(unit, "ActiveState,SubState,NRestarts", timeout)


def _systemd_unit_properties(unit: str, properties: str, timeout: float, *, show_empty: bool = False) -> dict[str, str]:
    args = ["show", f"--property={properties}", *(["--all"] if show_empty else []), unit]
    result = _run_systemctl(unit, args, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        raise SystemdUnitStartError(unit, f"systemctl show failed: {detail}", result.stdout or "")
    state: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            state[key.strip()] = value.strip()
    return state


def systemd_exec_commands(unit: str, directive: str = "ExecStart", timeout: float = 10) -> list[tuple[str, bool]]:
    """Команды директивы, которые systemd реально выполнит (с учётом drop-in): (argv, ignore_errors для префикса `-`)."""
    command = ["systemctl", "show", f"--property={directive}", unit]
    result = process.run_capture(command, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        raise ExternalCommandError(
            f"systemctl show {unit} failed: {detail}",
            command=command,
            returncode=result.returncode,
            output=result.stdout or "",
        )
    commands: list[tuple[str, bool]] = []
    for line in (result.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        value = value.strip()
        if key.strip() != directive or not sep or not value.startswith("{"):
            continue
        fields: dict[str, str] = {}
        for item in value.strip("{} ").split(" ; "):
            name, item_sep, item_value = item.partition("=")
            if item_sep:
                fields[name.strip()] = item_value.strip()
        if "argv[]" in fields:
            commands.append((fields["argv[]"], fields.get("ignore_errors") == "yes"))
    return commands


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
    closed = [(host, port) for host, port in endpoints if not process.tcp_endpoint_open(host, port)]
    if closed:
        ports_text = ", ".join(f"{host}:{port}" for host, port in closed)
        raise SystemdUnitStartError(unit, f"ports not open after {verify_timeout:g}s: {ports_text} ({summary})")
    return state


def _int_or_zero(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


# Старое имя остаётся алиасом той же реализации.
restart_systemd_unit = restart_unit


def _run_checked_systemctl_units(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Новый API: отказ systemctl становится ExternalCommandError."""
    return process.run(['systemctl', *args], check=True)


@contextmanager
def writers_paused(units=WRITER_UNITS):
    """Остановить всех писателей и восстановить ранее активные units в finally.

    Выполняющийся Type=oneshot без RemainAfterExit находится в activating,
    поэтому не попадает в is-active. Stop всех переданных units также закрывает
    запуск service таймером между проверкой состояния и началом остановки.

    Legacy restore/cleanup продолжают вызывать stop_writer_units/restart_units:
    они останавливают всех писателей и не перезапускают минутные service.
    """
    units = tuple(units)
    selected = active_units(units)
    try:
        if units:
            _run_checked_systemctl_units(['stop', *units])
        yield selected
    finally:
        if selected:
            _run_checked_systemctl_units(['start', *selected])


def daemon_reload() -> None:
    process.run(['systemctl', 'daemon-reload'], check=True)


def try_restart(unit: str) -> None:
    """systemd try-restart: не запускает неактивный unit, ошибки передаёт вызывающему."""
    process.run(['systemctl', 'try-restart', unit], check=True)
