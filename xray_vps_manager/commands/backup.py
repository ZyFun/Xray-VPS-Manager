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
HOST_SPECIFIC_SERVER_ENV_KEYS = ("SERVER_ADDR", "SECURITY_AUDIT_LAST_RUN")

BACKUP_FILES = [
    ("usr/local/etc/xray/config.json", CONFIG_PATH, True),
    (SERVER_ENV_ARCNAME, SERVER_ENV_PATH, True),
    (MANAGER_DB_ARCNAME, MANAGER_DB_PATH, True),
    (CADDY_RANDOM_TLS_ENV_ARCNAME, CADDY_RANDOM_TLS_ENV_PATH, False),
]
BACKUP_DIRS = []


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
    configured = CADDY_RANDOM_TLS_ENV_PATH.exists()
    return {
        "configured": configured,
        "enabled": systemctl_is_enabled(xray_caddy.RANDOM_TLS_TIMER),
        "envArchive": CADDY_RANDOM_TLS_ENV_ARCNAME if configured else "",
        "service": xray_caddy.RANDOM_TLS_SERVICE,
        "timer": xray_caddy.RANDOM_TLS_TIMER,
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
        with tarfile.open(archive, "w:gz") as tar:
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

            add_json(
                tar,
                "manifest.json",
                {
                    "createdAt": utc_stamp(),
                    "xrayVersion": xray_version(),
                    "hostSpecificServerEnvKeysOmitted": list(HOST_SPECIFIC_SERVER_ENV_KEYS),
                    "caddyRandomTls": caddy_random_tls_backup_state(),
                    "files": files,
                    "warning": "This archive contains Xray private keys, client UUIDs, traffic data, and activity metadata.",
                },
            )

    os.chmod(archive, 0o600)
    if path_only:
        print(archive)
    elif not quiet:
        print(f"Backup created: {archive}")
        print(f"Size: {format_size(archive.stat().st_size)}")
        print("Contains: config.json, portable server.env, and manager.db.")
        print("Host-specific server.env values such as SERVER_ADDR are not stored; restore keeps the current server values.")
        print("Keep this file private: it contains Reality private keys and client data.")
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


def set_caddy_random_tls_enabled(enabled):
    if enabled:
        return run_systemctl(["enable", "--now", xray_caddy.RANDOM_TLS_TIMER], timeout=20)
    disable = run_systemctl(["disable", "--now", xray_caddy.RANDOM_TLS_TIMER], timeout=20)
    run_systemctl(["stop", xray_caddy.RANDOM_TLS_SERVICE], timeout=10)
    return disable


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
        result = set_caddy_random_tls_enabled(False)
        if result.returncode == 0:
            messages.append("Caddy TLS randomizer timer restored: disabled")
        else:
            messages.append(f"Caddy TLS randomizer disable failed: {compact_output(result)}")
        return messages

    if not CADDY_RANDOM_TLS_ENV_PATH.exists():
        result = set_caddy_random_tls_enabled(False)
        messages.append(f"Caddy TLS randomizer config missing after restore: {CADDY_RANDOM_TLS_ENV_PATH}")
        if result.returncode != 0:
            messages.append(f"Caddy TLS randomizer disable failed: {compact_output(result)}")
        return messages

    try:
        xray_caddy.write_random_tls_systemd_units()
    except OSError as exc:
        messages.append(f"Caddy TLS randomizer unit restore failed: {exc}")
        return messages

    daemon_reload = run_systemctl(["daemon-reload"], timeout=20)
    if daemon_reload.returncode != 0:
        messages.append(f"systemctl daemon-reload failed for Caddy TLS randomizer: {compact_output(daemon_reload)}")

    result = set_caddy_random_tls_enabled(enabled)
    if result.returncode == 0:
        status = "enabled" if enabled else "disabled"
        messages.append(f"Caddy TLS randomizer timer restored: {status}")
    else:
        action = "enable" if enabled else "disable"
        messages.append(f"Caddy TLS randomizer {action} failed: {compact_output(result)}")
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
        test_and_restart()
        random_tls_messages = restore_caddy_random_tls_state(temp_dir)
    except Exception as exc:
        print("Restore failed. Rolling back to pre-restore backup...", file=sys.stderr)
        rollback_dir = extract_archive(pre_backup)
        apply_restore(rollback_dir)
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
