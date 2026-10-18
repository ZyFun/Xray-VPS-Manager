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

Команда также удаляет per-client cascade routing rules и balancers из `config.json`, чтобы отдельные клиентские правила не продолжали направлять трафик через `cascade-*` после отключения каскадного маршрута. Метаданные каскадов и выбранные страны в SQLite сохраняются; пока каскадный маршрут отключён, клиентские правила не возвращаются (см. [Клиентские маршруты](#клиентские-маршруты)).

### Клиентские маршруты

Для клиента с выбранной страной менеджер держит в `config.json` правило `client-route-*` выше общего catch-all: оно отправляет трафик клиента в `cascade-*` этой страны. Страну выбирают командой `xray-client route`, в меню `Клиенты` -> `Изменить страну подключения` или кнопкой `Страна подключения` в Telegram-боте.

Такие правила существуют, только пока общим catch-all маршрутом служит каскад. При включённом WARP и после `xray-set-cascade --disable` менеджер снимает их и не возвращает при следующих записях клиентских данных — командах `xray-client` и минутном таймере, — иначе трафик этих клиентов шёл бы мимо `warp-out` или direct. Выбранные страны остаются в SQLite. Когда каскад снова становится общим маршрутом (`xray-warp disable`, `xray-warp remove`, `xray-set-cascade use`), правила восстанавливаются по ним, а клиент без выбранной страны получает активный каскад.

### Маршрут по умолчанию

Трафик, для которого не нашлось ни одного routing rule, Xray отправляет в первый outbound. Поэтому менеджер всегда держит `direct` первым в `config.json`, а `cascade-*` и `bypass-*` добавляет после него. Если catch-all правила нет — после `xray-set-cascade --disable`, удаления последнего каскада или отключения WARP без каскадов, — такой трафик идёт напрямую.

Без catch-all правила строка `Current catch-all route` в выводе `xray-set-cascade` и строка `route` в `xray-warp status` показывают фактический первый outbound, обычно `default direct`. В `config.json`, записанном прежней версией менеджера, первым может стоять `cascade-*` или `bypass-*`. Тогда статус показывает этот outbound, например `default bypass-ru`, а первая же команда `xray-set-cascade`, `xray-set-bypass` или `xray-warp`, которая сохраняет новый `config.json`, переставляет `direct` на первое место.


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

Если первый A-record `api.cloudflareclient.com` зависает на TLS handshake, менеджер попробует другие известные IPv4. Найденный рабочий адрес закрепляется managed-строкой `IP api.cloudflareclient.com # xray-vps-manager WARP API pin` в `/etc/hosts` только с согласия: в интерактивном запуске (в том числе из меню) менеджер показывает строку и спрашивает подтверждение, для неинтерактивного запуска есть флаги `xray-warp create --pin-hosts` / `--no-pin-hosts` (те же флаги принимает `xray-warp enable`, если профиля ещё нет). Без терминала и без флага `/etc/hosts` не меняется: печатается `WARN` с подсказкой `--pin-hosts`, создание профиля продолжается. Перед записью делается backup `/etc/hosts.bak.xray-warp.*`; прежняя managed-строка заменяется. Строки без управляемого комментария менеджер не удаляет: если для `api.cloudflareclient.com` уже есть своя запись, пин не добавляется, а `WARN` показывает эту запись для ручной проверки.
Endpoint `engage.cloudflareclient.com:2408` из профиля сначала тестируется как есть. IPv4 fallback записывается в `config.json` только если доменный endpoint не прошёл WARP-тест.

Включить WARP для всего управляемого исходящего tcp/udp-трафика Xray:

```bash
xray-warp enable
```

При включении WARP менеджер удаляет сохранённые в `config.json` per-client cascade routing rules и balancers, чтобы клиентские правила `user -> cascade-*` не перекрывали общий маршрут `warp-out`. Сами записи каскадов и выбранные страны в SQLite не удаляются, а пока WARP включён, клиентские правила не возвращаются (см. [Клиентские маршруты](#клиентские-маршруты)).
Команды WARP также очищают зависшие временные test/verify SOCKS inbounds и routing rules, если предыдущая проверка была прервана до автоматического восстановления.

Если при включённом WARP в `config.json` всё же есть клиентские правила — например, их вернула прежняя версия менеджера, — `xray-warp status` после таблицы печатает предупреждение с их числом:

```text
WARN: WARP включён, но в config.json остались клиентские правила client-route-* (2): трафик этих клиентов идёт мимо warp-out. Сними их командой xray-client sync-routes.
```

Проверить WARP без постоянного включения:

```bash
xray-warp test
```

Тест временно добавляет SOCKS inbound `127.0.0.1:10809`, направляет только его через `warp-out`, проверяет внешний IP и `https://www.cloudflare.com/cdn-cgi/trace`, затем возвращает исходный `config.json`.

Отключить WARP-маршрут:

```bash
xray-warp disable
```

Если в `config.json` ещё есть cascade outbounds, отключение WARP возвращает общий catch-all маршрут на первый доступный `cascade-*` и восстанавливает клиентские правила по выбранным странам из SQLite; если каскадов нет, обычный маршрут становится direct. Если `manager.db` прочитать не удалось, команда всё равно отключает WARP, печатает `WARN: client route DB sync skipped: …`, а клиентские правила потом восстанавливает `xray-client sync-routes`.
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

Если WARP был включён, удаление возвращает общий маршрут и клиентские правила так же, как `xray-warp disable`.
`xray-warp remove` также снимает managed-строку пина `api.cloudflareclient.com` из `/etc/hosts` (с backup), даже если WARP outbound уже был удалён раньше; чужие строки остаются, а без managed-строки файл не меняется.


## Одновременные операции

Команды, которые меняют маршрутизацию, выполняются по одной вместе с `xray-client`, `xray-telegram` и пунктами меню подключений: на время чтения и записи `config.json` и `manager.db` с перезапуском Xray они берут блокировку `/usr/local/etc/xray/manager.lock`. Это `xray-set-cascade` (`add`, `use`, `country`, `remove`, `--disable`), `xray-set-bypass` (`add`, `enable`, `region`, `target`, `disable`, `remove`) и `xray-warp` (`create`, `enable`, `disable`, `verify-disabled`, `remove`). Проверки `xray-set-cascade --test`, `test` и `test-select`, `xray-set-bypass test` и `xray-warp test`, проверка endpoint WARP при `create` и `enable` и проверка после `disable` временно меняют `config.json` и перезапускают Xray, поэтому держат блокировку всё время проверки. Регистрация WARP-аккаунта и генерация профиля wgcf выполняются до блокировки.

Когда маршрут применяется — `xray-set-cascade` (`add`, `use`, `remove`, `--disable`), `xray-set-bypass` (`add`, `enable`, `region`, `disable`, `remove`) и `xray-warp` (`create`, `enable`, `disable`, `remove`), — менеджер после перезапуска Xray в течение 3 секунд проверяет, что `xray.service` остаётся активным, а TCP-порты inbound из `config.json` принимают соединения; если Xray падает или порты не открываются, конфиг откатывается на предыдущий, Xray перезапускается с ним, и команда завершается ошибкой `xray did not start after restart: …`. Временные проверки WARP — `xray-warp test`, проверка endpoint при `create` и `enable`, `verify-disabled` и проверка после `disable` — используют ту же проверку старта: если временный Xray не стартует, проба считается неуспешной (для endpoint профиля менеджер переходит к запасным IPv4 endpoint), а исходный `config.json` восстанавливается и перезапускается с той же проверкой. Тестовые перезапуски `xray-set-cascade --test`, `test`, `test-select` и `xray-set-bypass test` пока выполняются без этой проверки.

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
