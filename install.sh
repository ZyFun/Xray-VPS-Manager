#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
XRAY_GITHUB_ZIP_URL="https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip"
XRAY_GITHUB_DGST_URL="https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip.dgst"
XRAY_SOURCE="${XRAY_SOURCE:-github}"
XRAY_ZIP_URL="${XRAY_ZIP_URL:-}"
XRAY_DGST_URL="${XRAY_DGST_URL:-}"
XRAY_LOCAL_ZIP="${XRAY_LOCAL_ZIP:-}"
XRAY_LOCAL_DGST="${XRAY_LOCAL_DGST:-}"
XRAY_DOWNLOAD_ATTEMPTS="${XRAY_DOWNLOAD_ATTEMPTS:-4}"
PORT="${PORT:-443}"
REALITY_SNI="${REALITY_SNI:-www.microsoft.com}"
REALITY_DEST=""
CLIENT_NAME="${CLIENT_NAME:-starter}"
SERVER_NAME="${SERVER_NAME:-Xray}"
FINGERPRINT="${FINGERPRINT:-chrome}"
REALITY_TRANSPORT="${REALITY_TRANSPORT:-tcp}"
GRPC_SERVICE_NAME="${GRPC_SERVICE_NAME:-vless-grpc}"
XHTTP_PATH="${XHTTP_PATH:-/vless-xhttp}"
XHTTP_MODE="${XHTTP_MODE:-auto}"
MANAGER_TIMEZONE="${MANAGER_TIMEZONE:-}"
TIMEZONE_SEARCH_LIMIT=30
TIMEZONE_PRESETS=(
  "Europe/Moscow|Москва"
  "Europe/Kaliningrad|Калининград"
  "Europe/Samara|Самара"
  "Asia/Yekaterinburg|Екатеринбург"
  "Asia/Omsk|Омск"
  "Asia/Novosibirsk|Новосибирск"
  "Asia/Krasnoyarsk|Красноярск"
  "Asia/Irkutsk|Иркутск"
  "Asia/Yakutsk|Якутск"
  "Asia/Vladivostok|Владивосток"
  "Asia/Magadan|Магадан"
  "Asia/Sakhalin|Сахалин"
  "Asia/Kamchatka|Камчатка"
  "UTC|UTC"
)

if [[ "$(id -u)" != "0" ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

need_file() {
  if [[ ! -f "$SCRIPT_DIR/$1" ]]; then
    echo "Missing required file: $SCRIPT_DIR/$1" >&2
    exit 1
  fi
}

need_file xray-client
need_file xray-set-cascade
need_file xray-menu
need_file xray-activity
need_file xray-traffic-sync
need_file xray-update
need_file xray-backup
need_file xray-test
need_file xray-warp
need_file xray-telegram
need_file xray-vps-manager
if [[ ! -d "$SCRIPT_DIR/xray_vps_manager" ]]; then
  echo "Missing required directory: $SCRIPT_DIR/xray_vps_manager" >&2
  exit 1
fi

detect_server_addr() {
  if [[ -n "${SERVER_ADDR:-}" ]]; then
    printf '%s\n' "$SERVER_ADDR"
    return
  fi
  for url in https://ifconfig.me/ip https://icanhazip.com https://checkip.amazonaws.com; do
    addr="$(curl -4 --connect-timeout 8 --max-time 15 -fsS "$url" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$addr" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      printf '%s\n' "$addr"
      return
    fi
  done
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    printf '%s\n' "$(awk '{print $3}' <<<"$SSH_CONNECTION")"
    return
  fi
  echo "Could not detect public server IP. Re-run with SERVER_ADDR=SERVER_HOST bash install.sh" >&2
  exit 1
}

validate_host() {
  local value="$1"
  local label="$2"
  if [[ -z "$value" || "$value" == *"/"* || "$value" == *":"* ]]; then
    echo "${label} must be a domain without scheme, path, or port." >&2
    exit 1
  fi
  if [[ ! "$value" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "${label} may contain only A-Z, a-z, 0-9, dots, and hyphens." >&2
    exit 1
  fi
}

validate_port() {
  local value="$1"
  local label="$2"
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    echo "${label} must be a number from 1 to 65535." >&2
    exit 1
  fi
  local number=$((10#$value))
  if (( number < 1 || number > 65535 )); then
    echo "${label} must be a number from 1 to 65535." >&2
    exit 1
  fi
}

validate_fingerprint() {
  local value="$1"
  case "$value" in
    chrome|firefox|safari|ios|android|edge|360|qq|random|randomized)
      ;;
    *)
      echo "FINGERPRINT must be one of: chrome, firefox, safari, ios, android, edge, 360, qq, random, randomized." >&2
      exit 1
      ;;
  esac
}

validate_transport() {
  local value="$1"
  case "$value" in
    tcp|grpc|xhttp)
      ;;
    *)
      echo "REALITY_TRANSPORT must be one of: tcp, grpc, xhttp." >&2
      exit 1
      ;;
  esac
}

validate_grpc_service_name() {
  local value="$1"
  if [[ ! "$value" =~ ^[A-Za-z0-9_.-]{1,128}$ ]]; then
    echo "GRPC_SERVICE_NAME must be 1-128 chars: A-Z a-z 0-9 _ . -" >&2
    exit 1
  fi
}

validate_xhttp_path() {
  local value="$1"
  if [[ ! "$value" =~ ^/[A-Za-z0-9._~/-]{0,255}$ ]]; then
    echo "XHTTP_PATH must start with / and contain only A-Z a-z 0-9 . _ ~ - / characters." >&2
    exit 1
  fi
}

validate_xhttp_mode() {
  local value="$1"
  case "$value" in
    auto|packet-up|stream-up|stream-one)
      ;;
    *)
      echo "XHTTP_MODE must be one of: auto, packet-up, stream-up, stream-one." >&2
      exit 1
      ;;
  esac
}

validate_server_name() {
  local value="$1"
  if [[ ! "$value" =~ ^[A-Za-z0-9_.@-]{1,64}$ ]]; then
    echo "SERVER_NAME must be 1-64 chars: A-Z a-z 0-9 _ . @ -" >&2
    exit 1
  fi
}

validate_manager_timezone() {
  local value="$1"
  if [[ -z "$value" ]]; then
    return
  fi
  if [[ "$value" == /* || "$value" == *".."* || "$value" == *" "* ]]; then
    echo "MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time." >&2
    exit 1
  fi
  if ! [[ "$value" =~ ^[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)+$ ]]; then
    echo "MANAGER_TIMEZONE must be an IANA timezone like Europe/Moscow, or empty for server local time." >&2
    exit 1
  fi
}

resolve_install_path() {
  local value="$1"
  if [[ "$value" == /* ]]; then
    printf '%s\n' "$value"
  else
    printf '%s\n' "$SCRIPT_DIR/$value"
  fi
}

validate_xray_source() {
  XRAY_SOURCE="$(printf '%s' "$XRAY_SOURCE" | tr '[:upper:]' '[:lower:]')"
  case "$XRAY_SOURCE" in
    github)
      XRAY_ZIP_URL="$XRAY_GITHUB_ZIP_URL"
      XRAY_DGST_URL="$XRAY_GITHUB_DGST_URL"
      XRAY_LOCAL_ZIP=""
      XRAY_LOCAL_DGST=""
      ;;
    custom)
      if [[ -z "$XRAY_ZIP_URL" ]]; then
        echo "XRAY_ZIP_URL is required when XRAY_SOURCE=custom." >&2
        exit 1
      fi
      if [[ "$XRAY_ZIP_URL" != http://* && "$XRAY_ZIP_URL" != https://* ]]; then
        echo "XRAY_ZIP_URL must start with http:// or https://." >&2
        exit 1
      fi
      if [[ -n "$XRAY_DGST_URL" && "$XRAY_DGST_URL" != http://* && "$XRAY_DGST_URL" != https://* ]]; then
        echo "XRAY_DGST_URL must start with http:// or https://, or be empty." >&2
        exit 1
      fi
      XRAY_LOCAL_ZIP=""
      XRAY_LOCAL_DGST=""
      ;;
    local)
      if [[ -z "$XRAY_LOCAL_ZIP" ]]; then
        echo "XRAY_LOCAL_ZIP is required when XRAY_SOURCE=local." >&2
        exit 1
      fi
      XRAY_LOCAL_ZIP="$(resolve_install_path "$XRAY_LOCAL_ZIP")"
      if [[ ! -f "$XRAY_LOCAL_ZIP" ]]; then
        echo "XRAY_LOCAL_ZIP not found: $XRAY_LOCAL_ZIP" >&2
        exit 1
      fi
      if [[ -n "$XRAY_LOCAL_DGST" ]]; then
        XRAY_LOCAL_DGST="$(resolve_install_path "$XRAY_LOCAL_DGST")"
        if [[ ! -f "$XRAY_LOCAL_DGST" ]]; then
          echo "XRAY_LOCAL_DGST not found: $XRAY_LOCAL_DGST" >&2
          exit 1
        fi
      fi
      XRAY_ZIP_URL=""
      XRAY_DGST_URL=""
      ;;
    *)
      echo "XRAY_SOURCE must be one of: github, custom, local." >&2
      exit 1
      ;;
  esac
}

validate_install_options() {
  FINGERPRINT="$(printf '%s' "$FINGERPRINT" | tr '[:upper:]' '[:lower:]')"
  REALITY_TRANSPORT="$(printf '%s' "$REALITY_TRANSPORT" | tr '[:upper:]' '[:lower:]')"
  XHTTP_MODE="$(printf '%s' "$XHTTP_MODE" | tr '[:upper:]' '[:lower:]')"
  validate_port "$PORT" "PORT"
  validate_host "$REALITY_SNI" "REALITY_SNI"
  validate_fingerprint "$FINGERPRINT"
  validate_transport "$REALITY_TRANSPORT"
  if [[ "$REALITY_TRANSPORT" == "grpc" ]]; then
    validate_grpc_service_name "$GRPC_SERVICE_NAME"
  elif [[ "$REALITY_TRANSPORT" == "xhttp" ]]; then
    validate_xhttp_path "$XHTTP_PATH"
    validate_xhttp_mode "$XHTTP_MODE"
  fi
  validate_manager_timezone "$MANAGER_TIMEZONE"

  if [[ ! "$CLIENT_NAME" =~ ^[A-Za-z0-9_.@-]{1,64}$ ]]; then
    echo "CLIENT_NAME must be 1-64 chars: A-Z a-z 0-9 _ . @ -" >&2
    exit 1
  fi
  validate_server_name "$SERVER_NAME"
  validate_xray_source

  REALITY_DEST="${REALITY_SNI}:443"
}

prompt_fingerprint() {
  while true; do
    echo "FINGERPRINT: маскировка браузера/uTLS для клиентской VLESS-ссылки."
    echo "Обычно оставляют chrome. Если клиент поддерживает Reality/uTLS, можно выбрать другой профиль."
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
    read -r -p "FINGERPRINT [${FINGERPRINT}] (номер или значение): " input
    input="${input:-$FINGERPRINT}"
    input="$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')"
    case "$input" in
      1) FINGERPRINT="chrome" ;;
      2) FINGERPRINT="firefox" ;;
      3) FINGERPRINT="safari" ;;
      4) FINGERPRINT="ios" ;;
      5) FINGERPRINT="android" ;;
      6) FINGERPRINT="edge" ;;
      7) FINGERPRINT="360" ;;
      8) FINGERPRINT="qq" ;;
      9) FINGERPRINT="random" ;;
      10) FINGERPRINT="randomized" ;;
      chrome|firefox|safari|ios|android|edge|360|qq|random|randomized)
        FINGERPRINT="$input"
        ;;
      *)
        echo "Неверное значение. Выбери номер из списка или введи одно из допустимых значений."
        echo
        continue
        ;;
    esac
    break
  done
}

prompt_transport() {
  while true; do
    echo "REALITY_TRANSPORT: transport для первого VLESS Reality подключения."
    echo "  1) tcp   - TCP transport с Vision flow"
    echo "  2) grpc  - gRPC поверх HTTP/2"
    echo "  3) xhttp - XHTTP/XMUX"
    read -r -p "REALITY_TRANSPORT [${REALITY_TRANSPORT}] (номер или значение): " input
    input="${input:-$REALITY_TRANSPORT}"
    input="$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')"
    case "$input" in
      1) REALITY_TRANSPORT="tcp" ;;
      2) REALITY_TRANSPORT="grpc" ;;
      3) REALITY_TRANSPORT="xhttp" ;;
      tcp|grpc|xhttp) REALITY_TRANSPORT="$input" ;;
      *)
        echo "Выбери номер 1-3 или значение tcp/grpc/xhttp."
        continue
        ;;
    esac

    if [[ "$REALITY_TRANSPORT" == "grpc" ]]; then
      read -r -p "GRPC_SERVICE_NAME [${GRPC_SERVICE_NAME}]: " grpc_input
      GRPC_SERVICE_NAME="${grpc_input:-$GRPC_SERVICE_NAME}"
    elif [[ "$REALITY_TRANSPORT" == "xhttp" ]]; then
      read -r -p "XHTTP_PATH [${XHTTP_PATH}]: " xhttp_path_input
      XHTTP_PATH="${xhttp_path_input:-$XHTTP_PATH}"
      read -r -p "XHTTP_MODE [${XHTTP_MODE}]: " xhttp_mode_input
      XHTTP_MODE="${xhttp_mode_input:-$XHTTP_MODE}"
    fi
    break
  done
}

timezone_candidates() {
  if command -v timedatectl >/dev/null 2>&1; then
    timedatectl list-timezones 2>/dev/null || true
    return
  fi
  if [[ -f /usr/share/zoneinfo/zone1970.tab ]]; then
    awk '!/^#/ {print $3}' /usr/share/zoneinfo/zone1970.tab
    return
  fi
  if [[ -f /usr/share/zoneinfo/zone.tab ]]; then
    awk '!/^#/ {print $3}' /usr/share/zoneinfo/zone.tab
  fi
}

print_timezone_presets() {
  local index=1
  local item value label
  printf '  %2s) %-24s %s\n' "0" "server" "системное время сервера"
  for item in "${TIMEZONE_PRESETS[@]}"; do
    value="${item%%|*}"
    label="${item#*|}"
    printf '  %2s) %-24s %s\n' "$index" "$value" "$label"
    index=$((index + 1))
  done
  printf '  %2s) %-24s %s\n' "S" "Поиск" "найти другой часовой пояс"
}

prompt_timezone_search() {
  local query choice index
  local -a matches=()
  while true; do
    read -r -p "Фильтр timezone, например Moscow или Europe (Enter - назад): " query
    if [[ -z "$query" ]]; then
      return 1
    fi
    mapfile -t matches < <(timezone_candidates | grep -i -F -- "$query" | head -n "$TIMEZONE_SEARCH_LIMIT" || true)
    if (( ${#matches[@]} == 0 )); then
      echo "По этому фильтру ничего не найдено."
      continue
    fi
    for index in "${!matches[@]}"; do
      printf '  %2s) %s\n' "$((index + 1))" "${matches[$index]}"
    done
    printf '  %2s) %s\n' "0" "назад"
    read -r -p "Часовой пояс: " choice
    if [[ "$choice" == "0" || -z "$choice" ]]; then
      return 1
    fi
    if [[ "$choice" =~ ^[0-9]+$ ]]; then
      index=$((10#$choice))
      if (( index >= 1 && index <= ${#matches[@]} )); then
        MANAGER_TIMEZONE="${matches[$((index - 1))]}"
        return 0
      fi
    fi
    echo "Неверное значение. Выбери номер из списка."
  done
}

prompt_manager_timezone() {
  local input lower index item
  while true; do
    echo "MANAGER_TIMEZONE: часовой пояс для сроков доступа, лимитов трафика, отчётов и отображения времени."
    echo "Выбери номер из списка. Нажми Enter, чтобы оставить: ${MANAGER_TIMEZONE:-server local time}."
    print_timezone_presets
    read -r -p "MANAGER_TIMEZONE [${MANAGER_TIMEZONE:-server}] (номер или S): " input
    if [[ -z "$input" ]]; then
      return
    fi
    lower="$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')"
    case "$lower" in
      0|server|local|default|system)
        MANAGER_TIMEZONE=""
        return
        ;;
      s|search|поиск)
        if prompt_timezone_search; then
          return
        fi
        echo
        continue
        ;;
    esac
    if [[ "$input" =~ ^[0-9]+$ ]]; then
      index=$((10#$input))
      if (( index >= 1 && index <= ${#TIMEZONE_PRESETS[@]} )); then
        item="${TIMEZONE_PRESETS[$((index - 1))]}"
        MANAGER_TIMEZONE="${item%%|*}"
        return
      fi
    fi
    echo "Неверное значение. Выбери номер из списка или S для поиска."
    echo
  done
}

prompt_xray_source() {
  local input
  echo "Не удалось скачать Xray из текущего источника после всех попыток."
  echo "Выбери, что сделать дальше:"
  echo "  1) повторить текущий источник"
  echo "  2) ввести свой URL на Xray-linux-64.zip"
  echo "  3) использовать локальный Xray-linux-64.zip, заранее скопированный на сервер"
  echo "  0) остановить установку"
  read -r -p "Действие [1]: " input
  input="${input:-1}"
  input="$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')"
  case "$input" in
    0|abort|stop|exit)
      echo "Установка остановлена: Xray не скачан." >&2
      exit 1
      ;;
    1|retry|again|повторить)
      ;;
    2|custom)
      XRAY_SOURCE="custom"
      echo "Введи прямую ссылку на Xray-linux-64.zip. Это должен быть источник, которому ты доверяешь."
      read -r -p "XRAY_ZIP_URL: " XRAY_ZIP_URL
      echo "Если есть ссылка на .dgst с SHA2-256, введи её. Если нет - нажми Enter."
      read -r -p "XRAY_DGST_URL [empty]: " XRAY_DGST_URL
      ;;
    3|local)
      XRAY_SOURCE="local"
      echo "Укажи путь к Xray-linux-64.zip на сервере. Относительный путь считается от папки install.sh."
      read -r -p "XRAY_LOCAL_ZIP [Xray-linux-64.zip]: " XRAY_LOCAL_ZIP
      XRAY_LOCAL_ZIP="${XRAY_LOCAL_ZIP:-Xray-linux-64.zip}"
      echo "Если рядом есть .dgst с SHA2-256, укажи путь. Если нет - нажми Enter."
      read -r -p "XRAY_LOCAL_DGST [empty]: " XRAY_LOCAL_DGST
      ;;
    *)
      echo "Неверное значение. Повторяю текущий источник."
      ;;
  esac
  validate_xray_source
}

prompt_install_options() {
  if [[ ! -t 0 ]]; then
    validate_install_options
    return
  fi

  echo
  echo "Начальные настройки Xray. Нажми Enter, чтобы оставить значение по умолчанию."
  echo
  echo "PORT: публичный TCP-порт для подключения клиентов. Рекомендуется оставить 443."
  read -r -p "PORT [${PORT}]: " input
  PORT="${input:-$PORT}"
  echo
  echo "REALITY_SNI: домен, видимый в TLS handshake. Вводи реальный HTTPS-домен без https:// и без порта."
  echo "REALITY_DEST будет создан автоматически как REALITY_SNI:443."
  read -r -p "REALITY_SNI [${REALITY_SNI}]: " input
  REALITY_SNI="${input:-$REALITY_SNI}"
  echo
  echo "CLIENT_NAME: имя первого клиента, для которого будет создана ссылка. Разрешены: A-Z a-z 0-9 _ . @ -"
  read -r -p "CLIENT_NAME [${CLIENT_NAME}]: " input
  CLIENT_NAME="${input:-$CLIENT_NAME}"
  echo
  echo "SERVER_NAME: отображаемое имя сервера в конце VLESS-ссылки после #."
  echo "Оно видно пользователю в приложении, но не раскрывает внутреннее имя клиента."
  echo "Разрешены: A-Z a-z 0-9 _ . @ -"
  read -r -p "SERVER_NAME [${SERVER_NAME}]: " input
  SERVER_NAME="${input:-$SERVER_NAME}"
  echo
  prompt_manager_timezone
  echo
  prompt_fingerprint
  echo
  prompt_transport

  validate_install_options
  echo
  echo "Выбранные настройки:"
  echo "  PORT=${PORT}"
  echo "  REALITY_SNI=${REALITY_SNI}"
  echo "  REALITY_DEST=${REALITY_DEST} (создан автоматически)"
  echo "  CLIENT_NAME=${CLIENT_NAME}"
  echo "  SERVER_NAME=${SERVER_NAME}"
  echo "  FINGERPRINT=${FINGERPRINT}"
  echo "  REALITY_TRANSPORT=${REALITY_TRANSPORT}"
  if [[ "$REALITY_TRANSPORT" == "grpc" ]]; then
    echo "  GRPC_SERVICE_NAME=${GRPC_SERVICE_NAME}"
  elif [[ "$REALITY_TRANSPORT" == "xhttp" ]]; then
    echo "  XHTTP_PATH=${XHTTP_PATH}"
    echo "  XHTTP_MODE=${XHTTP_MODE}"
  fi
  echo "  MANAGER_TIMEZONE=${MANAGER_TIMEZONE:-server local time}"
  echo "  XRAY_SOURCE=${XRAY_SOURCE} (альтернативу можно выбрать, если скачивание не удастся)"
  echo
}

prompt_install_options

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
      echo "Timed out waiting for apt/dpkg locks." >&2
      return "$status"
    fi
    echo "apt/dpkg lock is busy, waiting 5s... (${attempt}/${max_attempts})" >&2
    sleep 5
    attempt=$((attempt + 1))
  done
}

prepare_xray_archive() {
  local target_dir="$1"
  local max_attempts attempt retries_left delay status
  case "$XRAY_SOURCE" in
    github|custom)
      max_attempts="$XRAY_DOWNLOAD_ATTEMPTS"
      if [[ ! "$max_attempts" =~ ^[0-9]+$ ]] || (( max_attempts < 1 )); then
        max_attempts=4
      fi
      while true; do
        attempt=1
        while (( attempt <= max_attempts )); do
          retries_left=$((max_attempts - attempt))
          echo "Downloading Xray archive from ${XRAY_SOURCE}: attempt ${attempt}/${max_attempts}, retries left: ${retries_left}"
          if curl -fL --connect-timeout 20 --max-time 240 -o "$target_dir/Xray-linux-64.zip" "$XRAY_ZIP_URL"; then
            break 2
          fi
          status=$?
          if (( retries_left > 0 )); then
            delay=$((attempt * 2))
            echo "Xray archive download failed with exit code ${status}. Retries left: ${retries_left}. Waiting ${delay}s..."
            sleep "$delay"
          else
            echo "Xray archive download failed with exit code ${status}. Retries left: 0." >&2
          fi
          attempt=$((attempt + 1))
        done
        if [[ ! -t 0 ]]; then
          return 1
        fi
        prompt_xray_source
        if [[ "$XRAY_SOURCE" == "local" ]]; then
          cp -f "$XRAY_LOCAL_ZIP" "$target_dir/Xray-linux-64.zip"
          if [[ -n "$XRAY_LOCAL_DGST" ]]; then
            cp -f "$XRAY_LOCAL_DGST" "$target_dir/Xray-linux-64.zip.dgst"
          fi
          return 0
        fi
      done
      if [[ -n "$XRAY_DGST_URL" ]]; then
        max_attempts=2
        attempt=1
        while (( attempt <= max_attempts )); do
          retries_left=$((max_attempts - attempt))
          echo "Downloading Xray digest: attempt ${attempt}/${max_attempts}, retries left: ${retries_left}"
          if curl -fL --connect-timeout 20 --max-time 90 -o "$target_dir/Xray-linux-64.zip.dgst" "$XRAY_DGST_URL"; then
            break
          fi
          status=$?
          if (( retries_left > 0 )); then
            echo "Xray digest download failed with exit code ${status}. Retries left: ${retries_left}. Waiting 2s..."
            sleep 2
          else
            echo "Xray digest download failed with exit code ${status}. Continuing without digest." >&2
          fi
          attempt=$((attempt + 1))
        done
      fi
      ;;
    local)
      cp -f "$XRAY_LOCAL_ZIP" "$target_dir/Xray-linux-64.zip"
      if [[ -n "$XRAY_LOCAL_DGST" ]]; then
        cp -f "$XRAY_LOCAL_DGST" "$target_dir/Xray-linux-64.zip.dgst"
      fi
      ;;
  esac
}

export DEBIAN_FRONTEND=noninteractive
apt_get_with_lock_retry update
apt_get_with_lock_retry install -y ca-certificates curl unzip openssl python3 tzdata

echo "Xray source: ${XRAY_SOURCE}"
if [[ "$XRAY_SOURCE" == "local" ]]; then
  echo "Xray local archive: ${XRAY_LOCAL_ZIP}"
else
  echo "Xray URL: ${XRAY_ZIP_URL}"
fi

workdir="$(mktemp -d)"
cleanup() {
  rm -rf "$workdir"
}
trap cleanup EXIT

prepare_xray_archive "$workdir"

if [[ -s "$workdir/Xray-linux-64.zip.dgst" ]]; then
  expected_sha256="$(awk -F'= ' '/^SHA2-256=/ {print $2}' "$workdir/Xray-linux-64.zip.dgst" | tr -d '[:space:]')"
  if [[ -n "$expected_sha256" ]]; then
    printf '%s  %s\n' "$expected_sha256" "$workdir/Xray-linux-64.zip" | sha256sum -c -
  fi
fi

unzip -q -o "$workdir/Xray-linux-64.zip" -d "$workdir/xray"

install -d -m 0755 /usr/local/bin
install -d -m 0755 /usr/local/share/xray
install -m 0755 "$workdir/xray/xray" /usr/local/bin/xray
install -m 0644 "$workdir/xray/geoip.dat" /usr/local/share/xray/geoip.dat
install -m 0644 "$workdir/xray/geosite.dat" /usr/local/share/xray/geosite.dat

if ! getent passwd xray >/dev/null; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin xray
fi

install -d -o root -g xray -m 0750 /usr/local/etc/xray
install -d -o xray -g xray -m 0755 /var/log/xray
touch /var/log/xray/access.log /var/log/xray/error.log
chown xray:xray /var/log/xray/access.log /var/log/xray/error.log
chmod 0644 /var/log/xray/access.log /var/log/xray/error.log
install -d -o root -g root -m 0700 /root/xray_activity_exports

if [[ -f /usr/local/etc/xray/config.json ]]; then
  cp -a /usr/local/etc/xray/config.json "/usr/local/etc/xray/config.json.bak.$(date -u +%Y%m%d%H%M%S)"
fi

state_bak_stamp="$(date -u +%Y%m%d%H%M%S)"
backup_and_remove_manager_db() {
  local path="$1"
  if [[ -f "$path" ]]; then
    cp -a "$path" "${path}.bak.${state_bak_stamp}"
    rm -f "$path"
  fi
}

backup_and_remove_manager_db /usr/local/etc/xray/manager.db

server_addr="$(detect_server_addr)"
created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
uuid="$(/usr/local/bin/xray uuid)"
keys="$(/usr/local/bin/xray x25519)"
private_key="$(printf '%s\n' "$keys" | awk -F': ' '/^PrivateKey:/ || /^Private key:/ {print $2}')"
public_key="$(printf '%s\n' "$keys" | awk -F': ' '/^Password \(PublicKey\):/ || /^PublicKey:/ || /^Public key:/ {print $2}')"
short_id="$(openssl rand -hex 8)"

if [[ -z "$uuid" || -z "$private_key" || -z "$public_key" || -z "$short_id" ]]; then
  echo "Failed to generate Xray credentials." >&2
  exit 1
fi

client_flow_json=""
client_flow_query=""
starter_flow=""
transport_settings_json=""
transport_link_query=""
case "$REALITY_TRANSPORT" in
  tcp)
    client_flow_json='              "flow": "xtls-rprx-vision",'
    client_flow_query="&flow=xtls-rprx-vision"
    starter_flow="xtls-rprx-vision"
    ;;
  grpc)
    transport_settings_json=$(cat <<JSON
,
        "grpcSettings": {
          "serviceName": "${GRPC_SERVICE_NAME}"
        }
JSON
)
    transport_link_query="&serviceName=${GRPC_SERVICE_NAME}"
    ;;
  xhttp)
    xhttp_path_query="${XHTTP_PATH//\//%2F}"
    transport_settings_json=$(cat <<JSON
,
        "xhttpSettings": {
          "path": "${XHTTP_PATH}",
          "mode": "${XHTTP_MODE}"
        }
JSON
)
    transport_link_query="&path=${xhttp_path_query}&mode=${XHTTP_MODE}"
    ;;
esac

cat >/usr/local/etc/xray/config.json <<EOF
{
  "log": {
    "loglevel": "warning",
    "access": "/var/log/xray/access.log",
    "error": "/var/log/xray/error.log"
  },
  "api": {
    "tag": "api",
    "services": [
      "RoutingService",
      "StatsService"
    ]
  },
  "policy": {
    "levels": {
      "0": {
        "statsUserUplink": true,
        "statsUserDownlink": true
      }
    },
    "system": {
      "statsInboundUplink": true,
      "statsInboundDownlink": true,
      "statsOutboundUplink": true,
      "statsOutboundDownlink": true
    }
  },
  "stats": {},
  "inbounds": [
    {
      "tag": "vless-reality",
      "listen": "0.0.0.0",
      "port": ${PORT},
      "protocol": "vless",
      "settings": {
        "clients": [
            {
              "id": "${uuid}",
${client_flow_json}
              "level": 0,
              "email": "${CLIENT_NAME}|created=${created}"
            }
        ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "${REALITY_TRANSPORT}",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "${REALITY_DEST}",
          "xver": 0,
          "serverNames": [
            "${REALITY_SNI}"
          ],
          "privateKey": "${private_key}",
          "shortIds": [
            "${short_id}"
          ]
        }${transport_settings_json}
      },
      "sniffing": {
        "enabled": true,
        "destOverride": [
          "http",
          "tls",
          "quic"
        ]
      }
    },
    {
      "tag": "api",
      "listen": "127.0.0.1",
      "port": 10085,
      "protocol": "dokodemo-door",
      "settings": {
        "address": "127.0.0.1"
      }
    }
  ],
  "outbounds": [
    {
      "tag": "direct",
      "protocol": "freedom"
    },
    {
      "tag": "blocked",
      "protocol": "blackhole"
    }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
      {
        "type": "field",
        "inboundTag": [
          "api"
        ],
        "outboundTag": "api"
      },
      {
        "type": "field",
        "protocol": [
          "bittorrent"
        ],
        "outboundTag": "blocked"
      },
      {
        "type": "field",
        "ip": [
          "geoip:private"
        ],
        "outboundTag": "blocked"
      }
    ]
  }
}
EOF

cat >/usr/local/etc/xray/server.env <<EOF
SERVER_ADDR=${server_addr}
SERVER_NAME=${SERVER_NAME}
PORT=${PORT}
REALITY_SNI=${REALITY_SNI}
REALITY_DEST=${REALITY_DEST}
FINGERPRINT=${FINGERPRINT}
REALITY_TRANSPORT=${REALITY_TRANSPORT}
GRPC_SERVICE_NAME=${GRPC_SERVICE_NAME}
XHTTP_PATH=${XHTTP_PATH}
XHTTP_MODE=${XHTTP_MODE}
MANAGER_TIMEZONE=${MANAGER_TIMEZONE}
ACTIVITY_LOGGING_ENABLED=false
ACTIVITY_RETENTION_DAYS=365
ACTIVITY_RISK_BURST_EVENTS=1000
ACTIVITY_RISK_BURST_WINDOW_MINUTES=15
ACTIVITY_RISK_UNIQUE_HOSTS=500
ACTIVITY_RISK_UNIQUE_PORTS=20
ACTIVITY_XRAY_GEOIP_WARNING_CODE=
EOF

chown root:xray /usr/local/etc/xray/config.json /usr/local/etc/xray/server.env
chmod 0640 /usr/local/etc/xray/config.json /usr/local/etc/xray/server.env

install -o root -g root -m 0755 "$SCRIPT_DIR/xray-client" /usr/local/sbin/xray-client
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-set-cascade" /usr/local/sbin/xray-set-cascade
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-menu" /usr/local/sbin/xray-menu
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-activity" /usr/local/sbin/xray-activity
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-traffic-sync" /usr/local/sbin/xray-traffic-sync
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-update" /usr/local/sbin/xray-update
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-backup" /usr/local/sbin/xray-backup
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-test" /usr/local/sbin/xray-test
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-warp" /usr/local/sbin/xray-warp
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-telegram" /usr/local/sbin/xray-telegram
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-vps-manager" /usr/local/sbin/xray-vps-manager
install -d -o root -g root -m 0755 /usr/local/lib/xray-vps-manager
rm -rf /usr/local/lib/xray-vps-manager/xray_vps_manager
cp -a "$SCRIPT_DIR/xray_vps_manager" /usr/local/lib/xray-vps-manager/
find /usr/local/lib/xray-vps-manager/xray_vps_manager -name '._*' -delete
chown -R root:root /usr/local/lib/xray-vps-manager/xray_vps_manager
find /usr/local/lib/xray-vps-manager/xray_vps_manager -type d -exec chmod 0755 {} \;
find /usr/local/lib/xray-vps-manager/xray_vps_manager -type f -exec chmod 0644 {} \;

INSTALL_CLIENT_NAME="$CLIENT_NAME" \
INSTALL_CREATED="$created" \
INSTALL_PORT="$PORT" \
INSTALL_REALITY_SNI="$REALITY_SNI" \
INSTALL_REALITY_DEST="$REALITY_DEST" \
INSTALL_FINGERPRINT="$FINGERPRINT" \
INSTALL_REALITY_TRANSPORT="$REALITY_TRANSPORT" \
INSTALL_GRPC_SERVICE_NAME="$GRPC_SERVICE_NAME" \
INSTALL_XHTTP_PATH="$XHTTP_PATH" \
INSTALL_XHTTP_MODE="$XHTTP_MODE" \
INSTALL_PUBLIC_KEY="$public_key" \
INSTALL_SHORT_ID="$short_id" \
INSTALL_UUID="$uuid" \
PYTHONPATH=/usr/local/lib/xray-vps-manager \
python3 <<'PY'
import os

from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients, connections, settings
from xray_vps_manager.core.paths import MANAGER_DB_PATH

client_name = os.environ["INSTALL_CLIENT_NAME"]
created = os.environ["INSTALL_CREATED"]
connection_tag = "vless-reality"
client_uuid = os.environ["INSTALL_UUID"]
transport = os.environ["INSTALL_REALITY_TRANSPORT"]
connection_record = {
    "tag": connection_tag,
    "name": "default",
    "created": created,
    "port": int(os.environ["INSTALL_PORT"]),
    "sni": os.environ["INSTALL_REALITY_SNI"],
    "dest": os.environ["INSTALL_REALITY_DEST"],
    "fingerprint": os.environ["INSTALL_FINGERPRINT"],
    "publicKey": os.environ["INSTALL_PUBLIC_KEY"],
    "shortId": os.environ["INSTALL_SHORT_ID"],
    "transport": transport,
}
if transport == "grpc":
    connection_record["grpcServiceName"] = os.environ["INSTALL_GRPC_SERVICE_NAME"]
elif transport == "xhttp":
    connection_record["xhttpPath"] = os.environ["INSTALL_XHTTP_PATH"]
    connection_record["xhttpMode"] = os.environ["INSTALL_XHTTP_MODE"]

client = {
    "id": client_uuid,
    "level": 0,
    "email": f"{client_name}|created={created}",
}
if transport == "tcp":
    client["flow"] = "xtls-rprx-vision"

connection = database.open_database(MANAGER_DB_PATH)
try:
    with database.transaction(connection):
        connections.upsert_connection(
            connection,
            connection_tag,
            connection_record,
        )
        clients.upsert_client(
            connection,
            client_name,
            {
                "id": client_uuid,
                "created": created,
                "enabled": True,
                "connection": connection_tag,
                "client": client,
            },
        )
        settings.set_metadata(connection, "jsonImport.completed", "true")
finally:
    connection.close()
PY
chown root:xray /usr/local/etc/xray/manager.db
chmod 0640 /usr/local/etc/xray/manager.db

client_uri="vless://${uuid}@${server_addr}:${PORT}?security=reality&encryption=none&pbk=${public_key}&fp=${FINGERPRINT}&type=${REALITY_TRANSPORT}${client_flow_query}&sni=${REALITY_SNI}&sid=${short_id}&spx=%2F${transport_link_query}#${SERVER_NAME}"

cat >/root/xray-reality-client.txt <<EOF
CLIENT_URI=${client_uri}
SERVER=${server_addr}
PORT=${PORT}
PROTOCOL=VLESS
SECURITY=REALITY
TRANSPORT=${REALITY_TRANSPORT}
FLOW=${starter_flow}
UUID=${uuid}
PUBLIC_KEY=${public_key}
SHORT_ID=${short_id}
SNI=${REALITY_SNI}
DEST=${REALITY_DEST}
FINGERPRINT=${FINGERPRINT}
GRPC_SERVICE_NAME=${GRPC_SERVICE_NAME}
XHTTP_PATH=${XHTTP_PATH}
XHTTP_MODE=${XHTTP_MODE}
CREATED=${created}
EOF
chmod 0600 /root/xray-reality-client.txt

cat >/etc/systemd/system/xray.service <<'EOF'
[Unit]
Description=Xray Service
Documentation=https://github.com/XTLS/Xray-core
After=network.target nss-lookup.target
Wants=network-online.target

[Service]
User=xray
Group=xray
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray/config.json
ExecStop=-+/usr/local/sbin/xray-traffic-sync --quiet
Restart=on-failure
RestartPreventExitStatus=23
LimitNPROC=10000
LimitNOFILE=1000000

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/xray-traffic-sync.service <<'EOF'
[Unit]
Description=Persist Xray user traffic counters
After=xray.service
ConditionPathExists=/usr/local/etc/xray/config.json

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/xray-traffic-sync --quiet
ExecStart=/usr/local/sbin/xray-activity sync --quiet
ExecStart=/usr/local/sbin/xray-telegram notify-geoip --quiet
ExecStart=/usr/local/sbin/xray-client enforce-limits --quiet
ExecStart=/usr/local/sbin/xray-client expire-due --quiet
ExecStart=/usr/local/sbin/xray-telegram notify-expiry --quiet
ExecStart=/usr/local/sbin/xray-telegram notify-daily-summary --quiet
EOF

cat >/etc/systemd/system/xray-telegram-poller.service <<'EOF'
[Unit]
Description=Poll Telegram user messages for Xray VPS Manager
After=network-online.target xray.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/sbin/xray-telegram run-poller
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/xray-traffic-sync.timer <<'EOF'
[Unit]
Description=Persist Xray user traffic counters every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s
Unit=xray-traffic-sync.service

[Install]
WantedBy=timers.target
EOF

cat >/etc/systemd/system/xray-client-expire.service <<'EOF'
[Unit]
Description=Disable expired Xray clients
After=xray.service
ConditionPathExists=/usr/local/etc/xray/config.json

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/xray-client expire-due --quiet
EOF

cat >/etc/systemd/system/xray-client-expire.timer <<'EOF'
[Unit]
Description=Disable expired Xray clients every day at midnight

[Timer]
OnCalendar=*-*-* 00:00:00
Persistent=true
AccuracySec=1min
Unit=xray-client-expire.service

[Install]
WantedBy=timers.target
EOF

/usr/local/bin/xray run -test -config /usr/local/etc/xray/config.json
systemctl daemon-reload
systemctl enable --now xray
systemctl restart xray
systemctl enable --now xray-traffic-sync.timer
systemctl enable --now xray-client-expire.timer
systemctl enable --now xray-telegram-poller.service
xray-traffic-sync --quiet || true
xray-client expire-due --quiet || true

echo "Installed: $(/usr/local/bin/xray version | sed -n '1p')"
echo "Xray status: $(systemctl is-active xray)"
echo "Server address: ${server_addr}"
echo
echo "Current clients:"
xray-client list || true
echo
if [[ -t 0 ]]; then
  read -r -p "Add cascade upstream VLESS link now? [y/N]: " add_cascade
  case "${add_cascade:-n}" in
    y|Y|yes|YES|Yes)
      echo "Paste upstream VLESS link and press Enter:"
      read -r upstream_uri
      if [[ -n "$upstream_uri" ]]; then
        if printf '%s\n' "$upstream_uri" | xray-set-cascade; then
          echo
          echo "Cascade test:"
          xray-set-cascade --test || true
          echo
        else
          echo
          echo "Cascade configuration failed. Xray installation is still complete."
          echo "You can retry later with: xray-set-cascade"
          echo
        fi
      else
        echo "Empty link, cascade was not configured."
        echo
      fi
      ;;
    *)
      echo "Cascade was not configured. You can add it later with: xray-set-cascade"
      echo
      ;;
  esac
else
  echo "Non-interactive mode: cascade was not configured."
  echo "You can add it later with: xray-set-cascade"
  echo
fi

echo "Initial client link:"
cat /root/xray-reality-client.txt
