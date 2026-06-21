# Подключения

[← README](../README.md)


Показать доступные VLESS-подключения:

```bash
xray-client connection-list
```

Создать дополнительное подключение:

```bash
xray-client add-connection ИМЯ PORT REALITY_SNI FINGERPRINT
xray-client add-connection ИМЯ PORT REALITY_SNI FINGERPRINT grpc --grpc-service-name vless-grpc
xray-client add-connection ИМЯ PORT REALITY_SNI FINGERPRINT xhttp --xhttp-path /vless-xhttp --xhttp-mode auto
```

Пример:

```bash
xray-client add-connection backup443 8443 www.microsoft.com chrome
```

Новое подключение создаёт отдельный VLESS Reality inbound с собственным портом, SNI, DEST, fingerprint и transport.
`REALITY_DEST` создаётся автоматически как `REALITY_SNI:443`.
Transport по умолчанию - `tcp`; для него используется Vision flow `xtls-rprx-vision`. Для `grpc` и `xhttp` flow в клиентский config и VLESS-ссылку не добавляется.
При добавлении клиента через меню, если подключений больше одного, появится выбор подключения.
При создании Reality/TLS-подключения или смене transport через меню `XHTTP_MODE` для `xhttp` выбирается из списка: `auto`, `packet-up`, `stream-up`, `stream-one`. В CLI то же значение передаётся через `--xhttp-mode`.

Переименовать подключение без изменения tag, порта, ключей и клиентских ссылок:

```bash
xray-client connection-rename ИМЯ_ИЛИ_TAG НОВОЕ_ИМЯ
```

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

В этом разделе Caddy разделён на подменю: `Состояние и проверка`, `Site configs`, `Управление сервисом` и `Бэкапы`. Через них можно установить Caddy, проверить config, посмотреть `Caddyfile` и site configs, создать или обновить site config из существующего TLS/XHTTP-подключения, создать site вручную, изменить TLS version, upstream local port или домен site, удалить site config, убрать дефолтный site `:80`, проверить TLS handshake, посмотреть логи, выполнить reload/restart Caddy, а также открыть backup для Caddy config и файлов сайта. При создании или изменении site config TLS выбирается из списка профилей: Caddy default, TLS 1.2, TLS 1.2 + TLS 1.3, TLS 1.3. Изменения site config валидируются через `caddy validate`; при ошибке менеджер откатывает изменённый файл из backup.

Через Telegram-владельца тот же TLS-профиль можно сменить в `/admin -> Настройки сервера -> TLS`. Бот показывает текущий профиль для каждого Caddy site config и время последнего изменения файла, затем применяет выбранный профиль с проверкой и reload Caddy.

Обычный `xray-backup` не включает Caddy config и файлы сайта. Для `/etc/caddy/Caddyfile`, `/etc/caddy/conf.d` и папки сайта используй `Подключения и TLS -> Caddy / TLS -> Бэкапы`.

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
