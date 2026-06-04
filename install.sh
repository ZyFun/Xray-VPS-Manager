#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
XRAY_ZIP_URL="https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip"
XRAY_DGST_URL="https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip.dgst"
PORT="${PORT:-443}"
REALITY_SNI="${REALITY_SNI:-www.microsoft.com}"
REALITY_DEST=""
CLIENT_NAME="${CLIENT_NAME:-starter}"
FINGERPRINT="${FINGERPRINT:-chrome}"

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
need_file xray-traffic-sync
need_file xray-update

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

validate_install_options() {
  FINGERPRINT="$(printf '%s' "$FINGERPRINT" | tr '[:upper:]' '[:lower:]')"
  validate_port "$PORT" "PORT"
  validate_host "$REALITY_SNI" "REALITY_SNI"
  validate_fingerprint "$FINGERPRINT"

  if [[ ! "$CLIENT_NAME" =~ ^[A-Za-z0-9_.@-]{1,64}$ ]]; then
    echo "CLIENT_NAME must be 1-64 chars: A-Z a-z 0-9 _ . @ -" >&2
    exit 1
  fi

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
  prompt_fingerprint

  validate_install_options
  echo
  echo "Выбранные настройки:"
  echo "  PORT=${PORT}"
  echo "  REALITY_SNI=${REALITY_SNI}"
  echo "  REALITY_DEST=${REALITY_DEST} (создан автоматически)"
  echo "  CLIENT_NAME=${CLIENT_NAME}"
  echo "  FINGERPRINT=${FINGERPRINT}"
  echo
}

prompt_install_options

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl unzip openssl python3

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
PORT=${PORT}
REALITY_SNI=${REALITY_SNI}
REALITY_DEST=${REALITY_DEST}
FINGERPRINT=${FINGERPRINT}
EOF

chown root:xray /usr/local/etc/xray/config.json /usr/local/etc/xray/clients.json /usr/local/etc/xray/server.env
chmod 0640 /usr/local/etc/xray/config.json /usr/local/etc/xray/clients.json /usr/local/etc/xray/server.env

install -o root -g root -m 0755 "$SCRIPT_DIR/xray-client" /usr/local/sbin/xray-client
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-set-cascade" /usr/local/sbin/xray-set-cascade
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-menu" /usr/local/sbin/xray-menu
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-traffic-sync" /usr/local/sbin/xray-traffic-sync
install -o root -g root -m 0755 "$SCRIPT_DIR/xray-update" /usr/local/sbin/xray-update

client_uri="vless://${uuid}@${server_addr}:${PORT}?security=reality&encryption=none&pbk=${public_key}&fp=${FINGERPRINT}&type=tcp&flow=xtls-rprx-vision&sni=${REALITY_SNI}&sid=${short_id}&spx=%2F#${CLIENT_NAME}"

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
ExecStart=/usr/local/sbin/xray-client enforce-limits --quiet
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
