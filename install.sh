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
INITIAL_PROTOCOL="${INITIAL_PROTOCOL:-vless}"
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
TROJAN_CONNECTION_NAME="${TROJAN_CONNECTION_NAME:-trojan}"
TROJAN_DOMAIN="${TROJAN_DOMAIN:-}"
TROJAN_LOCAL_PORT="${TROJAN_LOCAL_PORT:-10100}"
TROJAN_WS_PATH="${TROJAN_WS_PATH:-/trojan}"
TROJAN_TLS_MIN_VERSION="${TROJAN_TLS_MIN_VERSION:-tls1.2}"
TROJAN_TLS_MAX_VERSION="${TROJAN_TLS_MAX_VERSION:-tls1.3}"
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
need_file xray-set-bypass
need_file xray-menu
need_file xray-activity
need_file xray-traffic-sync
need_file xray-update
need_file xray-backup
need_file xray-test
need_file xray-warp
need_file xray-telegram
need_file xray-vps-manager
need_file xray-manager-update
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

normalize_initial_protocol() {
  INITIAL_PROTOCOL="$(printf '%s' "$INITIAL_PROTOCOL" | tr '[:upper:]' '[:lower:]')"
  case "$INITIAL_PROTOCOL" in
    vless|trojan|both)
      ;;
    *)
      echo "INITIAL_PROTOCOL must be one of: vless, trojan, both." >&2
      exit 1
      ;;
  esac
}

initial_has_vless() {
  [[ "$INITIAL_PROTOCOL" == "vless" || "$INITIAL_PROTOCOL" == "both" ]]
}

initial_has_trojan() {
  [[ "$INITIAL_PROTOCOL" == "trojan" || "$INITIAL_PROTOCOL" == "both" ]]
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

validate_trojan_ws_path() {
  local value="$1"
  if [[ ! "$value" =~ ^/[A-Za-z0-9._~/-]{0,255}$ ]]; then
    echo "TROJAN_WS_PATH must start with / and contain only A-Z a-z 0-9 . _ ~ - / characters." >&2
    exit 1
  fi
}

validate_trojan_connection_name() {
  local value="$1"
  if [[ -z "$value" || "$value" == *"|"* || "$value" == *$'\n'* || "$value" == *$'\r'* || ${#value} -gt 64 ]]; then
    echo "TROJAN_CONNECTION_NAME must be 1-64 chars without line breaks or |." >&2
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
  normalize_initial_protocol
  FINGERPRINT="$(printf '%s' "$FINGERPRINT" | tr '[:upper:]' '[:lower:]')"
  REALITY_TRANSPORT="$(printf '%s' "$REALITY_TRANSPORT" | tr '[:upper:]' '[:lower:]')"
  XHTTP_MODE="$(printf '%s' "$XHTTP_MODE" | tr '[:upper:]' '[:lower:]')"
  validate_fingerprint "$FINGERPRINT"
  if initial_has_vless; then
    validate_port "$PORT" "PORT"
    validate_host "$REALITY_SNI" "REALITY_SNI"
    validate_transport "$REALITY_TRANSPORT"
    if [[ "$REALITY_TRANSPORT" == "grpc" ]]; then
      validate_grpc_service_name "$GRPC_SERVICE_NAME"
    elif [[ "$REALITY_TRANSPORT" == "xhttp" ]]; then
      validate_xhttp_path "$XHTTP_PATH"
      validate_xhttp_mode "$XHTTP_MODE"
    fi
    REALITY_DEST="${REALITY_SNI}:443"
  else
    REALITY_DEST=""
  fi
  if initial_has_trojan; then
    validate_host "$TROJAN_DOMAIN" "TROJAN_DOMAIN"
    validate_port "$TROJAN_LOCAL_PORT" "TROJAN_LOCAL_PORT"
    validate_trojan_ws_path "$TROJAN_WS_PATH"
    validate_trojan_connection_name "$TROJAN_CONNECTION_NAME"
    if initial_has_vless && [[ "$PORT" == "443" ]]; then
      echo "PORT must not be 443 when INITIAL_PROTOCOL=both, because Caddy owns public 443 for Trojan." >&2
      exit 1
    fi
  fi
  validate_manager_timezone "$MANAGER_TIMEZONE"

  if [[ ! "$CLIENT_NAME" =~ ^[A-Za-z0-9_.@-]{1,64}$ ]]; then
    echo "CLIENT_NAME must be 1-64 chars: A-Z a-z 0-9 _ . @ -" >&2
    exit 1
  fi
  validate_server_name "$SERVER_NAME"
  validate_xray_source
}

prompt_fingerprint() {
  while true; do
    echo "FINGERPRINT: маскировка браузера/uTLS для клиентской ссылки."
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

prompt_initial_protocol() {
  while true; do
    echo "INITIAL_PROTOCOL: какие initial credentials создать для первого клиента."
    echo "  1) vless  - VLESS Reality starter, совместимо со старым install flow"
    echo "  2) trojan - Trojan WebSocket через Caddy/ACME"
    echo "  3) both   - VLESS Reality + Trojan WebSocket для одного клиента"
    read -r -p "INITIAL_PROTOCOL [${INITIAL_PROTOCOL}] (номер или значение): " input
    input="${input:-$INITIAL_PROTOCOL}"
    input="$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')"
    case "$input" in
      1) INITIAL_PROTOCOL="vless" ;;
      2) INITIAL_PROTOCOL="trojan" ;;
      3) INITIAL_PROTOCOL="both" ;;
      vless|trojan|both) INITIAL_PROTOCOL="$input" ;;
      *)
        echo "Выбери номер 1-3 или значение vless/trojan/both."
        continue
        ;;
    esac
    if [[ "$INITIAL_PROTOCOL" == "both" && "$PORT" == "443" ]]; then
      PORT="8443"
    fi
    break
  done
}

prompt_xhttp_mode() {
  local default_mode input
  default_mode="${1:-$XHTTP_MODE}"
  default_mode="$(printf '%s' "$default_mode" | tr '[:upper:]' '[:lower:]')"
  validate_xhttp_mode "$default_mode"
  while true; do
    echo "XHTTP_MODE: режим XHTTP/XMUX."
    echo "  1) auto"
    echo "  2) packet-up"
    echo "  3) stream-up"
    echo "  4) stream-one"
    read -r -p "XHTTP_MODE [${default_mode}] (номер из списка): " input
    input="${input:-$default_mode}"
    input="$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')"
    case "$input" in
      1) XHTTP_MODE="auto" ;;
      2) XHTTP_MODE="packet-up" ;;
      3) XHTTP_MODE="stream-up" ;;
      4) XHTTP_MODE="stream-one" ;;
      auto|packet-up|stream-up|stream-one)
        XHTTP_MODE="$input"
        ;;
      *)
        echo "Выбери номер 1-4 или нажми Enter для ${default_mode}."
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
      prompt_xhttp_mode "$XHTTP_MODE"
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
  prompt_initial_protocol
  echo
  if initial_has_vless; then
    echo "PORT: публичный TCP-порт для VLESS Reality. Рекомендуется 443, если Trojan/Caddy не используется."
    echo "Если выбран режим both, Caddy занимает 443 для Trojan, поэтому VLESS должен быть на другом порту."
    read -r -p "PORT [${PORT}]: " input
    PORT="${input:-$PORT}"
    echo
    echo "REALITY_SNI: домен, видимый в TLS handshake. Вводи реальный HTTPS-домен без https:// и без порта."
    echo "REALITY_DEST будет создан автоматически как REALITY_SNI:443."
    read -r -p "REALITY_SNI [${REALITY_SNI}]: " input
    REALITY_SNI="${input:-$REALITY_SNI}"
    echo
  fi
  if initial_has_trojan; then
    echo "TROJAN_DOMAIN: реальный домен, который указывает на этот сервер. Caddy выпустит для него TLS-сертификат."
    read -r -p "TROJAN_DOMAIN [${TROJAN_DOMAIN}]: " input
    TROJAN_DOMAIN="${input:-$TROJAN_DOMAIN}"
    echo
    echo "TROJAN_LOCAL_PORT: локальный порт Xray для Trojan WebSocket inbound."
    read -r -p "TROJAN_LOCAL_PORT [${TROJAN_LOCAL_PORT}]: " input
    TROJAN_LOCAL_PORT="${input:-$TROJAN_LOCAL_PORT}"
    echo
    echo "TROJAN_WS_PATH: WebSocket path, который будет проксировать Caddy."
    read -r -p "TROJAN_WS_PATH [${TROJAN_WS_PATH}]: " input
    TROJAN_WS_PATH="${input:-$TROJAN_WS_PATH}"
    echo
  fi
  echo
  echo "CLIENT_NAME: имя первого клиента, для которого будет создана ссылка. Разрешены: A-Z a-z 0-9 _ . @ -"
  read -r -p "CLIENT_NAME [${CLIENT_NAME}]: " input
  CLIENT_NAME="${input:-$CLIENT_NAME}"
  echo
  echo "SERVER_NAME: отображаемое имя сервера в конце клиентской ссылки после #."
  echo "Оно видно пользователю в приложении, но не раскрывает внутреннее имя клиента."
  echo "Разрешены: A-Z a-z 0-9 _ . @ -"
  read -r -p "SERVER_NAME [${SERVER_NAME}]: " input
  SERVER_NAME="${input:-$SERVER_NAME}"
  echo
  prompt_manager_timezone
  echo
  prompt_fingerprint
  echo
  if initial_has_vless; then
    prompt_transport
  fi

  validate_install_options
  echo
  echo "Выбранные настройки:"
  echo "  INITIAL_PROTOCOL=${INITIAL_PROTOCOL}"
  if initial_has_vless; then
    echo "  PORT=${PORT}"
    echo "  REALITY_SNI=${REALITY_SNI}"
    echo "  REALITY_DEST=${REALITY_DEST} (создан автоматически)"
  fi
  if initial_has_trojan; then
    echo "  TROJAN_CONNECTION_NAME=${TROJAN_CONNECTION_NAME}"
    echo "  TROJAN_DOMAIN=${TROJAN_DOMAIN}"
    echo "  TROJAN_LOCAL_PORT=${TROJAN_LOCAL_PORT}"
    echo "  TROJAN_WS_PATH=${TROJAN_WS_PATH}"
    echo "  TROJAN_TLS=${TROJAN_TLS_MIN_VERSION}..${TROJAN_TLS_MAX_VERSION}"
  fi
  echo "  CLIENT_NAME=${CLIENT_NAME}"
  echo "  SERVER_NAME=${SERVER_NAME}"
  echo "  FINGERPRINT=${FINGERPRINT}"
  if initial_has_vless; then
    echo "  REALITY_TRANSPORT=${REALITY_TRANSPORT}"
    if [[ "$REALITY_TRANSPORT" == "grpc" ]]; then
      echo "  GRPC_SERVICE_NAME=${GRPC_SERVICE_NAME}"
    elif [[ "$REALITY_TRANSPORT" == "xhttp" ]]; then
      echo "  XHTTP_PATH=${XHTTP_PATH}"
      echo "  XHTTP_MODE=${XHTTP_MODE}"
    fi
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
install_packages=(ca-certificates curl unzip openssl python3 tzdata)
if initial_has_trojan; then
  install_packages+=(caddy)
fi
apt_get_with_lock_retry install -y "${install_packages[@]}"

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
trojan_uuid="$(/usr/local/bin/xray uuid)"
keys="$(/usr/local/bin/xray x25519)"
private_key="$(printf '%s\n' "$keys" | awk -F': ' '/^PrivateKey:/ || /^Private key:/ {print $2}')"
public_key="$(printf '%s\n' "$keys" | awk -F': ' '/^Password \(PublicKey\):/ || /^PublicKey:/ || /^Public key:/ {print $2}')"
short_id="$(openssl rand -hex 8)"
trojan_password="$(openssl rand -hex 32)"

if [[ -z "$uuid" || -z "$trojan_uuid" || -z "$private_key" || -z "$public_key" || -z "$short_id" || -z "$trojan_password" ]]; then
  echo "Failed to generate Xray credentials." >&2
  exit 1
fi

client_flow_json=""
client_flow_query=""
starter_flow=""
transport_settings_json=""
transport_link_query=""
if initial_has_vless; then
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
fi

vless_inbound_json=""
trojan_inbound_json=""
managed_inbounds_json=""
if initial_has_vless; then
  vless_inbound_json=$(cat <<EOF
    {
      "tag": "vless-reality",
      "listen": "0.0.0.0",
      "port": ${PORT},
      "protocol": "vless",
      "settings": {
        "clients": [],
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
    }
EOF
)
fi
if initial_has_trojan; then
  trojan_inbound_json=$(cat <<EOF
    {
      "tag": "trojan-tls",
      "listen": "127.0.0.1",
      "port": ${TROJAN_LOCAL_PORT},
      "protocol": "trojan",
      "settings": {
        "clients": []
      },
      "streamSettings": {
        "network": "ws",
        "security": "none",
        "wsSettings": {
          "path": "${TROJAN_WS_PATH}"
        }
      },
      "sniffing": {
        "enabled": true,
        "destOverride": [
          "http",
          "tls",
          "quic"
        ]
      }
    }
EOF
)
fi
if initial_has_vless && initial_has_trojan; then
  managed_inbounds_json="${vless_inbound_json},
${trojan_inbound_json}"
elif initial_has_vless; then
  managed_inbounds_json="${vless_inbound_json}"
else
  managed_inbounds_json="${trojan_inbound_json}"
fi

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
${managed_inbounds_json},
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
INITIAL_PROTOCOL=${INITIAL_PROTOCOL}
PORT=${PORT}
REALITY_SNI=${REALITY_SNI}
REALITY_DEST=${REALITY_DEST}
FINGERPRINT=${FINGERPRINT}
REALITY_TRANSPORT=${REALITY_TRANSPORT}
GRPC_SERVICE_NAME=${GRPC_SERVICE_NAME}
XHTTP_PATH=${XHTTP_PATH}
XHTTP_MODE=${XHTTP_MODE}
TROJAN_CONNECTION_NAME=${TROJAN_CONNECTION_NAME}
TROJAN_DOMAIN=${TROJAN_DOMAIN}
TROJAN_LOCAL_PORT=${TROJAN_LOCAL_PORT}
TROJAN_PUBLIC_PORT=443
TROJAN_WS_PATH=${TROJAN_WS_PATH}
TROJAN_TLS_MIN_VERSION=${TROJAN_TLS_MIN_VERSION}
TROJAN_TLS_MAX_VERSION=${TROJAN_TLS_MAX_VERSION}
MANAGER_TIMEZONE=${MANAGER_TIMEZONE}
ACTIVITY_LOGGING_ENABLED=false
ACTIVITY_RETENTION_DAYS=365
ACTIVITY_RISK_BURST_EVENTS=1000
ACTIVITY_RISK_BURST_WINDOW_MINUTES=15
ACTIVITY_RISK_UNIQUE_HOSTS=500
ACTIVITY_RISK_UNIQUE_PORTS=20
ACTIVITY_XRAY_GEOIP_WARNING_CODE=
ACTIVITY_ALERTS_ENABLED=true
ACTIVITY_ALERT_RETENTION_DAYS=90
XRAY_ERROR_EVENT_RETENTION_DAYS=180
XRAY_ACCESS_LOG_RETENTION_DAYS=180
XRAY_ERROR_LOG_RETENTION_DAYS=180
XRAY_RAW_LOG_ROTATE_TIME=03:00
EOF

chown root:xray /usr/local/etc/xray/config.json /usr/local/etc/xray/server.env
chmod 0640 /usr/local/etc/xray/config.json /usr/local/etc/xray/server.env

install -o root -g root -m 0755 "$SCRIPT_DIR/xray-client" /usr/local/sbin/xray-client
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-set-cascade" /usr/local/sbin/xray-set-cascade
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-set-bypass" /usr/local/sbin/xray-set-bypass
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-menu" /usr/local/sbin/xray-menu
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-activity" /usr/local/sbin/xray-activity
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-traffic-sync" /usr/local/sbin/xray-traffic-sync
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-update" /usr/local/sbin/xray-update
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-backup" /usr/local/sbin/xray-backup
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-test" /usr/local/sbin/xray-test
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-warp" /usr/local/sbin/xray-warp
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-telegram" /usr/local/sbin/xray-telegram
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-vps-manager" /usr/local/sbin/xray-vps-manager
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-manager-update" /usr/local/sbin/xray-manager-update
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
INSTALL_INITIAL_PROTOCOL="$INITIAL_PROTOCOL" \
INSTALL_TROJAN_CONNECTION_NAME="$TROJAN_CONNECTION_NAME" \
INSTALL_TROJAN_DOMAIN="$TROJAN_DOMAIN" \
INSTALL_TROJAN_LOCAL_PORT="$TROJAN_LOCAL_PORT" \
INSTALL_TROJAN_WS_PATH="$TROJAN_WS_PATH" \
INSTALL_TROJAN_TLS_MIN_VERSION="$TROJAN_TLS_MIN_VERSION" \
INSTALL_TROJAN_TLS_MAX_VERSION="$TROJAN_TLS_MAX_VERSION" \
INSTALL_TROJAN_PASSWORD="$trojan_password" \
INSTALL_PUBLIC_KEY="$public_key" \
INSTALL_SHORT_ID="$short_id" \
INSTALL_UUID="$uuid" \
INSTALL_TROJAN_UUID="$trojan_uuid" \
PYTHONPATH=/usr/local/lib/xray-vps-manager \
python3 <<'PY'
import json
import os
import shutil

from pathlib import Path

from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.db import database
from xray_vps_manager.db.repositories import clients, connections, settings
from xray_vps_manager.core.paths import MANAGER_DB_PATH
from xray_vps_manager.xray.config import CONFIG_PATH

client_name = os.environ["INSTALL_CLIENT_NAME"]
created = os.environ["INSTALL_CREATED"]
initial_protocol = os.environ["INSTALL_INITIAL_PROTOCOL"]
transport = os.environ["INSTALL_REALITY_TRANSPORT"]
config_path = Path(CONFIG_PATH)
config = json.loads(config_path.read_text())
db = {"connections": {}, "clients": {}}


def has_vless() -> bool:
    return initial_protocol in {"vless", "both"}


def has_trojan() -> bool:
    return initial_protocol in {"trojan", "both"}


if has_vless():
    connection_record = {
        "tag": "vless-reality",
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
    db["connections"]["vless-reality"] = connection_record

if has_trojan():
    db["connections"]["trojan-tls"] = {
        "tag": "trojan-tls",
        "name": os.environ["INSTALL_TROJAN_CONNECTION_NAME"],
        "created": created,
        "protocol": "trojan",
        "security": "tls",
        "transport": "ws",
        "port": 443,
        "publicPort": 443,
        "localPort": int(os.environ["INSTALL_TROJAN_LOCAL_PORT"]),
        "publicHost": os.environ["INSTALL_TROJAN_DOMAIN"],
        "sni": os.environ["INSTALL_TROJAN_DOMAIN"],
        "dest": "",
        "fingerprint": os.environ["INSTALL_FINGERPRINT"],
        "publicKey": "",
        "shortId": "",
        "caddy": True,
        "wsPath": os.environ["INSTALL_TROJAN_WS_PATH"],
        "tlsMinVersion": os.environ["INSTALL_TROJAN_TLS_MIN_VERSION"],
        "tlsMaxVersion": os.environ["INSTALL_TROJAN_TLS_MAX_VERSION"],
    }

uuid_values = [os.environ["INSTALL_UUID"]]
if initial_protocol == "both":
    uuid_values.append(os.environ["INSTALL_TROJAN_UUID"])


def uuid_factory() -> str:
    if uuid_values:
        return uuid_values.pop(0)
    return os.environ["INSTALL_TROJAN_UUID"]


def password_factory() -> str:
    return os.environ["INSTALL_TROJAN_PASSWORD"]


if has_vless():
    client_crud.add_client(
        config,
        db,
        client_name,
        access_days=None,
        connection_tag="vless-reality",
        uuid_factory=uuid_factory,
        password_factory=password_factory,
    )
if has_trojan():
    client_crud.add_client(
        config,
        db,
        client_name,
        access_days=None,
        connection_tag="trojan-tls",
        uuid_factory=uuid_factory,
        password_factory=password_factory,
    )

tmp_path = config_path.with_suffix(".json.tmp")
tmp_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
shutil.chown(tmp_path, user="root", group="xray")
os.chmod(tmp_path, 0o640)
tmp_path.replace(config_path)

connection = database.open_database(MANAGER_DB_PATH)
try:
    with database.transaction(connection):
        for tag, record in db["connections"].items():
            connections.upsert_connection(connection, tag, record)
        for name, entry in db["clients"].items():
            clients.upsert_client(connection, name, entry)
        settings.set_metadata(connection, "jsonImport.completed", "true")
finally:
    connection.close()
PY
chown root:xray /usr/local/etc/xray/manager.db
chmod 0640 /usr/local/etc/xray/manager.db

if initial_has_trojan; then
  INSTALL_TROJAN_DOMAIN="$TROJAN_DOMAIN" \
  INSTALL_TROJAN_LOCAL_PORT="$TROJAN_LOCAL_PORT" \
  INSTALL_TROJAN_WS_PATH="$TROJAN_WS_PATH" \
  INSTALL_TROJAN_TLS_MIN_VERSION="$TROJAN_TLS_MIN_VERSION" \
  INSTALL_TROJAN_TLS_MAX_VERSION="$TROJAN_TLS_MAX_VERSION" \
  PYTHONPATH=/usr/local/lib/xray-vps-manager \
  python3 <<'PY'
import os

from xray_vps_manager.xray import caddy

site_path = caddy.setup_caddy_for_trojan_ws(
    os.environ["INSTALL_TROJAN_DOMAIN"],
    int(os.environ["INSTALL_TROJAN_LOCAL_PORT"]),
    os.environ["INSTALL_TROJAN_WS_PATH"],
    tls_min_version=os.environ["INSTALL_TROJAN_TLS_MIN_VERSION"],
    tls_max_version=os.environ["INSTALL_TROJAN_TLS_MAX_VERSION"],
    install=False,
)
print(f"Caddy Trojan site config: {site_path}")
PY
fi

vless_client_uri=""
trojan_client_uri=""
client_uri=""
if initial_has_vless; then
  vless_client_uri="vless://${uuid}@${server_addr}:${PORT}?security=reality&encryption=none&pbk=${public_key}&fp=${FINGERPRINT}&type=${REALITY_TRANSPORT}${client_flow_query}&sni=${REALITY_SNI}&sid=${short_id}&spx=%2F${transport_link_query}#${SERVER_NAME}"
  client_uri="$vless_client_uri"
fi
if initial_has_trojan; then
  trojan_path_query="${TROJAN_WS_PATH//\//%2F}"
  trojan_client_uri="trojan://${trojan_password}@${TROJAN_DOMAIN}:443?security=tls&type=ws&path=${trojan_path_query}&host=${TROJAN_DOMAIN}&sni=${TROJAN_DOMAIN}&fp=${FINGERPRINT}#${SERVER_NAME}"
  if [[ -z "$client_uri" ]]; then
    client_uri="$trojan_client_uri"
  fi
fi
client_uri_protocol="$INITIAL_PROTOCOL"
client_uri_security="REALITY"
client_uri_transport="$REALITY_TRANSPORT"
if ! initial_has_vless; then
  client_uri_protocol="trojan"
  client_uri_security="TLS"
  client_uri_transport="ws"
elif initial_has_trojan; then
  client_uri_protocol="vless+trojan"
fi

cat >/root/xray-reality-client.txt <<EOF
INITIAL_PROTOCOL=${INITIAL_PROTOCOL}
CLIENT_URI=${client_uri}
VLESS_CLIENT_URI=${vless_client_uri}
TROJAN_CLIENT_URI=${trojan_client_uri}
SERVER=${server_addr}
PORT=${PORT}
PROTOCOL=${client_uri_protocol}
SECURITY=${client_uri_security}
TRANSPORT=${client_uri_transport}
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
TROJAN_DOMAIN=${TROJAN_DOMAIN}
TROJAN_LOCAL_PORT=${TROJAN_LOCAL_PORT}
TROJAN_PUBLIC_PORT=443
TROJAN_WS_PATH=${TROJAN_WS_PATH}
TROJAN_TLS=${TROJAN_TLS_MIN_VERSION}..${TROJAN_TLS_MAX_VERSION}
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

cat >/etc/systemd/system/xray-caddy-random-tls@.service <<'EOF'
[Unit]
Description=Randomize Caddy TLS protocol profile for %i
After=network-online.target caddy.service
Wants=network-online.target
ConditionPathExists=/usr/local/etc/xray/caddy-random-tls.d/%i.env

[Service]
Type=oneshot
EnvironmentFile=/usr/local/etc/xray/caddy-random-tls.d/%i.env
ExecStart=/usr/local/sbin/xray-vps-manager caddy random-tls-run --domain %i --quiet
EOF

cat >/etc/systemd/system/xray-caddy-random-tls@.timer <<'EOF'
[Unit]
Description=Randomize Caddy TLS protocol profile for %i every 15-60 minutes

[Timer]
OnBootSec=15min
OnUnitActiveSec=15min
RandomizedDelaySec=45min
AccuracySec=1min
Unit=xray-caddy-random-tls@%i.service

[Install]
WantedBy=timers.target
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

xray-activity raw-log-timer-sync --no-systemctl

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
systemctl enable --now xray-raw-log-rotate.timer
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
