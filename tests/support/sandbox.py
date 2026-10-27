"""Временный сервер для контрактных тестов. Legacy-швы сосредоточены здесь.

Фасад рассчитан на последовательные тесты в одном процессе. Это подмена
зависимостей, а не граница безопасности для исполнения недоверенного кода.
Канонические часы подключены в R1.2, процессы и HTTP — в R1.3.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, redirect_stdout, redirect_stderr
from dataclasses import dataclass
from datetime import date, datetime, timezone
from importlib import import_module
import inspect
import io
import json
import os
from pathlib import Path
import pkgutil
import random
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest import mock
from zoneinfo import ZoneInfo

import xray_vps_manager
from xray_vps_manager.core import paths as path_module
from xray_vps_manager.core import clock, process, http
from xray_vps_manager.core.server_env import read_server_env
from xray_vps_manager.db import database
from xray_vps_manager.runner import command_main

TESTS = Path(__file__).resolve().parents[1]
# Явный реестр копий, включая производные пути и коллекции. Тест сверяет
# полноту реестра с загруженными модулями; при переносах записи удаляются.
LEGACY_PATH_ATTRS = (
    ('xray_vps_manager.activity.backfill', 'ACCESS_LOG_PATH'),
    ('xray_vps_manager.activity.blocklist', 'CONFIG_PATH'),
    ('xray_vps_manager.activity.blocklist', 'XRAY_BIN'),
    ('xray_vps_manager.activity.bypass', 'CONFIG_PATH'),
    ('xray_vps_manager.activity.cleanup', 'MANAGER_DB_PATH'),
    ('xray_vps_manager.activity.constants', 'CONFIG_PATH'),
    ('xray_vps_manager.activity.constants', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.activity.constants', 'EXPORT_DIR'),
    ('xray_vps_manager.activity.constants', 'LOCK_PATH'),
    ('xray_vps_manager.activity.constants', 'ACCESS_LOG_PATH'),
    ('xray_vps_manager.activity.constants', 'ERROR_LOG_PATH'),
    ('xray_vps_manager.activity.constants', 'XRAY_BIN'),
    ('xray_vps_manager.activity.constants', 'GEOIP_PATHS'),
    ('xray_vps_manager.activity.controls', 'CONFIG_PATH'),
    ('xray_vps_manager.activity.exports', 'EXPORT_DIR'),
    ('xray_vps_manager.activity.parser', 'GEOIP_PATHS'),
    ('xray_vps_manager.activity.raw_logs', 'ACCESS_LOG_PATH'),
    ('xray_vps_manager.activity.raw_logs', 'ERROR_LOG_PATH'),
    ('xray_vps_manager.activity.raw_logs', 'SYSTEMD_UNIT_DIR'),
    ('xray_vps_manager.activity.raw_logs', 'RAW_LOG_ROTATE_SERVICE_PATH'),
    ('xray_vps_manager.activity.raw_logs', 'RAW_LOG_ROTATE_TIMER_PATH'),
    ('xray_vps_manager.activity.repository', 'EXPORT_DIR'),
    ('xray_vps_manager.activity.settings', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.activity.status', 'CONFIG_PATH'),
    ('xray_vps_manager.activity.status', 'MANAGER_DB_PATH'),
    ('xray_vps_manager.activity.sync', 'ACCESS_LOG_PATH'),
    ('xray_vps_manager.activity.sync', 'CONFIG_PATH'),
    ('xray_vps_manager.clients.settings', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.commands.activity', 'EXPORT_DIR'),
    ('xray_vps_manager.commands.activity', 'LOCK_PATH'),
    ('xray_vps_manager.commands.backup', 'BACKUP_DIR'),
    ('xray_vps_manager.commands.backup', 'CONFIG_DIR'),
    ('xray_vps_manager.commands.backup', 'CONFIG_PATH'),
    ('xray_vps_manager.commands.backup', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.commands.backup', 'MANAGER_DB_PATH'),
    ('xray_vps_manager.commands.backup', 'CADDY_RANDOM_TLS_ENV_PATH'),
    ('xray_vps_manager.commands.backup', 'CADDY_RANDOM_TLS_CONFIG_DIR'),
    ('xray_vps_manager.commands.backup', 'CADDYFILE_PATH'),
    ('xray_vps_manager.commands.backup', 'CADDY_CONF_DIR'),
    ('xray_vps_manager.commands.backup', 'BACKUP_FILES'),
    ('xray_vps_manager.commands.backup', 'BACKUP_DIRS'),
    ('xray_vps_manager.commands.backup', 'CADDY_CONFIG_FILES'),
    ('xray_vps_manager.commands.backup', 'CADDY_CONFIG_DIRS'),
    ('xray_vps_manager.commands.client', 'CONFIG_PATH'),
    ('xray_vps_manager.commands.client', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.commands.client', 'TRAFFIC_SYNC'),
    ('xray_vps_manager.commands.client', 'XRAY_TELEGRAM'),
    ('xray_vps_manager.commands.manager_update', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.commands.manager_update', 'SOURCE_DIR'),
    ('xray_vps_manager.commands.manager_update', 'SBIN_DIR'),
    ('xray_vps_manager.commands.manager_update', 'MANAGER_LIB_DIR'),
    ('xray_vps_manager.commands.manager_update', 'MANAGER_PACKAGE_DIR'),
    ('xray_vps_manager.commands.manager_update', 'BACKUP_DIR'),
    ('xray_vps_manager.commands.menu_activity_actions', 'CONFIG_PATH'),
    ('xray_vps_manager.commands.menu_activity_actions', 'XRAY_BIN'),
    ('xray_vps_manager.commands.menu_reality_actions', 'CONFIG_PATH'),
    ('xray_vps_manager.commands.menu_reality_actions', 'XRAY_BIN'),
    ('xray_vps_manager.commands.menu_reality_actions', 'CLIENT_LINK_PATH'),
    ('xray_vps_manager.commands.menu_security_actions', 'SSHD_CONFIG_PATH'),
    ('xray_vps_manager.commands.menu_security_actions', 'SSHD_DROPIN_PATH'),
    ('xray_vps_manager.commands.menu_security_actions', 'SSHD_LEGACY_DROPIN_PATH'),
    ('xray_vps_manager.commands.menu_security_actions', 'AUTHORIZED_KEYS_PATH'),
    ('xray_vps_manager.commands.menu_status', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.commands.menu_status', 'XRAY_ASSET_DIR'),
    ('xray_vps_manager.commands.menu_traffic_actions', 'XRAY_TRAFFIC_SYNC'),
    ('xray_vps_manager.commands.menu_xray_actions', 'CONFIG_PATH'),
    ('xray_vps_manager.commands.menu_xray_actions', 'CLIENT_LINK_PATH'),
    ('xray_vps_manager.commands.set_bypass', 'ACCESS_LOG_PATH'),
    ('xray_vps_manager.commands.set_bypass', 'CONFIG_PATH'),
    ('xray_vps_manager.commands.set_bypass', 'XRAY_BIN'),
    ('xray_vps_manager.commands.set_cascade', 'CONFIG_PATH'),
    ('xray_vps_manager.commands.sqlite', 'MANAGER_DB_PATH'),
    ('xray_vps_manager.commands.telegram', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.commands.telegram', 'MANAGER_DB_PATH'),
    ('xray_vps_manager.commands.telegram', 'XRAY_CLIENT'),
    ('xray_vps_manager.commands.test', 'CONFIG_PATH'),
    ('xray_vps_manager.commands.test', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.commands.test', 'MANAGER_DB_PATH'),
    ('xray_vps_manager.commands.test', 'XRAY_BIN'),
    ('xray_vps_manager.commands.test', 'XRAY_ASSET_DIR'),
    ('xray_vps_manager.commands.test', 'MANAGER_PACKAGE_DIR'),
    ('xray_vps_manager.commands.traffic_sync', 'MANAGER_DB_PATH'),
    ('xray_vps_manager.commands.traffic_sync', 'CONFIG_PATH'),
    ('xray_vps_manager.commands.traffic_sync', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.commands.traffic_sync', 'LOCK_PATH'),
    ('xray_vps_manager.commands.traffic_sync', 'ACCESS_LOG_PATH'),
    ('xray_vps_manager.commands.update', 'CONFIG_PATH'),
    ('xray_vps_manager.commands.update', 'XRAY_BIN'),
    ('xray_vps_manager.commands.update', 'XRAY_ASSET_DIR'),
    ('xray_vps_manager.commands.update', 'BACKUP_DIR'),
    ('xray_vps_manager.commands.warp', 'CONFIG_PATH'),
    ('xray_vps_manager.commands.warp', 'WARP_DIR'),
    ('xray_vps_manager.commands.warp', 'WGCF_BIN'),
    ('xray_vps_manager.commands.warp', 'WGCF_ACCOUNT'),
    ('xray_vps_manager.commands.warp', 'WGCF_PROFILE'),
    ('xray_vps_manager.commands.warp', 'HOSTS_PATH'),
    ('xray_vps_manager.core.server_env', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.core.time', 'SERVER_ENV_PATH'),
    ('xray_vps_manager.db.database', 'MANAGER_DB_PATH'),
    ('xray_vps_manager.db.schema', 'MANAGER_DB_PATH'),
    ('xray_vps_manager.telegram.admin', 'AdminContext.xray_client'),
    ('xray_vps_manager.telegram.notifications', 'MANAGER_DB_PATH'),
    ('xray_vps_manager.telegram.notifications', 'REBOOT_REQUIRED_PATH'),
    ('xray_vps_manager.telegram.notifications', 'REBOOT_REQUIRED_PACKAGES_PATH'),
    ('xray_vps_manager.telegram.setup', 'CONFIG_PATH'),
    ('xray_vps_manager.telegram.setup', 'XRAY_BIN'),
    ('xray_vps_manager.xray.caddy', 'CADDYFILE_PATH'),
    ('xray_vps_manager.xray.caddy', 'CADDY_CONF_DIR'),
    ('xray_vps_manager.xray.caddy', 'CADDY_BACKUP_DIR'),
    ('xray_vps_manager.xray.caddy', 'CADDY_SITE_BACKUP_DIR'),
    ('xray_vps_manager.xray.caddy', 'CADDY_RANDOM_TLS_ENV_PATH'),
    ('xray_vps_manager.xray.caddy', 'CADDY_RANDOM_TLS_CONFIG_DIR'),
    ('xray_vps_manager.xray.caddy', 'RANDOM_TLS_LOCK_PATH'),
    ('xray_vps_manager.xray.caddy', 'SYSTEMD_DIR'),
    ('xray_vps_manager.xray.config', 'CONFIG_PATH'),
)

# Защитные списки сохраняют исходные запреты, а не только временные пути.
LEGACY_GUARD_PATH_ATTRS = (
    ('xray_vps_manager.xray.caddy', 'DANGEROUS_SITE_ROOTS'),
)

LEGACY_CACHE_ATTRS = (
    ('xray_vps_manager.activity.parser', '_GEOIP_CODES_CACHE'),
)


class UnexpectedCommand(AssertionError):
    """Внешняя операция не задана тестом; реальная команда не запускается."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int = 0
    stdout: str = ''
    stderr: str = ''


class FakeProcesses:
    """Точные argv → ответ или конечная последовательность ответов; журнал всех попыток."""
    def __init__(self):
        self.calls = []
        self.requests = []
        self.unexpected = []
        self.executables = {}
        self._scripts = {}

    def _reject(self, message):
        self.unexpected.append(message)
        raise UnexpectedCommand(message)

    def deny(self, *args, **kwargs):
        self._reject('Реальная внешняя операция запрещена в Sandbox: '+repr(args))

    def which(self, cmd, mode=os.F_OK | os.X_OK, path=None):
        value = self.executables.get(os.fsdecode(cmd))
        return str(value) if value is not None else None

    @staticmethod
    def result(returncode=0, stdout='', stderr=''):
        return ProcessResult(returncode, stdout, stderr)

    @staticmethod
    def ok(stdout=''):
        return ProcessResult(stdout=stdout)

    def script(self, responses):
        for command, response in responses.items():
            key = tuple(shlex.split(command)) if isinstance(command, str) else tuple(map(str, command))
            self._scripts[key] = list(response) if isinstance(response, list) else response

    def run(self, args, *positional, **kwargs):
        if isinstance(args, (str, bytes)) or kwargs.get('shell') or positional:
            self._reject('Sandbox запрещает shell и неоднозначный запуск: '+repr(args))
        key = tuple(map(str, args))
        self.calls.append(key)
        self.requests.append((key, dict(kwargs)))
        response = self._scripts.get(key)
        if isinstance(response, list):
            response = response.pop(0) if response else None
        if response is None:
            self._reject('Не задана внешняя команда: '+shlex.join(key))
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(list(args), **kwargs)
        text_mode = kwargs.get('text') or kwargs.get('universal_newlines') or kwargs.get('encoding')
        stdout, stderr = response.stdout, response.stderr
        if not text_mode:
            stdout, stderr = stdout.encode(kwargs.get('encoding') or 'utf-8'), stderr.encode(kwargs.get('encoding') or 'utf-8')
        if kwargs.get('stderr') == subprocess.STDOUT:
            stdout += stderr
            stderr = None
        result = subprocess.CompletedProcess(args, response.returncode, stdout, stderr)
        # Повторяем видимые потоки subprocess.run при отсутствии PIPE.
        for name, output in (('stdout', stdout), ('stderr', stderr)):
            target = kwargs.get(name)
            if output is None:
                continue
            if kwargs.get('capture_output') or target == subprocess.PIPE:
                continue
            if target == subprocess.DEVNULL:
                pass
            elif target is None:
                stream = sys.stdout if name == 'stdout' else sys.stderr
                stream.write(output.decode() if isinstance(output, bytes) else output)
            elif hasattr(target, 'write'):
                target.write(output)
            else:
                self._reject('Неподдерживаемое перенаправление '+name)
            setattr(result, name, None)
        if kwargs.get('check'):
            result.check_returncode()
        return result

    def check_output(self, args, **kwargs):
        kwargs.update(check=True, stdout=subprocess.PIPE)
        return self.run(args, **kwargs).stdout

    def call(self, args, **kwargs):
        return self.run(args, **kwargs).returncode

    def check_call(self, args, **kwargs):
        self.run(args, check=True, **kwargs)
        return 0


class FakeHTTP:
    """Точные URL → байты/ошибка; журнал параметров и запрет неизвестных запросов."""
    def __init__(self):
        self.calls = []
        self.unexpected = []
        self._scripts = {}

    def script(self, responses):
        self._scripts.update(responses)

    @staticmethod
    def json(value):
        return json.dumps(value).encode('utf-8')

    def open(self, request, timeout=None, **kwargs):
        url = request if isinstance(request, str) else request.full_url
        headers = {} if isinstance(request, str) else dict(request.header_items())
        self.calls.append((url, headers, timeout))
        response = self._scripts.get(url)
        if isinstance(response, list):
            response = response.pop(0) if response else None
        if response is None:
            message = 'Не задан HTTP-запрос: '+url
            self.unexpected.append(message)
            raise UnexpectedCommand(message)
        if isinstance(response, BaseException):
            raise response
        return io.BytesIO(response)


@dataclass(frozen=True)
class CliResult:
    stdout: str
    stderr: str
    exit_code: int


def _modules():
    return [import_module(info.name) for info in pkgutil.walk_packages(
        xray_vps_manager.__path__, xray_vps_manager.__name__+'.')
        if not info.name.endswith('.__main__')]


def _legacy_owner(module_name, attribute):
    owner = import_module(module_name)
    *parents, name = attribute.split('.')
    for parent in parents:
        owner = getattr(owner, parent)
    return owner, name


def _contains_path(value):
    if isinstance(value, Path):
        return value.is_absolute()
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_path(item) for item in value)
    return False


class Sandbox:
    def __init__(self, *, config='fixtures/config/reality-single.json',
                 env='fixtures/env/default.env', db='fixtures/db/one-client.sql',
                 now='2026-10-06T12:00:00Z', tz='Europe/Moscow'):
        self._config, self._env, self._db = config, env, db
        self.now = datetime.fromisoformat(now.replace('Z', '+00:00'))
        if self.now.tzinfo is None:
            raise ValueError('Sandbox.now требует часовой пояс')
        self.tz = ZoneInfo(tz)
        self.processes = FakeProcesses()
        self.http = FakeHTTP()
        self._stack = None

    def _patch(self, obj, name, value):
        self._stack.enter_context(mock.patch.object(obj, name, value))

    def _relocate(self, value):
        if isinstance(value, Path) and value.is_absolute():
            # Вложенный Sandbox получает значения внешнего, но сохраняет
            # семантический серверный путь, не наращивая два tmp-префикса.
            if self._outer_root is not None and value.is_relative_to(self._outer_root):
                value = Path('/') / value.relative_to(self._outer_root)
            return self.root / value.relative_to('/')
        if isinstance(value, (list, tuple, set, frozenset)):
            return type(value)(self._relocate(item) for item in value)
        return value

    def __enter__(self):
        if self._stack is not None:
            raise RuntimeError('Повторный вход в тот же Sandbox')
        self._stack = ExitStack()
        try:
            self.root = Path(self._stack.enter_context(tempfile.TemporaryDirectory(prefix='xray-sandbox-'))).resolve()
            self.paths = path_module.Paths.under(self.root)
            # Импорт до подмены: захваченные defaults остаются известны фасаду.
            self._modules = _modules()
            for module_name, attribute in LEGACY_CACHE_ATTRS:
                owner, name = _legacy_owner(module_name, attribute)
                self._patch(owner, name, None)
            old_config = path_module.paths().config
            suffix = path_module.Paths.default().config.relative_to('/')
            self._outer_root = old_config.parents[len(suffix.parts)-1] if old_config != Path('/')/suffix else None
            self._assert_inventory()
            self._stack.enter_context(path_module.use(self.paths))
            for module_name, attribute in LEGACY_PATH_ATTRS:
                owner, name = _legacy_owner(module_name, attribute)
                self._patch(owner, name, self._relocate(getattr(owner, name)))
                if inspect.isclass(owner) and name in getattr(owner, '__dataclass_fields__', {}):
                    field = owner.__dataclass_fields__[name]
                    self._patch(field, 'default', self._relocate(field.default))
            for module_name, attribute in LEGACY_GUARD_PATH_ATTRS:
                owner, name = _legacy_owner(module_name, attribute)
                original = getattr(owner, name)
                protected = set(original) | set(self._relocate(original))
                protected |= {path.resolve() for path in protected}
                self._patch(owner, name, type(original)(protected))
            self._install_processes_and_defaults()
            self._install_clock()
            self._install_sqlite_clock()
            self._stack.enter_context(mock.patch.dict(os.environ, {'NO_COLOR':'1', 'TZ':str(self.tz)}))
            self.paths.config_dir.mkdir(parents=True)
            config = json.loads(self._fixture(self._config).read_text())
            for name in ('access', 'error'):
                value = config.get('log', {}).get(name)
                if value and value.startswith('/'):
                    config['log'][name] = str(self._relocate(Path(value)))
            self.paths.config.write_text(json.dumps(config, indent=2)+'\n')
            env_lines = self._fixture(self._env).read_text().splitlines()
            env_lines = [line for line in env_lines if not line.startswith('MANAGER_TIMEZONE=')]
            self.paths.server_env.write_text('\n'.join(env_lines)+f'\nMANAGER_TIMEZONE={self.tz}\n')
            self.paths.log_dir.mkdir(parents=True)
            self.paths.access_log.touch()
            self.paths.error_log.touch()
            with closing(database.initialize_database(self.paths.manager_db)) as conn:
                if self._db is not None:
                    conn.executescript(self._fixture(self._db).read_text())
                conn.commit()
            return self
        except BaseException:
            self._stack.close()
            self._stack = None
            raise

    def __exit__(self, *exc):
        try:
            suppressed = self._stack.__exit__(*exc)
        finally:
            self._stack = None
        if (exc[0] is None or suppressed) and (self.processes.unexpected or self.http.unexpected):
            raise AssertionError('Незапланированные внешние операции в Sandbox:\n'
                                 +'\n'.join(self.processes.unexpected + self.http.unexpected))
        return suppressed

    @staticmethod
    def _fixture(value):
        path = Path(value)
        return path if path.is_absolute() else TESTS / path

    def _assert_inventory(self):
        actual = {(module.__name__, name) for module in self._modules
                  if module is not path_module for name, value in vars(module).items()
                  if name.isupper() and _contains_path(value)}
        for module in self._modules:
            for name, value in vars(module).items():
                if inspect.isclass(value) and value.__module__ == module.__name__:
                    actual.update((module.__name__, name+'.'+attribute)
                                  for attribute, default in vars(value).items()
                                  if _contains_path(default))
        if set(LEGACY_PATH_ATTRS) & set(LEGACY_GUARD_PATH_ATTRS):
            raise AssertionError('Рабочие пути и защитные списки должны иметь разные реестры')
        expected = set(LEGACY_PATH_ATTRS) | set(LEGACY_GUARD_PATH_ATTRS)
        if actual != expected:
            raise AssertionError(f'Обновите реестры LEGACY_PATH_ATTRS/LEGACY_GUARD_PATH_ATTRS: новые={actual-expected}, устарели={expected-actual}')

    def assert_legacy_paths(self):
        self._assert_inventory()
        def check(value):
            if isinstance(value, Path):
                if not value.is_relative_to(self.root):
                    raise AssertionError('Путь вне Sandbox: '+str(value))
            elif isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    check(item)
        for module_name, attribute in LEGACY_PATH_ATTRS:
            owner, name = _legacy_owner(module_name, attribute)
            check(getattr(owner, name))

    def _install_processes_and_defaults(self):
        self._stack.enter_context(process.fake(self.processes))
        self._stack.enter_context(http.fake(self.http.open))
        # Включая runner=subprocess.run, захваченный при определении функции.
        replacements = [(subprocess.run, self.processes.run),
                        (subprocess.check_output, self.processes.check_output),
                        (subprocess.call, self.processes.call),
                        (subprocess.check_call, self.processes.check_call),
                        (shutil.which, self.processes.which)]
        seen = set()
        def replace(value):
            for original, fake in replacements:
                if value == original:
                    return fake
            return self._relocate(value)
        for module in self._modules:
            for name, value in list(vars(module).items()):
                for original, fake in replacements:
                    if value is original:
                        self._patch(module, name, fake)
                functions = [value] if inspect.isfunction(value) else []
                if inspect.isclass(value) and value.__module__ == module.__name__:
                    functions.extend(f.__func__ if isinstance(f, (staticmethod, classmethod)) else f
                                     for f in vars(value).values()
                                     if inspect.isfunction(f) or isinstance(f, (staticmethod, classmethod)))
                for function in functions:
                    if hasattr(function, '__wrapped__'):
                        functions.append(function.__wrapped__)
                    if id(function) in seen or not function.__module__.startswith('xray_vps_manager.'):
                        continue
                    seen.add(id(function))
                    if function.__defaults__:
                        self._patch(function, '__defaults__', tuple(replace(v) for v in function.__defaults__))
                    if function.__kwdefaults__:
                        self._patch(function, '__kwdefaults__', {k:replace(v) for k,v in function.__kwdefaults__.items()})
        for name in ('run','check_output','call','check_call'):
            self._patch(subprocess, name, getattr(self.processes, name))
        self._patch(shutil, 'which', self.processes.which)
        self._patch(subprocess, 'Popen', self.processes.deny)
        for name in ('system','popen','execv','execve','execvp','execvpe','posix_spawn','posix_spawnp'):
            if hasattr(os, name):
                self._patch(os, name, self.processes.deny)
        # Проверка root относится к интерфейсу; владение хоста не меняем.
        self._patch(os, 'geteuid', lambda: 0)
        self._patch(os, 'chown', lambda *a, **kw: None)
        self._patch(shutil, 'chown', lambda *a, **kw: None)
        import urllib.request
        original_open = urllib.request.urlopen
        for module in self._modules:
            for name, value in list(vars(module).items()):
                if value is original_open:
                    self._patch(module, name, self.http.open)
        self._patch(urllib.request, 'urlopen', self.http.open)
        import socket
        self._patch(socket, 'create_connection', self.processes.deny)
        self._patch(socket, 'getaddrinfo', self.processes.deny)
        self._patch(socket, 'socket', self.processes.deny)

    def _install_sqlite_clock(self):
        original_connect = sqlite3.connect
        # Штатный SQLite сохраняет собственные форматы (%f, %s) и NULL
        # для невалидных значений; подменяется только аргумент now.
        formatter = self._stack.enter_context(closing(original_connect(':memory:')))
        instant = self.now.astimezone(timezone.utc).isoformat()

        def strftime(format_string, value):
            if isinstance(value, str) and value.lower() == 'now':
                value = instant
            return formatter.execute('SELECT strftime(?, ?)', (format_string, value)).fetchone()[0]

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            try:
                connection.create_function('strftime', 2, strftime)
            except BaseException:
                connection.close()
                raise
            return connection

        self._patch(sqlite3, 'connect', connect)

    def _install_clock(self):
        instant, zone = self.now, self.tz
        self._stack.enter_context(clock.frozen(instant, local_timezone=zone))
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                result = instant.astimezone(tz if tz is not None else zone)
                # Python 3.9 теряет fold в combine() для подкласса datetime.
                return cls.combine(result.date(), result.timetz() if tz is not None else result.time()).replace(fold=result.fold)
            @classmethod
            def utcnow(cls):
                result = instant.astimezone(timezone.utc)
                return cls.combine(result.date(), result.time())
            def astimezone(self, tz=None):
                if self.tzinfo is None:
                    return self.replace(tzinfo=zone).astimezone(tz or zone)
                return super().astimezone(tz or zone)
        class FrozenDate(date):
            @classmethod
            def today(cls):
                return instant.astimezone(zone).date()
        for module in self._modules:
            for name, value in list(vars(module).items()):
                # Поддерживает классы предыдущего вложенного Sandbox.
                if inspect.isclass(value) and issubclass(value, datetime):
                    self._patch(module, name, FrozenDateTime)
                elif inspect.isclass(value) and issubclass(value, date):
                    self._patch(module, name, FrozenDate)
        # Сейчас Trojan использует secrets, а shortId — openssl через FakeProcesses.
        rng = random.Random(0)
        from xray_vps_manager.xray import crypto
        self._patch(crypto.secrets, 'token_urlsafe', lambda n=32: ''.join(rng.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_') for _ in range((n*8+5)//6)))

    def run_cli(self, command, *argv, stdin=''):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, 'argv', ['xray-'+command, *argv]), mock.patch.object(sys, 'stdin', io.StringIO(stdin)), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                returned = command_main(command)()
                if isinstance(returned, int):
                    code = returned
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                if exc.code is not None and not isinstance(exc.code, int):
                    print(exc.code, file=sys.stderr)
        return CliResult(stdout.getvalue(), stderr.getvalue(), code)

    def run_menu(self, answers):
        return self.run_cli('menu', stdin='\n'.join(answers)+'\n')

    def config(self):
        return json.loads(self.paths.config.read_text())

    def env(self):
        return read_server_env(self.paths.server_env)

    def db_rows(self, *tables):
        lines = []
        with closing(sqlite3.connect(self.paths.manager_db)) as conn:
            known = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in tables:
                if table not in known:
                    raise ValueError('Неизвестная таблица: '+table)
                quoted = '"'+table.replace('"','""')+'"'
                info = list(conn.execute('PRAGMA table_info('+quoted+')'))
                keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]] or [row[1] for row in info]
                order = ','.join('"'+key.replace('"','""')+'"' for key in keys)
                lines.append('['+table+']')
                for row in conn.execute('SELECT * FROM '+quoted+' ORDER BY '+order):
                    lines.append(json.dumps(list(row), ensure_ascii=False))
        return '\n'.join(lines)+'\n'

    def assert_restarted(self, times=1):
        actual = sum(call == ('systemctl', 'restart', 'xray') for call in self.processes.calls)
        if actual != times:
            raise AssertionError(f'Ожидалось рестартов {times}, получено {actual}')

    def assert_golden(self, scenario, files, *, root=None):
        import difflib
        root = Path(root) if root is not None else TESTS/'golden'
        folder = root/scenario
        if Path(scenario).is_absolute() or '..' in Path(scenario).parts:
            raise ValueError('Golden-сценарий должен быть относительным путём')
        for name, content in files.items():
            if Path(name).name != name:
                raise ValueError('Имя golden-файла не должно содержать каталоги')
            normalized = content.replace(str(self.root), '<ROOT>')
            target = folder/name
            if os.environ.get('UPDATE_GOLDEN') == '1':
                folder.mkdir(parents=True, exist_ok=True)
                target.write_text(normalized)
            elif not target.exists():
                raise AssertionError('Нет golden: '+str(target)+'; обновление только UPDATE_GOLDEN=1')
            else:
                expected = target.read_text()
                if expected != normalized:
                    raise AssertionError(''.join(difflib.unified_diff(expected.splitlines(True), normalized.splitlines(True), fromfile=str(target), tofile='actual')))
