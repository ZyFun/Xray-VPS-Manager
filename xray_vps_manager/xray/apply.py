"""Применение кандидата config.json с проверкой старта и атомарным откатом."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil

from xray_vps_manager.core import clock, fs, paths, process, systemd
from xray_vps_manager.core.errors import ApplyConfigError


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
    stamp = clock.now().strftime("%Y%m%d%H%M%S%f")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    # Повторное применение в одну микросекунду не должно затирать прежнюю копию.
    while backup.exists():
        stamp = str(int(stamp) + 1)
        backup = path.with_name(f"{path.name}.bak.{stamp}")
    return backup


def _detail(error: BaseException, limit: int = 12) -> str:
    parts = [str(error) or type(error).__name__]
    for name in ("detail", "stderr", "stdout", "output"):
        value = getattr(error, name, "")
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        if value and value not in parts:
            parts.append(str(value))
    return "\n".join("\n".join(parts).splitlines()[:limit])


def apply_config(config: dict, *, restart: bool = True,
                 verify_timeout: float = systemd.DEFAULT_VERIFY_TIMEOUT,
                 reason: str = "") -> ApplyResult:
    """Проверить кандидат, записать и применить; manager.lock берёт вызывающий сценарий.

    БД и server.env принадлежат сценарию. При ошибке старый файл восстанавливается
    байт-в-байт; restored=True означает также успешную проверку рестарта отката.
    Ротация копий вводится отдельно в VIREI-126. reason — контекст ошибки без вывода.
    """
    from xray_vps_manager.xray import invariants
    from xray_vps_manager.xray.config import inbound_listen_endpoints

    path = paths.paths().config
    candidate = path.with_suffix(".json.tmp")
    backup = None
    stage = "invariants"
    label = "Не удалось применить конфигурацию Xray" + (f" ({reason})" if reason else "")
    try:
        invariants.validate(config)
        stage = "test"
        data = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            fs.replace_file(candidate, data, prepare=_permissions)
            process.run([str(paths.paths().xray_bin), "run", "-test", "-format", "json", "-config", str(candidate)], check=True)
        except BaseException as error:
            try:
                candidate.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise ApplyConfigError(label, stage="test", detail=_detail(error, 6)
                                       + "\nОчистка кандидата: " + _detail(cleanup_error, 6)) from cleanup_error
            raise
        # Ошибка удаления кандидата тоже останавливает применение до изменения рабочего файла.
        candidate.unlink(missing_ok=True)
        stage = "write"
        original = path.read_bytes()
        if data == original:
            return ApplyResult(changed=False)
        old_config = json.loads(original)
        ports = [endpoint for endpoint in inbound_listen_endpoints(config)
                 if endpoint[0] in {"127.0.0.1", "::1"}]
        old_ports = [endpoint for endpoint in inbound_listen_endpoints(old_config)
                     if endpoint[0] in {"127.0.0.1", "::1"}]
        backup_path = _backup_path(path)
        fs.replace_file(backup_path, original, prepare=_permissions)
        backup = backup_path
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
                fs.replace_file(path, original, prepare=_permissions)
                if restart:
                    systemd.restart_unit("xray", verify_timeout=verify_timeout, ports=old_ports)
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
