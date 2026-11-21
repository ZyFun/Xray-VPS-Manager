"""Операции с процессом Caddy и применение конфигурации."""

import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from xray_vps_manager.caddy import model, sites
from xray_vps_manager.core import fs, process, systemd
from xray_vps_manager.core.errors import ConflictError, ManagerError, StateError
from xray_vps_manager.core.locks import manager_lock
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


@manager_lock(purpose="restore Caddy config")
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


@manager_lock(purpose="write Caddy site")
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


@manager_lock(purpose="update Caddy site TLS")
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


def binary_available() -> bool:
    return shutil.which("caddy") is not None


def install_caddy_if_needed(runner: CaddyCommandRunner | None = None) -> bool:
    if binary_available():
        return False
    if not shutil.which("apt-get"):
        raise RuntimeError("Caddy is not installed and apt-get is unavailable.")
    _run(["apt-get", "update"], runner=runner)
    _run(["apt-get", "install", "-y", "caddy"], runner=runner)
    return True


@dataclass(frozen=True)
class _CaddyActivation:
    active: bool
    enablement: str

    @classmethod
    def read(cls, *, before_install: bool = False):
        state = systemd.systemd_unit_activation_state("caddy", include_load_state=before_install)
        if before_install and state.get("LoadState") == "not-found":
            return cls(False, "disabled")
        active = state.get("ActiveState")
        enablement = state.get("UnitFileState")
        if not active or not enablement:
            raise StateError("Could not determine the active and enabled state of Caddy.")
        return cls(active in ("active", "reloading", "refreshing", "activating"), enablement)

    def restore_runtime(self, runner: CaddyCommandRunner | None) -> None:
        if self.active:
            validate_config(runner=runner)
            _reload_caddy_with_restart_fallback(runner)
        else:
            _run(["systemctl", "stop", "caddy"], runner=runner)

    def restore_enablement(self, runner: CaddyCommandRunner | None) -> None:
        _run(["systemctl", "disable", "caddy"], runner=runner)
        if self.enablement == "enabled-runtime":
            _run(["systemctl", "enable", "--runtime", "caddy"], runner=runner)

    def rollback_actions(self, runner: CaddyCommandRunner | None):
        actions = [("Служба Caddy", lambda: self.restore_runtime(runner))]
        if self.enablement in ("disabled", "enabled-runtime"):
            actions.append(("Автозагрузка Caddy", lambda: self.restore_enablement(runner)))
        return actions


@dataclass
class StagedCaddyPackage:
    archive: Path
    binary: Path
    initial_activation: _CaddyActivation | None = field(default=None, init=False)

    def install(self) -> None:
        if binary_available():
            raise ConflictError("Caddy was installed by another operation. Reload the connection settings and retry.")
        try:
            self.initial_activation = _CaddyActivation.read(before_install=True)
            # Пакетные скрипты не должны запускать службу с конфигом по умолчанию.
            process.run_stream(
                ["apt-get", "install", "-y", str(self.archive)],
                env={**os.environ, "SYSTEMD_OFFLINE": "1"}, check=True,
            )
            process.run_stream(["systemctl", "daemon-reload"], check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise StateError("Caddy installation failed while applying the connection.", detail=str(exc)) from exc

    def rollback_actions(self):
        if self.initial_activation is None or not binary_available():
            return []
        return self.initial_activation.rollback_actions(process.run_stream)


def stage_caddy_package(directory: Path) -> StagedCaddyPackage:
    """Загрузить пакет и извлечь бинарник для проверки без установки."""
    if not shutil.which("apt-get") or not shutil.which("dpkg-deb"):
        raise StateError("Caddy is not installed and apt-get or dpkg-deb is unavailable.")
    try:
        process.run_stream(["apt-get", "update"], check=True)
        process.run_stream(["apt-get", "download", "caddy"], cwd=directory, check=True)
        archives = list(directory.glob("caddy_*.deb"))
        if len(archives) != 1:
            raise StateError("Could not identify the downloaded Caddy package.")
        archive = archives[0]
        extracted = directory / "extracted"
        process.run_stream(["dpkg-deb", "--extract", str(archive), str(extracted)], check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StateError("Could not stage the Caddy package for site validation.", detail=str(exc)) from exc
    binary = extracted / "usr" / "bin" / "caddy"
    if not binary.is_file():
        raise StateError("Downloaded Caddy package does not contain /usr/bin/caddy.")
    return StagedCaddyPackage(archive, binary)


def validate_and_reload_caddy(runner: CaddyCommandRunner | None = None) -> None:
    validate_config(runner=runner)
    _run(["systemctl", "enable", "--now", "caddy"], runner=runner)
    _reload_caddy_with_restart_fallback(runner)


def _reload_caddy_with_restart_fallback(runner: CaddyCommandRunner | None) -> None:
    if _run(["systemctl", "reload", "caddy"], check=False, runner=runner) != 0:
        _run(["systemctl", "restart", "caddy"], runner=runner)


@dataclass(frozen=True)
class SiteCandidate:
    domain: str
    content: str
    previous_domain: str | None = None

    def validate(self, *, binary: str | Path = "caddy") -> None:
        """Проверить будущий site-файл без изменения рабочих файлов Caddy."""
        try:
            with TemporaryDirectory(prefix="xray-caddy-candidate-") as directory:
                candidate = Path(directory) / "Caddyfile"
                candidate.write_text(self.content)
                process.run([str(binary), "validate", "--config", str(candidate)], check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise StateError("Caddy site validation failed before applying the connection.",
                             detail=getattr(exc, "detail", "") or str(exc)) from exc

    def prepare_update(self, *, runner: CaddyCommandRunner | None = None) -> "SiteUpdate":
        target = sites.site_config_path(self.domain)
        previous_domain = self.previous_domain or None
        previous = sites.site_config_path(previous_domain) if previous_domain is not None else target
        creates_site = previous_domain is None or previous != target
        if creates_site:
            # Сценарий уже держит общую блокировку. После применения Xray
            # домен проверяется ещё раз, включая конфликт под другим именем файла.
            sites.require_site_config_absent(self.domain)
        paths_to_backup = (previous,) if previous_domain is not None else ()
        backups = {path: sites.backup_file(path) for path in paths_to_backup}
        backups.setdefault(target, None)
        caddyfile = paths().caddyfile
        return SiteUpdate(self, target, previous, backups, caddyfile, runner, creates_site)


@dataclass
class SiteUpdate:
    """Запись site с сохранённым состоянием для компенсации всей операции."""

    candidate: SiteCandidate
    target: Path
    previous: Path
    backups: dict[Path, Path | None]
    caddyfile: Path
    runner: CaddyCommandRunner | None
    creates_site: bool
    new_site_identity: tuple[int, int] | None = None
    caddyfile_change: tuple[bytes | None, bytes] | None = None
    site_overwrite_started: bool = False
    previous_delete_started: bool = False
    initial_activation: _CaddyActivation | None = None

    def _prepare_created_site(self, path: Path) -> None:
        path.chmod(0o644)
        info = path.stat()
        self.new_site_identity = (info.st_dev, info.st_ino)

    def _remove_created_site(self) -> None:
        try:
            info = self.target.lstat()
        except FileNotFoundError:
            return
        if (info.st_dev, info.st_ino) == self.new_site_identity:
            sites.delete_site_config(self.target, keep_backup=False)

    def _remember_caddyfile_write(self, before: bytes | None, after: bytes) -> None:
        self.caddyfile_change = before, after

    def _activate_caddy(self) -> None:
        self.initial_activation = _CaddyActivation.read()
        if self.initial_activation.enablement in ("disabled", "enabled-runtime", "enabled"):
            _run(["systemctl", "enable", "--now", "caddy"], runner=self.runner)
        else:
            _run(["systemctl", "start", "caddy"], runner=self.runner)

    def apply(self) -> None:
        try:
            if self.creates_site:
                fs.create_file(self.target, self.candidate.content.encode(), prepare=self._prepare_created_site)
                sites.ensure_caddyfile_import(self.caddyfile, before_write=self._remember_caddyfile_write)
            else:
                self.site_overwrite_started = True
                sites.write_site_text(
                    self.candidate.domain, self.candidate.content, caddyfile_path=self.caddyfile,
                    before_caddyfile_write=self._remember_caddyfile_write,
                )
            if self.previous != self.target:
                self.previous_delete_started = True
                sites.delete_site_config(self.previous, keep_backup=False)
            validate_config(runner=self.runner)
            self._activate_caddy()
            _run(["systemctl", "reload", "caddy"], runner=self.runner)
        except ConflictError:
            # Проверка ожидаемых байтов отказала до публикации Caddyfile.
            self.caddyfile_change = None
            raise
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise StateError("Caddy could not apply the connection site.", detail=str(exc)) from exc

    def _restore_caddyfile(self) -> None:
        before, written = self.caddyfile_change
        if sites.read_caddyfile(self.caddyfile) != before:
            sites.replace_caddyfile(self.caddyfile, before, expected=written)

    def rollback_actions(self, *, restore_activation: bool = True):
        actions = []
        if self.creates_site:
            if self.new_site_identity is not None:
                actions.append((f"Caddy site {self.target.name}", self._remove_created_site))
            if self.previous_delete_started:
                actions.append((f"Caddy site {self.previous.name}", lambda: sites.restore_file(
                    self.backups[self.previous], self.previous,
                )))
        elif self.site_overwrite_started:
            actions.append((f"Caddy site {self.target.name}", lambda: sites.restore_file(
                self.backups[self.target], self.target,
            )))
        if self.caddyfile_change is not None:
            actions.append(("Caddyfile", self._restore_caddyfile))
        if restore_activation and self.initial_activation is not None:
            actions.extend(self.initial_activation.rollback_actions(self.runner))
        return actions

    def prune_backups(self) -> None:
        sites.prune_site_backups(*self.backups, protect=tuple(self.backups.values()))


@manager_lock(purpose="create Caddy XHTTP site")
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


@manager_lock(purpose="create Caddy Trojan site")
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


@manager_lock(purpose="set up Caddy config")
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


@manager_lock(purpose="rename Caddy site")
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


@manager_lock(purpose="remove Caddy site")
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


@manager_lock(purpose="remove Caddy default HTTP site")
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
