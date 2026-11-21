"""Конфигурация и запуск опционального TLS randomizer для Caddy."""

import fcntl
import secrets
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from xray_vps_manager.caddy import model, service, sites
from xray_vps_manager.core.locks import manager_lock
from xray_vps_manager.core.paths import paths
from xray_vps_manager.core.settings import read_env, write_env

LEGACY_RANDOM_TLS_SERVICE = "xray-caddy-random-tls.service"
LEGACY_RANDOM_TLS_TIMER = "xray-caddy-random-tls.timer"
RANDOM_TLS_SERVICE_TEMPLATE = "xray-caddy-random-tls@.service"
RANDOM_TLS_TIMER_TEMPLATE = "xray-caddy-random-tls@.timer"


@dataclass(frozen=True)
class RandomTlsConfig:
    domain: str
    local_port: int


@dataclass(frozen=True)
class RandomTlsApplyResult:
    domain: str
    previous_tls_min_version: str
    previous_tls_max_version: str
    tls_min_version: str
    tls_max_version: str
    path: Path
    backup: Path | None
STRICT_RANDOM_TLS_PAIRS: tuple[tuple[str, str], ...] = (
    ("tls1.2", "tls1.2"),
    ("tls1.3", "tls1.3"),
)
RANDOM_TLS_ENV_KEYS = [
    "TLS_RANDOM_DOMAIN",
    "TLS_RANDOM_LOCAL_PORT",
]


def choose_random_tls_pair(
    current_min_version: str | None,
    current_max_version: str | None,
    *,
    chooser=secrets.choice,
) -> tuple[str, str]:
    current = model.normalize_tls_version_pair(current_min_version, current_max_version)
    options = list(STRICT_RANDOM_TLS_PAIRS)
    if current in options:
        options = [pair for pair in options if pair != current]
    return chooser(options)


def next_random_tls_label(current_min_version: str | None, current_max_version: str | None) -> str:
    current = model.normalize_tls_version_pair(current_min_version, current_max_version)
    if current == ("tls1.2", "tls1.2"):
        return model.tls_version_label("tls1.3", "tls1.3")
    if current == ("tls1.3", "tls1.3"):
        return model.tls_version_label("tls1.2", "tls1.2")
    return "Случайно: TLS 1.2 или TLS 1.3"


def random_tls_env_path(domain: str, config_dir: Path | None = None) -> Path:
    config_dir = paths().caddy_random_tls_dir if config_dir is None else config_dir
    return config_dir / f"{model.validate_domain(domain)}.env"


def random_tls_service_instance(domain: str) -> str:
    return f"xray-caddy-random-tls@{model.validate_domain(domain)}.service"


def random_tls_timer_instance(domain: str) -> str:
    return f"xray-caddy-random-tls@{model.validate_domain(domain)}.timer"


def list_random_tls_config_paths(config_dir: Path | None = None) -> list[Path]:
    config_dir = paths().caddy_random_tls_dir if config_dir is None else config_dir
    if not config_dir.exists():
        return []
    return sorted(path for path in config_dir.glob("*.env") if path.is_file())


def list_random_tls_configs(config_dir: Path | None = None) -> list[RandomTlsConfig]:
    config_dir = paths().caddy_random_tls_dir if config_dir is None else config_dir
    configs = []
    for path in list_random_tls_config_paths(config_dir):
        try:
            configs.append(read_random_tls_config(path))
        except (RuntimeError, ValueError):
            continue
    return configs


def read_random_tls_config(path: Path | None = None, *, domain: str | None = None) -> RandomTlsConfig:
    if path is None:
        if domain:
            path = random_tls_env_path(domain)
        else:
            configs = list_random_tls_configs()
            if len(configs) == 1:
                return configs[0]
            if len(configs) > 1:
                raise RuntimeError("Multiple Caddy TLS randomizer configs exist; pass --domain.")
            path = paths().caddy_random_tls_env
    values = read_env(path, strict=True, require_exists=True)
    domain = model.validate_domain(values.get("TLS_RANDOM_DOMAIN", ""))
    local_port = model.validate_port(values.get("TLS_RANDOM_LOCAL_PORT", ""))
    return RandomTlsConfig(domain=domain, local_port=local_port)


def write_random_tls_config(
    domain: str,
    local_port: int | str,
    path: Path | None = None,
) -> RandomTlsConfig:
    config = RandomTlsConfig(domain=model.validate_domain(domain), local_port=model.validate_port(local_port))
    if path is None:
        path = random_tls_env_path(config.domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_env(
        {
            "TLS_RANDOM_DOMAIN": config.domain,
            "TLS_RANDOM_LOCAL_PORT": str(config.local_port),
        },
        path,
        ordered_keys=RANDOM_TLS_ENV_KEYS,
    )
    return config


@contextmanager
def random_tls_lock(lock_path: Path | None = None):
    lock_path = paths().caddy_random_tls_lock if lock_path is None else lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def apply_random_tls_switch(
    config: RandomTlsConfig | None = None,
    *,
    domain: str | None = None,
    runner: service.CaddyCommandRunner | None = None,
    chooser=secrets.choice,
) -> RandomTlsApplyResult:
    config = config or read_random_tls_config(domain=domain)
    with manager_lock(purpose="randomize Caddy site TLS"), random_tls_lock():
        site = sites.site_config_for_domain(config.domain)
        local_port = site.local_port or config.local_port
        tls_min, tls_max = choose_random_tls_pair(site.tls_min_version, site.tls_max_version, chooser=chooser)
        result = service.update_site_config(
            config.domain,
            local_port,
            tls_min_version=tls_min,
            tls_max_version=tls_max,
            upstream_transport=site.upstream_transport,
            route_path=site.match_path,
            runner=runner,
        )
    return RandomTlsApplyResult(
        domain=config.domain,
        previous_tls_min_version=site.tls_min_version,
        previous_tls_max_version=site.tls_max_version,
        tls_min_version=tls_min,
        tls_max_version=tls_max,
        path=result.path,
        backup=result.backup,
    )


def enable_random_tls(
    domain: str,
    local_port: int,
    *,
    write_units: Callable[[], dict[str, Path]],
    runner: service.CaddyCommandRunner | None = None,
) -> tuple[RandomTlsConfig, dict[str, Path]]:
    config = write_random_tls_config(domain, local_port)
    units = write_units()
    service.enable_random_tls_timer(
        random_tls_timer_instance(config.domain),
        legacy_timer=LEGACY_RANDOM_TLS_TIMER,
        legacy_service=LEGACY_RANDOM_TLS_SERVICE,
        runner=runner,
    )
    return config, units


def disable_random_tls(config: RandomTlsConfig):
    return service.disable_random_tls_timer(
        random_tls_timer_instance(config.domain), random_tls_service_instance(config.domain),
    )
