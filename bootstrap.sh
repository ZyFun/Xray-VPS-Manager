#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${XRAY_MANAGER_REPO:-ZyFun/Xray-VPS-Manager}"
INSTALL_DIR="${XRAY_MANAGER_INSTALL_DIR:-/root/xray_server}"
REQUESTED_VERSION="${XRAY_MANAGER_VERSION:-}"
RUN_INSTALL="${XRAY_MANAGER_RUN_INSTALL:-true}"
FORCE_INSTALL="false"
MANAGER_PYTHON="${MANAGER_PYTHON:-}"
XRAY_PYTHON=""

usage() {
  cat <<'EOF'
Usage:
  bootstrap.sh [TAG]
  bootstrap.sh --version TAG
  bootstrap.sh --no-install
  bootstrap.sh --force-install [TAG]

Examples:
  TAG="$(curl -fsSL https://api.github.com/repos/ZyFun/Xray-VPS-Manager/releases/latest | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
  curl -fsSL "https://raw.githubusercontent.com/ZyFun/Xray-VPS-Manager/${TAG}/bootstrap.sh" | bash -s -- "$TAG"

Environment:
  XRAY_MANAGER_VERSION      Release tag to install, for example v2.1.1.
  XRAY_MANAGER_INSTALL_DIR  Target source directory, default /root/xray_server.
  XRAY_MANAGER_RUN_INSTALL  Set false to download only.
  MANAGER_PYTHON            Absolute path to Python >= 3.12 for the manager.
                            Default: python3.13, python3.12, then python3 if it is >= 3.12.
EOF
}

log() {
  printf '==> %s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

normalize_tag() {
  local value="$1"
  if [[ "$value" =~ ^[0-9]+(\.[0-9]+){1,3}([-+][A-Za-z0-9._-]+)?$ ]]; then
    printf 'v%s\n' "$value"
  else
    printf '%s\n' "$value"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --version)
      [[ $# -ge 2 ]] || die "--version requires a tag."
      REQUESTED_VERSION="$2"
      shift 2
      ;;
    --install-dir)
      [[ $# -ge 2 ]] || die "--install-dir requires a path."
      INSTALL_DIR="$2"
      shift 2
      ;;
    --no-install)
      RUN_INSTALL="false"
      shift
      ;;
    --force-install)
      FORCE_INSTALL="true"
      shift
      ;;
    --)
      shift
      break
      ;;
    -*)
      die "Unknown option: $1"
      ;;
    *)
      if [[ -n "$REQUESTED_VERSION" ]]; then
        die "Version is already set: $REQUESTED_VERSION"
      fi
      REQUESTED_VERSION="$1"
      shift
      ;;
  esac
done

if [[ "$(id -u)" != "0" ]]; then
  die "Run bootstrap.sh as root."
fi

case "$INSTALL_DIR" in
  /*) ;;
  *) die "XRAY_MANAGER_INSTALL_DIR must be an absolute path." ;;
esac

if [[ "$INSTALL_DIR" == "/" || "$INSTALL_DIR" == "/root" || "$INSTALL_DIR" == "/usr" || "$INSTALL_DIR" == "/usr/local" ]]; then
  die "Refusing unsafe install directory: $INSTALL_DIR"
fi

if [[ "$FORCE_INSTALL" != "true" ]]; then
  if [[ -e /usr/local/etc/xray/config.json || -e /usr/local/etc/xray/manager.db ]]; then
    die "Xray VPS Manager already looks installed. Use xray-manager-update --check and xray-manager-update --update instead of bootstrap.sh."
  fi
fi

apt_get_with_lock_retry() {
  local attempts=12
  local attempt
  local output
  local status
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    output="$(apt-get -o DPkg::Lock::Timeout=300 "$@" 2>&1)" && {
      printf '%s\n' "$output"
      return 0
    }
    status=$?
    if ! grep -Eq "Could not get lock|Unable to lock|Could not open lock|dpkg frontend lock" <<<"$output"; then
      printf '%s\n' "$output" >&2
      return "$status"
    fi
    log "apt/dpkg lock is busy, waiting 5s... (${attempt}/${attempts})"
    sleep 5
  done
  printf '%s\n' "$output" >&2
  return "$status"
}

install_dependencies() {
  if ! command -v apt-get >/dev/null 2>&1; then
    die "This bootstrap currently supports Debian/Ubuntu servers with apt-get."
  fi
  log "Installing bootstrap dependencies"
  apt_get_with_lock_retry update
  apt_get_with_lock_retry install -y ca-certificates curl tar python3
}

# Python >= 3.12 для процессов менеджера (шаг 0a плана миграции). Порядок выбора (решение 2026-10-21):
# MANAGER_PYTHON, python3.13, python3.12, затем python3, если он сам >= 3.12 (берётся его версионный путь).
# Кандидат без модулей sqlite3, ssl, zlib, lzma или zoneinfo (сборка без dev-пакетов) пропускается.
# Системный python3 не заменяется и не затеняется. Функции одинаковы в bootstrap.sh и install.sh.
manager_python_probe() {
  # Версия X.Y.Z и через пробел модули, которые не импортируются; -I — без текущего каталога в sys.path.
  "$1" -I -c '
import importlib, sys
missing = []
for name in ("sqlite3", "ssl", "zlib", "lzma", "zoneinfo"):
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)
print("%d.%d.%d" % sys.version_info[:3], *missing)
' 2>/dev/null
}

manager_python_supported() {
  local path="$1"
  local probe version missing major minor
  # Только ограничения shebang, как в core.interpreter.check_interpreter: абсолютный путь без пробельных
  # символов ASCII, строка `#!<путь>` не длиннее 127 байт (буфер ядра до Linux 5.1, иначе Exec format error).
  # LC_ALL=C: длина считается в байтах и в UTF-8-локали.
  ( LC_ALL=C; [[ "$path" == /* && "$path" != *[[:space:]]* ]] && (( ${#path} <= 125 )) ) || return 1
  [[ -f "$path" && -x "$path" ]] || return 1
  probe="$(manager_python_probe "$path")" || return 1
  read -r version missing <<<"$probe"
  [[ -z "$missing" ]] || return 1
  IFS=. read -r major minor _ <<<"$version"
  [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] || return 1
  (( major > 3 || (major == 3 && minor >= 12) ))
}

find_manager_python() {
  local candidate path
  if [[ -n "${MANAGER_PYTHON:-}" ]]; then
    manager_python_supported "$MANAGER_PYTHON" || return 1
    printf '%s\n' "$MANAGER_PYTHON"
    return 0
  fi
  for candidate in python3.13 python3.12; do
    path="$(command -v "$candidate" 2>/dev/null || true)"
    if manager_python_supported "$path"; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  path="$(command -v python3 2>/dev/null || true)"
  if [[ "$path" == /* ]] && path="$(readlink -f "$path")" && manager_python_supported "$path"; then
    printf '%s\n' "$path"
    return 0
  fi
  return 1
}

require_manager_python() {
  local name path probe version missing found=""
  if XRAY_PYTHON="$(find_manager_python)"; then
    return 0
  fi
  if [[ -n "${MANAGER_PYTHON:-}" ]]; then
    echo "MANAGER_PYTHON=${MANAGER_PYTHON} must be an absolute path to an executable Python >= 3.12 with the sqlite3, ssl, zlib, lzma and zoneinfo modules, without whitespace and at most 125 bytes long." >&2
    exit 1
  fi
  for name in python3.13 python3.12 python3; do
    path="$(command -v "$name" 2>/dev/null || true)"
    [[ -n "$path" ]] || continue
    probe="$(manager_python_probe "$path" || true)"
    read -r version missing <<<"$probe"
    found="${found:+${found}; }${name} ${version:-(does not run)}${missing:+ without ${missing}}"
  done
  cat >&2 <<EOF
Xray VPS Manager needs Python >= 3.12 with the sqlite3, ssl, zlib, lzma and zoneinfo modules next to the system python3 (found: ${found:-no python3}).
Fresh install is supported on Ubuntu 24.04+ and Debian 13+, where the system Python is already 3.12 or newer.
Ubuntu 22.04: apt-get install -y software-properties-common && add-apt-repository -y ppa:deadsnakes/ppa && apt-get install -y python3.12
Debian 12/11: install the dev packages first (libsqlite3-dev libssl-dev zlib1g-dev liblzma-dev and others from docs/installation.md),
build Python 3.12 from source with "make altinstall" (it installs /usr/local/bin/python3.12 and keeps the system python3),
or pass its path explicitly: MANAGER_PYTHON=/path/to/python3.12.
Never replace the system python3 ("make install", python3 symlinks, update-alternatives).
See docs/installation.md, section "Python 3.12", then run the installer again.
EOF
  exit 1
}

latest_release_tag() {
  local api_url="https://api.github.com/repos/${REPO}/releases/latest"
  local payload
  local tag
  payload="$(curl -fsSL "$api_url")" || die "Could not fetch latest release from ${api_url}. Publish a release first or pass a tag explicitly."
  tag="$(printf '%s\n' "$payload" | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
  [[ -n "$tag" ]] || die "Could not parse latest release tag from GitHub API."
  printf '%s\n' "$tag"
}

resolve_version() {
  local value="$1"
  if [[ -z "$value" || "$value" == "latest" ]]; then
    latest_release_tag
  else
    normalize_tag "$value"
  fi
}

download_release() {
  local tag="$1"
  local target_dir="$2"
  local archive="$target_dir/source.tar.gz"
  local url="https://github.com/${REPO}/archive/refs/tags/${tag}.tar.gz"

  log "Downloading ${REPO} ${tag}"
  curl -fL --connect-timeout 20 --max-time 240 -o "$archive" "$url" \
    || die "Could not download release archive: $url"

  mkdir -p "$target_dir/extract"
  tar -xzf "$archive" --strip-components=1 -C "$target_dir/extract" \
    || die "Could not extract release archive."
}

prepare_install_dir() {
  local source_dir="$1"
  local timestamp
  local backup_dir

  if [[ -e "$INSTALL_DIR" ]]; then
    timestamp="$(date -u '+%Y%m%d%H%M%S')"
    backup_dir="${INSTALL_DIR}.bootstrap-backup.${timestamp}"
    log "Moving existing source directory to ${backup_dir}"
    mv "$INSTALL_DIR" "$backup_dir"
  fi

  mkdir -p "$INSTALL_DIR"
  cp -a "$source_dir"/. "$INSTALL_DIR"/
  find "$INSTALL_DIR" -name '._*' -delete
  chmod 0755 "$INSTALL_DIR/install.sh"
  find "$INSTALL_DIR" -maxdepth 1 -type f -name 'xray-*' -exec chmod 0755 {} \;
  chown -R root:root "$INSTALL_DIR"
}

validate_release_source() {
  local source_dir="$1"
  local required=(
    "install.sh"
    "xray-manager-update"
    "xray-menu"
    "xray-vps-manager"
    "xray_vps_manager"
  )
  local item
  for item in "${required[@]}"; do
    [[ -e "$source_dir/$item" ]] || die "Release archive is missing required item: $item"
  done
}

main() {
  local version
  local tmp_dir

  install_dependencies
  if [[ "$RUN_INSTALL" != "false" ]]; then
    require_manager_python
    log "Manager Python: ${XRAY_PYTHON}"
  fi
  version="$(resolve_version "$REQUESTED_VERSION")"
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT

  download_release "$version" "$tmp_dir"
  validate_release_source "$tmp_dir/extract"
  prepare_install_dir "$tmp_dir/extract"

  log "Xray VPS Manager ${version} is ready in ${INSTALL_DIR}"
  if [[ "$RUN_INSTALL" == "false" ]]; then
    log "Install step skipped because XRAY_MANAGER_RUN_INSTALL=false or --no-install was used."
    return
  fi

  log "Starting install.sh"
  cd "$INSTALL_DIR"
  bash "$INSTALL_DIR/install.sh"
}

main "$@"
