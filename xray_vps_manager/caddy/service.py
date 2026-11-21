"""Операции с процессом Caddy и применение конфигурации."""

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from xray_vps_manager.caddy import model, sites
from xray_vps_manager.core import process
from xray_vps_manager.core.errors import ManagerError
from xray_vps_manager.core.paths import paths

class CaddyCommandRunner(Protocol):
    def __call__(self, command: list[str], *, check: bool = True) -> int: ...


def _run(command: list[str], *, check: bool = True, runner: CaddyCommandRunner | None = None) -> int:
    if runner is not None:
        return runner(command, check=check)
    result = process.run(command, timeout=None)
    if check:
        result.check_returncode()
    return result.returncode


def restore_config_backup(
    value: str,
    *,
    backup_dir: Path | None = None,
    caddyfile_path: Path | None = None,
    conf_dir: Path | None = None,
    runner: CaddyCommandRunner | None = None,
) -> tuple[Path, Path, list[Path]]:
    backup_dir = paths().caddy_backup_dir if backup_dir is None else backup_dir
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
    caddyfile_path = paths().caddyfile if caddyfile_path is None else caddyfile_path
    archive = sites.resolve_config_backup(value, backup_dir)
    pre_backup = sites.create_config_backup(
        backup_dir=backup_dir,
        caddyfile_path=caddyfile_path,
        conf_dir=conf_dir,
    )
    temp_dir = sites.extract_config_backup(archive)
    try:
        restored = sites.apply_config_restore(temp_dir, caddyfile_path=caddyfile_path, conf_dir=conf_dir)
        validate_and_reload_caddy(runner)
    except Exception:
        rollback_dir = sites.extract_config_backup(pre_backup)
        try:
            sites.apply_config_restore(rollback_dir, caddyfile_path=caddyfile_path, conf_dir=conf_dir)
            validate_and_reload_caddy(runner)
        finally:
            shutil.rmtree(rollback_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return archive, pre_backup, restored


def update_site_config(
    domain: str,
    local_port: int,
    *,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
    upstream_transport: str = "xhttp",
    route_path: str = "",
    runner: CaddyCommandRunner | None = None,
) -> model.SiteWriteResult:
    domain = model.validate_domain(domain)
    site_path = sites.site_config_path(domain)
    backup = sites.backup_file(site_path)
    try:
        written = sites.write_site_config(
            domain,
            local_port,
            tls_min_version=tls_min_version,
            tls_max_version=tls_max_version,
            upstream_transport=upstream_transport,
            route_path=route_path,
        )
        validate_and_reload_caddy(runner)
    except Exception:
        sites.restore_file(backup, site_path)
        try:
            validate_and_reload_caddy(runner)
        except Exception:
            pass
        raise
    sites.prune_site_backups(site_path, protect=(backup,))
    return model.SiteWriteResult(written, backup)


def update_site_tls_config(
    path: str | Path,
    *,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
    runner: CaddyCommandRunner | None = None,
) -> model.SiteWriteResult:
    site_path = Path(path)
    if not site_path.exists():
        raise FileNotFoundError(f"Caddy site config not found: {site_path}")
    backup = sites.backup_file(site_path)
    try:
        site_path.write_text(
            model.set_site_tls_versions_in_text(
                site_path.read_text(),
                tls_min_version,
                tls_max_version,
            )
        )
        validate_and_reload_caddy(runner)
    except Exception:
        sites.restore_file(backup, site_path)
        try:
            validate_and_reload_caddy(runner)
        except Exception:
            pass
        raise
    sites.prune_site_backups(site_path, protect=(backup,))
    return model.SiteWriteResult(site_path, backup)


def install_caddy_if_needed(runner: CaddyCommandRunner | None = None) -> bool:
    if shutil.which("caddy"):
        return False
    if not shutil.which("apt-get"):
        raise RuntimeError("Caddy is not installed and apt-get is unavailable.")
    _run(["apt-get", "update"], runner=runner)
    _run(["apt-get", "install", "-y", "caddy"], runner=runner)
    return True


def validate_and_reload_caddy(runner: CaddyCommandRunner | None = None) -> None:
    validate_config(runner=runner)
    _run(["systemctl", "enable", "--now", "caddy"], runner=runner)
    if _run(["systemctl", "reload", "caddy"], check=False, runner=runner) != 0:
        _run(["systemctl", "restart", "caddy"], runner=runner)


def setup_caddy_for_xhttp(
    domain: str,
    local_port: int,
    *,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
    install: bool = True,
    runner: CaddyCommandRunner | None = None,
) -> Path:
    sites.require_site_config_absent(domain)
    if install:
        install_caddy_if_needed(runner)
    site_path = sites.write_site_config(
        domain,
        local_port,
        tls_min_version=tls_min_version,
        tls_max_version=tls_max_version,
    )
    validate_and_reload_caddy(runner)
    return site_path


def setup_caddy_for_trojan_ws(
    domain: str,
    local_port: int,
    ws_path: str,
    *,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
    install: bool = True,
    runner: CaddyCommandRunner | None = None,
) -> Path:
    sites.require_site_config_absent(domain)
    if install:
        install_caddy_if_needed(runner)
    site_path = sites.write_site_config(
        domain,
        local_port,
        tls_min_version=tls_min_version,
        tls_max_version=tls_max_version,
        upstream_transport="http",
        route_path=ws_path,
    )
    validate_and_reload_caddy(runner)
    return site_path


def installed() -> bool:
    return process.run(["sh", "-c", "command -v caddy"], timeout=5).returncode == 0


def status(*, runner: CaddyCommandRunner | None = None) -> None:
    _run(["systemctl", "--no-pager", "--full", "status", "caddy.service"], runner=runner)


def validate_config(*, runner: CaddyCommandRunner | None = None) -> None:
    _run(["caddy", "validate", "--config", str(paths().caddyfile)], runner=runner)


def install_and_reload(*, runner: CaddyCommandRunner | None = None) -> bool:
    installed_now = install_caddy_if_needed(runner)
    sites.ensure_caddyfile_import()
    validate_and_reload_caddy(runner)
    return installed_now


def restart(*, runner: CaddyCommandRunner | None = None) -> None:
    validate_config(runner=runner)
    _run(["systemctl", "restart", "caddy"], runner=runner)


def show_logs(*, runner: CaddyCommandRunner | None = None) -> None:
    _run(["journalctl", "-u", "caddy", "-n", "120", "--no-pager"], runner=runner)


def check_tls_handshake(domain: str, version: str) -> subprocess.CompletedProcess[str]:
    return process.run(
        ["timeout", "10", "openssl", "s_client", "-connect", f"{domain}:443",
         "-servername", domain, f"-{version}", "-brief"],
        timeout=15,
    )


def query_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return process.run(["systemctl", *args], timeout=10)


def enable_random_tls_timer(
    timer: str, *, legacy_timer: str, legacy_service: str, runner: CaddyCommandRunner | None = None,
) -> None:
    _run(["systemctl", "daemon-reload"], runner=runner)
    process.run(["systemctl", "disable", "--now", legacy_timer], timeout=20)
    process.run(["systemctl", "stop", legacy_service], timeout=10)
    _run(["systemctl", "enable", "--now", timer], runner=runner)


def disable_random_tls_timer(timer: str, service: str) -> Iterator[tuple[list[str], subprocess.CompletedProcess[str]]]:
    for command in (["systemctl", "disable", "--now", timer], ["systemctl", "stop", service]):
        yield command, process.run(command, timeout=20)


def run_systemctl(*args: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    command = ["systemctl", *args]
    try:
        return process.run_capture(command, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(command, 127, "", "systemctl not found")


def restore_random_tls_timer(timer: str, unit: str, enabled: bool) -> subprocess.CompletedProcess[str]:
    if enabled:
        return run_systemctl("enable", "--now", timer)
    result = run_systemctl("disable", "--now", timer)
    run_systemctl("stop", unit, timeout=10)
    return result


class SiteChangeError(ManagerError):
    def __init__(self, cause: Exception, backup: Path | None):
        super().__init__(str(cause))
        self.backup = backup


def _restore_and_reload(*backups: tuple[Path | None, Path], runner: CaddyCommandRunner | None) -> None:
    for backup, path in backups:
        sites.restore_file(backup, path)
    try:
        validate_and_reload_caddy(runner)
    except Exception:
        pass


def rename_site(
    site: model.SiteConfig, new_domain: str, *, local_port: int, runner: CaddyCommandRunner | None = None,
) -> None:
    old_path = site.path
    new_path = sites.site_config_path(new_domain)
    old_backup = sites.backup_file(old_path)
    new_backup = sites.backup_file(new_path)
    try:
        sites.write_site_config(
            new_domain,
            local_port,
            tls_min_version=site.tls_min_version,
            tls_max_version=site.tls_max_version,
            upstream_transport=site.upstream_transport,
            route_path=site.match_path,
        )
        sites.delete_site_config(old_path, keep_backup=False)
        validate_and_reload_caddy(runner)
    except (OSError, subprocess.CalledProcessError) as exc:
        _restore_and_reload((old_backup, old_path), (new_backup, new_path), runner=runner)
        raise SiteChangeError(exc, old_backup) from exc
    sites.prune_site_backups(old_path, new_path, protect=(old_backup, new_backup))


def remove_site(path: Path, *, runner: CaddyCommandRunner | None = None) -> Path | None:
    backup = sites.backup_file(path)
    try:
        sites.delete_site_config(path, keep_backup=False)
        validate_and_reload_caddy(runner)
    except (OSError, subprocess.CalledProcessError) as exc:
        _restore_and_reload((backup, path), runner=runner)
        raise SiteChangeError(exc, backup) from exc
    sites.prune_site_backups(path, protect=(backup,))
    return backup


def remove_default_http_site(*, runner: CaddyCommandRunner | None = None) -> tuple[bool, Path | None]:
    path = paths().caddyfile
    backup = sites.backup_file(path)
    try:
        if not sites.remove_site_block_from_caddyfile(":80"):
            return False, backup
        validate_and_reload_caddy(runner)
    except (OSError, subprocess.CalledProcessError) as exc:
        _restore_and_reload((backup, path), runner=runner)
        raise SiteChangeError(exc, backup) from exc
    sites.prune_site_backups(path, protect=(backup,))
    return True, backup
