"""Caddy helpers for TLS-terminated XHTTP connections."""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from xray_vps_manager.core.server_env import read_server_env, write_server_env

CADDYFILE_PATH = Path("/etc/caddy/Caddyfile")
CADDY_CONF_DIR = Path("/etc/caddy/conf.d")
CADDY_BACKUP_DIR = Path("/root/xray_caddy_backups")
CADDY_SITE_BACKUP_DIR = Path("/root/xray_caddy_site_backups")
CADDY_RANDOM_TLS_ENV_PATH = Path("/usr/local/etc/xray/caddy-random-tls.env")
SYSTEMD_DIR = Path("/etc/systemd/system")
RANDOM_TLS_SERVICE = "xray-caddy-random-tls.service"
RANDOM_TLS_TIMER = "xray-caddy-random-tls.timer"
CADDYFILE_ARCNAME = "etc/caddy/Caddyfile"
CADDY_CONF_DIR_ARCNAME = "etc/caddy/conf.d"
CADDY_SITE_ARCNAME = "site"
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+$")
TLS_VERSIONS = {"default", "tls1.2", "tls1.3"}
REVERSE_PROXY_RE = re.compile(r"reverse_proxy\s+h2c://127\.0\.0\.1:(?P<port>[0-9]+)")
PROTOCOLS_RE = re.compile(r"protocols\s+(?P<min>tls1\.[23])\s+(?P<max>tls1\.[23])")
ROOT_DIRECTIVE_RE = re.compile(r"(?m)^\s*root\s+(?:\*\s+)?(?P<path>/\S+)")
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


@dataclass(frozen=True)
class SiteConfig:
    path: Path
    domain: str
    local_port: int | None
    tls_min_version: str
    tls_max_version: str
    modified_at: str = ""


@dataclass(frozen=True)
class TlsVersionChoice:
    key: str
    label: str
    tls_min_version: str
    tls_max_version: str


@dataclass(frozen=True)
class SiteWriteResult:
    path: Path
    backup: Path | None


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


TLS_VERSION_CHOICES: tuple[TlsVersionChoice, ...] = (
    TlsVersionChoice("default", "Caddy default", "default", "default"),
    TlsVersionChoice("tls12", "TLS 1.2", "tls1.2", "tls1.2"),
    TlsVersionChoice("tls12_13", "TLS 1.2 + TLS 1.3", "tls1.2", "tls1.3"),
    TlsVersionChoice("tls13", "TLS 1.3", "tls1.3", "tls1.3"),
)
STRICT_RANDOM_TLS_PAIRS: tuple[tuple[str, str], ...] = (
    ("tls1.2", "tls1.2"),
    ("tls1.3", "tls1.3"),
)
RANDOM_TLS_ENV_KEYS = [
    "TLS_RANDOM_DOMAIN",
    "TLS_RANDOM_LOCAL_PORT",
]


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


def normalize_tls_version_pair(tls_min_version: str | None, tls_max_version: str | None) -> tuple[str, str]:
    tls_min = normalize_tls_version(tls_min_version, "tls1.2")
    tls_max = normalize_tls_version(tls_max_version, tls_min)
    if "default" in (tls_min, tls_max) and (tls_min, tls_max) != ("default", "default"):
        raise ValueError("TLS default must be selected for both min and max versions.")
    if (tls_min, tls_max) == ("tls1.3", "tls1.2"):
        raise ValueError("TLS max version must not be lower than TLS min version.")
    return tls_min, tls_max


def tls_version_choice(value: str) -> TlsVersionChoice:
    key = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "caddy": "default",
        "auto": "default",
        "tls1.2": "tls12",
        "tls12": "tls12",
        "tls_12": "tls12",
        "tls1.2+tls1.3": "tls12_13",
        "tls1.2_1.3": "tls12_13",
        "tls12_13": "tls12_13",
        "tls1.3": "tls13",
        "tls13": "tls13",
        "tls_13": "tls13",
    }
    key = aliases.get(key, key)
    for choice in TLS_VERSION_CHOICES:
        if choice.key == key:
            return choice
    raise ValueError("TLS choice must be one of: " + ", ".join(choice.key for choice in TLS_VERSION_CHOICES))


def tls_version_label(tls_min_version: str | None, tls_max_version: str | None) -> str:
    tls_min, tls_max = normalize_tls_version_pair(tls_min_version, tls_max_version)
    for choice in TLS_VERSION_CHOICES:
        if (choice.tls_min_version, choice.tls_max_version) == (tls_min, tls_max):
            return choice.label
    return f"{tls_min}..{tls_max}"


def tls_version_choice_key(tls_min_version: str | None, tls_max_version: str | None) -> str:
    tls_min, tls_max = normalize_tls_version_pair(tls_min_version, tls_max_version)
    for choice in TLS_VERSION_CHOICES:
        if (choice.tls_min_version, choice.tls_max_version) == (tls_min, tls_max):
            return choice.key
    return ""


def validate_port(value: int | str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("TLS random local port must be a number from 1 to 65535.") from exc
    if port < 1 or port > 65535:
        raise ValueError("TLS random local port must be a number from 1 to 65535.")
    return port


def choose_random_tls_pair(
    current_min_version: str | None,
    current_max_version: str | None,
    *,
    chooser=secrets.choice,
) -> tuple[str, str]:
    current = normalize_tls_version_pair(current_min_version, current_max_version)
    options = list(STRICT_RANDOM_TLS_PAIRS)
    if current in options:
        options = [pair for pair in options if pair != current]
    return chooser(options)


def next_random_tls_label(current_min_version: str | None, current_max_version: str | None) -> str:
    current = normalize_tls_version_pair(current_min_version, current_max_version)
    if current == ("tls1.2", "tls1.2"):
        return tls_version_label("tls1.3", "tls1.3")
    if current == ("tls1.3", "tls1.3"):
        return tls_version_label("tls1.2", "tls1.2")
    return "Случайно: TLS 1.2 или TLS 1.3"


def read_random_tls_config(path: Path = CADDY_RANDOM_TLS_ENV_PATH) -> RandomTlsConfig:
    values = read_server_env(path, strict=True, require_exists=True)
    domain = validate_domain(values.get("TLS_RANDOM_DOMAIN", ""))
    local_port = validate_port(values.get("TLS_RANDOM_LOCAL_PORT", ""))
    return RandomTlsConfig(domain=domain, local_port=local_port)


def write_random_tls_config(
    domain: str,
    local_port: int | str,
    path: Path = CADDY_RANDOM_TLS_ENV_PATH,
) -> RandomTlsConfig:
    config = RandomTlsConfig(domain=validate_domain(domain), local_port=validate_port(local_port))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_server_env(
        {
            "TLS_RANDOM_DOMAIN": config.domain,
            "TLS_RANDOM_LOCAL_PORT": str(config.local_port),
        },
        path,
        ordered_keys=RANDOM_TLS_ENV_KEYS,
    )
    return config


def random_tls_service_unit(env_path: Path = CADDY_RANDOM_TLS_ENV_PATH) -> str:
    return (
        "[Unit]\n"
        "Description=Randomize Caddy TLS protocol profile for Xray VPS Manager\n"
        "After=network-online.target caddy.service\n"
        "Wants=network-online.target\n"
        f"ConditionPathExists={env_path}\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"EnvironmentFile={env_path}\n"
        "ExecStart=/usr/local/sbin/xray-vps-manager caddy random-tls-run --quiet\n"
    )


def random_tls_timer_unit(service_name: str = RANDOM_TLS_SERVICE) -> str:
    return (
        "[Unit]\n"
        "Description=Randomize Caddy TLS protocol profile every 15-60 minutes\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=15min\n"
        "OnUnitActiveSec=15min\n"
        "RandomizedDelaySec=45min\n"
        "AccuracySec=1min\n"
        f"Unit={service_name}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def write_random_tls_systemd_units(systemd_dir: Path = SYSTEMD_DIR) -> dict[str, Path]:
    systemd_dir.mkdir(parents=True, exist_ok=True)
    service_path = systemd_dir / RANDOM_TLS_SERVICE
    timer_path = systemd_dir / RANDOM_TLS_TIMER
    service_path.write_text(random_tls_service_unit())
    timer_path.write_text(random_tls_timer_unit())
    os.chmod(service_path, 0o644)
    os.chmod(timer_path, 0o644)
    return {
        "service": service_path,
        "timer": timer_path,
    }


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


def safe_backup_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip("/ ") or "root").strip("_") or "site"


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


def site_root_candidates(
    *,
    caddyfile_path: Path = CADDYFILE_PATH,
    conf_dir: Path = CADDY_CONF_DIR,
) -> list[Path]:
    candidates: list[Path] = []
    sources = [caddyfile_path]
    sources.extend(list_site_config_paths(conf_dir))
    for source in sources:
        if not source.exists():
            continue
        text = source.read_text()
        for match in ROOT_DIRECTIVE_RE.finditer(text):
            path = Path(match.group("path")).expanduser()
            if path not in candidates:
                candidates.append(path)
    for fallback in (Path("/usr/share/caddy"), Path("/var/www"), Path("/var/www/html"), Path("/srv/www")):
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
    backup_dir: Path = CADDY_SITE_BACKUP_DIR,
    quiet: bool = False,
) -> Path:
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
    if not quiet:
        print(f"Caddy site backup created: {archive}")
        print(f"Site root: {root}")
        print(f"Size: {format_size(archive.stat().st_size)}")
        print("Contains: static site files from the selected site root.")
    return archive


def site_backup_paths(backup_dir: Path = CADDY_SITE_BACKUP_DIR) -> list[Path]:
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


def site_backup_rows(backup_dir: Path = CADDY_SITE_BACKUP_DIR) -> list[list[str]]:
    rows = []
    for path in site_backup_paths(backup_dir):
        try:
            stat = path.stat()
        except OSError:
            continue
        manifest = backup_manifest(path)
        created = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        rows.append([str(path), manifest.get("siteRoot") or "-", created, format_size(stat.st_size)])
    return rows


def resolve_site_backup(value: str, backup_dir: Path = CADDY_SITE_BACKUP_DIR) -> Path:
    path = Path(value).expanduser()
    if path.exists():
        return path
    path = backup_dir / value
    if path.exists():
        return path
    raise FileNotFoundError(f"Caddy site backup archive not found: {value}")


def resolve_site_backup_for_delete(value: str, backup_dir: Path = CADDY_SITE_BACKUP_DIR) -> Path:
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
    if member.issym() or member.islnk() or member.isdev():
        raise ValueError(f"Unsupported archive member type: {name}")
    allowed = name == "manifest.json" or name == CADDY_SITE_ARCNAME or name.startswith(CADDY_SITE_ARCNAME + "/")
    if not allowed:
        raise ValueError(f"Unexpected archive member: {name}")


def extract_site_backup(archive: Path) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="caddy-site-restore-"))
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            validate_site_archive_member(member)
        tar.extractall(temp_dir)
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
    backup_dir: Path = CADDY_SITE_BACKUP_DIR,
) -> tuple[Path, Path | None, Path]:
    archive = resolve_site_backup(value, backup_dir)
    manifest = backup_manifest(archive)
    target = validate_site_root(target_root or manifest.get("siteRoot") or "", must_exist=False)
    pre_backup = create_site_backup(target, backup_dir=backup_dir, quiet=True) if target.exists() else None
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


def delete_site_backup(value: str, backup_dir: Path = CADDY_SITE_BACKUP_DIR) -> Path:
    archive = resolve_site_backup_for_delete(value, backup_dir)
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
    modified_at = ""
    if path.exists():
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return SiteConfig(
        path=path,
        domain=domain,
        local_port=int(port_match.group("port")) if port_match else None,
        tls_min_version=protocols_match.group("min") if protocols_match else "default",
        tls_max_version=protocols_match.group("max") if protocols_match else "default",
        modified_at=modified_at,
    )


def list_site_configs(conf_dir: Path = CADDY_CONF_DIR) -> list[SiteConfig]:
    return [parse_site_config(path) for path in list_site_config_paths(conf_dir)]


def site_config_for_domain(domain: str, conf_dir: Path = CADDY_CONF_DIR) -> SiteConfig:
    site_path = site_config_path(domain, conf_dir)
    if not site_path.exists():
        raise FileNotFoundError(f"Caddy site config not found: {site_path}")
    return parse_site_config(site_path)


def caddy_site_block(
    domain: str,
    local_port: int,
    *,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
) -> str:
    domain = validate_domain(domain)
    tls_min_version, tls_max_version = normalize_tls_version_pair(tls_min_version, tls_max_version)
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


def update_site_config(
    domain: str,
    local_port: int,
    *,
    tls_min_version: str = "tls1.2",
    tls_max_version: str = "tls1.2",
    runner=subprocess.run,
) -> SiteWriteResult:
    domain = validate_domain(domain)
    site_path = site_config_path(domain)
    backup = backup_file(site_path)
    try:
        written = write_site_config(domain, local_port, tls_min_version=tls_min_version, tls_max_version=tls_max_version)
        validate_and_reload_caddy(runner)
    except Exception:
        restore_file(backup, site_path)
        try:
            validate_and_reload_caddy(runner)
        except Exception:
            pass
        raise
    return SiteWriteResult(written, backup)


def apply_random_tls_switch(
    config: RandomTlsConfig | None = None,
    *,
    runner=subprocess.run,
    chooser=secrets.choice,
) -> RandomTlsApplyResult:
    config = config or read_random_tls_config()
    site = site_config_for_domain(config.domain)
    local_port = site.local_port or config.local_port
    tls_min, tls_max = choose_random_tls_pair(site.tls_min_version, site.tls_max_version, chooser=chooser)
    result = update_site_config(config.domain, local_port, tls_min_version=tls_min, tls_max_version=tls_max, runner=runner)
    return RandomTlsApplyResult(
        domain=config.domain,
        previous_tls_min_version=site.tls_min_version,
        previous_tls_max_version=site.tls_max_version,
        tls_min_version=tls_min,
        tls_max_version=tls_max,
        path=result.path,
        backup=result.backup,
    )


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
