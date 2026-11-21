"""Файлы конфигурации, сайты и архивы Caddy."""

import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from subprocess import TimeoutExpired

from xray_vps_manager.caddy import model
from xray_vps_manager.core import fs, process
from xray_vps_manager.core.errors import ConflictError
from xray_vps_manager.core.paths import paths

SITE_BACKUP_KEEP = 5
CADDYFILE_ARCNAME = "etc/caddy/Caddyfile"
CADDY_CONF_DIR_ARCNAME = "etc/caddy/conf.d"
CADDY_SITE_ARCNAME = "site"
DANGEROUS_SITE_ROOTS = {
    Path("/"),
    Path("/bin"),
    Path("/boot"),
    Path("/dev"),
    Path("/etc"),
    Path("/home"),
    Path("/lib"),
    Path("/lib64"),
    Path("/proc"),
    Path("/root"),
    Path("/run"),
    Path("/sbin"),
    Path("/sys"),
    Path("/tmp"),
    Path("/usr"),
    Path("/var"),
}


def site_config_path(domain: str, conf_dir: Path | None = None) -> Path:
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
    return conf_dir / model.site_filename(domain)


def list_site_config_paths(conf_dir: Path | None = None) -> list[Path]:
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
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


def prune_site_backups(*paths: Path, protect: tuple[Path | None, ...] = ()) -> None:
    """After a successful change keep only the SITE_BACKUP_KEEP newest copies of each file (VIREI-241).

    ``protect`` are the copies made by this operation: they stay even if the clock went back.
    """
    for path in paths:
        fs.prune_backups(path, keep=SITE_BACKUP_KEEP, protect=protect)


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


def caddy_version() -> str:
    try:
        result = process.run(["caddy", "version"], timeout=5)
    except (FileNotFoundError, TimeoutExpired):
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


def safe_backup_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip("/ ") or "root").strip("_") or "site"


def create_config_backup(
    *,
    backup_dir: Path | None = None,
    caddyfile_path: Path | None = None,
    conf_dir: Path | None = None,
) -> Path:
    backup_dir = paths().caddy_backup_dir if backup_dir is None else backup_dir
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
    caddyfile_path = paths().caddyfile if caddyfile_path is None else caddyfile_path
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
    return archive


def config_backup_paths(backup_dir: Path | None = None) -> list[Path]:
    backup_dir = paths().caddy_backup_dir if backup_dir is None else backup_dir
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("caddy-backup-*.tar.gz"), reverse=True)


def config_backup_rows(backup_dir: Path | None = None) -> list[list[str]]:
    backup_dir = paths().caddy_backup_dir if backup_dir is None else backup_dir
    rows = []
    for path in config_backup_paths(backup_dir):
        try:
            stat = path.stat()
        except OSError:
            continue
        created = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        rows.append([str(path), created, model.format_size(stat.st_size)])
    return rows


def resolve_config_backup(value: str, backup_dir: Path | None = None) -> Path:
    backup_dir = paths().caddy_backup_dir if backup_dir is None else backup_dir
    path = Path(value).expanduser()
    if path.exists():
        return path
    path = backup_dir / value
    if path.exists():
        return path
    raise FileNotFoundError(f"Caddy backup archive not found: {value}")


def resolve_config_backup_for_delete(value: str, backup_dir: Path | None = None) -> Path:
    backup_dir = paths().caddy_backup_dir if backup_dir is None else backup_dir
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
    if not (member.isfile() or member.isdir()):
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
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                validate_archive_member(member)
            tar.extractall(temp_dir, filter="data")
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
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


def apply_config_restore(temp_dir: Path, *, caddyfile_path: Path | None = None, conf_dir: Path | None = None) -> list[Path]:
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
    caddyfile_path = paths().caddyfile if caddyfile_path is None else caddyfile_path
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


def delete_config_backup(value: str, backup_dir: Path | None = None) -> Path:
    backup_dir = paths().caddy_backup_dir if backup_dir is None else backup_dir
    archive = resolve_config_backup_for_delete(value, backup_dir)
    archive.unlink()
    return archive


def site_root_candidates(
    *,
    caddyfile_path: Path | None = None,
    conf_dir: Path | None = None,
) -> list[Path]:
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
    caddyfile_path = paths().caddyfile if caddyfile_path is None else caddyfile_path
    candidates: list[Path] = []
    sources = [caddyfile_path]
    sources.extend(list_site_config_paths(conf_dir))
    for source in sources:
        if not source.exists():
            continue
        text = source.read_text()
        for match in model.ROOT_DIRECTIVE_RE.finditer(text):
            path = Path(match.group("path")).expanduser()
            if path not in candidates:
                candidates.append(path)
    for fallback in paths().caddy_site_roots:
        if fallback.exists() and fallback not in candidates:
            candidates.append(fallback)
    return candidates


def validate_site_root(value: str | Path, *, must_exist: bool = True) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Site root must be an absolute path.")
    try:
        resolved = path.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Site root not found: {path}") from exc
    if resolved in DANGEROUS_SITE_ROOTS:
        raise ValueError(f"Refusing to use dangerous site root: {resolved}")
    if must_exist and not resolved.is_dir():
        raise ValueError(f"Site root must be a directory: {resolved}")
    if not must_exist and resolved.exists() and not resolved.is_dir():
        raise ValueError(f"Site root must be a directory: {resolved}")
    return resolved


def create_site_backup(
    site_root: str | Path,
    *,
    backup_dir: Path | None = None,
) -> tuple[Path, Path]:
    backup_dir = paths().caddy_site_backup_dir if backup_dir is None else backup_dir
    root = validate_site_root(site_root)
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)

    archive = backup_dir / f"caddy-site-backup-{safe_backup_label(str(root))}-{archive_stamp()}.tar.gz"
    if archive.exists():
        archive = backup_dir / f"caddy-site-backup-{safe_backup_label(str(root))}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S%fZ')}.tar.gz"

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(root, arcname=CADDY_SITE_ARCNAME, recursive=True)
        add_json(
            tar,
            "manifest.json",
            {
                "createdAt": utc_stamp(),
                "siteRoot": str(root),
                "size": tree_size(root),
                "warning": "This archive contains Caddy static site files only, not Caddy configuration or certificate cache files.",
            },
        )

    os.chmod(archive, 0o600)
    return archive, root


def site_backup_paths(backup_dir: Path | None = None) -> list[Path]:
    backup_dir = paths().caddy_site_backup_dir if backup_dir is None else backup_dir
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("caddy-site-backup-*.tar.gz"), reverse=True)


def backup_manifest(archive: Path) -> dict:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.getmember("manifest.json")
            extracted = tar.extractfile(member)
            if extracted is None:
                return {}
            return json.loads(extracted.read().decode())
    except (KeyError, OSError, tarfile.TarError, json.JSONDecodeError):
        return {}


def site_backup_rows(backup_dir: Path | None = None) -> list[list[str]]:
    backup_dir = paths().caddy_site_backup_dir if backup_dir is None else backup_dir
    rows = []
    for path in site_backup_paths(backup_dir):
        try:
            stat = path.stat()
        except OSError:
            continue
        manifest = backup_manifest(path)
        created = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        rows.append([str(path), manifest.get("siteRoot") or "-", created, model.format_size(stat.st_size)])
    return rows


def resolve_site_backup(value: str, backup_dir: Path | None = None) -> Path:
    backup_dir = paths().caddy_site_backup_dir if backup_dir is None else backup_dir
    path = Path(value).expanduser()
    if path.exists():
        return path
    path = backup_dir / value
    if path.exists():
        return path
    raise FileNotFoundError(f"Caddy site backup archive not found: {value}")


def resolve_site_backup_for_delete(value: str, backup_dir: Path | None = None) -> Path:
    backup_dir = paths().caddy_site_backup_dir if backup_dir is None else backup_dir
    archive = resolve_site_backup(value, backup_dir)
    backup_root = backup_dir.resolve()
    if archive.resolve().parent != backup_root:
        raise ValueError(f"Refusing to delete a file outside {backup_dir}: {archive}")
    if not archive.name.endswith(".tar.gz"):
        raise ValueError("Refusing to delete a file that does not look like a .tar.gz backup archive.")
    return archive


def validate_site_archive_member(member: tarfile.TarInfo) -> None:
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        raise ValueError(f"Unsafe archive member: {name}")
    if not (member.isfile() or member.isdir()):
        raise ValueError(f"Unsupported archive member type: {name}")
    allowed = name == "manifest.json" or name == CADDY_SITE_ARCNAME or name.startswith(CADDY_SITE_ARCNAME + "/")
    if not allowed:
        raise ValueError(f"Unexpected archive member: {name}")


def extract_site_backup(archive: Path) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="caddy-site-restore-"))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                validate_site_archive_member(member)
            tar.extractall(temp_dir, filter="data")
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return temp_dir


def set_site_tree_permissions(path: Path) -> None:
    chown_root(path)
    os.chmod(path, 0o755)
    for child in path.rglob("*"):
        chown_root(child)
        os.chmod(child, 0o755 if child.is_dir() else 0o644)


def restore_site_dir(source: Path, target: Path) -> None:
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError("Caddy site backup does not contain site files.")
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    set_site_tree_permissions(target)


def restore_site_backup(
    value: str,
    *,
    target_root: str | Path | None = None,
    backup_dir: Path | None = None,
) -> tuple[Path, Path | None, Path]:
    backup_dir = paths().caddy_site_backup_dir if backup_dir is None else backup_dir
    archive = resolve_site_backup(value, backup_dir)
    manifest = backup_manifest(archive)
    target = validate_site_root(target_root or manifest.get("siteRoot") or "", must_exist=False)
    pre_backup = None
    if target.exists():
        pre_backup, _ = create_site_backup(target, backup_dir=backup_dir)
    temp_dir = extract_site_backup(archive)
    try:
        restore_site_dir(temp_dir / CADDY_SITE_ARCNAME, target)
    except Exception:
        if pre_backup is not None:
            rollback_dir = extract_site_backup(pre_backup)
            try:
                restore_site_dir(rollback_dir / CADDY_SITE_ARCNAME, target)
            finally:
                shutil.rmtree(rollback_dir, ignore_errors=True)
        elif target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return archive, pre_backup, target


def delete_site_backup(value: str, backup_dir: Path | None = None) -> Path:
    backup_dir = paths().caddy_site_backup_dir if backup_dir is None else backup_dir
    archive = resolve_site_backup_for_delete(value, backup_dir)
    archive.unlink()
    return archive


def parse_site_config(path: Path) -> model.SiteConfig:
    text = path.read_text() if path.exists() else ""
    domain = model.first_site_address(text) or path.stem
    port_match = model.REVERSE_PROXY_RE.search(text)
    upstream_transport = "xhttp" if port_match and port_match.group("scheme") else "http"
    route_path = model.normalized_site_match_path(model.first_path_matcher_path(text), upstream_transport)
    protocols_match = model.PROTOCOLS_RE.search(text)
    modified_at = ""
    if path.exists():
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return model.SiteConfig(
        path=path,
        domain=domain,
        local_port=int(port_match.group("port")) if port_match else None,
        tls_min_version=protocols_match.group("min") if protocols_match else "default",
        tls_max_version=protocols_match.group("max") if protocols_match else "default",
        modified_at=modified_at,
        upstream_transport=upstream_transport,
        match_path=route_path,
    )


def list_site_configs(conf_dir: Path | None = None) -> list[model.SiteConfig]:
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
    return [parse_site_config(path) for path in list_site_config_paths(conf_dir)]


def site_config_for_domain(domain: str, conf_dir: Path | None = None) -> model.SiteConfig:
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
    site_path = site_config_path(domain, conf_dir)
    if not site_path.exists():
        raise FileNotFoundError(f"Caddy site config not found: {site_path}")
    return parse_site_config(site_path)


def site_config_conflicts_for_domain(domain: str, conf_dir: Path | None = None) -> list[model.SiteConfig]:
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
    domain = model.validate_domain(domain)
    target_path = site_config_path(domain, conf_dir)
    matches = []
    seen_paths = set()
    if target_path.exists():
        matches.append(parse_site_config(target_path))
        seen_paths.add(target_path)
    for path in list_site_config_paths(conf_dir):
        if path in seen_paths:
            continue
        site = parse_site_config(path)
        if site.domain.lower() == domain.lower():
            matches.append(site)
            seen_paths.add(path)
    return matches


def require_site_config_absent(domain: str, conf_dir: Path | None = None) -> None:
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
    domain = model.validate_domain(domain)
    conflicts = site_config_conflicts_for_domain(domain, conf_dir)
    if not conflicts:
        return
    details = "; ".join(model.format_site_config_conflict(site) for site in conflicts)
    raise FileExistsError(
        f"Caddy site config already exists for {domain}: {details}. "
        "Creating a new TLS connection would overwrite this site and may break existing traffic. "
        "Review, update, or delete the existing site config through Caddy / TLS -> Site configs first."
    )


def read_caddyfile(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def replace_caddyfile(path: Path, content: bytes | None, *, expected: bytes | None) -> None:
    """Меняет файл через атомарную запись с проверкой ожидаемых байтов."""
    target = path.resolve()

    def check_expected() -> None:
        if path.resolve() != target or read_caddyfile(target) != expected:
            raise ConflictError("Файл изменился после начала операции. Сохранена его текущая версия.")

    check_expected()
    if content is None:
        target.unlink()
    elif expected is None:
        try:
            fs.create_file(target, content)
        except FileExistsError as exc:
            raise ConflictError("Файл появился во время операции. Сохранена его текущая версия.") from exc
    else:
        info = target.stat()

        def prepare(candidate: Path) -> None:
            if os.geteuid() == 0:
                os.chown(candidate, info.st_uid, info.st_gid)
            candidate.chmod(stat.S_IMODE(info.st_mode))
            check_expected()

        fs.replace_file(target, content, prepare=prepare, staging_mode=0o600)


def ensure_caddyfile_import(
    caddyfile_path: Path | None = None, conf_dir: Path | None = None, *,
    before_write: Callable[[bytes | None, bytes], None] | None = None,
) -> None:
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
    caddyfile_path = paths().caddyfile if caddyfile_path is None else caddyfile_path
    import_line = f"import {conf_dir}/*.caddy".encode()
    before = read_caddyfile(caddyfile_path)
    if before is None:
        after = import_line + b"\n"
    else:
        if import_line in before:
            return
        suffix = b"" if before.endswith(b"\n") else b"\n"
        after = before + suffix + b"\n# Managed by Xray VPS Manager\n" + import_line + b"\n"
    if before_write is not None:
        before_write(before, after)
    replace_caddyfile(caddyfile_path, after, expected=before)


def write_site_config(
    domain: str,
    local_port: int,
    *,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
    upstream_transport: str = "xhttp",
    route_path: str = "",
    conf_dir: Path | None = None,
    caddyfile_path: Path | None = None,
) -> Path:
    content = model.caddy_site_block(
        domain,
        local_port,
        tls_min_version=tls_min_version,
        tls_max_version=tls_max_version,
        upstream_transport=upstream_transport,
        route_path=route_path,
    )
    return write_site_text(domain, content, conf_dir=conf_dir, caddyfile_path=caddyfile_path)


def write_site_text(
    domain: str, content: str, *, conf_dir: Path | None = None, caddyfile_path: Path | None = None,
    before_caddyfile_write: Callable[[bytes | None, bytes], None] | None = None,
) -> Path:
    conf_dir = paths().caddy_conf_dir if conf_dir is None else conf_dir
    caddyfile_path = paths().caddyfile if caddyfile_path is None else caddyfile_path
    domain = model.validate_domain(domain)
    conf_dir.mkdir(parents=True, exist_ok=True)
    ensure_caddyfile_import(caddyfile_path, conf_dir, before_write=before_caddyfile_write)
    site_path = conf_dir / model.site_filename(domain)
    site_path.write_text(content)
    os.chmod(site_path, 0o644)
    return site_path


def delete_site_config(
    domain: str | Path, conf_dir: Path | None = None, *, keep_backup: bool = True,
) -> Path | None:
    """Удалить site. Сценарий с откатом может сам управлять копией и её ротацией."""
    site_path = domain if isinstance(domain, Path) else site_config_path(domain, conf_dir)
    backup = backup_file(site_path) if keep_backup else None
    if site_path.exists():
        site_path.unlink()
    if keep_backup:
        prune_site_backups(site_path, protect=(backup,))
    return backup


def remove_site_block_from_caddyfile(address: str, caddyfile_path: Path | None = None) -> bool:
    caddyfile_path = paths().caddyfile if caddyfile_path is None else caddyfile_path
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
