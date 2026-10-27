"""Запуск процессов, захват вывода и обратимая подмена для тестов."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import errno
import socket
import subprocess
import sys
from collections.abc import Sequence

from xray_vps_manager.core.errors import ExternalCommandError

_handler = ContextVar('process_handler', default=None)
WILDCARD_LISTEN_HOSTS = ('', '0.0.0.0', '::')


class UnexpectedProcessError(AssertionError):
    """Фейк не описывает команду; запуск настоящего процесса запрещён."""


class ProcessCommandError(ExternalCommandError, subprocess.CalledProcessError):
    """Ошибка нового run, совместимая с прежними обработчиками CalledProcessError."""
    def __init__(self, result):
        detail = (result.stderr or result.stdout or '').strip()
        # Не вызываем cooperative super: CalledProcessError требует другой init.
        self.message = detail or f'Command failed: {result.args}'
        Exception.__init__(self, self.message)
        self.command = list(result.args)
        self.returncode = result.returncode
        self.output = result.stdout
        self.detail = detail
        self.hint = ''
        self.cmd = result.args
        self.stderr = result.stderr


@contextmanager
def fake(handler):
    """Подменить процессы и вернуть журнал argv; вложенные контексты обратимы.

    Callable получает argv и возвращает CompletedProcess или None (неизвестная
    команда). Объект с run(argv, **kwargs) дополнительно получает параметры
    запуска — этот протокол использует FakeProcesses песочницы.
    """
    calls = []
    token = _handler.set((handler, calls))
    try:
        yield calls
    finally:
        _handler.reset(token)


def _invoke(command, **kwargs):
    binding = _handler.get()
    if binding is None:
        return subprocess.run(command, **kwargs)
    if isinstance(command, (str, bytes)) or kwargs.get('shell'):
        raise UnexpectedProcessError('Подмена процессов требует argv без shell')
    handler, calls = binding
    argv = list(command)
    calls.append(argv.copy())
    if hasattr(handler, 'run'):
        result = handler.run(argv, **kwargs)
    else:
        result = handler(argv)
        if result is not None:
            result = subprocess.CompletedProcess(argv, result.returncode, result.stdout, result.stderr)
            for name in ('stdout', 'stderr'):
                value = getattr(result, name)
                if kwargs.get(name) is None and not kwargs.get('capture_output'):
                    if value:
                        getattr(sys, name).write(value)
                    setattr(result, name, None)
    if result is None:
        raise UnexpectedProcessError('Не задана внешняя команда: '+repr(argv))
    if kwargs.get('check'):
        result.check_returncode()
    return result


def run(command: Sequence[str], timeout: float = 30, *, input_text=None,
        check: bool = False, **kwargs) -> subprocess.CompletedProcess[str]:
    """Текстовый stdout/stderr; прежний позиционный timeout остаётся рабочим."""
    if input_text is not None:
        kwargs['input'] = input_text
    result = _invoke(list(command), text=True, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, timeout=timeout, check=False, **kwargs)
    if check and result.returncode:
        raise ProcessCommandError(result)
    return result


def run_capture(command: Sequence[str], timeout: int = 30, input_text=None,
                **kwargs) -> subprocess.CompletedProcess[str]:
    """Совместимый адаптер: input=None и CalledProcessError как до переноса."""
    return _invoke(list(command), input=input_text, text=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, timeout=timeout, **kwargs)


def run_stream(command: Sequence[str], *, timeout=None) -> int:
    """Вывод непосредственно в терминал; использовать только из интерфейсов."""
    return _invoke(list(command), text=True, timeout=timeout).returncode


def tcp_endpoint_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def tcp_port_in_use(port: int, host: str = "") -> bool:
    """Занят ли TCP-порт в системе: пробный bind без listen перед сменой порта inbound (XR-REV-010).

    Пустой или wildcard `host` проверяет все адреса: `::` в dual-stack режиме, как слушает Xray, и `0.0.0.0`
    на случай выключенного IPv6; конкретный адрес — только его. SO_REUSEADDR убирает ложную занятость
    из-за TIME_WAIT, а bind на Linux всё равно отказывает, если порт слушает другой процесс (Caddy, другой
    сервис). Занятым считается только EADDRINUSE: недоступное семейство адресов и прочие ошибки bind смену
    не блокируют — фактический старт проверит `restart_systemd_unit`. Unix-сокеты TCP-порт не занимают.
    """
    host = str(host or "").strip()
    if host.startswith("/") or host.startswith("@"):
        return False
    if host in WILDCARD_LISTEN_HOSTS:
        candidates = ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0"))
    else:
        candidates = ((socket.AF_INET6 if ":" in host else socket.AF_INET, host),)
    for family, address in candidates:
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        with probe:
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6 and address == "::":
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                probe.bind((address, port))
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    return True
    return False




_SYSTEMD_COMPAT = ('DEFAULT_VERIFY_TIMEOUT', 'DEFAULT_POLL_INTERVAL', 'SHOW_TIMEOUT_FLOOR', 'UNIT_FAILED_SUBSTATES', 'WRITER_STOP_UNITS', 'WRITER_RESTART_UNITS', 'Runner', 'SystemdUnitStartError', 'active_units', 'stop_writer_units', 'restart_units', 'stop_systemd_unit', 'restart_systemd_unit', 'systemd_unit_state', 'systemd_exec_commands', 'verify_systemd_unit_started')


def __getattr__(name):
    if name in _SYSTEMD_COMPAT:
        from xray_vps_manager.core import systemd
        return getattr(systemd, name)
    raise AttributeError(name)
