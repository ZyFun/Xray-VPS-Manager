# Каскад и WARP

[← README](../README.md)

## Каскад

Каскады хранятся как именованные Xray outbounds с tag `cascade-{name}`. Имя должно состоять из латинских букв, цифр, `-` или `_`, до 32 символов; первый символ должен быть буквой или цифрой. Старый tag `cascade-upstream` остаётся совместимым и считается каскадом с именем `upstream`. Для каждого cascade в SQLite хранится отображаемая страна; таблица `xray-set-cascade list` показывает её отдельным столбцом.

Показать таблицу каскадов и текущий catch-all маршрут:

```bash
xray-set-cascade list
```

Добавить или заменить каскад интерактивно. Команда спросит имя каскада, страну, затем VLESS-ссылку и создаст tag `cascade-{name}`. Первый каскад становится активным автоматически; последующие каскады добавляются без переключения текущего catch-all маршрута:

```bash
xray-set-cascade
```

Добавить или заменить каскад с заранее заданным именем и, при необходимости, страной. Если это не первый каскад, активный маршрут не меняется:

```bash
xray-set-cascade add backup
xray-set-cascade add backup --country Германия
```

Каскад можно добавлять VLESS-ссылкой с transport `tcp`, `ws`, `grpc` или `xhttp`. Для `grpc` из ссылки читается `serviceName`, для `xhttp` читаются `path` и `mode`.

При первичной синхронизации пустая страна активного каскада заполняется как `Германия`, а пустая страна остальных настроенных каскадов как `США`. Уже сохранённые вручную страны не перетираются.

Выбрать активный каскад из таблицы или по имени:

```bash
xray-set-cascade use
xray-set-cascade use backup
```

Изменить отображаемую страну существующего каскада без изменения Xray config:

```bash
xray-set-cascade country backup США
```

Проверить активный каскад с самого сервера:

```bash
xray-set-cascade --test
```

Проверить конкретный каскад:

```bash
xray-set-cascade test backup
xray-set-cascade test-select
```

Удалить каскад:

```bash
xray-set-cascade remove backup
```

Отключить каскадный catch-all маршрут, сохранив настроенные cascade outbounds:

```bash
xray-set-cascade --disable
```

Команда также удаляет per-client cascade routing rules и balancers из `config.json`, чтобы отдельные клиентские правила не продолжали направлять трафик через `cascade-*` после отключения каскадного маршрута. Метаданные каскадов и выбранные страны в SQLite сохраняются.


## GeoIP bypass

GeoIP bypass - это отдельный режим региональной маршрутизации. Он не делает `bypass-*` активным catch-all маршрутом и не заменяет обычные `cascade-*`, WARP или direct. Для выбранного региона Xray создаёт notification-compatible rule вида `geoip:CODE -> geoip-warning-CODE`, а outbound `geoip-warning-CODE` копирует настройки выбранного VLESS outbound `bypass-{name}`. Поэтому activity/Telegram продолжают видеть обычное GeoIP-событие, но фактический выход в интернет идёт через bypass-сервер.

Добавить или заменить bypass-сервер по VLESS-ссылке:

```bash
xray-set-bypass add ru --region RU --test-target ya.ru
```

Если `--region` не передан в интерактивном режиме, команда откроет numbered list популярных GeoIP-регионов и поиск по `geoip.dat`. В non-interactive режиме `--region CODE` обязателен. Тестовая цель задаётся как домен, IP-адрес или `http`/`https` URL через `--test-target`; в интерактивном режиме её можно указать после выбора региона или пропустить.

Основные команды:

```bash
xray-set-bypass list
xray-set-bypass enable ru
xray-set-bypass region ru RU
xray-set-bypass target ru ya.ru
xray-set-bypass disable ru
xray-set-bypass test ru
xray-set-bypass test ru --test-target https://ya.ru/
xray-set-bypass remove ru
xray-set-bypass status
```

Для одного GeoIP-региона может быть включён только один bypass route. Если регион уже занят другим `bypass-*`, интерактивная команда предложит заменить активный route, а non-interactive команда требует `--replace`.

При включении bypass менеджер:

- сохраняет metadata в `manager.db` в таблице `bypass_routes`;
- добавляет/обновляет outbound `bypass-{name}` в `config.json`;
- добавляет marker route `geoip:code -> geoip-warning-CODE`;
- копирует bypass outbound в `geoip-warning-CODE`;
- сохраняет необязательную тестовую цель в metadata маршрута;
- переводит `routing.domainStrategy` в `IPOnDemand`, пока есть GeoIP-dependent routing.

Глобальный blocklist, `geoip:private -> blocked` и запрет BitTorrent остаются выше GeoIP bypass. Если bypass включён для региона, пункт блокировок `Добавить из GeoIP RU` предназначен только для точечных запретов отдельных доменов/IP, а не для обычной маршрутизации всего региона.

Проверка выбранного bypass временно добавляет два локальных SOCKS inbound и возвращает исходный config после теста. `127.0.0.1:10809` принудительно направляется через выбранный `bypass-*` и проверяет внешний IP. `127.0.0.1:18109` использует обычные Xray routing rules: настроенная тестовая цель должна попасть в `geoip-warning-CODE` выбранного региона, а контрольная зарубежная цель `https://example.com/` должна остаться на обычном catch-all маршруте, например `cascade-*`. Если тестовая цель не задана, route split часть проверки пропускается, а forced outbound health-check всё равно выполняется.


## WARP

WARP настраивается как `wireguard` outbound внутри Xray с tag `warp-out`. Это не меняет системный маршрут сервера и не должно ломать SSH-доступ. Создание профиля само по себе не включает WARP для пользователей.

Создать WARP-профиль и добавить outbound в `config.json`:

```bash
xray-warp create
```

Если первый A-record `api.cloudflareclient.com` зависает на TLS handshake, менеджер попробует другие известные IPv4 и добавит managed-строку в `/etc/hosts` с backup исходного файла.
Endpoint `engage.cloudflareclient.com:2408` из профиля сначала тестируется как есть. IPv4 fallback записывается в `config.json` только если доменный endpoint не прошёл WARP-тест.

Включить WARP для всего управляемого исходящего tcp/udp-трафика Xray:

```bash
xray-warp enable
```

При включении WARP менеджер удаляет сохранённые в `config.json` per-client cascade routing rules и balancers, чтобы клиентские правила `user -> cascade-*` не перекрывали общий маршрут `warp-out`. Сами записи каскадов и выбранные страны в SQLite не удаляются.
Команды WARP также очищают зависшие временные test/verify SOCKS inbounds и routing rules, если предыдущая проверка была прервана до автоматического восстановления.

Проверить WARP без постоянного включения:

```bash
xray-warp test
```

Тест временно добавляет SOCKS inbound `127.0.0.1:10809`, направляет только его через `warp-out`, проверяет внешний IP и `https://www.cloudflare.com/cdn-cgi/trace`, затем возвращает исходный `config.json`.

Отключить WARP-маршрут:

```bash
xray-warp disable
```

Если в `config.json` ещё есть cascade outbounds, отключение WARP возвращает общий catch-all маршрут на первый доступный `cascade-*`; если каскадов нет, обычный маршрут становится direct.
После отключения менеджер временно добавляет отдельный локальный SOCKS inbound без принудительного маршрута на `warp-out`, проверяет обычный Xray-маршрут через Cloudflare trace и завершится ошибкой, если увидит `warp=on`.
Успешная строка `OK normal Xray route does not use WARP` выводится зелёным цветом в интерактивном терминале.

Отдельно проверить, что обычный Xray-маршрут не использует WARP:

```bash
xray-warp verify-disabled
```

Удалить WARP outbound из `config.json`, оставив локальные файлы профиля:

```bash
xray-warp remove
```


## Одновременные операции

Команды, которые меняют маршрутизацию, выполняются по одной вместе с `xray-client`, `xray-telegram` и пунктами меню подключений: на время чтения и записи `config.json` и `manager.db` с перезапуском Xray они берут блокировку `/usr/local/etc/xray/manager.lock`. Это `xray-set-cascade` (`add`, `use`, `country`, `remove`, `--disable`), `xray-set-bypass` (`add`, `enable`, `region`, `target`, `disable`, `remove`) и `xray-warp` (`create`, `enable`, `disable`, `verify-disabled`, `remove`). Проверки `xray-set-cascade --test`, `test` и `test-select`, `xray-set-bypass test` и `xray-warp test`, проверка endpoint WARP при `create` и `enable` и проверка после `disable` временно меняют `config.json` и перезапускают Xray, поэтому держат блокировку всё время проверки. Регистрация WARP-аккаунта и генерация профиля wgcf выполняются до блокировки.

Вопросы — имя и страна каскада, VLESS-ссылка, выбор каскада или bypass из таблицы, GeoIP-регион, тестовая цель, подтверждение удаления или замены активного маршрута региона — задаются до блокировки, поэтому ожидание ввода не задерживает другие операции. После ответов команда заново читает `config.json` и `manager.db` уже под блокировкой, поэтому изменения, сделанные другими командами за это время (например, добавленный клиент), не теряются. Если другая операция менеджера ещё выполняется, команда ждёт до 60 секунд, затем завершается без изменений:

```text
ERROR: Другая операция менеджера ещё выполняется: pid=PID cmd=xray-client add since=2026-10-08T10:00:00Z
Дождись её завершения и повтори команду.
```

Если, пока ты отвечал на вопросы, другая операция изменила тот же bypass-маршрут, `xray-set-bypass enable` и `xray-set-bypass target` завершаются без изменений с ошибкой `GeoIP bypass route bypass-NAME was changed by another operation. Run the command again.` Подтверждение замены относится только к маршруту, названному в вопросе: если за это время регион занял другой `bypass-*`, команда завершается без изменений с ошибкой `GeoIP region CODE is already enabled on bypass-OTHER. Use --replace to replace it.`

`xray-set-cascade list` и выбор каскада из таблицы дописывают в `manager.db` каскады из `config.json`, которых ещё нет в базе, тоже под этой блокировкой. Если она занята дольше 5 секунд, таблица всё равно показывается, а запись откладывается с предупреждением:

```text
WARN: Другая операция менеджера ещё выполняется: pid=PID cmd=xray-client add since=2026-10-08T10:00:00Z. Запись маршрутов клиентов из config.json в manager.db пропущена, следующее чтение повторит её.
```

Минутные проверки таймера `enforce-limits` и `expire-due` ждут блокировку до 30 секунд, поэтому во время долгой проверки каскада, bypass или WARP они могут быть пропущены и повторятся при следующем запуске (см. [Клиенты](clients.md)).
