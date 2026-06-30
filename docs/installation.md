# Установка

[← README](../README.md)

## Установка последнего релиза

Быстрая установка без вопросов, со значениями по умолчанию:

```bash
apt update && apt install -y curl ca-certificates
curl -fsSL https://github.com/ZyFun/Xray-VPS-Manager/releases/latest/download/bootstrap.sh | bash
```

При таком запуске через pipe установщик работает без интерактивного stdin и принимает значения по умолчанию.

Интерактивная установка с вопросами по базовым настройкам:

```bash
apt update && apt install -y curl ca-certificates
curl -fsSL -o /tmp/xray-bootstrap.sh https://github.com/ZyFun/Xray-VPS-Manager/releases/latest/download/bootstrap.sh
bash /tmp/xray-bootstrap.sh
```

Этот вариант запускает установщик в интерактивном режиме: `install.sh` увидит обычный терминальный ввод и сначала спросит `INITIAL_PROTOCOL`: `vless`, `trojan` или `both`. Для VLESS он задаст вопросы по `PORT`, `REALITY_SNI` и `REALITY_TRANSPORT`; для Trojan - по `TROJAN_DOMAIN`, локальному порту и `TROJAN_WS_PATH`. Общие вопросы: `CLIENT_NAME`, `SERVER_NAME`, `MANAGER_TIMEZONE` и `FINGERPRINT`.

После установки открой меню:

```bash
xray-menu
```

Для второго сервера, который должен быть только download endpoint для XHTTP `downloadSettings`, не запускай основной `install.sh`. Используй отдельный Caddy-only установщик:

```bash
bash install-caddy-download-proxy.sh
```

Он устанавливает только Caddy и команду `caddy-menu`; Xray, `manager.db`, клиенты, Telegram bot и основной менеджер на такой сервер не ставятся. В `caddy-menu` настройки вводятся интерактивно: меню показывает, что именно будет проксироваться, текущие значения, описание каждого поля, ручной ввод `UPSTREAM_PORT` с дефолтом `443`, ручной ввод `XHTTP_PATH`, нумерованный выбор для `XHTTP_MODE`, `TLS_FINGERPRINT`, `TLS_ALPN` и `TLS_PROFILE`, preview Caddyfile и JSON для `downloadSettings`.

`install.sh` предназначен для новой установки менеджера на сервер. При запуске на уже настроенном сервере он создаёт новый Reality-конфиг, новые ключи, новый UUID и стартового клиента, поэтому для обновления установленного сервера нужно использовать меню: `xray-manager-update` обновляет сам менеджер, а `xray-update` обновляет Xray Core и geo assets.

`bootstrap.sh` - это первый вход для чистого сервера. Он ставит минимальные зависимости `curl`, `ca-certificates` и `tar`, скачивает release-архив `ZyFun/Xray-VPS-Manager`, переносит старую папку `/root/xray_server` в backup-папку при необходимости и запускает `install.sh`. Если на сервере уже есть `/usr/local/etc/xray/config.json` или `/usr/local/etc/xray/manager.db`, bootstrap останавливается и предлагает использовать `xray-manager-update`, чтобы не пересоздать рабочий конфиг.

Новая установка по умолчанию создаёт:

- VLESS Reality inbound;
- UUID стартового клиента `starter`;
- Reality private/public key и `shortId`;
- ссылку стартового клиента в `/root/xray-reality-client.txt`;
- основную SQLite-базу `/usr/local/etc/xray/manager.db`;
- systemd timers для синхронизации трафика и проверки сроков доступа;
- запрет BitTorrent-трафика через routing rule `protocol=bittorrent -> blocked`;
- локальные служебные команды в `/usr/local/sbin`;
- Python-пакет менеджера в `/usr/local/lib/xray-vps-manager`.

В интерактивной установке можно выбрать начальный протокол:

- `vless` - режим по умолчанию и совместимость со старым install flow;
- `trojan` - создать стартовый Trojan WebSocket credential через Caddy/ACME без VLESS Reality inbound;
- `both` - создать одному стартовому клиенту два credentials: VLESS Reality и Trojan WebSocket через Caddy.

Для initial Trojan нужен реальный домен в `TROJAN_DOMAIN`, заранее направленный на сервер. Caddy занимает публичный `443`, выпускает TLS-сертификат и проксирует `TROJAN_WS_PATH` на локальный Xray inbound `127.0.0.1:TROJAN_LOCAL_PORT`. Если выбран `both`, VLESS `PORT` не должен быть `443`; интерактивный установщик предложит `8443`.

Сохранённого `config.json` в репозитории нет: конфиг генерируется на сервере во время установки. Каскадный outbound также не хранится заранее и добавляется только после явной настройки.

По умолчанию Xray скачивается из официального источника Xray: XTLS/Xray-core GitHub Releases.
Используется последняя стабильная версия. Если доступен digest-файл, установщик проверяет SHA256.
Если архив не скачался после нескольких попыток, интерактивная установка покажет, сколько retry осталось, а затем предложит повторить попытку, ввести свой URL или использовать локальный zip-архив Xray.
После копирования папки установщик очищает служебные `._*` файлы, которые могут появиться при переносе проекта с некоторых desktop-систем.
Если на сервере уже была прежняя `manager.db`, установщик сохраняет её как `.bak.<timestamp>` и создаёт новую базу.

Перед установкой Xray можно изменить базовые параметры:

```text
PORT
REALITY_SNI
INITIAL_PROTOCOL
TROJAN_DOMAIN
TROJAN_LOCAL_PORT
TROJAN_WS_PATH
CLIENT_NAME
SERVER_NAME
FINGERPRINT
REALITY_TRANSPORT
MANAGER_TIMEZONE
```

Нажми Enter на любом вопросе, чтобы оставить значение по умолчанию.

Значения по умолчанию:

```text
PORT=443
REALITY_SNI=www.microsoft.com
INITIAL_PROTOCOL=vless
TROJAN_LOCAL_PORT=10100
TROJAN_WS_PATH=/trojan
CLIENT_NAME=starter
SERVER_NAME=Xray
FINGERPRINT=chrome
REALITY_TRANSPORT=tcp
MANAGER_TIMEZONE=server local time
```

`REALITY_DEST` создаётся автоматически из SNI и стандартного HTTPS-порта 443:

```text
REALITY_DEST=REALITY_SNI:443
```

`FINGERPRINT` задаёт маскировку браузера/uTLS в клиентской ссылке `vless://`.
Доступные варианты: `chrome`, `firefox`, `safari`, `ios`, `android`, `edge`, `360`, `qq`, `random`, `randomized`.

`REALITY_TRANSPORT` задаёт transport первого VLESS Reality-подключения. По умолчанию используется `tcp` с Vision flow `xtls-rprx-vision`. Также доступны `grpc` и `xhttp`; для `grpc` используется `GRPC_SERVICE_NAME` (по умолчанию `vless-grpc`), для `xhttp` используются `XHTTP_PATH` (по умолчанию `/vless-xhttp`) и `XHTTP_MODE` (по умолчанию `auto`).
В интерактивной установке `XHTTP_MODE` выбирается из нумерованного списка: `auto`, `packet-up`, `stream-up`, `stream-one`.

`MANAGER_TIMEZONE` можно оставить пустым, тогда будет использоваться системное время сервера.
В интерактивной установке часовой пояс выбирается из списка. Для редкой зоны можно выбрать поиск по IANA-списку, например по слову `Moscow`, `Europe` или `Novosibirsk`.

Во время установки появится вопрос:

```text
Add cascade upstream VLESS link now? [y/N]:
```

Нажми Enter или введи `n`, чтобы продолжить без каскада.
Введи `y`, чтобы сразу вставить ссылку исходящего/root-сервера вида `vless://...`.

## Альтернативный Источник Xray

Свой URL:

```bash
XRAY_SOURCE=custom \
XRAY_ZIP_URL=https://DOWNLOAD_HOST/Xray-linux-64.zip \
XRAY_DGST_URL=https://DOWNLOAD_HOST/Xray-linux-64.zip.dgst \
bash install.sh
```

Локальный архив, заранее скопированный на сервер:

```bash
XRAY_SOURCE=local \
XRAY_LOCAL_ZIP=/root/xray_server/Xray-linux-64.zip \
XRAY_LOCAL_DGST=/root/xray_server/Xray-linux-64.zip.dgst \
bash install.sh
```

`XRAY_LOCAL_DGST` и `XRAY_DGST_URL` можно не указывать, если digest-файла нет.
