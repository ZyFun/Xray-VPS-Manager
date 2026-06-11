#!/usr/bin/env python3
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path("/usr/local/etc/xray/config.json")
XRAY_BIN = Path("/usr/local/bin/xray")
XRAY_ASSET_DIR = Path("/usr/local/share/xray")
ASSET_NAMES = ("geoip.dat", "geosite.dat")
BACKUP_DIR = Path("/usr/local/lib/xray-backups")
GITHUB_API = "https://api.github.com/repos/XTLS/Xray-core/releases/latest"
LATEST_PAGE = "https://github.com/XTLS/Xray-core/releases/latest"
LATEST_ZIP = "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip"
LATEST_DGST = "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip.dgst"
LOYALSOLDIER_API = "https://api.github.com/repos/Loyalsoldier/v2ray-rules-dat/releases/latest"
LOYALSOLDIER_PAGE = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest"
LOYALSOLDIER_DOWNLOAD = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download"
V2FLY_GEOIP_API = "https://api.github.com/repos/v2fly/geoip/releases/latest"
V2FLY_GEOIP_PAGE = "https://github.com/v2fly/geoip/releases/latest"
V2FLY_GEOIP_DOWNLOAD = "https://github.com/v2fly/geoip/releases/latest/download"

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def color(text, code):
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{RESET}"


def ok(message):
    print(color(f"OK: {message}", GREEN))


def warn(message):
    print(color(f"WARN: {message}", YELLOW))


def fail(message):
    print(color(f"FAIL: {message}", RED))


def die(message, code=1):
    fail(message)
    sys.exit(code)


def run(command, timeout=60, check=False, env=None):
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        raise RuntimeError(detail)
    return result


def require_root():
    if os.geteuid() != 0:
        die("Run this script as root.")


def parse_version(value):
    match = re.search(r"(\d+(?:\.\d+){1,3})", value or "")
    if not match:
        return ""
    return match.group(1)


def version_tuple(value):
    parsed = parse_version(value)
    if not parsed:
        return ()
    return tuple(int(part) for part in parsed.split("."))


def current_version(binary=XRAY_BIN):
    if not binary.exists():
        return ""
    result = run([str(binary), "version"], timeout=10)
    if result.returncode != 0:
        return ""
    return parse_version(result.stdout.splitlines()[0] if result.stdout else "")


def require_path(path, label):
    if not path.exists():
        raise RuntimeError(f"{label} not found: {path}")
    return f"{path} exists"


def load_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Config not found: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text())


def find_reality_inbound(config):
    for inbound in config.get("inbounds", []):
        if inbound.get("tag") == "vless-reality":
            return inbound
    for inbound in config.get("inbounds", []):
        if inbound.get("protocol") == "vless" and inbound.get("streamSettings", {}).get("security") == "reality":
            return inbound
    return None


def systemctl_is_active(unit):
    result = run(["systemctl", "is-active", unit], timeout=10)
    return result.returncode == 0 and result.stdout.strip() == "active"


def check_tcp(host, port, timeout=2):
    with socket.create_connection((host, port), timeout=timeout):
        return True


def config_test(binary=XRAY_BIN, asset_dir=None):
    env = os.environ.copy()
    if asset_dir:
        env["XRAY_LOCATION_ASSET"] = str(asset_dir)
    return run([str(binary), "run", "-test", "-config", str(CONFIG_PATH)], timeout=30, env=env)


def preflight(verbose=True):
    failures = []

    def check(label, func):
        try:
            detail = func()
            if verbose:
                ok(detail or label)
        except Exception as exc:
            failures.append(f"{label}: {exc}")
            if verbose:
                fail(f"{label}: {exc}")

    def xray_binary():
        require_path(XRAY_BIN, "Xray binary")
        version = current_version()
        if not version:
            raise RuntimeError("could not read Xray version")
        return f"{XRAY_BIN} version {version}"

    def command_exists(command):
        path = shutil.which(command)
        if not path:
            raise RuntimeError(f"{command} not found in PATH")
        return f"{command} found: {path}"

    check("Xray binary", xray_binary)
    check("curl", lambda: command_exists("curl"))
    for name in ASSET_NAMES:
        check(name, lambda name=name: require_path(XRAY_ASSET_DIR / name, name))

    config_holder = {}

    def parse_config():
        config_holder["config"] = load_config()
        return f"{CONFIG_PATH} parsed"

    check("Config JSON", parse_config)

    def reality_settings():
        inbound = find_reality_inbound(config_holder.get("config", {}))
        if not inbound:
            raise RuntimeError("VLESS Reality inbound not found")
        port = int(inbound.get("port", 0))
        reality = inbound.get("streamSettings", {}).get("realitySettings", {})
        sni = (reality.get("serverNames") or [""])[0]
        dest = reality.get("dest", "")
        if not port or not sni or not dest:
            raise RuntimeError(f"incomplete settings: port={port}, sni={sni}, dest={dest}")
        config_holder["port"] = port
        return f"VLESS Reality port={port}, sni={sni}, dest={dest}"

    def current_config_test():
        result = config_test()
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "xray config test failed").strip())
        return "Configuration OK"

    def xray_service():
        if not systemctl_is_active("xray"):
            raise RuntimeError("xray.service is not active")
        return "xray.service active"

    check("Reality settings", reality_settings)
    check("Current config test", current_config_test)
    check("xray.service", xray_service)

    if "port" in config_holder:
        check("VLESS TCP port", lambda: f"127.0.0.1:{config_holder['port']} accepts TCP" if check_tcp("127.0.0.1", config_holder["port"]) else "")

    if any(item.get("tag") == "api" for item in config_holder.get("config", {}).get("inbounds", [])):
        def stats_api():
            result = run([str(XRAY_BIN), "api", "statsquery", "--server=127.0.0.1:10085", "-pattern", "user>>>"], timeout=10)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "stats API failed").strip())
            return "Stats API responds on 127.0.0.1:10085"

        check("Stats API", stats_api)

    timer_file = Path("/etc/systemd/system/xray-traffic-sync.timer")
    if timer_file.exists():
        def traffic_sync_timer():
            if not systemctl_is_active("xray-traffic-sync.timer"):
                raise RuntimeError("xray-traffic-sync.timer is not active")
            return "xray-traffic-sync.timer active"

        check("Traffic sync timer", traffic_sync_timer)

    expire_timer_file = Path("/etc/systemd/system/xray-client-expire.timer")
    if expire_timer_file.exists():
        def client_expire_timer():
            if not systemctl_is_active("xray-client-expire.timer"):
                raise RuntimeError("xray-client-expire.timer is not active")
            return "xray-client-expire.timer active"

        check("Client expire timer", client_expire_timer)

    return failures


def http_json(url, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": "xray-update"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_from_redirect():
    result = run(["curl", "-fsSLI", "-o", "/dev/null", "-w", "%{url_effective}", LATEST_PAGE], timeout=30)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to resolve latest release").strip())
    tag = result.stdout.rstrip("/").split("/")[-1]
    version = parse_version(tag)
    if not version:
        raise RuntimeError(f"could not parse version from {result.stdout.strip()}")
    return {
        "tag": tag,
        "version": version,
        "zip_url": LATEST_ZIP,
        "dgst_url": LATEST_DGST,
    }


def latest_release():
    try:
        data = http_json(GITHUB_API)
        tag = data.get("tag_name", "")
        assets = {item.get("name"): item.get("browser_download_url") for item in data.get("assets", [])}
        version = parse_version(tag)
        zip_url = assets.get("Xray-linux-64.zip") or LATEST_ZIP
        dgst_url = assets.get("Xray-linux-64.zip.dgst") or LATEST_DGST
        if not version:
            raise RuntimeError(f"could not parse tag_name={tag!r}")
        return {
            "tag": tag,
            "version": version,
            "zip_url": zip_url,
            "dgst_url": dgst_url,
        }
    except Exception as exc:
        warn(f"GitHub API недоступен, пробую latest redirect: {exc}")
        return latest_from_redirect()


def download(url, target, required=True):
    result = run(["curl", "-fL", "--retry", "3", "--connect-timeout", "20", "--max-time", "240", "-o", str(target), url], timeout=270)
    if result.returncode != 0:
        if required:
            raise RuntimeError((result.stderr or result.stdout or f"failed to download {url}").strip())
        return False
    return True


def latest_tag_from_redirect(latest_page):
    result = run(["curl", "-fsSLI", "-o", "/dev/null", "-w", "%{url_effective}", latest_page], timeout=30)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "failed to resolve latest release").strip())
    return result.stdout.rstrip("/").split("/")[-1]


def latest_github_release_label(api_url, latest_page):
    try:
        data = http_json(api_url)
        tag = data.get("tag_name") or data.get("name") or ""
        if tag:
            return tag
        raise RuntimeError("empty tag_name")
    except Exception as exc:
        warn(f"GitHub API недоступен, пробую latest redirect: {exc}")
        return latest_tag_from_redirect(latest_page)


def download_direct_assets(base_url, target_dir, names):
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        download(f"{base_url}/{name}", target_dir / name, required=True)


def prepare_asset_config_test_dir(asset_dir, names):
    asset_dir.mkdir(parents=True, exist_ok=True)
    for name in ASSET_NAMES:
        target = asset_dir / name
        if target.exists():
            continue
        current = XRAY_ASSET_DIR / name
        if current.exists():
            shutil.copy2(current, target)
    missing = [name for name in ASSET_NAMES if not (asset_dir / name).exists()]
    if missing:
        raise RuntimeError("Cannot test assets; missing current file(s): " + ", ".join(missing))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_digest(zip_path, dgst_path):
    if not dgst_path.exists() or dgst_path.stat().st_size == 0:
        warn("Digest file was not downloaded; SHA256 verification skipped.")
        return
    text = dgst_path.read_text(errors="replace")
    match = re.search(r"SHA2-256=\s*([A-Fa-f0-9]{64})", text)
    if not match:
        warn("Digest file does not contain SHA2-256; verification skipped.")
        return
    expected = match.group(1).lower()
    actual = sha256(zip_path).lower()
    if actual != expected:
        raise RuntimeError(f"SHA256 mismatch: expected {expected}, got {actual}")
    ok("SHA256 digest verified")


def download_release(release, workdir):
    zip_path = workdir / "Xray-linux-64.zip"
    dgst_path = workdir / "Xray-linux-64.zip.dgst"
    download(release["zip_url"], zip_path, required=True)
    download(release["dgst_url"], dgst_path, required=False)
    verify_digest(zip_path, dgst_path)
    extract_dir = workdir / "xray"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)
    candidate = extract_dir / "xray"
    if not candidate.exists():
        raise RuntimeError("Downloaded archive does not contain xray binary")
    candidate.chmod(0o755)
    return extract_dir, candidate


def test_candidate(release, workdir):
    extract_dir, candidate = download_release(release, workdir)
    result = config_test(candidate, asset_dir=extract_dir)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "candidate config test failed").strip())
    version = current_version(candidate)
    if version and version != release["version"]:
        warn(f"Downloaded binary reports version {version}, release tag is {release['version']}")
    ok(f"Latest Xray {release['version']} works with current config")
    return extract_dir, candidate


def version_message(current, latest):
    if version_tuple(current) < version_tuple(latest):
        return "update-available"
    return "no-update"


def backup_current(target_version):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    current = current_version() or "unknown"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = BACKUP_DIR / f"{timestamp}-{current}"
    backup.mkdir(mode=0o755)
    shutil.copy2(XRAY_BIN, backup / "xray")
    if (XRAY_ASSET_DIR / "geoip.dat").exists():
        shutil.copy2(XRAY_ASSET_DIR / "geoip.dat", backup / "geoip.dat")
    if (XRAY_ASSET_DIR / "geosite.dat").exists():
        shutil.copy2(XRAY_ASSET_DIR / "geosite.dat", backup / "geosite.dat")
    metadata = {
        "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "version": current,
        "targetVersion": target_version,
    }
    (backup / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    ok(f"Backup created: {backup}")
    return backup


def install_candidate(extract_dir):
    shutil.copy2(extract_dir / "xray", XRAY_BIN)
    XRAY_BIN.chmod(0o755)
    XRAY_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for name in ASSET_NAMES:
        src = extract_dir / name
        if src.exists():
            shutil.copy2(src, XRAY_ASSET_DIR / name)
            (XRAY_ASSET_DIR / name).chmod(0o644)


def require_release_assets(extract_dir):
    missing = [name for name in ASSET_NAMES if not (extract_dir / name).exists()]
    if missing:
        raise RuntimeError("Downloaded archive does not contain: " + ", ".join(missing))


def current_asset_hashes(directory=XRAY_ASSET_DIR, names=ASSET_NAMES):
    hashes = {}
    for name in names:
        path = directory / name
        hashes[name] = sha256(path) if path.exists() else ""
    return hashes


def backup_assets(target, names=ASSET_NAMES):
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = XRAY_ASSET_DIR / name
        if path.exists():
            shutil.copy2(path, target / name)


def restore_assets(source, names=ASSET_NAMES):
    XRAY_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = source / name
        if path.exists():
            shutil.copy2(path, XRAY_ASSET_DIR / name)
            (XRAY_ASSET_DIR / name).chmod(0o644)


def install_assets(extract_dir, names=ASSET_NAMES):
    XRAY_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy2(extract_dir / name, XRAY_ASSET_DIR / name)
        (XRAY_ASSET_DIR / name).chmod(0o644)


def restart_xray():
    run(["systemctl", "restart", "xray"], timeout=30, check=True)
    if not systemctl_is_active("xray"):
        raise RuntimeError("xray.service is not active after restart")


def backup_dirs():
    if not BACKUP_DIR.exists():
        return []
    return sorted([item for item in BACKUP_DIR.iterdir() if item.is_dir()], reverse=True)


def list_backups():
    backups = backup_dirs()
    if not backups:
        fail("Нет сохранённых предыдущих версий Xray.")
        return 1
    for item in backups:
        metadata_path = item / "metadata.json"
        version = "unknown"
        created = item.name
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text())
                version = metadata.get("version", version)
                created = metadata.get("created", created)
            except json.JSONDecodeError:
                pass
        print(f"{item.name}  version={version}  created={created}")
    return 0


def restore_backup(backup):
    if not (backup / "xray").exists():
        raise RuntimeError(f"Backup does not contain xray binary: {backup}")
    shutil.copy2(backup / "xray", XRAY_BIN)
    XRAY_BIN.chmod(0o755)
    XRAY_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("geoip.dat", "geosite.dat"):
        src = backup / name
        if src.exists():
            shutil.copy2(src, XRAY_ASSET_DIR / name)
            (XRAY_ASSET_DIR / name).chmod(0o644)
    result = config_test()
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "config test failed after rollback").strip())
    restart_xray()


def rollback(name=None):
    backups = backup_dirs()
    if not backups:
        die("Нет сохранённых предыдущих версий Xray для отката.")
    backup = BACKUP_DIR / name if name else backups[0]
    if not backup.exists():
        die(f"Backup not found: {backup}")
    restore_backup(backup)
    ok(f"Rollback complete: {current_version()} restored from {backup.name}")


def run_passthrough(command, timeout=120):
    return subprocess.run(command, check=False, text=True, timeout=timeout)


def run_diagnostics(stage):
    candidates = [Path("/usr/local/sbin/xray-test")]
    found = next((path for path in candidates if path.exists()), None)
    if not found:
        warn("xray-test не найден, использую встроенную базовую проверку.")
        failures = preflight(verbose=True)
        if failures:
            raise RuntimeError(f"диагностика не пройдена: {stage}")
        return

    warn(f"Запускаю диагностику сервера: {stage}")
    result = run_passthrough([str(found)], timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"диагностика не пройдена: {stage}")


def cmd_check():
    release = latest_release()
    current = current_version()
    if not current:
        die("Не удалось определить установленную версию Xray.")
    latest = release["version"]
    if version_message(current, latest) == "no-update":
        ok(f"Обновление не требуется: установлено {current}, official latest {latest}.")
        return
    warn(f"Доступно обновление: {current} -> {latest}.")


def cmd_test_latest():
    release = latest_release()
    current = current_version()
    if not current:
        die("Не удалось определить установленную версию Xray.")
    latest = release["version"]
    if version_message(current, latest) == "no-update":
        ok(f"Совместимость latest не требует проверки: установлено {current}, official latest {latest}.")
        return
    warn(f"Проверяю latest Xray {latest} с текущим config.json без установки...")
    with tempfile.TemporaryDirectory(prefix="xray-update-") as tmp:
        test_candidate(release, Path(tmp))


def cmd_update():
    try:
        run_diagnostics("перед обновлением")
    except Exception as exc:
        die(f"Обновление остановлено: {exc}")

    release = latest_release()
    current = current_version()
    latest = release["version"]
    if version_message(current, latest) == "no-update":
        ok(f"Обновление не требуется: установлено {current}, official latest {latest}.")
        return

    warn(f"Будет выполнено обновление Xray: {current} -> {latest}")
    with tempfile.TemporaryDirectory(prefix="xray-update-") as tmp:
        extract_dir, _ = test_candidate(release, Path(tmp))
        backup = backup_current(latest)
        try:
            install_candidate(extract_dir)
            result = config_test()
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "installed config test failed").strip())
            restart_xray()
            run_diagnostics("после обновления")
        except Exception as exc:
            fail(f"Update failed: {exc}")
            warn("Пробую автоматически откатиться на предыдущую версию...")
            restore_backup(backup)
            die("Обновление не применено, предыдущая версия восстановлена.")
    ok(f"Xray updated successfully: {current_version()}")


def asset_source(source_value):
    value = (source_value or "xray").strip().lower()
    aliases = {
        "official": "xray",
        "xray": "xray",
        "xray-release": "xray",
        "loyalsoldier": "loyalsoldier",
        "loyal": "loyalsoldier",
        "fresh": "loyalsoldier",
        "v2fly": "v2fly",
        "v2fly-geoip": "v2fly",
    }
    source = aliases.get(value)
    if source is None:
        die("Unknown asset source. Use: xray, loyalsoldier, or v2fly.")
    return source


def prepare_asset_source(source, workdir):
    if source == "xray":
        release = latest_release()
        extract_dir, _candidate = download_release(release, workdir)
        names = ASSET_NAMES
        require_release_assets(extract_dir)
        return extract_dir, names, f"official Xray {release['version']}"

    if source == "loyalsoldier":
        tag = latest_github_release_label(LOYALSOLDIER_API, LOYALSOLDIER_PAGE)
        asset_dir = workdir / "loyalsoldier"
        names = ASSET_NAMES
        download_direct_assets(LOYALSOLDIER_DOWNLOAD, asset_dir, names)
        return asset_dir, names, f"Loyalsoldier/v2ray-rules-dat {tag}"

    tag = latest_github_release_label(V2FLY_GEOIP_API, V2FLY_GEOIP_PAGE)
    asset_dir = workdir / "v2fly-geoip"
    names = ("geoip.dat",)
    download_direct_assets(V2FLY_GEOIP_DOWNLOAD, asset_dir, names)
    return asset_dir, names, f"v2fly/geoip {tag} (geoip.dat only)"


def cmd_update_assets(source_value="xray"):
    source = asset_source(source_value)
    source_label = {
        "xray": "official latest Xray",
        "loyalsoldier": "Loyalsoldier fresh rules",
        "v2fly": "v2fly geoip/domain-list source (geoip.dat only)",
    }[source]
    warn(f"Обновляю geo assets из {source_label}...")
    with tempfile.TemporaryDirectory(prefix="xray-assets-") as tmp:
        workdir = Path(tmp)
        asset_dir, names, source_detail = prepare_asset_source(source, workdir)
        prepare_asset_config_test_dir(asset_dir, names)

        result = config_test(asset_dir=asset_dir)
        if result.returncode != 0:
            die((result.stderr or result.stdout or "downloaded assets failed config test").strip())

        current_hashes = current_asset_hashes(names=names)
        new_hashes = current_asset_hashes(asset_dir, names)
        changed = [name for name in names if current_hashes.get(name) != new_hashes.get(name)]
        if not changed:
            ok(f"{', '.join(names)} already match {source_detail}.")
            return

        backup = workdir / "asset-backup"
        backup_assets(backup, names)
        try:
            install_assets(asset_dir, changed)
            result = config_test()
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "installed assets failed config test").strip())
            restart_xray()
        except Exception as exc:
            fail(f"Asset update failed: {exc}")
            warn("Восстанавливаю предыдущие geo assets...")
            restore_assets(backup, names)
            restore_result = config_test()
            if restore_result.returncode == 0:
                try:
                    restart_xray()
                except Exception as restart_exc:
                    warn(f"Не удалось перезапустить Xray после восстановления assets: {restart_exc}")
            die("Обновление geo assets не применено, предыдущие assets восстановлены.")

    ok("Updated assets: " + ", ".join(changed))
    ok(f"Source: {source_detail}")


def usage():
    print("""Usage:
  xray-update --check
  xray-update --test-latest
  xray-update --update
  xray-update --update-assets [xray|loyalsoldier|v2fly]
  xray-update --rollback [BACKUP_NAME]
  xray-update --backups
""")


def main():
    require_root()
    if len(sys.argv) < 2:
        usage()
        sys.exit(1)
    command = sys.argv[1]
    if command in ("--check", "check"):
        cmd_check()
    elif command in ("--test-latest", "test-latest"):
        cmd_test_latest()
    elif command in ("--update", "update"):
        cmd_update()
    elif command in ("--update-assets", "update-assets", "--update-geo"):
        cmd_update_assets(sys.argv[2] if len(sys.argv) > 2 else "xray")
    elif command in ("--update-assets-loyalsoldier", "update-assets-loyalsoldier"):
        cmd_update_assets("loyalsoldier")
    elif command in ("--update-geoip-v2fly", "update-geoip-v2fly"):
        cmd_update_assets("v2fly")
    elif command in ("--rollback", "rollback"):
        rollback(sys.argv[2] if len(sys.argv) > 2 else None)
    elif command in ("--backups", "backups", "--list-backups"):
        sys.exit(list_backups())
    else:
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
