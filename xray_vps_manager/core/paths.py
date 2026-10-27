"""Единый набор серверных путей и обратимый контекст подмены для тестов."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class Paths:
    """Неизменяемый набор путей; создание экземпляра не обращается к диску."""
    config_dir: Path
    config: Path
    server_env: Path
    manager_db: Path
    manager_lock: Path
    manager_update_lock: Path
    traffic_lock: Path
    activity_lock: Path
    db_backup_dir: Path
    warp_dir: Path
    caddy_random_tls_dir: Path
    xray_bin: Path
    asset_dir: Path
    log_dir: Path
    access_log: Path
    error_log: Path
    sbin_dir: Path
    lib_dir: Path
    bundle_dir: Path
    systemd_dir: Path
    backup_dir: Path
    export_dir: Path
    initial_link: Path
    caddyfile: Path
    caddy_conf_dir: Path
    sshd_config: Path
    sshd_dropin_dir: Path

    @classmethod
    def default(cls) -> Paths:
        return cls(
            config_dir=Path('/usr/local/etc/xray'),
            config=Path('/usr/local/etc/xray/config.json'),
            server_env=Path('/usr/local/etc/xray/server.env'),
            manager_db=Path('/usr/local/etc/xray/manager.db'),
            manager_lock=Path('/usr/local/etc/xray/manager.lock'),
            manager_update_lock=Path('/usr/local/etc/xray/manager-update.lock'),
            traffic_lock=Path('/usr/local/etc/xray/traffic.lock'),
            activity_lock=Path('/usr/local/etc/xray/activity.lock'),
            db_backup_dir=Path('/usr/local/etc/xray/manager-db-backups'),
            warp_dir=Path('/usr/local/etc/xray/warp'),
            caddy_random_tls_dir=Path('/usr/local/etc/xray/caddy-random-tls.d'),
            xray_bin=Path('/usr/local/bin/xray'),
            asset_dir=Path('/usr/local/share/xray'),
            log_dir=Path('/var/log/xray'),
            access_log=Path('/var/log/xray/access.log'),
            error_log=Path('/var/log/xray/error.log'),
            sbin_dir=Path('/usr/local/sbin'),
            lib_dir=Path('/usr/local/lib/xray-vps-manager'),
            bundle_dir=Path('/root/xray_server'),
            systemd_dir=Path('/etc/systemd/system'),
            backup_dir=Path('/root/xray_backups'),
            export_dir=Path('/root/xray_activity_exports'),
            initial_link=Path('/root/xray-reality-client.txt'),
            caddyfile=Path('/etc/caddy/Caddyfile'),
            caddy_conf_dir=Path('/etc/caddy/conf.d'),
            sshd_config=Path('/etc/ssh/sshd_config'),
            sshd_dropin_dir=Path('/etc/ssh/sshd_config.d'),
        )

    @classmethod
    def under(cls, root: Path) -> Paths:
        root = Path(root).absolute()
        default = cls.default()
        return cls(**{field.name: root / getattr(default, field.name).relative_to("/")
                      for field in fields(default)})


_current = Paths.default()


def paths() -> Paths:
    return _current


def _aliases(value: Paths) -> dict[str, Path]:
    return {
        "CONFIG_DIR": value.config_dir, "CONFIG_PATH": value.config,
        "SERVER_ENV_PATH": value.server_env, "XRAY_BIN": value.xray_bin,
        "XRAY_ASSET_DIR": value.asset_dir, "SBIN_DIR": value.sbin_dir,
        "XRAY_CLIENT": value.sbin_dir / "xray-client",
        "XRAY_ACTIVITY": value.sbin_dir / "xray-activity",
        "XRAY_TELEGRAM": value.sbin_dir / "xray-telegram",
        "XRAY_TRAFFIC_SYNC": value.sbin_dir / "xray-traffic-sync",
        "XRAY_TEST": value.sbin_dir / "xray-test",
        "MANAGER_LIB_DIR": value.lib_dir, "MANAGER_DB_PATH": value.manager_db,
        "MANAGER_LOCK_PATH": value.manager_lock,
        "MANAGER_UPDATE_LOCK_PATH": value.manager_update_lock,
    }


# Совместимые имена для ещё не перенесённых потребителей.
globals().update(_aliases(_current))


@contextmanager
def use(custom: Paths) -> Iterator[Paths]:
    """Подмена в одном потоке теста; скопированные импорты обслуживает Sandbox.

    Восстанавливаются исходный объект и точные значения алиасов, в том числе
    заданные внешним тестом. Вложенные контексты и исключения поддерживаются.
    """
    global _current
    previous = _current
    aliases = _aliases(custom)
    previous_aliases = {name: globals()[name] for name in aliases}
    try:
        _current = custom
        globals().update(aliases)
        yield custom
    finally:
        _current = previous
        globals().update(previous_aliases)
