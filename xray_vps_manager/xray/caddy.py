"""Caddy helpers for TLS-terminated XHTTP connections."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CADDYFILE_PATH = Path("/etc/caddy/Caddyfile")
CADDY_CONF_DIR = Path("/etc/caddy/conf.d")
CADDY_BACKUP_DIR = Path("/root/xray_caddy_backups")
CADDYFILE_ARCNAME = "etc/caddy/Caddyfile"
CADDY_CONF_DIR_ARCNAME = "etc/caddy/conf.d"
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+$")
TLS_VERSIONS = {"default", "tls1.2", "tls1.3"}
REVERSE_PROXY_RE = re.compile(r"reverse_proxy\s+h2c://127\.0\.0\.1:(?P<port>[0-9]+)")
PROTOCOLS_RE = re.compile(r"protocols\s+(?P<min>tls1\.[23])\s+(?P<max>tls1\.[23])")


@dataclass(frozen=True)
class SiteConfig:
    path: Path
    domain: str
    local_port: int | None
    tls_min_version: str
    tls_max_version: str


def validate_domain(value: str) -> str:
    domain = (value or "").strip().lower()
    if not domain or "/" in domain or ":" in domain or not DOMAIN_RE.fullmatch(domain):
        raise ValueError("TLS domain must be a domain without https://, path, or port.")
    return domain


def normalize_tls_version(value: str | None, default: str = "tls1.2") -> str:
    version = (value or default).strip().lower()
    if version not in TLS_VERSIONS:
        raise ValueError("TLS version must be one of: default, tls1.2, tls1.3")
    return version


def site_filename(domain: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", validate_domain(domain)) + ".caddy"


def site_config_path(domain: str, conf_dir: Path = CADDY_CONF_DIR) -> Path:
    return conf_dir / site_filename(domain)


def list_site_config_paths(conf_dir: Path = CADDY_CONF_DIR) -> list[Path]:
    if not conf_dir.exists():
        return []
    return sorted(path for path in conf_dir.glob("*.caddy") if path.is_file())


def backup_path(path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return path.with_name(f"{path.name}.bak.{timestamp}")


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = backup_path(path)
    shutil.copy2(path, backup)
    return backup


def restore_file(backup: Path | None, path: Path) -> None:
    if backup is None:
        if path.exists():
            path.unlink()
        return
    shutil.copy2(backup, path)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def archive_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def format_size(value: int) -> str:
    value = int(value or 0)
    if value < 1024:
        return f"{value}B"
    for suffix, size in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        next_size = size * 1024
        if value < next_size or suffix == "GB":
            return f"{value / size:.2f}{suffix}"
    return f"{value}B"


def caddy_version(runner=subprocess.run) -> str:
    try:
        result = runner(["caddy", "version"], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    output = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0] if output else "unknown"


def add_json(tar: tarfile.TarFile, name: str, payload: dict) -> None:
    data = json.dumps(payload, indent=2, ensure_ascii=False).encode() + b"\n"
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, io.BytesIO(data))


def tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())


def create_config_backup(
    *,
    backup_dir: Path = CADDY_BACKUP_DIR,
    caddyfile_path: Path = CADDYFILE_PATH,
    conf_dir: Path = CADDY_CONF_DIR,
    path_only: bool = False,
    quiet: bool = False,
) -> Path:
    if not caddyfile_path.exists():
        raise FileNotFoundError(f"Caddyfile not found: {caddyfile_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)

    archive = backup_dir / f"caddy-backup-{archive_stamp()}.tar.gz"
    if archive.exists():
        archive = backup_dir / f"caddy-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S%fZ')}.tar.gz"

    files = []
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(caddyfile_path, arcname=CADDYFILE_ARCNAME, recursive=False)
        files.append({"source": str(caddyfile_path), "archive": CADDYFILE_ARCNAME, "size": caddyfile_path.stat().st_size})

        if conf_dir.exists():
            tar.add(conf_dir, arcname=CADDY_CONF_DIR_ARCNAME, recursive=True)
            files.append({"source": str(conf_dir), "archive": CADDY_CONF_DIR_ARCNAME, "size": tree_size(conf_dir)})

        add_json(
            tar,
            "manifest.json",
            {
                "createdAt": utc_stamp(),
                "caddyVersion": caddy_version(),
                "files": files,
                "warning": "This archive contains Caddy configuration only, not certificate cache files.",
            },
        )

    os.chmod(archive, 0o600)
    if path_only:
        print(archive)
    elif not quiet:
        print(f"Caddy backup created: {archive}")
        print(f"Size: {format_size(archive.stat().st_size)}")
        print("Contains: /etc/caddy/Caddyfile and /etc/caddy/conf.d when present.")
        print("Certificate cache files are not included; Caddy can issue certificates again.")
    return archive


def config_backup_paths(backup_dir: Path = CADDY_BACKUP_DIR) -> list[Path]:
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("caddy-backup-*.tar.gz"), reverse=True)


def config_backup_rows(backup_dir: Path = CADDY_BACKUP_DIR) -> list[list[str]]:
    rows = []
    for path in config_backup_paths(backup_dir):
        try:
            stat = path.stat()
        except OSError:
            continue
        created = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        rows.append([str(path), created, format_size(stat.st_size)])
    return rows


def resolve_config_backup(value: str, backup_dir: Path = CADDY_BACKUP_DIR) -> Path:
    path = Path(value).expanduser()
    if path.exists():
        return path
    path = backup_dir / value
    if path.exists():
        return path
    raise FileNotFoundError(f"Caddy backup archive not found: {value}")


def resolve_config_backup_for_delete(value: str, backup_dir: Path = CADDY_BACKUP_DIR) -> Path:
    archive = resolve_config_backup(value, backup_dir)
    backup_root = backup_dir.resolve()
    if archive.resolve().parent != backup_root:
        raise ValueError(f"Refusing to delete a file outside {backup_dir}: {archive}")
    if not archive.name.endswith(".tar.gz"):
        raise ValueError("Refusing to delete a file that does not look like a .tar.gz backup archive.")
    return archive


def validate_archive_member(member: tarfile.TarInfo) -> None:
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        raise ValueError(f"Unsafe archive member: {name}")
    if member.issym() or member.islnk() or member.isdev():
        raise ValueError(f"Unsupported archive member type: {name}")
    allowed = (
        name == "manifest.json"
        or name == CADDYFILE_ARCNAME
        or name == CADDY_CONF_DIR_ARCNAME
        or name.startswith(CADDY_CONF_DIR_ARCNAME + "/")
    )
    if not allowed:
        raise ValueError(f"Unexpected archive member: {name}")


def extract_config_backup(archive: Path) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="caddy-restore-"))
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            validate_archive_member(member)
        tar.extractall(temp_dir)
    return temp_dir


def chown_root(path: Path) -> None:
    try:
        shutil.chown(path, user="root", group="root")
    except LookupError:
        try:
            shutil.chown(path, user="root")
        except PermissionError:
            pass
    except PermissionError:
        pass


def restore_caddy_file(source: Path, target: Path, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    chown_root(target)
    os.chmod(target, mode)


def restore_caddy_dir(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    chown_root(target)
    os.chmod(target, 0o755)
    for child in target.rglob("*"):
        chown_root(child)
        os.chmod(child, 0o755 if child.is_dir() else 0o644)


def apply_config_restore(temp_dir: Path, *, caddyfile_path: Path = CADDYFILE_PATH, conf_dir: Path = CADDY_CONF_DIR) -> list[Path]:
    source_caddyfile = temp_dir / CADDYFILE_ARCNAME
    if not source_caddyfile.exists():
        raise FileNotFoundError("Caddy backup does not contain Caddyfile.")

    restored = []
    restore_caddy_file(source_caddyfile, caddyfile_path, 0o644)
    restored.append(caddyfile_path)

    source_conf_dir = temp_dir / CADDY_CONF_DIR_ARCNAME
    if source_conf_dir.exists():
        restore_caddy_dir(source_conf_dir, conf_dir)
        restored.append(conf_dir)
    return restored


def restore_config_backup(
    value: str,
    *,
    backup_dir: Path = CADDY_BACKUP_DIR,
    caddyfile_path: Path = CADDYFILE_PATH,
    conf_dir: Path = CADDY_CONF_DIR,
    validator=None,
) -> tuple[Path, Path, list[Path]]:
    archive = resolve_config_backup(value, backup_dir)
    pre_backup = create_config_backup(
        backup_dir=backup_dir,
        caddyfile_path=caddyfile_path,
        conf_dir=conf_dir,
        quiet=True,
    )
    validate = validator or (lambda: validate_and_reload_caddy(subprocess.run))
    temp_dir = extract_config_backup(archive)
    try:
        restored = apply_config_restore(temp_dir, caddyfile_path=caddyfile_path, conf_dir=conf_dir)
        validate()
    except Exception:
        rollback_dir = extract_config_backup(pre_backup)
        try:
            apply_config_restore(rollback_dir, caddyfile_path=caddyfile_path, conf_dir=conf_dir)
            validate()
        finally:
            shutil.rmtree(rollback_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return archive, pre_backup, restored


def delete_config_backup(value: str, backup_dir: Path = CADDY_BACKUP_DIR) -> Path:
    archive = resolve_config_backup_for_delete(value, backup_dir)
    archive.unlink()
    return archive


def first_site_address(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("{"):
            return stripped[:-1].strip()
    return ""


def parse_site_config(path: Path) -> SiteConfig:
    text = path.read_text() if path.exists() else ""
    domain = first_site_address(text) or path.stem
    port_match = REVERSE_PROXY_RE.search(text)
    protocols_match = PROTOCOLS_RE.search(text)
    return SiteConfig(
        path=path,
        domain=domain,
        local_port=int(port_match.group("port")) if port_match else None,
        tls_min_version=protocols_match.group("min") if protocols_match else "default",
        tls_max_version=protocols_match.group("max") if protocols_match else "default",
    )


def list_site_configs(conf_dir: Path = CADDY_CONF_DIR) -> list[SiteConfig]:
    return [parse_site_config(path) for path in list_site_config_paths(conf_dir)]


def caddy_site_block(
    domain: str,
    local_port: int,
    *,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
) -> str:
    domain = validate_domain(domain)
    tls_min_version = normalize_tls_version(tls_min_version, "tls1.2")
    tls_max_version = normalize_tls_version(tls_max_version, tls_min_version)
    tls_block = ""
    if tls_min_version != "default":
        tls_block = (
            "    tls {\n"
            f"        protocols {tls_min_version} {tls_max_version}\n"
            "    }\n\n"
        )
    return (
        f"{domain} {{\n"
        f"{tls_block}"
        f"    reverse_proxy h2c://127.0.0.1:{local_port}\n"
        "}\n"
    )


def ensure_caddyfile_import(caddyfile_path: Path = CADDYFILE_PATH, conf_dir: Path = CADDY_CONF_DIR) -> None:
    caddyfile_path.parent.mkdir(parents=True, exist_ok=True)
    import_line = f"import {conf_dir}/*.caddy"
    if not caddyfile_path.exists():
        caddyfile_path.write_text(import_line + "\n")
        return
    content = caddyfile_path.read_text()
    if import_line in content:
        return
    suffix = "" if content.endswith("\n") else "\n"
    caddyfile_path.write_text(content + suffix + "\n# Managed by Xray VPS Manager\n" + import_line + "\n")


def write_site_config(
    domain: str,
    local_port: int,
    *,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
    conf_dir: Path = CADDY_CONF_DIR,
    caddyfile_path: Path = CADDYFILE_PATH,
) -> Path:
    domain = validate_domain(domain)
    conf_dir.mkdir(parents=True, exist_ok=True)
    ensure_caddyfile_import(caddyfile_path, conf_dir)
    site_path = conf_dir / site_filename(domain)
    site_path.write_text(
        caddy_site_block(
            domain,
            local_port,
            tls_min_version=tls_min_version,
            tls_max_version=tls_max_version,
        )
    )
    os.chmod(site_path, 0o644)
    return site_path


def delete_site_config(domain: str, conf_dir: Path = CADDY_CONF_DIR) -> Path | None:
    site_path = site_config_path(domain, conf_dir)
    backup = backup_file(site_path)
    if site_path.exists():
        site_path.unlink()
    return backup


def remove_site_block_from_caddyfile(address: str, caddyfile_path: Path = CADDYFILE_PATH) -> bool:
    if not caddyfile_path.exists():
        return False
    text = caddyfile_path.read_text()
    lines = text.splitlines(keepends=True)
    target = address.strip()
    start = None
    depth = 0
    end = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start is None:
            if stripped == f"{target} {{":
                start = index
                depth = stripped.count("{") - stripped.count("}")
                if depth <= 0:
                    end = index + 1
                    break
            continue
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            end = index + 1
            break
    if start is None or end is None:
        return False
    del lines[start:end]
    while lines and lines[0].strip() == "":
        lines.pop(0)
    caddyfile_path.write_text("".join(lines))
    return True


def install_caddy_if_needed(runner=subprocess.run) -> bool:
    if shutil.which("caddy"):
        return False
    if not shutil.which("apt-get"):
        raise RuntimeError("Caddy is not installed and apt-get is unavailable.")
    runner(["apt-get", "update"], check=True)
    runner(["apt-get", "install", "-y", "caddy"], check=True)
    return True


def validate_and_reload_caddy(runner=subprocess.run) -> None:
    runner(["caddy", "validate", "--config", str(CADDYFILE_PATH)], check=True)
    runner(["systemctl", "enable", "--now", "caddy"], check=True)
    result = runner(["systemctl", "reload", "caddy"], check=False)
    if result.returncode != 0:
        runner(["systemctl", "restart", "caddy"], check=True)


def setup_caddy_for_xhttp(
    domain: str,
    local_port: int,
    *,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
    install: bool = True,
    runner=subprocess.run,
) -> Path:
    if install:
        install_caddy_if_needed(runner)
    site_path = write_site_config(
        domain,
        local_port,
        tls_min_version=tls_min_version,
        tls_max_version=tls_max_version,
    )
    validate_and_reload_caddy(runner)
    return site_path
