#!/usr/bin/env python3
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from xray_vps_manager.core.server_env import ORDERED_ENV_KEYS, read_server_env, write_server_env
from xray_vps_manager.core.process import restart_systemd_unit
from xray_vps_manager.core.terminal import print_table
from xray_vps_manager.db import database as sqlite_database
from xray_vps_manager.xray import caddy as xray_caddy

BACKUP_DIR = Path("/root/xray_backups")
CONFIG_DIR = Path("/usr/local/etc/xray")
CONFIG_PATH = CONFIG_DIR / "config.json"
SERVER_ENV_PATH = CONFIG_DIR / "server.env"
MANAGER_DB_PATH = CONFIG_DIR / "manager.db"
SERVER_ENV_ARCNAME = "usr/local/etc/xray/server.env"
MANAGER_DB_ARCNAME = "usr/local/etc/xray/manager.db"
CADDY_RANDOM_TLS_ENV_PATH = xray_caddy.CADDY_RANDOM_TLS_ENV_PATH
CADDY_RANDOM_TLS_ENV_ARCNAME = "usr/local/etc/xray/caddy-random-tls.env"
CADDY_RANDOM_TLS_CONFIG_DIR = xray_caddy.CADDY_RANDOM_TLS_CONFIG_DIR
CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME = "usr/local/etc/xray/caddy-random-tls.d"
CADDYFILE_PATH = xray_caddy.CADDYFILE_PATH
CADDYFILE_ARCNAME = xray_caddy.CADDYFILE_ARCNAME
CADDY_CONF_DIR = xray_caddy.CADDY_CONF_DIR
CADDY_CONF_DIR_ARCNAME = xray_caddy.CADDY_CONF_DIR_ARCNAME
HOST_SPECIFIC_SERVER_ENV_KEYS = ("SERVER_ADDR", "SECURITY_AUDIT_LAST_RUN")

BACKUP_FILES = [
    ("usr/local/etc/xray/config.json", CONFIG_PATH, True),
    (SERVER_ENV_ARCNAME, SERVER_ENV_PATH, True),
    (MANAGER_DB_ARCNAME, MANAGER_DB_PATH, True),
    (CADDY_RANDOM_TLS_ENV_ARCNAME, CADDY_RANDOM_TLS_ENV_PATH, False),
]
BACKUP_DIRS = [
    (CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME, CADDY_RANDOM_TLS_CONFIG_DIR, False),
]
CADDY_CONFIG_FILES = [
    (CADDYFILE_ARCNAME, CADDYFILE_PATH, False),
]
CADDY_CONFIG_DIRS = [
    (CADDY_CONF_DIR_ARCNAME, CADDY_CONF_DIR, False),
]


def die(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def require_root():
    if os.geteuid() != 0:
        die("Run this script as root.")


def run(command, check=True, timeout=None):
    return subprocess.run(command, check=check, text=True, timeout=timeout)


def run_capture(command, timeout=10):
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def default_ssh_target():
    server_addr = read_server_env(SERVER_ENV_PATH).get("SERVER_ADDR") or os.environ.get("SERVER_ADDR", "")
    server_addr = server_addr.strip()
    if server_addr:
        return server_addr if "@" in server_addr else f"root@{server_addr}"
    return "root@SERVER_HOST"


def xray_version():
    try:
        result = run_capture(["/usr/local/bin/xray", "version"], timeout=5)
    except FileNotFoundError:
        return "unknown"
    if result.returncode != 0 or not result.stdout:
        return "unknown"
    return result.stdout.splitlines()[0]


def utc_stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def archive_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def add_json(tar, name, payload):
    data = json.dumps(payload, indent=2, ensure_ascii=False).encode() + b"\n"
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, io.BytesIO(data))


def server_env_text(values):
    values = dict(values)
    values.pop("ACTIVITY_GEOIP_WARNING_CODE", None)
    lines = [f"{key}={values.get(key, '')}" for key in ORDERED_ENV_KEYS if key in values]
    for key in sorted(values):
        if key not in ORDERED_ENV_KEYS:
            lines.append(f"{key}={values[key]}")
    return "\n".join(lines) + "\n"


def add_text(tar, name, text, mode=0o640):
    data = text.encode()
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, io.BytesIO(data))
    return len(data)


def portable_server_env_values(path=SERVER_ENV_PATH):
    values = read_server_env(path)
    for key in HOST_SPECIFIC_SERVER_ENV_KEYS:
        values.pop(key, None)
    return values


def add_portable_server_env(tar, arcname, source):
    return add_text(tar, arcname, server_env_text(portable_server_env_values(source)))


def compact_output(result):
    return (result.stderr or result.stdout or f"exit {result.returncode}").strip()


def systemctl_unit_missing(result):
    output = compact_output(result).lower()
    return result.returncode != 0 and "unit file" in output and "does not exist" in output


def direct_tls_certificate_files(config):
    items = []
    for inbound in config.get("inbounds", []):
        stream = inbound.get("streamSettings") if isinstance(inbound.get("streamSettings"), dict) else {}
        if stream.get("security") != "tls":
            continue
        tls = stream.get("tlsSettings") if isinstance(stream.get("tlsSettings"), dict) else {}
        certificates = tls.get("certificates") if isinstance(tls.get("certificates"), list) else []
        tag = inbound.get("tag") or "unknown"
        for index, certificate in enumerate(certificates):
            if not isinstance(certificate, dict):
                continue
            for field, mode in (("certificateFile", 0o644), ("keyFile", 0o640)):
                value = str(certificate.get(field) or "").strip()
                if value:
                    items.append((tag, index, field, Path(value), mode))
    return items


def restore_direct_tls_certificate_permissions(config_path=CONFIG_PATH):
    if not config_path.exists():
        return []
    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"WARNING: Direct TLS certificate permissions were not checked: {exc}"]

    messages = []
    restored = {"certificateFile": 0, "keyFile": 0}
    seen = set()
    for tag, index, field, path, mode in direct_tls_certificate_files(config):
        label = f"{tag}[{index}] {field}"
        key = (field, str(path))
        if key in seen:
            continue
        seen.add(key)
        if not path.is_absolute():
            messages.append(f"WARNING: Direct TLS {label} path is not absolute: {path}")
            continue
        if not path.exists():
            messages.append(f"WARNING: Direct TLS {label} file not found after restore: {path}")
            continue
        if not path.is_file():
            messages.append(f"WARNING: Direct TLS {label} is not a regular file after restore: {path}")
            continue
        try:
            chown_xray(path)
            os.chmod(path, mode)
        except OSError as exc:
            messages.append(f"WARNING: Direct TLS {label} permissions were not updated: {exc}")
            continue
        restored[field] += 1

    if restored["certificateFile"] or restored["keyFile"]:
        messages.insert(
            0,
            "Direct TLS certificate permissions restored: "
            f"certificateFile={restored['certificateFile']}, keyFile={restored['keyFile']}",
        )
    return messages


def run_systemctl(args, timeout=20):
    command = ["systemctl", *args]
    try:
        return run_capture(command, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(command, 127, "", "systemctl not found")


def systemctl_is_enabled(unit):
    result = run_systemctl(["is-enabled", unit], timeout=10)
    return result.returncode == 0


def caddy_random_tls_backup_state():
    sites = []
    for config in xray_caddy.list_random_tls_configs(CADDY_RANDOM_TLS_CONFIG_DIR):
        sites.append(
            {
                "domain": config.domain,
                "localPort": config.local_port,
                "enabled": systemctl_is_enabled(xray_caddy.random_tls_timer_instance(config.domain)),
                "envArchive": f"{CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME}/{config.domain}.env",
                "service": xray_caddy.random_tls_service_instance(config.domain),
                "timer": xray_caddy.random_tls_timer_instance(config.domain),
            }
        )
    legacy_configured = CADDY_RANDOM_TLS_ENV_PATH.exists()
    if legacy_configured and not sites:
        try:
            legacy_config = xray_caddy.read_random_tls_config(CADDY_RANDOM_TLS_ENV_PATH)
        except (RuntimeError, ValueError):
            legacy_config = None
        if legacy_config:
            sites.append(
                {
                    "domain": legacy_config.domain,
                    "localPort": legacy_config.local_port,
                    "enabled": systemctl_is_enabled(xray_caddy.LEGACY_RANDOM_TLS_TIMER),
                    "envArchive": CADDY_RANDOM_TLS_ENV_ARCNAME,
                    "service": xray_caddy.LEGACY_RANDOM_TLS_SERVICE,
                    "timer": xray_caddy.LEGACY_RANDOM_TLS_TIMER,
                    "legacy": True,
                }
            )
    configured = bool(sites or legacy_configured)
    return {
        "configured": configured,
        "enabled": any(bool(item.get("enabled")) for item in sites),
        "envArchive": CADDY_RANDOM_TLS_ENV_ARCNAME if legacy_configured else "",
        "configsArchive": CADDY_RANDOM_TLS_CONFIG_DIR_ARCNAME if CADDY_RANDOM_TLS_CONFIG_DIR.exists() else "",
        "service": xray_caddy.RANDOM_TLS_SERVICE_TEMPLATE,
        "timer": xray_caddy.RANDOM_TLS_TIMER_TEMPLATE,
        "sites": sites,
    }


def caddy_config_backup_state():
    caddyfile_exists = CADDYFILE_PATH.exists()
    conf_dir_exists = CADDY_CONF_DIR.exists()
    return {
        "configured": bool(caddyfile_exists or conf_dir_exists),
        "caddyfileArchive": CADDYFILE_ARCNAME if caddyfile_exists else "",
        "confDirArchive": CADDY_CONF_DIR_ARCNAME if conf_dir_exists else "",
    }


def sync_traffic():
    sync = Path("/usr/local/sbin/xray-traffic-sync")
    if sync.exists():
        try:
            run_capture([str(sync), "--quiet"], timeout=20)
        except subprocess.TimeoutExpired:
            pass
    activity = Path("/usr/local/sbin/xray-activity")
    if activity.exists():
        try:
            run_capture([str(activity), "sync", "--quiet"], timeout=20)
        except subprocess.TimeoutExpired:
            pass


def create_manager_db_archive_snapshot(snapshot_dir):
    snapshot = sqlite_database.backup_database(
        MANAGER_DB_PATH,
        backup_dir=snapshot_dir,
        label="archive-snapshot",
    )
    if snapshot is None:
        die(f"{MANAGER_DB_PATH} does not exist; refusing to create an incomplete backup.")
    return snapshot


def create_backup(path_only=False, quiet=False, sync=True):
    if sync:
        sync_traffic()

    if not MANAGER_DB_PATH.exists():
        die(f"{MANAGER_DB_PATH} does not exist; refusing to create an incomplete backup.")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)

    archive = BACKUP_DIR / f"xray-backup-{archive_stamp()}.tar.gz"
    if archive.exists():
        archive = BACKUP_DIR / f"xray-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S%fZ')}.tar.gz"

    files = []
    with tempfile.TemporaryDirectory(prefix=".xray-backup-snapshot-", dir=BACKUP_DIR) as snapshot_dir_raw:
        snapshot_dir = Path(snapshot_dir_raw)
        os.chmod(snapshot_dir, 0o700)
        with tarfile.open(archive, "w:gz", dereference=True) as tar:
            for arcname, source, required in BACKUP_FILES:
                if not source.exists():
                    if required:
                        die(f"Required file not found: {source}")
                    continue
                if arcname == SERVER_ENV_ARCNAME:
                    size = add_portable_server_env(tar, arcname, source)
                else:
                    archive_source = source
                    if arcname == MANAGER_DB_ARCNAME:
                        archive_source = create_manager_db_archive_snapshot(snapshot_dir)
                    tar.add(archive_source, arcname=arcname, recursive=False)
                    size = archive_source.stat().st_size
                files.append({
                    "source": str(source),
                    "archive": arcname,
                    "size": size,
                })

            for arcname, source, required in BACKUP_DIRS:
                if not source.exists():
                    if required:
                        die(f"Required directory not found: {source}")
                    continue
                tar.add(source, arcname=arcname, recursive=True)
                size = sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
                files.append({
                    "source": str(source),
                    "archive": arcname,
                    "size": size,
                })

            for arcname, source, required in CADDY_CONFIG_FILES:
                if not source.exists():
                    if required:
                        die(f"Required Caddy file not found: {source}")
                    continue
                tar.add(source, arcname=arcname, recursive=False)
                files.append({
                    "source": str(source),
                    "archive": arcname,
                    "size": source.stat().st_size,
                })

            for arcname, source, required in CADDY_CONFIG_DIRS:
                if not source.exists():
                    if required:
                        die(f"Required Caddy directory not found: {source}")
                    continue
                tar.add(source, arcname=arcname, recursive=True)
                size = xray_caddy.tree_size(source)
                files.append({
                    "source": str(source),
                    "archive": arcname,
                    "size": size,
                })

            add_json(
                tar,
                "manifest.json",
                {
                    "createdAt": utc_stamp(),
                    "xrayVersion": xray_version(),
                    "hostSpecificServerEnvKeysOmitted": list(HOST_SPECIFIC_SERVER_ENV_KEYS),
                    "caddyConfig": caddy_config_backup_state(),
                    "caddyRandomTls": caddy_random_tls_backup_state(),
                    "files": files,
                    "warning": "This archive contains Xray private keys, client UUIDs, Trojan passwords, traffic data, activity metadata, and Caddy site configs when present. It does not contain Caddy certificate cache files.",
                },
            )

    os.chmod(archive, 0o600)
    if path_only:
        print(archive)
    elif not quiet:
        print(f"Backup created: {archive}")
        print(f"Size: {format_size(archive.stat().st_size)}")
        print("Contains: config.json, portable server.env, manager.db, and Caddy config when present.")
        print("Host-specific server.env values such as SERVER_ADDR are not stored; restore keeps the current server values.")
        print("Keep this file private: it contains Reality private keys, Trojan passwords, and client data.")
    return archive


def format_size(value):
    value = int(value or 0)
    units = [("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3)]
    if value < 1024:
        return f"{value}B"
    for suffix, size in units:
        next_size = size * 1024
        if value < next_size or suffix == "GB":
            return f"{value / size:.2f}{suffix}"


def backup_rows():
    rows = []
    for path in sorted(BACKUP_DIR.glob("*.tar.gz"), reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        created = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        rows.append([str(path), created, format_size(stat.st_size)])
    return rows


def list_backups(plain=False):
    rows = backup_rows()
    if plain:
        for path, created, size in rows:
            print(f"{path}\t{created}\t{size}")
        return
    print_table(["PATH", "CREATED", "SIZE"], rows, empty_message="No backups found.")


def resolve_archive(value):
    path = Path(value).expanduser()
    if path.exists():
        return path
    path = BACKUP_DIR / value
    if path.exists():
        return path
    die(f"Backup archive not found: {value}")


def resolve_backup_archive_for_delete(value):
    archive = resolve_archive(value)
    try:
        backup_root = BACKUP_DIR.resolve()
        archive_parent = archive.resolve().parent
    except OSError:
        die(f"Backup archive not found: {value}")
    if archive_parent != backup_root:
        die(f"Refusing to delete a file outside {BACKUP_DIR}: {archive}")
    if not archive.name.endswith(".tar.gz"):
        die("Refusing to delete a file that does not look like a .tar.gz backup archive.")
    return archive


def validate_member(member):
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        die(f"Unsafe archive member: {name}")
    if member.issym() or member.islnk() or member.isdev():
        die(f"Unsupported archive member type: {name}")


def extract_archive(archive):
    temp_dir = Path(tempfile.mkdtemp(prefix="xray-restore-"))
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            validate_member(member)
        tar.extractall(temp_dir)
    return temp_dir


def backup_manifest(temp_dir):
    path = temp_dir / "manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def caddy_random_tls_restore_state(temp_dir):
    state = backup_manifest(temp_dir).get("caddyRandomTls")
    return state if isinstance(state, dict) else None


def chown_xray(path):
    try:
        shutil.chown(path, user="root", group="xray")
    except LookupError:
        shutil.chown(path, user="root")


def copy_if_exists(temp_dir, arcname, target, mode):
    source = temp_dir / arcname
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if str(target).startswith(str(CONFIG_DIR)):
        chown_xray(target)
    else:
        shutil.chown(target, user="root")
    os.chmod(target, mode)
    return True


def merged_restored_server_env(archive_values, current_values):
    merged = dict(archive_values)

    current_addr = (current_values.get("SERVER_ADDR") or os.environ.get("SERVER_ADDR", "")).strip()
    if current_addr:
        merged["SERVER_ADDR"] = current_addr
    else:
        merged.pop("SERVER_ADDR", None)

    for key in HOST_SPECIFIC_SERVER_ENV_KEYS:
        if key == "SERVER_ADDR":
            continue
        current_value = (current_values.get(key) or "").strip()
        if current_value:
            merged[key] = current_value
        else:
            merged.pop(key, None)
    return merged


def copy_server_env_if_exists(temp_dir, arcname, target):
    source = temp_dir / arcname
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    archive_values = read_server_env(source)
    current_values = read_server_env(target)
    write_server_env(
        merged_restored_server_env(archive_values, current_values),
        path=target,
        ordered_keys=ORDERED_ENV_KEYS,
    )
    return True


def chown_tree_xray(path):
    if path.is_dir():
        chown_xray(path)
        os.chmod(path, 0o750)
        for child in path.rglob("*"):
            chown_xray(child)
            os.chmod(child, 0o750 if child.is_dir() else 0o640)
    else:
        chown_xray(path)
        os.chmod(path, 0o640)


def copy_dir_if_exists(temp_dir, arcname, target):
    source = temp_dir / arcname
    if not source.exists():
        return False
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    chown_tree_xray(target)
    return True


def restore_caddy_config_if_present(temp_dir, validator=None):
    source_caddyfile = temp_dir / CADDYFILE_ARCNAME
    source_conf_dir = temp_dir / CADDY_CONF_DIR_ARCNAME
    if not source_caddyfile.exists() and not source_conf_dir.exists():
        return []
    if not source_caddyfile.exists():
        raise FileNotFoundError(f"Backup contains {CADDY_CONF_DIR_ARCNAME} but not {CADDYFILE_ARCNAME}.")
    restored = xray_caddy.apply_config_restore(
        temp_dir,
        caddyfile_path=CADDYFILE_PATH,
        conf_dir=CADDY_CONF_DIR,
    )
    validate = validator or (lambda: xray_caddy.validate_and_reload_caddy(subprocess.run))
    validate()
    return [str(path) for path in restored]


def set_caddy_random_tls_enabled(domain, enabled):
    timer = xray_caddy.random_tls_timer_instance(domain)
    service = xray_caddy.random_tls_service_instance(domain)
    if enabled:
        return run_systemctl(["enable", "--now", timer], timeout=20)
    disable = run_systemctl(["disable", "--now", timer], timeout=20)
    run_systemctl(["stop", service], timeout=10)
    return disable


def disable_legacy_caddy_random_tls():
    disable = run_systemctl(["disable", "--now", xray_caddy.LEGACY_RANDOM_TLS_TIMER], timeout=20)
    run_systemctl(["stop", xray_caddy.LEGACY_RANDOM_TLS_SERVICE], timeout=10)
    return disable


def restored_random_tls_sites(state):
    sites = state.get("sites")
    if isinstance(sites, list):
        return [item for item in sites if isinstance(item, dict)]
    if state.get("configured") and state.get("envArchive"):
        source = CADDY_RANDOM_TLS_ENV_PATH
        if source.exists():
            try:
                config = xray_caddy.read_random_tls_config(source)
            except (RuntimeError, ValueError):
                return []
            return [
                {
                    "domain": config.domain,
                    "localPort": config.local_port,
                    "enabled": bool(state.get("enabled")),
                    "envArchive": state.get("envArchive"),
                    "legacy": True,
                }
            ]
    return []


def ensure_restored_random_tls_site_config(site):
    domain = site.get("domain")
    local_port = site.get("localPort")
    if not domain or not local_port:
        env_archive = str(site.get("envArchive") or "")
        source = CADDY_RANDOM_TLS_ENV_PATH if env_archive == CADDY_RANDOM_TLS_ENV_ARCNAME else CADDY_RANDOM_TLS_CONFIG_DIR / Path(env_archive).name
        if source.exists():
            config = xray_caddy.read_random_tls_config(source)
            domain = config.domain
            local_port = config.local_port
    if not domain or not local_port:
        raise ValueError("Caddy TLS randomizer site state is missing domain or localPort")
    config_path = xray_caddy.random_tls_env_path(str(domain), CADDY_RANDOM_TLS_CONFIG_DIR)
    if not config_path.exists():
        xray_caddy.write_random_tls_config(str(domain), local_port, config_path)
    return xray_caddy.read_random_tls_config(config_path)


def restore_caddy_random_tls_state(temp_dir):
    state = caddy_random_tls_restore_state(temp_dir)
    if state is None:
        return []

    messages = []
    configured = bool(state.get("configured"))
    enabled = bool(state.get("enabled"))

    if not configured:
        try:
            CADDY_RANDOM_TLS_ENV_PATH.unlink()
            messages.append(f"Removed Caddy TLS randomizer config: {CADDY_RANDOM_TLS_ENV_PATH}")
        except FileNotFoundError:
            pass
        for config in xray_caddy.list_random_tls_configs(CADDY_RANDOM_TLS_CONFIG_DIR):
            set_caddy_random_tls_enabled(config.domain, False)
        result = disable_legacy_caddy_random_tls()
        if result.returncode == 0 or systemctl_unit_missing(result):
            messages.append("Caddy TLS randomizer timer restored: disabled")
        else:
            messages.append(f"Caddy TLS randomizer disable failed: {compact_output(result)}")
        return messages

    sites = restored_random_tls_sites(state)
    if not sites:
        disable_legacy_caddy_random_tls()
        messages.append("Caddy TLS randomizer config missing after restore")
        return messages

    try:
        xray_caddy.write_random_tls_systemd_units()
    except OSError as exc:
        messages.append(f"Caddy TLS randomizer unit restore failed: {exc}")
        return messages

    daemon_reload = run_systemctl(["daemon-reload"], timeout=20)
    if daemon_reload.returncode != 0:
        messages.append(f"systemctl daemon-reload failed for Caddy TLS randomizer: {compact_output(daemon_reload)}")

    disable_legacy_caddy_random_tls()
    for site in sites:
        try:
            config = ensure_restored_random_tls_site_config(site)
        except (RuntimeError, ValueError, OSError) as exc:
            messages.append(f"Caddy TLS randomizer config restore failed: {exc}")
            continue
        site_enabled = bool(site.get("enabled", enabled))
        result = set_caddy_random_tls_enabled(config.domain, site_enabled)
        if result.returncode == 0:
            status = "enabled" if site_enabled else "disabled"
            messages.append(f"Caddy TLS randomizer timer restored for {config.domain}: {status}")
        else:
            action = "enable" if site_enabled else "disable"
            messages.append(f"Caddy TLS randomizer {action} failed for {config.domain}: {compact_output(result)}")
    return messages


def apply_restore(temp_dir):
    if not (temp_dir / "usr/local/etc/xray/config.json").exists():
        die("Backup does not contain config.json.")
    restored = []
    for arcname, target, required in BACKUP_FILES:
        if arcname == SERVER_ENV_ARCNAME:
            copied = copy_server_env_if_exists(temp_dir, arcname, target)
        else:
            copied = copy_if_exists(temp_dir, arcname, target, 0o640)
        if copied:
            restored.append(str(target))
        elif required:
            die(f"Backup does not contain required file: {arcname}")
    for arcname, target, required in BACKUP_DIRS:
        if copy_dir_if_exists(temp_dir, arcname, target):
            restored.append(str(target))
        elif required:
            die(f"Backup does not contain required directory: {arcname}")
    return restored


def backup_manager_db_before_restore():
    if not MANAGER_DB_PATH.exists():
        return None
    backup_dir = BACKUP_DIR / "manager-db-pre-restore"
    try:
        return sqlite_database.backup_database(
            MANAGER_DB_PATH,
            backup_dir=backup_dir,
            label="pre-restore",
        )
    except Exception:
        backup_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        fallback = backup_dir / f"{MANAGER_DB_PATH.stem}-pre-restore-{archive_stamp()}.db"
        counter = 1
        while fallback.exists():
            fallback = backup_dir / f"{MANAGER_DB_PATH.stem}-pre-restore-{archive_stamp()}-{counter}.db"
            counter += 1
        shutil.copy2(MANAGER_DB_PATH, fallback)
        os.chmod(fallback, 0o600)
        return fallback


def stop_timer():
    run_capture(["systemctl", "stop", "xray-traffic-sync.timer"], timeout=10)


def start_timers():
    run_capture(["systemctl", "enable", "--now", "xray-traffic-sync.timer"], timeout=20)
    run_capture(["systemctl", "enable", "--now", "xray-client-expire.timer"], timeout=20)


def test_and_restart():
    run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG_PATH)])
    restart_systemd_unit("xray")
    active = run_capture(["systemctl", "is-active", "xray"], timeout=10)
    if active.returncode != 0:
        die("Xray did not become active after restore.")


def restore_backup(value):
    archive = resolve_archive(value)
    pre_backup = create_backup(quiet=True, sync=True)
    print(f"Pre-restore backup: {pre_backup}")
    stop_timer()
    pre_restore_manager_db = backup_manager_db_before_restore()
    if pre_restore_manager_db:
        print(f"Pre-restore SQLite backup: {pre_restore_manager_db}")
    temp_dir = extract_archive(archive)
    try:
        restored = apply_restore(temp_dir)
        restored.extend(restore_caddy_config_if_present(temp_dir))
        for message in restore_direct_tls_certificate_permissions(CONFIG_PATH):
            print(message)
        test_and_restart()
        random_tls_messages = restore_caddy_random_tls_state(temp_dir)
    except Exception as exc:
        print("Restore failed. Rolling back to pre-restore backup...", file=sys.stderr)
        rollback_dir = extract_archive(pre_backup)
        apply_restore(rollback_dir)
        restore_caddy_config_if_present(rollback_dir)
        for message in restore_direct_tls_certificate_permissions(CONFIG_PATH):
            print(message)
        test_and_restart()
        for message in restore_caddy_random_tls_state(rollback_dir):
            print(message)
        shutil.rmtree(rollback_dir, ignore_errors=True)
        start_timers()
        die(f"Restore failed: {exc}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    start_timers()
    print(f"Restored from: {archive}")
    print("Restored files:")
    for path in restored:
        print(f"  {path}")
    for message in random_tls_messages:
        print(message)
    print("Xray status: active")


def delete_backup(value):
    archive = resolve_backup_archive_for_delete(value)
    size = archive.stat().st_size
    archive.unlink()
    print(f"Deleted backup: {archive}")
    print(f"Freed: {format_size(size)}")


def download_command(value, ssh_target=None, local_path="~/Downloads"):
    archive = resolve_archive(value)
    ssh_target = ssh_target or default_ssh_target()
    target = local_path.rstrip("/") + "/"
    print(f"Run this command on your local computer:")
    print(f"scp {shlex.quote(ssh_target + ':' + str(archive))} {quote_local_path(target)}")


def upload_command(ssh_target=None, local_file="~/Downloads/xray-backup.tar.gz"):
    ssh_target = ssh_target or default_ssh_target()
    print("Run this command on your local computer:")
    print(f"scp {quote_local_path(local_file)} {shlex.quote(ssh_target + ':' + str(BACKUP_DIR) + '/')}")


def quote_local_path(value):
    if value == "~":
        return "~"
    if value.startswith("~/"):
        rest = value[2:]
        return "~/" + (shlex.quote(rest) if rest else "")
    return shlex.quote(value)


def usage():
    print("""Usage:
  xray-backup create [--path-only] [--no-sync]
  xray-backup list [--plain]
  xray-backup restore ARCHIVE_PATH_OR_NAME
  xray-backup delete ARCHIVE_PATH_OR_NAME
  xray-backup download-command ARCHIVE_PATH_OR_NAME [SSH_TARGET_OR_USER_HOST] [LOCAL_DIR]
  xray-backup upload-command [SSH_TARGET_OR_USER_HOST] [LOCAL_FILE]
""")


def main():
    require_root()
    if len(sys.argv) < 2:
        usage()
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]
    if command == "create":
        path_only = "--path-only" in args
        sync = "--no-sync" not in args
        create_backup(path_only=path_only, sync=sync)
    elif command == "list":
        list_backups(plain="--plain" in args)
    elif command == "restore" and len(args) == 1:
        restore_backup(args[0])
    elif command in ("delete", "remove", "rm") and len(args) == 1:
        delete_backup(args[0])
    elif command == "download-command" and len(args) in (1, 2, 3):
        download_command(args[0], args[1] if len(args) >= 2 else None, args[2] if len(args) >= 3 else "~/Downloads")
    elif command == "upload-command" and len(args) in (0, 1, 2):
        upload_command(args[0] if len(args) >= 1 else None, args[1] if len(args) >= 2 else "~/Downloads/xray-backup.tar.gz")
    else:
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
