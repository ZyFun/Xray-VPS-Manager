#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${XRAY_MANAGER_REPO:-ZyFun/Xray-VPS-Manager}"
INSTALL_DIR="${XRAY_MANAGER_INSTALL_DIR:-/root/xray_server}"
REQUESTED_VERSION="${XRAY_MANAGER_VERSION:-}"
RUN_INSTALL="${XRAY_MANAGER_RUN_INSTALL:-true}"
FORCE_INSTALL="false"

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
  XRAY_MANAGER_VERSION      Release tag to install, for example v2.0.0.
  XRAY_MANAGER_INSTALL_DIR  Target source directory, default /root/xray_server.
  XRAY_MANAGER_RUN_INSTALL  Set false to download only.
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
  apt_get_with_lock_retry install -y ca-certificates curl tar
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
