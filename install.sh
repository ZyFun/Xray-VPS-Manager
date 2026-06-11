#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
XRAY_ZIP_URL="https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip"
XRAY_DGST_URL="https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip.dgst"
PORT="${PORT:-443}"
REALITY_SNI="${REALITY_SNI:-www.microsoft.com}"
REALITY_DEST=""
CLIENT_NAME="${CLIENT_NAME:-starter}"
SERVER_NAME="${SERVER_NAME:-Virei}"
FINGERPRINT="${FINGERPRINT:-chrome}"
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

validate_install_options() {
  FINGERPRINT="$(printf '%s' "$FINGERPRINT" | tr '[:upper:]' '[:lower:]')"
  validate_port "$PORT" "PORT"
  validate_host "$REALITY_SNI" "REALITY_SNI"
  validate_fingerprint "$FINGERPRINT"
  validate_manager_timezone "$MANAGER_TIMEZONE"

  if [[ ! "$CLIENT_NAME" =~ ^[A-Za-z0-9_.@-]{1,64}$ ]]; then
    echo "CLIENT_NAME must be 1-64 chars: A-Z a-z 0-9 _ . @ -" >&2
    exit 1
  fi
  validate_server_name "$SERVER_NAME"

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

  validate_install_options
  echo
  echo "Выбранные настройки:"
  echo "  PORT=${PORT}"
  echo "  REALITY_SNI=${REALITY_SNI}"
  echo "  REALITY_DEST=${REALITY_DEST} (создан автоматически)"
  echo "  CLIENT_NAME=${CLIENT_NAME}"
  echo "  SERVER_NAME=${SERVER_NAME}"
  echo "  FINGERPRINT=${FINGERPRINT}"
  echo "  MANAGER_TIMEZONE=${MANAGER_TIMEZONE:-server local time}"
  echo
}

prompt_install_options

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl unzip openssl python3 tzdata

echo "Xray download source: official GitHub Releases"
echo "Xray URL: ${XRAY_ZIP_URL}"

workdir="$(mktemp -d)"
cleanup() {
  rm -rf "$workdir"
}
trap cleanup EXIT

curl -fL --retry 3 --connect-timeout 20 --max-time 240 -o "$workdir/Xray-linux-64.zip" "$XRAY_ZIP_URL"
curl -fL --retry 3 --connect-timeout 20 --max-time 90 -o "$workdir/Xray-linux-64.zip.dgst" "$XRAY_DGST_URL" || true

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
install -d -o root -g xray -m 0750 /usr/local/etc/xray/activity
install -d -o root -g xray -m 0750 /usr/local/etc/xray/activity/clients
install -d -o root -g root -m 0700 /root/xray_activity_exports
if [[ ! -f /usr/local/etc/xray/activity-exceptions.json ]]; then
  printf '{\n  "version": 1,\n  "items": []\n}\n' >/usr/local/etc/xray/activity-exceptions.json
  chown root:xray /usr/local/etc/xray/activity-exceptions.json
  chmod 0640 /usr/local/etc/xray/activity-exceptions.json
fi

if [[ -f /usr/local/etc/xray/config.json ]]; then
  cp -a /usr/local/etc/xray/config.json "/usr/local/etc/xray/config.json.bak.$(date -u +%Y%m%d%H%M%S)"
fi

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
              "flow": "xtls-rprx-vision",
              "level": 0,
              "email": "${CLIENT_NAME}|created=${created}"
            }
        ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
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

cat >/usr/local/etc/xray/clients.json <<EOF
{
  "connections": {
    "vless-reality": {
      "tag": "vless-reality",
      "name": "default",
      "created": "${created}",
      "port": ${PORT},
      "sni": "${REALITY_SNI}",
      "dest": "${REALITY_DEST}",
      "fingerprint": "${FINGERPRINT}",
      "publicKey": "${public_key}",
      "shortId": "${short_id}"
    }
  },
  "clients": {
    "${CLIENT_NAME}": {
      "id": "${uuid}",
      "created": "${created}",
      "enabled": true,
      "connection": "vless-reality",
      "client": {
        "id": "${uuid}",
        "flow": "xtls-rprx-vision",
        "level": 0,
        "email": "${CLIENT_NAME}|created=${created}"
      }
    }
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
MANAGER_TIMEZONE=${MANAGER_TIMEZONE}
ACTIVITY_LOGGING_ENABLED=false
ACTIVITY_RETENTION_DAYS=365
ACTIVITY_RISK_BURST_EVENTS=1000
ACTIVITY_RISK_BURST_WINDOW_MINUTES=15
ACTIVITY_RISK_UNIQUE_HOSTS=500
ACTIVITY_RISK_UNIQUE_PORTS=20
ACTIVITY_XRAY_GEOIP_WARNING_CODE=
EOF

chown root:xray /usr/local/etc/xray/config.json /usr/local/etc/xray/clients.json /usr/local/etc/xray/server.env
chmod 0640 /usr/local/etc/xray/config.json /usr/local/etc/xray/clients.json /usr/local/etc/xray/server.env

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

client_uri="vless://${uuid}@${server_addr}:${PORT}?security=reality&encryption=none&pbk=${public_key}&fp=${FINGERPRINT}&type=tcp&flow=xtls-rprx-vision&sni=${REALITY_SNI}&sid=${short_id}&spx=%2F#${SERVER_NAME}"

cat >/root/xray-reality-client.txt <<EOF
CLIENT_URI=${client_uri}
SERVER=${server_addr}
PORT=${PORT}
PROTOCOL=VLESS
SECURITY=REALITY
FLOW=xtls-rprx-vision
UUID=${uuid}
PUBLIC_KEY=${public_key}
SHORT_ID=${short_id}
SNI=${REALITY_SNI}
DEST=${REALITY_DEST}
FINGERPRINT=${FINGERPRINT}
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
