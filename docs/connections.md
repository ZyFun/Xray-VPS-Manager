# Подключения

[← README](../README.md)


Показать доступные managed подключения VLESS и Trojan:

```bash
xray-client connection-list
```

В таблице `connection-list` протокол выводится отдельной колонкой `PROTOCOL`, а `SECURITY` показывает только тип защиты подключения (`reality`, `tls` и т.д.), без смешивания с протоколом.

Создать дополнительное подключение:

```bash
xray-client add-connection ИМЯ PORT REALITY_SNI FINGERPRINT
xray-client add-connection ИМЯ PORT REALITY_SNI FINGERPRINT grpc --grpc-service-name vless-grpc
xray-client add-connection ИМЯ PORT REALITY_SNI FINGERPRINT xhttp --xhttp-path /vless-xhttp --xhttp-mode auto
xray-client add-connection ИМЯ PORT REALITY_SNI FINGERPRINT xhttp --xhttp-path /vless-xhttp --xhttp-mode auto --xhttp-extra-json '{"xPaddingBytes":"100-1000","scStreamUpServerSecs":"20-80","xmux":{"maxConcurrency":"16-32","maxConnections":0,"cMaxReuseTimes":0,"hMaxRequestTimes":"600-900","hMaxReusableSecs":"1800-3000","hKeepAlivePeriod":0}}'
```

Пример:

```bash
xray-client add-connection backup443 8443 www.microsoft.com chrome
```

Новое подключение создаёт отдельный VLESS Reality inbound с собственным портом, SNI, DEST, fingerprint и transport.
`REALITY_DEST` создаётся автоматически как `REALITY_SNI:443`.
Transport по умолчанию - `tcp`; для него используется Vision flow `xtls-rprx-vision`. Для `grpc` и `xhttp` flow в клиентский config и VLESS-ссылку не добавляется.
При добавлении клиента через меню, если подключений больше одного, появится выбор подключения.
Для TLS/XHTTP через Caddy fingerprint хранится как клиентский параметр подключения и попадает в VLESS-ссылку как top-level `fp`. Если используется `downloadSettings`, его `tlsSettings.fingerprint` настраивает отдельный fingerprint только для download endpoint.
При создании Reality/TLS-подключения или смене transport через меню `XHTTP_MODE` для `xhttp` выбирается из списка: `auto`, `packet-up`, `stream-up`, `stream-one`. В CLI то же значение передаётся через `--xhttp-mode`.
При создании xHTTP-подключения меню спрашивает, использовать ли расширенные XHTTP-настройки. Если выбрать режим по умолчанию, `extra` не записывается и Xray использует собственные defaults. Если выбрать расширенный режим, менеджер предложит базовые padding/stream параметры, опциональные packet-up параметры, `noGRPCHeader`/`noSSEHeader`, custom headers, XMUX и отдельный мастер `downloadSettings`. Server-side часть попадёт в `config.json`, а полный профиль сохранится в `manager.db` и будет добавлен в новые VLESS-ссылки как `extra`.

Изменить расширенные XHTTP-настройки существующего подключения:

```bash
xray-client connection-xhttp-extra ИМЯ_ИЛИ_TAG --xhttp-extra-json '{"xPaddingBytes":"100-1000","scStreamUpServerSecs":"20-80","xmux":{"maxConcurrency":"16-32","maxConnections":0,"cMaxReuseTimes":0,"hMaxRequestTimes":"600-900","hMaxReusableSecs":"1800-3000","hKeepAlivePeriod":0}}'
xray-client connection-xhttp-extra ИМЯ_ИЛИ_TAG --clear-xhttp-extra
```

Через меню это доступно в `Подключения и TLS -> Подключения VLESS / Reality -> Расширенные XHTTP настройки`. После изменения XMUX, client-only packet-up полей, `noGRPCHeader` или `downloadSettings` нужно выдать клиентам новые ссылки: эти поля являются клиентской частью профиля и не меняют поведение уже импортированной старой ссылки.
Если для XMUX включается `maxConnections`, `maxConcurrency` можно выключить значением `0` или диапазоном `0-0`; такой нулевой диапазон не считается конфликтом.

Переименовать подключение без изменения tag, порта, ключей и клиентских ссылок:

```bash
xray-client connection-rename ИМЯ_ИЛИ_TAG НОВОЕ_ИМЯ
```

## Trojan через Caddy

Основной поддерживаемый способ работы с Trojan в менеджере - через Caddy: Caddy слушает публичный домен на `443`, выпускает и обновляет TLS-сертификат, принимает WebSocket-запрос по отдельному path и проксирует его на локальный Xray inbound `protocol=trojan`.
Trojan/WebSocket используется как compatibility/DPI-bypass режим, а не как долгосрочный целевой default всей системы. Новое Trojan-подключение через CLI и меню создаётся как WebSocket за Caddy, с TLS 1.2+1.3 и автоматической настройкой Caddy site config.

```text
client -> vpn.example.com:443 -> Caddy /trojan -> 127.0.0.1:10100 -> Xray Trojan WS
```

Менеджер хранит Trojan-пользователей в `settings.clients`, выдаёт `trojan://` ссылки и использует внутренний UUID клиента для SQLite, маршрутизации и Telegram. В самом Xray Trojan credential аутентифицируется по `password`, а `email` используется для stats/routing и включает connection metadata.

DNS-запись домена должна заранее указывать на сервер:

```text
A vpn.example.com -> SERVER_PUBLIC_IP
```

Caddy должен иметь возможность слушать `80/tcp` и `443/tcp`: порт `80` нужен для ACME HTTP challenge и редиректов, порт `443` - для клиентского TLS/WebSocket подключения.

Путь в меню:

```text
Подключения и TLS -> Подключения Trojan -> Создать Trojan TLS подключение
```

Мастер спрашивает имя подключения, локальный порт Xray, TLS-домен, публичный порт Caddy, WebSocket path, fingerprint и TLS-профиль Caddy. Production default - Caddy/WebSocket с TLS 1.2+1.3; сертификат и ключ вручную указывать не нужно: Caddy сам управляет ACME lifecycle.

Создать Trojan Caddy/WebSocket-подключение через CLI. `--transport ws` и `--install-caddy` указывать не нужно: это значения по умолчанию.

```bash
xray-client add-trojan-connection trojan-main 10100 vpn.example.com chrome
```

Если нужно явно задать WebSocket path, публичный порт Caddy или TLS-профиль:

```bash
xray-client add-trojan-connection trojan-main 10100 vpn.example.com chrome \
  --ws-path /private-trojan \
  --public-port 443 \
  --tls-min-version tls1.2 \
  --tls-max-version tls1.3
```

Поля:

- `LOCAL_PORT` - локальный порт Xray inbound на `127.0.0.1`.
- `DOMAIN` - публичный TLS/SNI домен, который будет указан в клиентской ссылке.
- `--ws-path` - WebSocket path, который попадёт в Caddy route и `trojan://` ссылку.
- `--public-port` - публичный порт Caddy, обычно `443`.
- `FINGERPRINT` - клиентский fingerprint в ссылке; по умолчанию `chrome`.
- `--no-caddy` - исключение из основного режима Trojan в менеджере: создать локальный Trojan/WebSocket inbound без установки или обновления Caddy site.

Команда заранее проверяет, что для `DOMAIN` ещё нет Caddy site config. Если site уже существует, создание подключения останавливается до изменения Xray config и SQLite, чтобы не перезаписать рабочий сайт или другое TLS-подключение. Для изменения существующего site используй `Подключения и TLS -> Caddy / TLS -> Site configs`.

Если конфликтов нет, команда создаёт локальный Xray inbound на `127.0.0.1:LOCAL_PORT`, добавляет запись подключения в SQLite, проверяет и перезапускает Xray, затем создаёт новый Caddy site config для `DOMAIN`. Caddy site валидируется через `caddy validate`; при ошибке менеджер сообщает backup Xray config и детали ошибки.

После создания подключения добавь нового пользователя обычной командой. Если Trojan connection на сервере один, можно выбрать его по протоколу:

```bash
xray-client add alice 30 --protocol trojan --payment paid
xray-client link alice
```

Если Trojan-подключений несколько, явно укажи нужный connection:

```bash
xray-client add alice 30 --connection trojan-tls --payment paid
xray-client link alice
```

Через меню новый клиент добавляется так же:

```text
Клиенты -> Добавить клиента
```

Если подключений несколько, меню покажет таблицу VLESS и Trojan connections. Выбери Trojan connection, чтобы получить `trojan://` ссылку.

Если на сервере уже есть несколько подключений одного протокола, `--connection` обязателен. Для Trojan-клиента менеджер генерирует внутренний UUID и отдельный Trojan password. В активный Xray config попадает только `password`, `email` и `level`; внутренний UUID остаётся в `manager.db`.
Если `alice` уже существует, основной путь в меню:

```text
Клиенты -> Добавить подключение к клиенту
```

Этот пункт выбирает существующего клиента из таблицы, затем показывает только connections, которых у клиента ещё нет. CLI-команда `xray-client add alice --connection trojan-tls` остаётся совместимым способом добавить новый Trojan credential без создания отдельного клиента. Клиентский UUID останется прежним, а Trojan password будет отдельным credential secret.

Ссылку нужно выдать заново, если изменились домен, публичный порт, `WS_PATH`, fingerprint или TLS-параметры клиентской ссылки. Если менялся только серверный Caddy site config без изменения этих параметров, уже импортированную ссылку обычно менять не нужно.

Для managed Trojan WebSocket/Caddy connection эти параметры можно обновить одной CLI-командой:

```bash
xray-client update-trojan-connection trojan-tls \
  --domain vpn.example.com \
  --local-port 10101 \
  --public-port 443 \
  --ws-path /trojan2 \
  --fingerprint firefox \
  --tls-min-version tls1.2 \
  --tls-max-version tls1.3
```

Команда обновляет SQLite metadata, локальный Trojan inbound в Xray config и Caddy site config для этого домена. Если меняется домен site config, старый Caddy site заменяется новым с rollback при ошибке `caddy validate` или reload. После успешного изменения параметров подключения выдай клиентам новые ссылки через `xray-client link NAME`, потому что старая `trojan://` ссылка продолжит указывать на прежний домен, порт, path или fingerprint.

Legacy-режим direct TLS/TCP с ручными cert/key path сохранён для совместимости и автоматизации, но не является основным способом:

```bash
xray-client add-trojan-connection trojan-direct 8443 vpn.example.com /etc/ssl/vpn/fullchain.pem /etc/ssl/vpn/privkey.pem chrome --transport tcp
```

Ограничения:

- перенос клиента между VLESS и Trojan подключениями пока запрещён, потому что у протоколов разные credentials;
- Telegram-подписки привязываются к внутреннему UUID клиента через `vpn-key:UUID` или VLESS-ссылку; распознавание `trojan://` ссылки по password остаётся отдельной задачей;
- старый формат Trojan пользователей `settings.users` не используется: для Xray 26 активные Trojan credentials должны лежать в `settings.clients`.

## XHTTP через TLS и Caddy

Для XHTTP с обычным TLS можно создать отдельное TLS-подключение. В этой схеме Caddy слушает публичный домен на `443`, автоматически выпускает сертификат и проксирует HTTP/2 cleartext на локальный Xray inbound:

```text
client -> api.example.com:443 -> Caddy -> 127.0.0.1:10000 -> Xray XHTTP
```

DNS-запись домена должна заранее указывать на сервер:

```text
A api.example.com -> SERVER_PUBLIC_IP
```

Команда создаёт локальный XHTTP inbound и, если указан `--install-caddy`, устанавливает/настраивает Caddy:

```bash
xray-client add-connection web-api 10000 api.example.com \
  --security tls \
  --transport xhttp \
  --xhttp-path /vless-xhttp \
  --xhttp-mode auto \
  --xhttp-extra-json '{"xPaddingBytes":"100-1000","scStreamUpServerSecs":"20-80","xmux":{"maxConcurrency":"16-32","maxConnections":0,"cMaxReuseTimes":0,"hMaxRequestTimes":"600-900","hMaxReusableSecs":"1800-3000","hKeepAlivePeriod":0}}' \
  --public-port 443 \
  --tls-min-version tls1.2 \
  --tls-max-version tls1.2 \
  --install-caddy
```

Для TLS 1.2+1.3 вместо жёсткого TLS 1.2 можно передать:

```bash
--tls-min-version tls1.2 --tls-max-version tls1.3
```

Для профиля Caddy default без явного `protocols` используй:

```bash
--tls-min-version default --tls-max-version default
```

Важно: Caddy должен занять публичный `443`. Если существующий Reality inbound уже слушает `443`, сначала перенеси его на другой публичный порт или не запускай `--install-caddy`. Менеджер не переносит существующие подключения автоматически, чтобы не сломать рабочие клиентские ссылки.

Если `--install-caddy` не указан, менеджер только добавит локальный Xray inbound. Caddy можно настроить вручную:

```caddyfile
api.example.com {
    tls {
        protocols tls1.2 tls1.2
    }

    reverse_proxy h2c://127.0.0.1:10000
}
```

Клиентская ссылка для такого подключения будет использовать `security=tls`, `type=xhttp`, `sni=api.example.com`, публичный порт `443` и тот же `path`.

Управление Caddy доступно через меню:

```text
Подключения и TLS -> Caddy / TLS
```

В этом разделе Caddy разделён на подменю: `Состояние и проверка`, `Site configs`, `Управление сервисом` и `Бэкапы`. Через них можно установить Caddy, проверить config, посмотреть `Caddyfile` и site configs, создать или обновить site config из существующего TLS-подключения, создать site вручную, изменить TLS version, upstream local port или домен site, удалить site config, убрать дефолтный site `:80`, проверить TLS handshake, посмотреть логи, выполнить reload/restart Caddy, а также открыть backup для Caddy config и файлов сайта. Для VLESS/XHTTP site config проксирует `h2c://127.0.0.1:LOCAL_PORT`, для Trojan/WebSocket - выбранный `WS_PATH` на `127.0.0.1:LOCAL_PORT`. При создании или изменении site config TLS выбирается из списка профилей: Caddy default, TLS 1.2, TLS 1.2 + TLS 1.3, TLS 1.3. Смена TLS version редактирует только TLS-директиву существующего site config, поэтому подходит и для статического сайта без upstream local port. Изменения site config валидируются через `caddy validate`; при ошибке менеджер откатывает изменённый файл из backup.

`xray-test` дополнительно выполняет warning-level TLS certificate diagnostics: для legacy direct TLS/TCP он проверяет `certificateFile`, `keyFile`, базовые права, срок действия сертификата и соответствие `SNI`; для managed Caddy/TLS-подключений сверяет Caddy site config с SQLite metadata и делает live TLS handshake к `DOMAIN:PUBLIC_PORT`. В режиме `xray-test --all` диагностика глубже проверяет Caddy endpoint: для Trojan/WebSocket отправляет пробный WebSocket upgrade на `WS_PATH`, для TLS/XHTTP проверяет HTTP route и отдельно предупреждает о deprecated Trojan/WebSocket в Xray.

Через Telegram-владельца тот же TLS-профиль можно сменить в `/admin -> Настройки сервера -> TLS`. Бот показывает текущий профиль для каждого Caddy site config и время последнего изменения файла, затем применяет выбранный профиль с проверкой и reload Caddy.

Обычный `xray-backup` включает `/etc/caddy/Caddyfile` и `/etc/caddy/conf.d`, если Caddy настроен, чтобы TLS/Caddy-подключения восстанавливались вместе с Xray config и `manager.db`. Файлы сайта и config-only операции Caddy доступны отдельно через `Подключения и TLS -> Caddy / TLS -> Бэкапы`.

## Отдельный Caddy-Endpoint Для downloadSettings

Для `downloadSettings` можно поднять второй сервер, на котором работает только Caddy. Он принимает клиентский download/downstream на отдельном домене и проксирует запросы на основной XHTTP/TLS endpoint:

```text
client upload   -> api.example.com -> основной Caddy/Xray
client download -> cdn.example.com -> второй Caddy -> https://api.example.com -> основной Caddy/Xray
```

На втором сервере не нужен Xray, `xray-menu`, `manager.db`, Telegram bot или клиенты. Для такого сценария есть отдельный установщик:

```bash
bash install-caddy-download-proxy.sh
```

Он ставит только Caddy и команду:

```bash
caddy-menu
```

В меню можно установить/проверить Caddy, открыть интерактивную настройку `DOWNLOAD_DOMAIN -> UPSTREAM_DOMAIN`, показать текущий Caddy config, вывести готовый JSON для `downloadSettings`, проверить DNS/TLS/proxy, сделать `validate + reload` и посмотреть статус/логи Caddy.

Пункт `Настроить download proxy` показывает, что именно настраивается: клиентский download endpoint, upstream на основной сервер, текущие значения `DOWNLOAD_DOMAIN`, `UPSTREAM_DOMAIN`, `UPSTREAM_PORT`, `XHTTP_PATH`, `XHTTP_MODE`, `TLS_FINGERPRINT`, `TLS_ALPN` и `TLS_PROFILE`, а также пояснение каждого поля. Значения редактируются по одному: `XHTTP_MODE`, `TLS_FINGERPRINT`, `TLS_ALPN` и `TLS_PROFILE` выбираются из нумерованных списков с возможностью ручного ввода там, где это нужно; домены, `UPSTREAM_PORT` и `XHTTP_PATH` вводятся вручную с проверкой формата. Дефолт для `UPSTREAM_PORT` - `443`. Перед применением можно посмотреть preview Caddyfile и JSON `downloadSettings`. Config записывается только после выбора `Применить настройки`.

Для установки без интерактивных вопросов:

```bash
DOWNLOAD_DOMAIN=cdn.example.com \
UPSTREAM_DOMAIN=api.example.com \
UPSTREAM_PORT=443 \
XHTTP_PATH=/vless-xhttp \
XHTTP_MODE=auto \
bash install-caddy-download-proxy.sh
```

Установщик создаёт `/etc/caddy/conf.d/xhttp-download-proxy.caddy` с reverse proxy на основной endpoint:

```caddyfile
cdn.example.com {
    reverse_proxy https://api.example.com {
        header_up Host api.example.com
        flush_interval -1
        transport http {
            tls_server_name api.example.com
        }
    }
}
```

Для xHTTP download endpoint не включай `encode zstd gzip`: сжатие Caddy может нарушить streaming-ответ `downloadSettings` и привести к обрыву клиентского TLS-туннеля.

После настройки нужно вставить JSON из `caddy-menu -> Показать JSON downloadSettings` в расширенные настройки xHTTP-подключения и выдать клиентам новые ссылки. Это client-side часть профиля: уже импортированные старые ссылки не узнают о новом download endpoint автоматически.

Сменить transport существующего подключения:

```bash
xray-client connection-transport ИМЯ_ИЛИ_TAG tcp
xray-client connection-transport ИМЯ_ИЛИ_TAG grpc --grpc-service-name vless-grpc
xray-client connection-transport ИМЯ_ИЛИ_TAG xhttp --xhttp-path /vless-xhttp --xhttp-mode auto
```

После смены transport нужно выдать клиентам новые VLESS-ссылки через `xray-client link ИМЯ` или Telegram-кнопку получения актуальной ссылки.

Удалить подключение вместе со всеми клиентами в нём:

```bash
xray-client remove-connection ИМЯ_ИЛИ_TAG
```

Удаление убирает VLESS inbound из `config.json`, запись подключения из `manager.db`, всех клиентов этого подключения и их историю трафика.
Последнее VLESS-подключение удалить нельзя.
Последнее Reality-подключение удалить нельзя.
Удаление Trojan-подключения убирает Trojan inbound, запись подключения и credentials этого подключения. Если у клиента были другие credentials, сам клиент остаётся в базе; если это был последний credential клиента, клиент удаляется вместе с подключением. Последнее VLESS/Reality-подключение остаётся защищено от удаления.
