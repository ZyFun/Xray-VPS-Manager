"""Применение кандидата config.json с проверкой старта и атомарным откатом."""

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil

from xray_vps_manager.core import clock, fs, paths, process, systemd
from xray_vps_manager.core.errors import ApplyConfigError

CONFIG_BACKUP_KEEP = 20


@dataclass(frozen=True)
class ApplyResult:
    changed: bool
    backup: Path | None = None
    restarted: bool = False
    verified_ports: tuple[int, ...] = ()


def _permissions(path: Path) -> None:
    fs.chown_xray(path)
    path.chmod(0o640)


def _backup_path(path: Path) -> Path:
    moment = clock.now()
    # Повторное применение в одну микросекунду не должно затирать прежнюю копию.
    while True:
        stamp = moment.strftime("%Y%m%d%H%M%S%f")
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        if not os.path.lexists(backup):
            return backup
        moment += timedelta(microseconds=1)


def _backup_stamp_key(stamp: str, legacy_tag: str | None) -> str | None:
    # install.sh пишет обычные копии с UTC до секунд; Python-пути, включая старый
    # Telegram, — до микросекунд. Иные числовые суффиксы не участвуют в ротации.
    if len(stamp) == 20:
        return stamp
    if legacy_tag is None and len(stamp) == 14:
        return stamp + "000000"
    return None


def _detail(error: BaseException, limit: int = 12) -> str:
    parts = [str(error) or type(error).__name__]
    for name in ("detail", "stderr", "stdout", "output"):
        value = getattr(error, name, "")
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        if value and value not in parts:
            parts.append(str(value))
    return "\n".join("\n".join(parts).splitlines()[:limit])


def config_test_command(path: Path | None = None, *, binary: Path | None = None,
                        json_format: bool = False) -> list[str]:
    """Команда проверки для обычного запуска и потокового вывода ручного меню."""
    command = [str(binary or paths.paths().xray_bin), "run", "-test"]
    if json_format:
        command.extend(["-format", "json"])
    command.extend(["-config", str(path or paths.paths().config)])
    return command


def test_config_file(path: Path | None = None, *, binary: Path | None = None,
                     asset_dir: Path | None = None, json_format: bool = False,
                     check: bool = True):
    """Проверить указанный файл выбранным бинарником/assets, ничего не устанавливая."""
    command = config_test_command(path, binary=binary, json_format=json_format)
    env = {**os.environ, "XRAY_LOCATION_ASSET": str(asset_dir)} if asset_dir is not None else None
    return process.run(command, timeout=30, check=check, env=env)


def _local_ports(config: dict) -> list[tuple[str, int]]:
    from xray_vps_manager.xray.config import inbound_listen_endpoints
    return [endpoint for endpoint in inbound_listen_endpoints(config)
            if endpoint[0] in {"127.0.0.1", "::1"}]


def restart_installed_config(*, verify_timeout: float = systemd.DEFAULT_VERIFY_TIMEOUT,
                             reason: str = "") -> ApplyResult:
    """Проверить и обязательно запустить установленный комплект под lock сценария.

    Байты config.json и технические копии не меняются. Откат архива, бинарника
    или assets выполняет владеющий ими сценарий, включая отмену операции.
    """
    stage = "test"
    try:
        config = json.loads(paths.paths().config.read_bytes())
        test_config_file()
        ports = _local_ports(config)
        stage = "restart"
        systemd.restart_unit("xray", verify_timeout=verify_timeout, ports=ports)
        return ApplyResult(False, restarted=True,
                           verified_ports=tuple(port for _, port in ports) if verify_timeout > 0 else ())
    except BaseException as error:
        failure = ApplyConfigError(
            "Не удалось запустить установленную конфигурацию Xray" + (f" ({reason})" if reason else ""),
            stage="verify" if isinstance(error, systemd.SystemdUnitStartError) else stage,
            detail=_detail(error),
        )
        if isinstance(error, KeyboardInterrupt):
            failure.exit_code = 130
        raise failure from error


def _restore(data: bytes, *, restart: bool, verify_timeout: float) -> None:
    ports = _local_ports(json.loads(data)) if restart else ()
    fs.replace_file(paths.paths().config, data, prepare=_permissions)
    if restart:
        systemd.restart_unit("xray", verify_timeout=verify_timeout, ports=ports)


def _test_candidate(data: bytes) -> None:
    """Проверить временный файл и убрать его до изменения рабочего конфига."""
    candidate = paths.paths().config.with_suffix(".json.tmp")
    try:
        fs.replace_file(candidate, data, prepare=_permissions)
        test_config_file(candidate, json_format=True)
    except BaseException as error:
        try:
            candidate.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise ApplyConfigError("Не удалось проверить конфигурацию Xray", stage="test",
                                   detail=_detail(error, 6) + "\nОчистка кандидата: "
                                   + _detail(cleanup_error, 6)) from cleanup_error
        raise
    candidate.unlink(missing_ok=True)


@contextmanager
def temporary_config(config: dict, *, verify_timeout: float = systemd.DEFAULT_VERIFY_TIMEOUT):
    """Установить конфиг пробы и обязательно вернуть C0 без технического бэкапа.

    Сценарий держит manager.lock всё время пробы. Диагностика не нормализует
    старый конфиг и не требует инвариантов постоянного apply. Даже совпавший
    кандидат запускается заново. Ошибка возврата всегда фатальна.
    """
    path = paths.paths().config
    stage = "test"
    installed = False
    original = None
    error = None
    try:
        data = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        _test_candidate(data)
        stage = "write"
        original = path.read_bytes()
        json.loads(original)
        ports = _local_ports(config)
        # Отмена после rename также должна вернуть исходные байты.
        installed = True
        fs.replace_file(path, data, prepare=_permissions)
        stage = "restart"
        systemd.restart_unit("xray", verify_timeout=verify_timeout, ports=ports)
        stage = "probe"
        yield
    except BaseException as exc:
        error = exc
    finally:
        if installed:
            try:
                _restore(original, restart=True, verify_timeout=verify_timeout)
            except BaseException as rollback_error:
                raise ApplyConfigError(
                    "Не удалось восстановить Xray после временной пробы", stage="rollback",
                    detail=("Проба: " + _detail(error, 6) + "\n" if error else "")
                    + "Возврат: " + _detail(rollback_error, 6),
                ) from rollback_error
    if error is not None:
        if stage == "probe":
            raise error
        failure = ApplyConfigError(
            "Не удалось запустить временную конфигурацию Xray",
            stage="verify" if isinstance(error, systemd.SystemdUnitStartError) else stage,
            restored=installed, detail=_detail(error),
        )
        if isinstance(error, KeyboardInterrupt):
            failure.exit_code = 130
        raise failure from error


def restore_config_backup(backup: Path | None, *, restart: bool = True,
                          verify_timeout: float = systemd.DEFAULT_VERIFY_TIMEOUT) -> None:
    """Компенсировать успешный apply после ошибки следующего шага БД/Caddy.

    Не вызывается после ошибки самого apply. None означает, что apply не менял
    файл. Копия возвращается байт-в-байт, без нового бэкапа и без ротации.
    """
    if backup is None:
        return
    try:
        _restore(backup.read_bytes(), restart=restart, verify_timeout=verify_timeout)
    except BaseException as error:
        raise ApplyConfigError("Не удалось восстановить конфигурацию Xray", stage="rollback",
                               backup=backup, detail=_detail(error)) from error


def apply_config(config: dict, *, restart: bool = True,
                 verify_timeout: float = systemd.DEFAULT_VERIFY_TIMEOUT,
                 reason: str = "") -> ApplyResult:
    """Проверить кандидат, записать и применить; manager.lock берёт вызывающий сценарий.

    БД и server.env принадлежат сценарию. При ошибке старый файл восстанавливается
    байт-в-байт; restored=True означает также успешную проверку рестарта отката.
    Перед заменой остаётся до 20 технических копий, включая копию для отката.
    reason — контекст ошибки без вывода.
    """
    from xray_vps_manager.xray import invariants

    path = paths.paths().config
    backup = None
    stage = "invariants"
    label = "Не удалось применить конфигурацию Xray" + (f" ({reason})" if reason else "")
    try:
        invariants.validate(config)
        stage = "test"
        data = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        _test_candidate(data)
        stage = "write"
        original = path.read_bytes()
        if data == original:
            return ApplyResult(changed=False)
        json.loads(original)  # Отказ до записи, если исходный файл не пригоден для отката.
        ports = _local_ports(config)
        backup_path = _backup_path(path)
        fs.replace_file(backup_path, original, prepare=_permissions)
        backup = backup_path
        fs.prune_backups(path, keep=CONFIG_BACKUP_KEEP, protect=(backup,),
                         legacy_tags=("telegram",), timestamp_key=_backup_stamp_key)
        try:
            fs.replace_file(path, data, prepare=_permissions)
            if restart:
                stage = "restart"
                systemd.restart_unit("xray", verify_timeout=verify_timeout, ports=ports)
            return ApplyResult(True, backup, restart,
                               tuple(port for _, port in ports) if restart and verify_timeout > 0 else ())
        except BaseException as error:
            failed_stage = "verify" if isinstance(error, systemd.SystemdUnitStartError) else stage
            if stage == "write":
                # Отмена может прийти уже после rename. Ошибка чтения не доказывает,
                # что рабочий файл сохранился, поэтому в этом случае нужен откат.
                try:
                    unchanged = path.read_bytes() == original
                except BaseException:
                    unchanged = False
                if unchanged:
                    raise
            try:
                _restore(original, restart=restart, verify_timeout=verify_timeout)
            except BaseException as rollback_error:
                raise ApplyConfigError(label, stage="rollback", backup=backup,
                                       detail="Применение: " + _detail(error, 6)
                                       + "\nОткат: " + _detail(rollback_error, 6)) from rollback_error
            failure = ApplyConfigError(label, stage=failed_stage, backup=backup, restored=restart,
                                       detail=_detail(error))
            if isinstance(error, KeyboardInterrupt):
                failure.exit_code = 130
            raise failure from error
    except ApplyConfigError:
        raise
    except BaseException as error:
        failure = ApplyConfigError(label, stage=stage, backup=backup, detail=_detail(error))
        if isinstance(error, KeyboardInterrupt):
            failure.exit_code = 130
        raise failure from error


def save_config(config: dict, path: Path) -> Path:
    """Переходный писатель: сохраняет порядок и исключения старых адаптеров T3.

    Он намеренно не вызывает новый apply_config до переноса VIREI-124/125.
    """
    timestamp = clock.now().strftime("%Y%m%d%H%M%S%f")
    backup = path.with_name(f"{path.name}.bak.{timestamp}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    shutil.chown(tmp, user="root", group="xray")
    os.chmod(tmp, 0o640)
    tmp.replace(path)
    return backup
