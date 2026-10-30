"""Сценарии доставки units с настройками областей и совместимый raw-log CLI."""
from __future__ import annotations

import subprocess
from pathlib import Path

from xray_vps_manager.activity.raw_logs import raw_log_on_calendar
from xray_vps_manager.core import process, systemd
from xray_vps_manager.core.errors import ExternalCommandError, StateError
from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.core.paths import paths
from xray_vps_manager.system import units


def sync_units(*, systemctl: bool = True, check: bool = False) -> list[str]:
    if check:
        return units.check_units(on_calendar=raw_log_on_calendar())
    try:
        with manager_lock(purpose="units sync"):
            return units.install_units(systemctl=systemctl, on_calendar=raw_log_on_calendar())
    except subprocess.TimeoutExpired as exc:
        raise ExternalCommandError("Истекло время синхронизации units", command=exc.cmd) from exc
    except OSError as exc:
        raise StateError("Не удалось синхронизировать units", detail=str(exc)) from exc


def raw_log_service_unit() -> str:
    return units.unit_text("xray-raw-log-rotate.service")


def raw_log_timer_unit(env: dict[str, str] | None = None) -> str:
    return units.unit_text("xray-raw-log-rotate.timer", on_calendar=raw_log_on_calendar(env))


def sync_raw_log_timer(*, run_systemctl: bool = True, service_path: Path | None = None,
                       timer_path: Path | None = None,
                       env: dict[str, str] | None = None) -> dict[str, str]:
    """Прежние результат, enable/restart и ошибка raw-log-timer-sync сохранены."""
    service_path = service_path if service_path is not None else paths().systemd_dir / "xray-raw-log-rotate.service"
    timer_path = timer_path if timer_path is not None else paths().systemd_dir / "xray-raw-log-rotate.timer"
    calendar = raw_log_on_calendar(env)
    try:
        units.write_unit(service_path, raw_log_service_unit())
        units.write_unit(timer_path, units.unit_text("xray-raw-log-rotate.timer", on_calendar=calendar))
    except StateError as exc:
        raise OSError(exc.detail or exc.message) from exc
    if run_systemctl:
        try:
            systemd.daemon_reload()
            process.run(["systemctl", "enable", "--now", "xray-raw-log-rotate.timer"], check=True)
            process.run(["systemctl", "restart", "xray-raw-log-rotate.timer"], check=True)
        except ExternalCommandError as exc:
            # Старый адаптер ловит RuntimeError и сохраняет текст ERROR.
            raise RuntimeError(exc.message) from exc
    return {"path": str(timer_path), "servicePath": str(service_path),
            "timerPath": str(timer_path), "onCalendar": calendar,
            "systemctl": "yes" if run_systemctl else "no"}
