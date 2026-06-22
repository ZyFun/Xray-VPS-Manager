#!/usr/bin/env bash
set -Eeuo pipefail

SELF_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")"
SELF_NAME="$(basename -- "$0")"

CADDYFILE_PATH="${CADDYFILE_PATH:-/etc/caddy/Caddyfile}"
CADDY_CONF_DIR="${CADDY_CONF_DIR:-/etc/caddy/conf.d}"
PROXY_ENV_PATH="${PROXY_ENV_PATH:-/etc/caddy/xhttp-download-proxy.env}"
PROXY_SITE_PATH="${PROXY_SITE_PATH:-/etc/caddy/conf.d/xhttp-download-proxy.caddy}"
INSTALLED_MENU_PATH="${INSTALLED_MENU_PATH:-/usr/local/sbin/caddy-menu}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
RANDOM_TLS_SERVICE_NAME="${RANDOM_TLS_SERVICE_NAME:-xhttp-download-proxy-random-tls.service}"
RANDOM_TLS_TIMER_NAME="${RANDOM_TLS_TIMER_NAME:-xhttp-download-proxy-random-tls.timer}"

DOWNLOAD_DOMAIN="${DOWNLOAD_DOMAIN:-}"
UPSTREAM_DOMAIN="${UPSTREAM_DOMAIN:-}"
UPSTREAM_PORT="${UPSTREAM_PORT:-443}"
XHTTP_PATH="${XHTTP_PATH:-/vless-xhttp}"
XHTTP_MODE="${XHTTP_MODE:-auto}"
TLS_FINGERPRINT="${TLS_FINGERPRINT:-chrome}"
TLS_ALPN="${TLS_ALPN:-h2}"
TLS_PROFILE="${TLS_PROFILE:-default}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_root() {
  if [[ "$(id -u)" != "0" ]]; then
    die "Run this command as root."
  fi
}

usage() {
  cat <<'EOF'
Usage:
  bash install-caddy-download-proxy.sh [--no-config]
  caddy-menu
  caddy-menu configure
  caddy-menu show
  caddy-menu preview-caddyfile
  caddy-menu print-download-settings
  caddy-menu tls
  caddy-menu random-tls-enable
  caddy-menu random-tls-disable
  caddy-menu random-tls-run [--quiet]
  caddy-menu random-tls-status
  caddy-menu test
  caddy-menu validate
  caddy-menu logs

Environment for unattended setup:
  DOWNLOAD_DOMAIN=cdn.example.com
  UPSTREAM_DOMAIN=api.example.com
  UPSTREAM_PORT=443
  XHTTP_PATH=/vless-xhttp
  XHTTP_MODE=auto
  TLS_FINGERPRINT=chrome
  TLS_ALPN=h2
  TLS_PROFILE=default
EOF
}

validate_host() {
  local value="$1"
  local label="$2"
  if [[ -z "$value" || "$value" == *"/"* || "$value" == *":"* ]]; then
    die "${label} must be a domain without scheme, path, or port."
  fi
  if [[ ! "$value" =~ ^[A-Za-z0-9.-]+$ ]]; then
    die "${label} may contain only A-Z, a-z, 0-9, dots, and hyphens."
  fi
}

validate_port() {
  local value="$1"
  local label="$2"
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    die "${label} must be a number from 1 to 65535."
  fi
  local number=$((10#$value))
  if (( number < 1 || number > 65535 )); then
    die "${label} must be a number from 1 to 65535."
  fi
}

validate_xhttp_path() {
  local value="$1"
  if [[ ! "$value" =~ ^/[A-Za-z0-9._~/-]{0,255}$ ]]; then
    die "XHTTP_PATH must start with / and contain only A-Z a-z 0-9 . _ ~ - / characters."
  fi
}

validate_xhttp_mode() {
  local value="$1"
  case "$value" in
    auto|packet-up|stream-up|stream-one) ;;
    *) die "XHTTP_MODE must be one of: auto, packet-up, stream-up, stream-one." ;;
  esac
}

validate_fingerprint() {
  local value="$1"
  case "$value" in
    chrome|firefox|safari|ios|android|edge|360|qq|random|randomized) ;;
    *) die "TLS_FINGERPRINT must be one of: chrome, firefox, safari, ios, android, edge, 360, qq, random, randomized." ;;
  esac
}

validate_alpn() {
  local value="$1"
  if [[ -z "$value" ]]; then
    die "TLS_ALPN must not be empty. Use h2 for Caddy download endpoints."
  fi
  if [[ ! "$value" =~ ^[A-Za-z0-9.,_/-]+$ ]]; then
    die "TLS_ALPN must be a comma-separated list like h2 or h2,http/1.1."
  fi
}

normalize_tls_profile() {
  local value
  value="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr '-' '_')"
  case "$value" in
    default|caddy|auto)
      printf 'default'
      ;;
    tls12|tls1.2|tls_12)
      printf 'tls12'
      ;;
    tls12_13|tls1.2+tls1.3|tls1.2_1.3|tls_12_13)
      printf 'tls12_13'
      ;;
    tls13|tls1.3|tls_13)
      printf 'tls13'
      ;;
    *)
      return 1
      ;;
  esac
}

validate_tls_profile() {
  local normalized
  if ! normalized="$(normalize_tls_profile "$1")"; then
    die "TLS_PROFILE must be one of: default, tls12, tls12_13, tls13."
  fi
  TLS_PROFILE="$normalized"
}

tls_profile_label() {
  case "$1" in
    default) printf 'Caddy default' ;;
    tls12) printf 'TLS 1.2 only' ;;
    tls12_13) printf 'TLS 1.2 + TLS 1.3' ;;
    tls13) printf 'TLS 1.3 only' ;;
    *) printf '%s' "$1" ;;
  esac
}

validate_settings() {
  validate_host "$DOWNLOAD_DOMAIN" "DOWNLOAD_DOMAIN"
  validate_host "$UPSTREAM_DOMAIN" "UPSTREAM_DOMAIN"
  validate_port "$UPSTREAM_PORT" "UPSTREAM_PORT"
  validate_xhttp_path "$XHTTP_PATH"
  validate_xhttp_mode "$XHTTP_MODE"
  validate_fingerprint "$TLS_FINGERPRINT"
  validate_alpn "$TLS_ALPN"
  validate_tls_profile "$TLS_PROFILE"
}

apt_get_with_lock_retry() {
  local attempt=1
  local max_attempts=60
  local output status
  while true; do
    output="$(apt-get -o DPkg::Lock::Timeout=300 "$@" 2>&1)" && {
      printf '%s\n' "$output"
      return 0
    }
    status=$?
    if ! grep -qiE 'Could not get lock|Unable to lock|Could not open lock|is another process using it' <<<"$output"; then
      printf '%s\n' "$output" >&2
      return "$status"
    fi
    if (( attempt >= max_attempts )); then
      printf '%s\n' "$output" >&2
      die "Timed out waiting for apt/dpkg locks."
    fi
    echo "apt/dpkg lock is busy, waiting 5s... (${attempt}/${max_attempts})" >&2
    sleep 5
    attempt=$((attempt + 1))
  done
}

install_caddy_if_needed() {
  require_root
  if command -v caddy >/dev/null 2>&1; then
    echo "Caddy already installed: $(caddy version 2>/dev/null | head -n 1 || true)"
    return
  fi
  command -v apt-get >/dev/null 2>&1 || die "Caddy is not installed and apt-get is unavailable."
  export DEBIAN_FRONTEND=noninteractive
  apt_get_with_lock_retry update
  apt_get_with_lock_retry install -y ca-certificates curl caddy
}

ensure_caddyfile_import() {
  require_root
  install -d -m 0755 "$(dirname -- "$CADDYFILE_PATH")"
  install -d -m 0755 "$CADDY_CONF_DIR"
  local import_line="import ${CADDY_CONF_DIR}/*.caddy"
  if [[ ! -f "$CADDYFILE_PATH" ]]; then
    printf '%s\n' "$import_line" >"$CADDYFILE_PATH"
    chmod 0644 "$CADDYFILE_PATH"
    return
  fi
  if grep -Fqx "$import_line" "$CADDYFILE_PATH"; then
    return
  fi
  {
    printf '\n'
    printf '# Managed by xHTTP download proxy installer\n'
    printf '%s\n' "$import_line"
  } >>"$CADDYFILE_PATH"
}

upstream_url() {
  if [[ "$UPSTREAM_PORT" == "443" ]]; then
    printf 'https://%s' "$UPSTREAM_DOMAIN"
  else
    printf 'https://%s:%s' "$UPSTREAM_DOMAIN" "$UPSTREAM_PORT"
  fi
}

load_proxy_defaults() {
  load_env_config
  DOWNLOAD_DOMAIN="${DOWNLOAD_DOMAIN:-cdn.example.com}"
  UPSTREAM_DOMAIN="${UPSTREAM_DOMAIN:-api.example.com}"
  UPSTREAM_PORT="${UPSTREAM_PORT:-443}"
  XHTTP_PATH="${XHTTP_PATH:-/vless-xhttp}"
  XHTTP_MODE="${XHTTP_MODE:-auto}"
  TLS_FINGERPRINT="${TLS_FINGERPRINT:-chrome}"
  TLS_ALPN="${TLS_ALPN:-h2}"
  TLS_PROFILE="${TLS_PROFILE:-default}"
  validate_tls_profile "$TLS_PROFILE"
}

backup_file() {
  local path="$1"
  if [[ -f "$path" ]]; then
    cp -a "$path" "${path}.bak.$(date -u +%Y%m%d%H%M%S)"
  fi
}

write_env_config() {
  install -d -m 0755 "$(dirname -- "$PROXY_ENV_PATH")"
  cat >"$PROXY_ENV_PATH" <<EOF
DOWNLOAD_DOMAIN=${DOWNLOAD_DOMAIN}
UPSTREAM_DOMAIN=${UPSTREAM_DOMAIN}
UPSTREAM_PORT=${UPSTREAM_PORT}
XHTTP_PATH=${XHTTP_PATH}
XHTTP_MODE=${XHTTP_MODE}
TLS_FINGERPRINT=${TLS_FINGERPRINT}
TLS_ALPN=${TLS_ALPN}
TLS_PROFILE=${TLS_PROFILE}
EOF
  chmod 0644 "$PROXY_ENV_PATH"
}

load_env_config() {
  if [[ -f "$PROXY_ENV_PATH" ]]; then
    # shellcheck disable=SC1090
    source "$PROXY_ENV_PATH"
  fi
}

write_proxy_site() {
  require_root
  validate_settings
  ensure_caddyfile_import
  backup_file "$PROXY_SITE_PATH"
  proxy_site_config >"$PROXY_SITE_PATH"
  chmod 0644 "$PROXY_SITE_PATH"
  write_env_config
}

proxy_site_config() {
  validate_settings
  cat <<EOF
# Managed by xHTTP download proxy installer.
# Client download endpoint: ${DOWNLOAD_DOMAIN}
# Main XHTTP endpoint: ${UPSTREAM_DOMAIN}:${UPSTREAM_PORT}
${DOWNLOAD_DOMAIN} {
$(tls_site_block)
    encode zstd gzip

    reverse_proxy $(upstream_url) {
        header_up Host ${UPSTREAM_DOMAIN}
        flush_interval -1
        transport http {
            tls_server_name ${UPSTREAM_DOMAIN}
        }
    }
}
EOF
}

tls_site_block() {
  case "$TLS_PROFILE" in
    default)
      return
      ;;
    tls12)
      cat <<'EOF'
    tls {
        protocols tls1.2 tls1.2
    }

EOF
      ;;
    tls12_13)
      cat <<'EOF'
    tls {
        protocols tls1.2 tls1.3
    }

EOF
      ;;
    tls13)
      cat <<'EOF'
    tls {
        protocols tls1.3 tls1.3
    }

EOF
      ;;
  esac
}

validate_caddy() {
  require_root
  caddy validate --config "$CADDYFILE_PATH"
}

reload_caddy() {
  require_root
  validate_caddy
  systemctl enable --now caddy
  if ! systemctl reload caddy; then
    systemctl restart caddy
  fi
}

require_saved_proxy_config() {
  if [[ ! -f "$PROXY_ENV_PATH" ]]; then
    die "Download proxy is not configured yet. Run caddy-menu configure first."
  fi
  load_env_config
  validate_settings
}

configure_proxy_noninteractive() {
  require_root
  load_env_config
  validate_settings
  install_caddy_if_needed
  write_proxy_site
  reload_caddy
  echo "Download proxy configured:"
  echo "  client download: https://${DOWNLOAD_DOMAIN}${XHTTP_PATH}"
  echo "  upstream: $(upstream_url)"
  echo "  config: ${PROXY_SITE_PATH}"
  print_download_settings
}

prompt_value() {
  local var_name="$1"
  local prompt="$2"
  local current="$3"
  local input
  read -r -p "${prompt} [${current}]: " input
  printf -v "$var_name" '%s' "${input:-$current}"
}

prompt_with_validation() {
  local var_name="$1"
  local label="$2"
  local current="$3"
  local validator="$4"
  local input
  while true; do
    read -r -p "${label} [${current}]: " input
    input="${input:-$current}"
    case "$validator" in
      host)
        if ( validate_host "$input" "$label" ); then
          printf -v "$var_name" '%s' "$input"
          return
        fi
        ;;
      port)
        if ( validate_port "$input" "$label" ); then
          printf -v "$var_name" '%s' "$input"
          return
        fi
        ;;
      path)
        if ( validate_xhttp_path "$input" ); then
          printf -v "$var_name" '%s' "$input"
          return
        fi
        ;;
      fingerprint)
        if ( validate_fingerprint "$input" ); then
          printf -v "$var_name" '%s' "$input"
          return
        fi
        ;;
      alpn)
        if ( validate_alpn "$input" ); then
          printf -v "$var_name" '%s' "$input"
          return
        fi
        ;;
    esac
    echo "Попробуй ещё раз."
  done
}

prompt_xhttp_mode() {
  local input
  echo "XHTTP_MODE: режим xHTTP для downloadSettings."
  echo "  1) auto"
  echo "  2) packet-up"
  echo "  3) stream-up"
  echo "  4) stream-one"
  while true; do
    read -r -p "XHTTP_MODE [${XHTTP_MODE}] (номер из списка): " input
    input="${input:-$XHTTP_MODE}"
    case "$input" in
      1|auto) XHTTP_MODE="auto"; return ;;
      2|packet-up) XHTTP_MODE="packet-up"; return ;;
      3|stream-up) XHTTP_MODE="stream-up"; return ;;
      4|stream-one) XHTTP_MODE="stream-one"; return ;;
      *) echo "Выбери 1-4 или введи auto/packet-up/stream-up/stream-one." ;;
    esac
  done
}

print_settings_overview() {
  echo
  echo "Настройки xHTTP download proxy"
  echo
  echo "Что сейчас настраивается:"
  echo "  Клиент будет ходить за download/downstream на https://${DOWNLOAD_DOMAIN}${XHTTP_PATH}"
  echo "  Этот Caddy проксирует запросы на $(upstream_url)"
  echo "  Upload/upstream остаётся на основном сервере и в основной VLESS-ссылке."
  echo
  echo "Текущие значения:"
  echo "  1) DOWNLOAD_DOMAIN=${DOWNLOAD_DOMAIN}"
  echo "     Публичный домен второго сервера. Он попадёт в downloadSettings.address и TLS SNI клиента."
  echo "  2) UPSTREAM_DOMAIN=${UPSTREAM_DOMAIN}"
  echo "     Основной xHTTP/TLS домен первого сервера, куда второй Caddy отправит reverse_proxy."
  echo "  3) UPSTREAM_PORT=${UPSTREAM_PORT}"
  echo "     HTTPS-порт основного endpoint. Обычно 443."
  echo "  4) XHTTP_PATH=${XHTTP_PATH}"
  echo "     Тот же xHTTP path, который настроен на основном подключении."
  echo "  5) XHTTP_MODE=${XHTTP_MODE}"
  echo "     Режим xHTTP в клиентском downloadSettings: auto, packet-up, stream-up или stream-one."
  echo "  6) TLS_FINGERPRINT=${TLS_FINGERPRINT}"
  echo "     Fingerprint для клиентского TLS профиля в downloadSettings. Обычно chrome."
  echo "  7) TLS_ALPN=${TLS_ALPN}"
  echo "     ALPN для клиентского TLS профиля. Для Caddy обычно h2."
  echo "  8) TLS_PROFILE=${TLS_PROFILE} ($(tls_profile_label "$TLS_PROFILE"))"
  echo "     TLS versions для участка client -> ${DOWNLOAD_DOMAIN}. Randomizer меняет именно этот профиль."
}

edit_download_domain() {
  echo
  echo "DOWNLOAD_DOMAIN"
  echo "Это поддомен второго сервера, который смотрит на IP этого Caddy-only сервера."
  echo "Вводи только домен без https://, порта и path. Пример: cdn.example.com"
  prompt_with_validation DOWNLOAD_DOMAIN "DOWNLOAD_DOMAIN" "$DOWNLOAD_DOMAIN" host
}

edit_upstream_domain() {
  echo
  echo "UPSTREAM_DOMAIN"
  echo "Это основной xHTTP/TLS endpoint первого сервера. Второй Caddy будет проксировать туда download-запросы."
  echo "Вводи только домен без https://, порта и path. Пример: api.example.com"
  prompt_with_validation UPSTREAM_DOMAIN "UPSTREAM_DOMAIN" "$UPSTREAM_DOMAIN" host
}

edit_upstream_port() {
  echo
  echo "UPSTREAM_PORT"
  echo "Это HTTPS-порт основного endpoint. Если основной Caddy слушает стандартный TLS, оставь 443."
  prompt_with_validation UPSTREAM_PORT "UPSTREAM_PORT" "${UPSTREAM_PORT:-443}" port
}

edit_xhttp_path() {
  echo
  echo "XHTTP_PATH"
  echo "Это path основного xHTTP-подключения. Он должен совпадать с path на первом сервере."
  echo "Начинается с /. Пример: /vless-xhttp или /api/v1/sync"
  prompt_with_validation XHTTP_PATH "XHTTP_PATH" "$XHTTP_PATH" path
}

edit_xhttp_mode() {
  echo
  echo "XHTTP_MODE"
  echo "Это режим xHTTP, который попадёт в клиентский downloadSettings."
  echo "Обычно оставляй auto, если нет причины принудительно выбрать конкретный режим."
  prompt_xhttp_mode
}

edit_tls_fingerprint() {
  echo
  echo "TLS_FINGERPRINT"
  echo "Это browser fingerprint для клиентской TLS-маскировки в downloadSettings."
  echo "Обычно оставляют chrome. Для маскировки под iOS-приложение выбирай ios."
  prompt_tls_fingerprint
}

prompt_tls_fingerprint() {
  local input
  while true; do
    echo "Выбери TLS_FINGERPRINT:"
    echo "  1) chrome"
    echo "  2) firefox"
    echo "  3) safari"
    echo "  4) ios"
    echo "  5) android"
    echo "  6) edge"
    echo "  7) 360"
    echo "  8) qq"
    echo "  9) random"
    echo "  10) randomized"
    read -r -p "TLS_FINGERPRINT [${TLS_FINGERPRINT}] (номер или значение): " input
    input="${input:-$TLS_FINGERPRINT}"
    input="$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')"
    case "$input" in
      1) TLS_FINGERPRINT="chrome" ;;
      2) TLS_FINGERPRINT="firefox" ;;
      3) TLS_FINGERPRINT="safari" ;;
      4) TLS_FINGERPRINT="ios" ;;
      5) TLS_FINGERPRINT="android" ;;
      6) TLS_FINGERPRINT="edge" ;;
      7) TLS_FINGERPRINT="360" ;;
      8) TLS_FINGERPRINT="qq" ;;
      9) TLS_FINGERPRINT="random" ;;
      10) TLS_FINGERPRINT="randomized" ;;
      chrome|firefox|safari|ios|android|edge|360|qq|random|randomized)
        TLS_FINGERPRINT="$input"
        ;;
      *)
        echo "Неверное значение. Выбери номер из списка или введи одно из допустимых значений."
        echo
        continue
        ;;
    esac
    return
  done
}

edit_tls_alpn() {
  echo
  echo "TLS_ALPN"
  echo "Это список ALPN для клиентского TLS профиля. Для Caddy download endpoint обычно h2."
  echo "Можно указать несколько через запятую, например: h2,http/1.1"
  prompt_tls_alpn
}

prompt_tls_alpn() {
  local input
  while true; do
    echo "Выбери TLS_ALPN:"
    echo "  1) h2 (рекомендуется для XHTTP через Caddy)"
    echo "  2) h2,http/1.1 (совместимость с HTTP/1.1 fallback)"
    echo "  3) оставить текущее значение: ${TLS_ALPN}"
    echo "  4) ввести вручную"
    read -r -p "TLS_ALPN [${TLS_ALPN}] (номер или список): " input
    input="${input:-3}"
    case "$input" in
      1) TLS_ALPN="h2"; return ;;
      2) TLS_ALPN="h2,http/1.1"; return ;;
      3|current|same|оставить) return ;;
      4|manual|custom|m|свой)
        prompt_with_validation TLS_ALPN "TLS_ALPN" "$TLS_ALPN" alpn
        return
        ;;
      *)
        if ( validate_alpn "$input" ); then
          TLS_ALPN="$input"
          return
        fi
        echo "Выбери 1-4 или введи список ALPN через запятую."
        ;;
    esac
  done
}

edit_tls_profile() {
  echo
  echo "TLS_PROFILE"
  echo "Это TLS version policy для входящего TLS на втором Caddy: client -> ${DOWNLOAD_DOMAIN}."
  echo "Обычно оставляют Caddy default или TLS 1.2 + TLS 1.3."
  prompt_tls_profile
}

prompt_tls_profile() {
  local input normalized
  while true; do
    echo "Выбери TLS_PROFILE:"
    echo "  1) default (Caddy default)"
    echo "  2) tls12 (только TLS 1.2)"
    echo "  3) tls12_13 (TLS 1.2 + TLS 1.3)"
    echo "  4) tls13 (только TLS 1.3)"
    read -r -p "TLS_PROFILE [${TLS_PROFILE}] (номер или значение): " input
    input="${input:-$TLS_PROFILE}"
    case "$input" in
      1) TLS_PROFILE="default"; return ;;
      2) TLS_PROFILE="tls12"; return ;;
      3) TLS_PROFILE="tls12_13"; return ;;
      4) TLS_PROFILE="tls13"; return ;;
      *)
        if normalized="$(normalize_tls_profile "$input")"; then
          TLS_PROFILE="$normalized"
          return
        fi
        echo "Неверное значение. Выбери 1-4 или введи default/tls12/tls12_13/tls13."
        ;;
    esac
  done
}

preview_caddyfile() {
  load_proxy_defaults
  preview_caddyfile_current
}

preview_caddyfile_current() {
  echo
  echo "Preview Caddy site config (${PROXY_SITE_PATH}):"
  echo
  proxy_site_config
}

apply_proxy_settings() {
  validate_settings
  install_caddy_if_needed
  write_proxy_site
  reload_caddy
  echo
  echo "Caddy download proxy настроен."
  print_download_settings_current
}

next_random_tls_profile() {
  validate_tls_profile "$TLS_PROFILE"
  case "$TLS_PROFILE" in
    tls12)
      printf 'tls13'
      ;;
    tls13)
      printf 'tls12'
      ;;
    *)
      if (( RANDOM % 2 )); then
        printf 'tls12'
      else
        printf 'tls13'
      fi
      ;;
  esac
}

next_random_tls_label() {
  local next
  next="$(next_random_tls_profile)"
  tls_profile_label "$next"
}

random_tls_service_path() {
  printf '%s/%s' "$SYSTEMD_DIR" "$RANDOM_TLS_SERVICE_NAME"
}

random_tls_timer_path() {
  printf '%s/%s' "$SYSTEMD_DIR" "$RANDOM_TLS_TIMER_NAME"
}

write_random_tls_units() {
  require_root
  install -d -m 0755 "$SYSTEMD_DIR"
  cat >"$(random_tls_service_path)" <<EOF
[Unit]
Description=Randomize Caddy TLS protocol profile for xHTTP download proxy
After=network-online.target caddy.service
Wants=network-online.target
ConditionPathExists=${PROXY_ENV_PATH}

[Service]
Type=oneshot
ExecStart=${INSTALLED_MENU_PATH} random-tls-run --quiet
EOF
  chmod 0644 "$(random_tls_service_path)"
  cat >"$(random_tls_timer_path)" <<EOF
[Unit]
Description=Randomize Caddy TLS protocol profile every 15-60 minutes

[Timer]
OnBootSec=15min
OnUnitActiveSec=15min
RandomizedDelaySec=45min
AccuracySec=1min
Unit=${RANDOM_TLS_SERVICE_NAME}

[Install]
WantedBy=timers.target
EOF
  chmod 0644 "$(random_tls_timer_path)"
}

random_tls_timer_value() {
  local command="$1"
  systemctl "$command" "$RANDOM_TLS_TIMER_NAME" 2>/dev/null || true
}

random_tls_status() {
  load_proxy_defaults
  echo "TLS profile: ${TLS_PROFILE} ($(tls_profile_label "$TLS_PROFILE"))"
  echo "Next random strict profile: $(next_random_tls_label)"
  echo "Timer: ${RANDOM_TLS_TIMER_NAME}"
  echo "Service: ${RANDOM_TLS_SERVICE_NAME}"
  echo "Timer enabled: $(random_tls_timer_value is-enabled)"
  echo "Timer active: $(random_tls_timer_value is-active)"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl list-timers --no-pager --all "$RANDOM_TLS_TIMER_NAME" || true
  fi
}

enable_random_tls() {
  require_root
  require_saved_proxy_config
  write_random_tls_units
  systemctl daemon-reload
  systemctl enable --now "$RANDOM_TLS_TIMER_NAME"
  echo "Random TLS timer enabled: ${RANDOM_TLS_TIMER_NAME}"
  echo "It will switch between TLS 1.2 only and TLS 1.3 only every 15-60 minutes."
  echo "Current profile: $(tls_profile_label "$TLS_PROFILE")"
  echo "Next possible switch: $(next_random_tls_label)"
}

disable_random_tls() {
  require_root
  systemctl disable --now "$RANDOM_TLS_TIMER_NAME" || true
  systemctl daemon-reload || true
  echo "Random TLS timer disabled: ${RANDOM_TLS_TIMER_NAME}"
}

run_random_tls_once() {
  local quiet="${1:-false}"
  require_root
  require_saved_proxy_config
  local previous next
  previous="$TLS_PROFILE"
  next="$(next_random_tls_profile)"
  TLS_PROFILE="$next"
  write_proxy_site
  reload_caddy
  if [[ "$quiet" != "true" ]]; then
    echo "Random TLS switched: $(tls_profile_label "$previous") -> $(tls_profile_label "$TLS_PROFILE")"
    echo "Site config: ${PROXY_SITE_PATH}"
    echo "Next switch target: $(next_random_tls_label)"
  fi
}

random_tls_logs() {
  journalctl -u "$RANDOM_TLS_SERVICE_NAME" -u "$RANDOM_TLS_TIMER_NAME" -n 120 --no-pager || true
}

set_tls_profile_interactive() {
  require_root
  require_saved_proxy_config
  edit_tls_profile
  write_proxy_site
  reload_caddy
  echo "TLS profile applied: ${TLS_PROFILE} ($(tls_profile_label "$TLS_PROFILE"))"
}

tls_menu_loop() {
  require_root
  while true; do
    echo
    echo "TLS / Randomizer"
    random_tls_status
    echo
    echo "1) Изменить TLS profile"
    echo "2) Включить random TLS timer"
    echo "3) Отключить random TLS timer"
    echo "4) Переключить TLS случайно сейчас"
    echo "5) Показать random TLS logs"
    echo "0) Назад"
    read -r -p "Выбор: " choice
    case "$choice" in
      1) set_tls_profile_interactive ;;
      2) enable_random_tls ;;
      3) disable_random_tls ;;
      4) run_random_tls_once false ;;
      5) random_tls_logs ;;
      0) return ;;
      *) echo "Неизвестный пункт." ;;
    esac
  done
}

configure_proxy_interactive() {
  require_root
  load_proxy_defaults

  echo
  echo "Интерактивная настройка второго Caddy-сервера для xHTTP downloadSettings."
  echo "На этом сервере устанавливаются только Caddy и команда caddy-menu."
  echo "Выбери параметр, посмотри описание, измени значение и затем применяй конфиг."

  while true; do
    print_settings_overview
    echo
    echo "Действия:"
    echo "  1) Изменить DOWNLOAD_DOMAIN"
    echo "  2) Изменить UPSTREAM_DOMAIN"
    echo "  3) Изменить UPSTREAM_PORT"
    echo "  4) Изменить XHTTP_PATH"
    echo "  5) Изменить XHTTP_MODE"
    echo "  6) Изменить TLS_FINGERPRINT"
    echo "  7) Изменить TLS_ALPN"
    echo "  8) Изменить TLS_PROFILE"
    echo "  9) Preview Caddyfile"
    echo "  10) Preview JSON downloadSettings"
    echo "  A) Применить настройки: записать Caddy config, validate и reload"
    echo "  0) Назад без применения"
    read -r -p "Выбор: " choice
    case "$choice" in
      1) edit_download_domain ;;
      2) edit_upstream_domain ;;
      3) edit_upstream_port ;;
      4) edit_xhttp_path ;;
      5) edit_xhttp_mode ;;
      6) edit_tls_fingerprint ;;
      7) edit_tls_alpn ;;
      8) edit_tls_profile ;;
      9) preview_caddyfile_current ;;
      10) print_download_settings_current ;;
      a|A|а|А) apply_proxy_settings; return ;;
      0) echo "Настройка отменена."; return ;;
      *) echo "Неизвестный пункт." ;;
    esac
  done
}

json_alpn_array() {
  local raw="$TLS_ALPN"
  local result="["
  local first=1
  local item
  IFS=',' read -ra items <<<"$raw"
  for item in "${items[@]}"; do
    item="$(printf '%s' "$item" | xargs)"
    [[ -n "$item" ]] || continue
    if (( first )); then
      first=0
    else
      result+=", "
    fi
    result+="\"${item}\""
  done
  result+="]"
  printf '%s' "$result"
}

print_download_settings() {
  load_proxy_defaults
  print_download_settings_current
}

print_download_settings_current() {
  validate_settings
  cat <<EOF

Добавь эти значения в xHTTP downloadSettings:

{
  "downloadSettings": {
    "address": "${DOWNLOAD_DOMAIN}",
    "port": 443,
    "network": "xhttp",
    "security": "tls",
    "tlsSettings": {
      "serverName": "${DOWNLOAD_DOMAIN}",
      "fingerprint": "${TLS_FINGERPRINT}",
      "alpn": $(json_alpn_array)
    },
    "xhttpSettings": {
      "path": "${XHTTP_PATH}",
      "mode": "${XHTTP_MODE}"
    }
  }
}
EOF
}

show_config() {
  load_proxy_defaults
  echo "Paths:"
  echo "  Caddyfile: ${CADDYFILE_PATH}"
  echo "  proxy site: ${PROXY_SITE_PATH}"
  echo "  proxy env: ${PROXY_ENV_PATH}"
  echo
  print_settings_overview
  echo
  if [[ -f "$PROXY_ENV_PATH" ]]; then
    echo "Current settings:"
    sed 's/^/  /' "$PROXY_ENV_PATH"
  else
    echo "Current settings: not configured"
  fi
  echo
  if [[ -f "$PROXY_SITE_PATH" ]]; then
    echo "Caddy site config:"
    sed 's/^/  /' "$PROXY_SITE_PATH"
  else
    echo "Caddy site config: not found"
  fi
}

test_proxy() {
  load_env_config
  validate_settings
  echo "DNS:"
  getent hosts "$DOWNLOAD_DOMAIN" || true
  echo
  echo "Caddy validate:"
  validate_caddy
  echo
  echo "TLS/proxy request:"
  if command -v curl >/dev/null 2>&1; then
    curl -vk --connect-timeout 10 --max-time 25 "https://${DOWNLOAD_DOMAIN}${XHTTP_PATH}" || true
  else
    echo "curl is not installed."
  fi
  echo
  echo "Для XHTTP path нормален не-html ответ. Главное: DNS, TLS и проксирование доходят до основного сервера."
}

show_status() {
  systemctl --no-pager --full status caddy.service || true
}

show_logs() {
  journalctl -u caddy.service -n 120 --no-pager || true
}

install_self_as_menu() {
  require_root
  install -d -m 0755 "$(dirname -- "$INSTALLED_MENU_PATH")"
  install -m 0755 "$SELF_PATH" "$INSTALLED_MENU_PATH"
  echo "Installed menu command: ${INSTALLED_MENU_PATH}"
}

run_installer() {
  local no_config="false"
  while (($#)); do
    case "$1" in
      -h|--help|help)
        usage
        return
        ;;
      --no-config)
        no_config="true"
        ;;
      *)
        die "Unknown installer argument: $1"
        ;;
    esac
    shift
  done

  require_root
  install_caddy_if_needed
  install_self_as_menu

  if [[ "$no_config" == "true" ]]; then
    echo "Run caddy-menu when you are ready to configure the download proxy."
    return
  fi

  if [[ -n "$DOWNLOAD_DOMAIN" && -n "$UPSTREAM_DOMAIN" ]]; then
    configure_proxy_noninteractive
    return
  fi

  if [[ -t 0 ]]; then
    local answer
    read -r -p "Настроить xHTTP download proxy сейчас? [Y/n]: " answer
    case "${answer:-Y}" in
      y|Y|yes|YES|д|Д|да|Да) configure_proxy_interactive ;;
      *) echo "Run caddy-menu when you are ready to configure it." ;;
    esac
  else
    echo "Caddy and caddy-menu are installed."
    echo "For unattended config, re-run with DOWNLOAD_DOMAIN and UPSTREAM_DOMAIN, or run caddy-menu interactively."
  fi
}

menu_loop() {
  require_root
  while true; do
    echo
    echo "xHTTP Download Proxy / Caddy"
    echo "1) Установить или проверить Caddy"
    echo "2) Настроить download proxy"
    echo "3) Показать текущую настройку"
    echo "4) Preview Caddyfile"
    echo "5) Показать JSON downloadSettings"
    echo "6) Проверить DNS/TLS/proxy"
    echo "7) TLS / Randomizer"
    echo "8) Validate + reload Caddy"
    echo "9) Статус Caddy"
    echo "10) Логи Caddy"
    echo "0) Выход"
    read -r -p "Выбор: " choice
    case "$choice" in
      1) install_caddy_if_needed ;;
      2) configure_proxy_interactive ;;
      3) show_config ;;
      4) preview_caddyfile ;;
      5) print_download_settings ;;
      6) test_proxy ;;
      7) tls_menu_loop ;;
      8) reload_caddy ;;
      9) show_status ;;
      10) show_logs ;;
      0) return ;;
      *) echo "Неизвестный пункт." ;;
    esac
  done
}

run_menu_command() {
  local command="${1:-menu}"
  if (($#)); then
    shift
  fi
  case "$command" in
    -h|--help|help) usage ;;
    menu) menu_loop ;;
    install-caddy) install_caddy_if_needed ;;
    configure) configure_proxy_interactive ;;
    configure-noninteractive) configure_proxy_noninteractive ;;
    show) show_config ;;
    preview-caddyfile) preview_caddyfile ;;
    print-download-settings) print_download_settings ;;
    tls) tls_menu_loop ;;
    tls-profile) set_tls_profile_interactive ;;
    random-tls-enable) enable_random_tls ;;
    random-tls-disable) disable_random_tls ;;
    random-tls-run)
      local quiet="false"
      if [[ "${1:-}" == "--quiet" ]]; then
        quiet="true"
      fi
      run_random_tls_once "$quiet"
      ;;
    random-tls-status) random_tls_status ;;
    random-tls-logs) random_tls_logs ;;
    test) test_proxy ;;
    validate) validate_caddy ;;
    reload) reload_caddy ;;
    status) show_status ;;
    logs) show_logs ;;
    *) die "Unknown caddy-menu command: ${command}" ;;
  esac
}

main() {
  if [[ "$SELF_NAME" == "caddy-menu" ]]; then
    run_menu_command "$@"
    return
  fi
  if [[ "${1:-}" == "menu" ]]; then
    shift
    run_menu_command "${1:-menu}" "${@:2}"
    return
  fi
  run_installer "$@"
}

main "$@"
