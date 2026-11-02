"""Канонические тексты systemd units и их доставка (архитектура, раздел 3.6).

Настройки расписания передаёт сценарий system/unit_sync.py. Здесь только
формат файлов и операции с systemd; настройки областей не читаются.
"""
from __future__ import annotations

import stat
from pathlib import Path

from xray_vps_manager.core import fs, process, systemd
from xray_vps_manager.core.errors import StateError, ValidationError
from xray_vps_manager.core.paths import Paths, paths


UNIT_NAMES = ('xray.service', 'xray-traffic-sync.service', 'xray-telegram-poller.service', 'xray-caddy-random-tls@.service', 'xray-caddy-random-tls@.timer', 'xray-traffic-sync.timer', 'xray-raw-log-rotate.service', 'xray-raw-log-rotate.timer', 'xray-client-expire.service', 'xray-client-expire.timer')

_ONESHOT_UNITS = frozenset({
    'xray-traffic-sync.service', 'xray-client-expire.service',
    'xray-raw-log-rotate.service', 'xray-caddy-random-tls@.service',
})

_TEXTS = {
    'xray.service': """[Unit]
Description=Xray Service
Documentation=https://github.com/XTLS/Xray-core
After=network.target nss-lookup.target
Wants=network-online.target

[Service]
User=xray
Group=xray
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray/config.json
ExecStop=-+/usr/local/sbin/xray-traffic-sync --quiet
Restart=on-failure
RestartPreventExitStatus=23
LimitNPROC=10000
LimitNOFILE=1000000

[Install]
WantedBy=multi-user.target
""",
    'xray-traffic-sync.service': """[Unit]
Description=Persist Xray user traffic counters
After=xray.service
ConditionPathExists=/usr/local/etc/xray/config.json

[Service]
Type=oneshot
ExecStart=-/usr/local/sbin/xray-traffic-sync --quiet
ExecStart=-/usr/local/sbin/xray-activity sync --quiet
ExecStart=-/usr/local/sbin/xray-telegram notify-geoip --quiet
ExecStart=-/usr/local/sbin/xray-client enforce-limits --quiet
ExecStart=-/usr/local/sbin/xray-client expire-due --quiet
ExecStart=-/usr/local/sbin/xray-telegram notify-expiry --quiet
ExecStart=-/usr/local/sbin/xray-telegram notify-daily-summary --quiet
""",
    'xray-telegram-poller.service': """[Unit]
Description=Poll Telegram user messages for Xray VPS Manager
After=network-online.target xray.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/sbin/xray-telegram run-poller
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
""",
    'xray-caddy-random-tls@.service': """[Unit]
Description=Randomize Caddy TLS protocol profile for %i
After=network-online.target caddy.service
Wants=network-online.target
ConditionPathExists={env_template}

[Service]
Type=oneshot
EnvironmentFile={env_template}
ExecStart=/usr/local/sbin/xray-vps-manager caddy random-tls-run --domain %i --quiet
""",
    'xray-caddy-random-tls@.timer': """[Unit]
Description=Randomize Caddy TLS protocol profile for %i every 15-60 minutes

[Timer]
OnBootSec=15min
OnUnitActiveSec=15min
RandomizedDelaySec=45min
AccuracySec=1min
Unit={service_name}

[Install]
WantedBy=timers.target
""",
    'xray-traffic-sync.timer': """[Unit]
Description=Persist Xray user traffic counters every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s
Unit=xray-traffic-sync.service

[Install]
WantedBy=timers.target
""",
    'xray-raw-log-rotate.service': """[Unit]
Description=Rotate raw Xray access/error logs
After=xray.service
ConditionPathExists=/usr/local/etc/xray/config.json

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/xray-activity rotate-raw-logs --due
""",
    'xray-raw-log-rotate.timer': """[Unit]
Description=Rotate raw Xray access/error logs

[Timer]
OnCalendar={on_calendar}
Persistent=true
AccuracySec=1min
Unit=xray-raw-log-rotate.service

[Install]
WantedBy=timers.target
""",
    'xray-client-expire.service': """[Unit]
Description=Disable expired Xray clients
After=xray.service
ConditionPathExists=/usr/local/etc/xray/config.json

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/xray-client expire-due --quiet
""",
    'xray-client-expire.timer': """[Unit]
Description=Disable expired Xray clients every day at midnight

[Timer]
OnCalendar={expire_on_calendar}
Persistent=true
AccuracySec=1min
Unit=xray-client-expire.service

[Install]
WantedBy=timers.target
""",
}


def unit_text(name: str, *, on_calendar: str = "*-*-* 03:00:00",
              expire_on_calendar: str = "*-*-* 00:00:00", config_dir: Path | None = None,
              service_name: str = "xray-caddy-random-tls@%i.service") -> str:
    """Текст unit. Сценарий передаёт отдельные расписания ротации и истечения доступа.

    Без параметров — штатные пути и расписание новой установки. Параметры
    random-TLS сохраняют прежний контракт генераторов.
    """
    if name not in _TEXTS:
        raise ValidationError(f"Неизвестный systemd unit: {name}")
    template = _TEXTS[name]
    if name == "xray-raw-log-rotate.timer":
        return template.format(on_calendar=on_calendar)
    if name == "xray-client-expire.timer":
        return template.format(expire_on_calendar=expire_on_calendar)
    if name == "xray-caddy-random-tls@.service":
        directory = config_dir if config_dir is not None else Paths.default().caddy_random_tls_dir
        return template.format(env_template=directory / "%i.env")
    if name == "xray-caddy-random-tls@.timer":
        return template.format(service_name=service_name)
    return template


def write_unit(path: Path, text: str) -> bool:
    """Атомарно записать изменившийся unit с правами 0644; вернуть факт изменения."""
    try:
        if not unit_differs(path, text):
            return False
        fs.replace_file(path, text.encode("utf-8"), prepare=lambda candidate: candidate.chmod(0o644))
        return True
    except OSError as exc:
        raise StateError(f"Не удалось записать unit {path}", detail=str(exc)) from exc


def unit_differs(path: Path, text: str) -> bool:
    """Проверка текста и режима без создания файлов и без systemctl."""
    try:
        if path.is_symlink():
            raise StateError(f"Ожидался обычный unit-файл, получена ссылка: {path}")
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return True
        return data != text.encode("utf-8") or stat.S_IMODE(path.stat().st_mode) != 0o644
    except OSError as exc:
        raise StateError(f"Не удалось прочитать unit {path}", detail=str(exc)) from exc


def check_units(*, on_calendar: str = "*-*-* 03:00:00",
                expire_on_calendar: str = "*-*-* 00:00:00") -> list[str]:
    """Имена отсутствующих/отличающихся unit-файлов, в порядке установки."""
    changed = []
    for name in UNIT_NAMES:
        text = unit_text(name, on_calendar=on_calendar, expire_on_calendar=expire_on_calendar)
        if unit_differs(paths().systemd_dir / name, text):
            changed.append(name)
    return changed


def _restart_changed(name: str) -> None:
    if name in _ONESHOT_UNITS:
        # Выполняющийся oneshot находится в activating: try-restart прервёт
        # его и станет ждать нового запуска, который может ждать manager.lock
        # у sync. После reload новое определение применится при следующем старте.
        return
    if "@." not in name:
        # try-restart работает и для таймера: перезапускает только активный.
        systemd.try_restart(name)
        return
    prefix, suffix = name.split("@.", 1)
    pattern = prefix + "@*." + suffix
    result = process.run(["systemctl", "list-units", "--state=active", "--plain",
                          "--no-legend", "--no-pager", pattern], check=True)
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        instance = fields[0]
        if (instance.startswith(prefix + "@") and instance.endswith("." + suffix)
                and instance != name):
            systemd.try_restart(instance)


def install_units(*, systemctl: bool, on_calendar: str = "*-*-* 03:00:00",
                  expire_on_calendar: str = "*-*-* 00:00:00") -> list[str]:
    """Записать изменения; reload и try-restart изменённых units, кроме oneshot.

    Oneshot продолжает текущий запуск; новое определение — со следующего.
    Для шаблонов таймеров перезапускаются только уже активные экземпляры.
    Неактивные службы/таймеры остаются неактивными, enable не вызывается.
    """
    changed = check_units(on_calendar=on_calendar, expire_on_calendar=expire_on_calendar)
    previous = {}
    try:
        for name in changed:
            path = paths().systemd_dir / name
            previous[name] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
    except OSError as exc:
        raise StateError("Не удалось сохранить прежние units", detail=str(exc)) from exc
    written = []
    reloaded = False
    try:
        for name in changed:
            text = unit_text(name, on_calendar=on_calendar, expire_on_calendar=expire_on_calendar)
            write_unit(paths().systemd_dir / name, text)
            written.append(name)
        if systemctl and changed:
            systemd.daemon_reload()
            reloaded = True
            for name in changed:
                _restart_changed(name)
    except Exception as exc:
        # После ошибки следующий sync должен снова видеть изменения и повторить
        # применение, а не сообщать об успехе по уже записанным файлам.
        failures = []
        for name in reversed(written):
            path = paths().systemd_dir / name
            try:
                old = previous[name]
                if old is None:
                    path.unlink()
                else:
                    data, mode = old
                    fs.replace_file(path, data, prepare=lambda candidate: candidate.chmod(mode))
            except OSError as restore_error:
                failures.append(f"{name}: {restore_error}")
        if reloaded:
            try:
                systemd.daemon_reload()
            except Exception as reload_error:
                failures.append(str(reload_error))
        if failures:
            raise StateError("Ошибка синхронизации; откат units неполный",
                             detail=f"{exc}; " + "; ".join(failures)) from exc
        raise
    return changed


def random_tls_service_unit(config_dir: Path | None = None) -> str:
    return unit_text("xray-caddy-random-tls@.service", config_dir=config_dir)


def random_tls_timer_unit(service_name: str = "xray-caddy-random-tls@%i.service") -> str:
    return unit_text("xray-caddy-random-tls@.timer", service_name=service_name)


def write_random_tls_systemd_units(systemd_dir: Path | None = None) -> dict[str, Path]:
    directory = systemd_dir if systemd_dir is not None else paths().systemd_dir
    service = directory / "xray-caddy-random-tls@.service"
    timer = directory / "xray-caddy-random-tls@.timer"
    try:
        write_unit(service, random_tls_service_unit())
        write_unit(timer, random_tls_timer_unit())
    except StateError as exc:
        raise OSError(exc.detail or exc.message) from exc
    return {"service": service, "timer": timer}
