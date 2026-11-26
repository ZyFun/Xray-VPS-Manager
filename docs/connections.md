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
Ключи Reality создаются через `xray x25519`; публичный ключ сохраняется в записи подключения в `manager.db`. Параметр `pbk` в VLESS-ссылках (`xray-client link`, Telegram-бот, стартовая ссылка) берётся из этой записи, поэтому выдача ссылок не запускает Xray и не зависит от формата его вывода. `xray x25519 -i` вызывается только как запасной путь, если сохранённого ключа нет или он некорректен (например, inbound добавлен в `config.json` вручную). Если вручную заменить `privateKey` Reality в `config.json`, сохранённый ключ не обновится и новые ссылки получат старый `pbk` - для новых ключей создавай новое подключение.
При добавлении клиента через меню, если подключений больше одного, появится выбор подключения.
Для TLS/XHTTP через Caddy fingerprint хранится как клиентский параметр подключения и попадает в VLESS-ссылку как top-level `fp`. Если используется `downloadSettings`, его `tlsSettings.fingerprint` настраивает отдельный fingerprint только для download endpoint.
При обновлении параметров подключения существующий файл стартовых ссылок также обновляется в поддерживаемых сценариях. Для конкретного клиента и подключения текущую ссылку можно получить через `xray-client link ИМЯ --connection TAG`. Формат файла и ограничения описаны в [инструкции по установке](installation.md).
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

Caddy должен иметь доступ к публичным `80/tcp` и `443/tcp`. Порт `80` нужен для ACME HTTP challenge и редиректов, порт `443` — для клиентского TLS/WebSocket подключения. Если внутренний `https_port` изменён, публичный `443` нужно явно направить на него.

Путь в меню:

```text
Подключения и TLS -> Подключения Trojan -> Создать Trojan TLS подключение
```

Мастер спрашивает имя подключения, локальный порт Xray, TLS-домен, WebSocket path, fingerprint и TLS-профиль Caddy. При автоматической настройке Caddy публичный порт всегда `443` и отдельно не запрашивается. При ручной настройке TLS мастер спрашивает публичный порт. Production default - Caddy/WebSocket с TLS 1.2+1.3; сертификат и ключ вручную указывать не нужно: Caddy сам управляет ACME lifecycle.

Создать Trojan Caddy/WebSocket-подключение через CLI. `--transport ws` и `--install-caddy` указывать не нужно: это значения по умолчанию.

```bash
xray-client add-trojan-connection trojan-main 10100 vpn.example.com chrome
```

Если нужно явно задать WebSocket path, порт `443` или TLS-профиль:

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
- `--public-port` - публичный порт подключения: при автоматической настройке Caddy допустим только `443` (значение по умолчанию). С `--no-caddy` можно задать другой порт для ручной настройки TLS.
- `FINGERPRINT` - клиентский fingerprint в ссылке; по умолчанию `chrome`.
- `--no-caddy` - исключение из основного режима Trojan в менеджере: создать локальный Trojan/WebSocket inbound без установки или обновления Caddy site.

Команда заранее проверяет, что для `DOMAIN` ещё нет Caddy site config. Если site уже существует, создание подключения останавливается до изменения Xray config и SQLite, чтобы не перезаписать рабочий сайт или другое TLS-подключение. Для изменения существующего site используй `Подключения и TLS -> Caddy / TLS -> Site configs`.

Если конфликтов нет, в режиме с Caddy команда готовит локальный Xray inbound на `127.0.0.1:LOCAL_PORT` и предварительно проверяет текст нового Caddy site во временном Caddyfile через `caddy validate`. Затем команда сохраняет запись подключения в SQLite, применяет Xray config и записывает site config. После записи проверяется полный рабочий Caddyfile со всеми сайтами. Менеджер запускает или включает службу Caddy при необходимости и выполняет reload. При ошибке он пытается восстановить затронутые данные SQLite, Xray config, Caddy site, Caddyfile, состояние службы Caddy и её автозапуск. Если восстановление тоже не удалось, состояние требует проверки. При совпадении байтов Xray config его копия не создаётся. Подробнее о применении и восстановлении — в [клиентских командах](clients.md).

При первой установке менеджер скачивает пакет Caddy и проверяет будущий сайт извлечённым бинарником. После успешного применения Xray config он запоминает состояние службы и её автозапуска до установки пакета. Автоматический запуск службы на этапе установки отключён. Менеджер запускает Caddy после записи нового сайта и проверки полного Caddyfile. При последующей ошибке он пытается вернуть прежнее состояние службы и автозапуска. После отката пакет остаётся установленным.

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

При обновлении managed Trojan-WS с перестроением site допустим только публичный порт `443`. Для старой записи с другим портом укажи `--public-port 443` явно, иначе команда откажет до записи и создания backup. Прежние записи сохраняются при чтении, диагностике и штатном переименовании site.

Команда обновляет SQLite metadata и локальный Trojan inbound в Xray config. Caddy site config обновляется только для управляемого подключения. Для Trojan-WS, созданного с `--no-caddy`, публичный порт относится к внешнему TLS и при обновлении не сопоставляется с локальными inbound. `LOCAL_PORT` проверяется в обоих режимах. Если меняется `--local-port`, а этот TCP-порт на адресе inbound уже слушает другой процесс на сервере, команда завершается ошибкой `LOCAL_PORT is already in use on this server by another process: PORT` до изменения Xray config и SQLite. Если меняется домен site config, старый Caddy site заменяется новым с rollback при ошибке `caddy validate` или reload. После успешного изменения параметров подключения выдай клиентам новые ссылки через `xray-client link NAME`, потому что старая `trojan://` ссылка продолжит указывать на прежний домен, порт, path или fingerprint.

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

При `--install-caddy` допустим только публичный порт `443`, другой порт отклоняется до записи SQLite, конфигов, backup и перезапусков. Без `--install-caddy` можно задать другой `--public-port` для вручную настроенного TLS. При автоматической настройке мастер порт не спрашивает, а при ручной спрашивает.

Важно: публичный `443` должен быть направлен на Caddy. Если глобальный `https_port` Caddy изменён, настрой явное перенаправление публичного `443` на внутренний порт Caddy. Сама настройка `https_port` этого не делает. Если существующий Reality inbound уже слушает `443`, сначала перенеси его на другой публичный порт или не запускай `--install-caddy`. Менеджер не переносит существующие подключения автоматически, чтобы не сломать рабочие клиентские ссылки.

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

В `Состояние и проверка -> Проверить TLS handshake` при ручном вводе домена укажи порт (по умолчанию `443`). Обе пробы TLS 1.2 и TLS 1.3 используют его. При выборе сохранённого site с адресом `DOMAIN` меню запускает обе пробы на `443` без вопроса о порте. Для адреса `DOMAIN:PORT` обе пробы используют указанный порт, а в SNI передаётся домен без порта.

Пункт `Site configs -> Изменить домен site` сохраняет настройки TLS и upstream выбранного site и обновляет `publicHost` и `sni` в SQLite у управляемых подключений VLESS/TLS/xHTTP и Trojan-WS, если прежний домен site совпадает с `publicHost` подключения. Выбранный site должен иметь одиночный адрес `DOMAIN` или `DOMAIN:PORT`. Новое имя вводится без порта, а явно заданный публичный порт сохраняется. При неподдерживаемом формате адреса операция завершается до записи. Для `DOMAIN` и `DOMAIN:PORT` публичный порт ссылки и локальный upstream должны подтверждаться выбранным слушателем Caddy. Менеджер проверяет адрес TCP-слушателя для прежнего и нового адресов ссылки, а альтернативный слушатель — для прежнего адреса. Совпадения порта и upstream недостаточно при привязке к loopback. Конкретный IP в bind подтверждается совпадением с числовым адресом ссылки. Для доменного адреса ссылки такой bind без локального подтверждения приводит к отказу. Общий TCP wildcard сохраняет штатное поведение, а `tcp4` и `tcp6` подтверждают только подходящее известное семейство адресов. Имя в bind без подтверждённого адреса считается возможным маршрутом и при неоднозначности вызывает отказ. DNS для этой проверки не используется. Если связь неизвестна, операция отклоняется до записи SQLite, создания backup и reload. Если связь с отдельным статическим слушателем вне loopback подтверждена, ссылка сохраняет прежний домен, даже когда выбранный site содержит динамический маршрут. Если порты не совпадают и маршрут не подтверждён, в том числе при внешнем proxy или перенаправлении, операция отклоняется до записи SQLite, создания backup и reload. TLS-диагностика `xray-test` учитывает адрес site с портом. Если `publicHost` не задан или пуст, сравнивается `sni`. Тип подключения и наличие управляемого inbound определяются по конфигу Xray. Менеджер сопоставляет локальный порт inbound со статическими upstream `127.0.0.1:PORT` из результата `caddy adapt --validate` для выбранного site. Если после учёта явно заданного публичного порта проверяемая конфигурация содержит несколько блоков для нового домена, операция отклоняется до записи. Обновляются только подключения, для которых подтверждён такой маршрут. Один site может обслуживать оба протокола. Если по проверенному Caddyfile несколько маршрутов могут обслуживать один inbound и принимать прежний домен, менеджер учитывает публичный порт ссылки. Среди них учитываются маршруты с маской имени и без условия по хосту. При однозначной связи с другим site ссылка сохраняет прежний домен. Если установить связь невозможно, операция отклоняется до записи. Если состав upstream определён и маршрута к другому подключению нет, его адрес и SNI сохраняются. Если маршрут определить не удалось, например при динамическом upstream без подтверждённого маршрута или совпадении порта с неоднозначным адресом inbound, операция останавливается до изменения рабочих файлов и SQLite и показывает теги спорных подключений. Это относится и к site, созданному вручную. Прямой Trojan/TCP продолжает использовать прежний домен и свой сертификат. Site без связанных подключений также можно переименовать, если определён upstream local port. Перед записью менеджер проверяет временное дерево Caddyfile и статических относительных и абсолютных импортов внутри каталога Caddy, включая вложенные glob-импорты. В этом дереве выбранный site заменён вариантом с новым доменом, а импорты направлены во временное дерево вместо рабочего `conf.d`. Если построить эквивалентное дерево нельзя, например при импорте за пределами каталога, через симлинк или с подстановкой пути из окружения, операция останавливается до записи SQLite, создания backup и reload. Конфликт нового домена в основном Caddyfile или другом блоке управляемого `conf.d` отклоняется до записи. Если новый домен уже занят другим site, операция отклоняется до записи, создания backup и reload. Для site без связанных подключений при ошибке reload менеджер пробует restart Caddy. Если обе команды завершились ошибкой, менеджер восстанавливает прежний site. Для site со связанными подключениями ошибка reload сразу запускает восстановление файлов site и прежних записей SQLite. При отмене до или после удаления либо замены файла менеджер восстанавливает прежний site, Caddyfile и SQLite. При восстановлении проверяются байты файлов, чтобы сохранить независимые изменения. Если исходный site изменён параллельно, менеджер останавливает смену домена и при необходимости восстанавливает прежние записи SQLite. Если во время восстановления после ошибки reload файл site появился или изменился, менеджер сохраняет его, сообщает о конфликте и продолжает остальные действия восстановления.

После смены домена подключений перевыпусти через `xray-client link NAME` и выдай клиентам новые ссылки VLESS/TLS/xHTTP и Trojan-WS. Ранее выданные ссылки сохраняют старый домен. Проверь новый домен в `xray-client connection-list`, адрес и SNI новых ссылок и диагностику `xray-test`. DNS нового домена должен указывать на сервер, чтобы Caddy мог обслуживать TLS.

Если прежний домен используется отдельным xHTTP download proxy как `UPSTREAM_DOMAIN`, на вторичной точке открой `caddy-menu -> Настроить download proxy -> Изменить UPSTREAM_DOMAIN`, укажи новый домен без порта и выбери «Применить настройки». Proxy использует `UPSTREAM_DOMAIN` для Host и TLS SNI при обращении к основному endpoint. Затем выполни `Проверить DNS/TLS/proxy` и проверь загрузку с клиента через `downloadSettings`. `DOWNLOAD_DOMAIN` принадлежит отдельной точке и при смене основного домена сам по себе не требует изменения.

`xray-test` дополнительно выполняет warning-level TLS certificate diagnostics: для legacy direct TLS/TCP он проверяет `certificateFile`, `keyFile`, базовые права, срок действия сертификата и соответствие `SNI`; для managed Caddy/TLS-подключений сверяет Caddy site config с SQLite metadata и делает live TLS handshake к `DOMAIN:PUBLIC_PORT`. В режиме `xray-test --all` диагностика глубже проверяет Caddy endpoint: для Trojan/WebSocket отправляет пробный WebSocket upgrade на `WS_PATH`, для TLS/XHTTP проверяет HTTP route и отдельно предупреждает о deprecated Trojan/WebSocket в Xray.

Через Telegram-владельца тот же TLS-профиль можно сменить в `/admin -> Настройки сервера -> TLS`. Бот показывает текущий профиль для каждого Caddy site config и время последнего изменения файла, затем применяет выбранный профиль с проверкой и reload Caddy.

Обычный `xray-backup` включает `/etc/caddy/Caddyfile` и `/etc/caddy/conf.d`, если Caddy настроен, чтобы TLS/Caddy-подключения восстанавливались вместе с Xray config и `manager.db`. Файлы сайта и config-only операции Caddy доступны отдельно через `Подключения и TLS -> Caddy / TLS -> Бэкапы`.

## Типовые проблемы

Если после изменения TLS, Caddy, VLESS/XHTTP или Trojan/WebSocket подключение не работает, сначала запусти безопасную диагностику:

```bash
xray-test --all
```

Эта команда не меняет конфигурацию и не перезапускает сервисы. Она проверяет Xray config, SQLite, TLS certificate diagnostics, Caddy site configs, Caddy endpoint probes и выводит warning-level подсказки по deprecated Trojan/WebSocket.

Типовые проблемы:

- Несовпадение сертификата: клиентская ссылка использует один `sni`/`TLS_DOMAIN`, а Caddy или direct TLS сертификат выпущен на другой домен. Проверь `xray-client connection-list`, `Подключения и TLS -> Caddy / TLS -> Site configs` и вывод `xray-test --all`. Для Caddy-подключений сертификат должен соответствовать публичному домену подключения, а DNS должен указывать на сервер.
- Конфликт порта: публичный `443` может слушать только один сервис. Для Trojan/WebSocket и TLS/XHTTP через Caddy публичный `443` должен принадлежать Caddy, а Xray inbound должен слушать локальный `127.0.0.1:LOCAL_PORT`. Если Reality/VLESS уже занимает `443`, перенеси его на другой порт перед настройкой Caddy.
- Неподдерживаемый клиент: не каждый VPN-клиент одинаково поддерживает Trojan/WebSocket, VLESS XHTTP, XHTTP `downloadSettings`, XMUX и новые параметры Xray. Если ссылка импортируется, но соединение не поднимается, проверь поддержку конкретного transport/security в клиенте и выдай свежую ссылку через `xray-client link NAME`.
- Нет статистики трафика: трафик появляется после первого успешного соединения и очередного `xray-traffic-sync`. Проверь, что credential включён, email активного Xray client совпадает с записью в SQLite, `xray-traffic-sync --quiet` проходит без ошибок, а `xray-test --all` не показывает рассинхрон `SQLite matches active managed connections`.
- Ошибка разбора ключей Reality: `Failed to generate Reality key pair.` или `Failed to derive Reality public key.` выводит сырой вывод `xray x25519` (приватный ключ заменён на `<hidden>`). Менеджер понимает известные форматы Xray (`Private key`/`Public key`, `PrivateKey`/`Password`/`Hash32`, `PrivateKey`/`Password (PublicKey)`/`Hash32`): метку с `Private` считает приватным ключом, с `Public` или `Password` - публичным, значение должно быть base64url из 43 символов. Если формат снова изменился, сохрани этот вывод для исправления разбора.
- Telegram не принимает ссылку: Telegram-бот больше не принимает `vless://` или `trojan://` для привязки подписки. Для подключения к боту клиенту нужен ключ доступа формата `vpn-key:00000000-0000-0000-0000-000000000000`, который можно выдать через `xray-client key NAME` или админ-панель Telegram. Protocol-ссылки выдаются отдельно через `xray-client link NAME` или кнопку `Получить VPN-ссылку` уже после привязки.

Если менялись домен, публичный порт, `WS_PATH`, `XHTTP_PATH`, fingerprint, TLS profile или XHTTP advanced profile, старую клиентскую ссылку лучше считать устаревшей и выдать новую.

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

Удалить подключение и credentials клиентов в нём:

```bash
xray-client remove-connection ИМЯ_ИЛИ_TAG
```

Удаление убирает VLESS или Trojan inbound из `config.json`, запись подключения из `manager.db` и credentials клиентов этого подключения вместе с их статистикой трафика по credential.
Клиент, у которого это был последний credential, удаляется вместе с общим трафиком и историей трафика; команда печатает его в строке `Removed clients`.
Клиент с другими credentials остаётся в базе: удаляется только credential этого подключения, а общий трафик клиента, его история и трафик оставшихся credentials сохраняются. Такие клиенты перечислены в строке `Kept clients with other connections`.
Последнее VLESS-подключение удалить нельзя.
Последнее Reality-подключение удалить нельзя.
