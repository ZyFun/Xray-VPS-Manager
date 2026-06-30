# Журнал активности

[← README](../README.md)


Журнал активности собирает только метаданные из Xray access log: время, клиент, подключение, исходный адрес, назначение, порт, inbound/outbound и признаки риска. Он не расшифровывает HTTPS, не сохраняет содержимое сайтов, запросов, сообщений, файлов или тела запросов.

Сейчас активность разделена на три слоя:

- `activity_events` - подробный detailed log. Его можно отключить, включить для всех клиентов или включить только для выбранных клиентов.
- `activity_alert_events` - отдельный alert-log для GeoIP/split-tunneling и других риск-событий. Он работает независимо от detailed log и хранится 90 дней по умолчанию.
- `activity_client_counters` - лёгкие счётчики по клиентам и часам/дням. Они обновляются даже при выключенной подробной записи и не хранят список адресов в открытом виде.

Xray `access.log` не отключается командами активности. Он остаётся raw-источником для трафика, counters, alert-log и возможного ручного backfill. В свежей установке он настроен как `/var/log/xray/access.log`.

## Подробная запись

Включить подробную запись по всем клиентам:

```bash
xray-activity enable
```

То же самое явно через mode:

```bash
xray-activity detail-mode all
```

Отключить только detailed log:

```bash
xray-activity disable
```

или:

```bash
xray-activity detail-mode off
```

При `off` новые строки access.log всё равно читаются: обновляются alert-log, лёгкие счётчики, blocklist hits и offset чтения. Просто подробные строки не попадают в `activity_events`.

Включить detailed log только для выбранных клиентов:

```bash
xray-activity detail-clients set alice bob
xray-activity detail-mode selected
```

Показать режим и выбранных клиентов:

```bash
xray-activity detail-mode
xray-activity detail-clients
```

Очистить список выбранных клиентов:

```bash
xray-activity detail-clients clear
```

Ручной backfill detailed log из текущего и ротированных raw `access.log` архивов:

```bash
xray-activity backfill alice 2026-06-01 2026-06-30 --dry-run
xray-activity backfill alice 2026-06-01 2026-06-30 --apply --yes
xray-activity backfill all 2026-06-01 2026-06-30 --dry-run
```

Backfill всегда запускается явно. Сначала используй `--dry-run`: команда покажет target, период, найденные raw-файлы, сколько raw-строк было просмотрено, сколько событий распарсилось, сколько событий совпало с target/period, а также клиентов, риски и дубли. Режим `--apply` требует `--yes` и вставляет только отсутствующие detailed-события, чтобы повторный импорт не создавал дубли.
События старше текущего `ACTIVITY_RETENTION_DAYS` для detailed log не импортируются и учитываются как `Retention skipped`.

Показать статус:

```bash
xray-activity status
```

В статусе строка `Manager DB` показывает текущую SQLite-базу менеджера и её размер.
Строка `First event` показывает дату самого раннего детального события, которое сейчас осталось в `manager.db`, и сколько календарных дней назад оно было записано.

Синхронизация выполняется раз в минуту тем же `xray-traffic-sync.timer`, а вручную её можно запустить так:

```bash
xray-activity sync
```

## Alert-log и счётчики

Показать последние alert-события:

```bash
xray-activity alerts
```

Показать только GeoIP alert-события:

```bash
xray-activity alerts 50 geoip
```

Включить или выключить alert detection отдельно от detailed log:

```bash
xray-activity alert-detection on
xray-activity alert-detection off
```

Проверить GeoIP warning status и общий retention overview:

```bash
xray-activity geoip-status
xray-activity retention-overview
```

Срок хранения alert-log:

```bash
xray-activity alert-retention
xray-activity alert-retention 90
```

После изменения старые alert-события старше нового срока удаляются сразу.

`xray-activity suspicious` строится по `activity_alert_events`, а не по detailed `activity_events`. Поэтому сводка продолжает работать при `detail-mode off`. В неё попадают event-level риски `smtp`, `admin-port`, `blocked`, `torrent`, `xray-geoip:CODE`, а также window-level риски `burst`, `unique-hosts` и `unique-ports`; короткое состояние для rolling/bucket-порогов хранится в `manager.db` без полного raw-журнала адресов.

Если для региона включён GeoIP bypass, access/activity всё равно сохраняет обычный trigger `xray-geoip:CODE` через outbound `geoip-warning-CODE`. Дополнительно detailed events, counters и отчёты могут показывать метку `xray-bypass:CODE`. Она означает ожидаемую маршрутизацию через bypass-сервер и сама по себе не считается suspicious alert.

Показать лёгкую статистику клиентов:

```bash
xray-activity counters day 50
xray-activity counters hour 50
xray-activity counters-today 50
xray-activity counters-week 50
xray-activity counters-growth 50
```

Счётчики показываются агрегированно по клиенту и bucket: если у клиента несколько protocol credentials/connections, их события суммируются в одной строке клиента. Счётчики включают общее количество событий, GeoIP-события, risk-события, blocked-события, количество уникальных host/port и последнее время активности в bucket. Для точного подсчёта уникальных host/port менеджер хранит только хэши значений в `activity_client_counter_uniques`, а не сами адреса.
`xray-activity counters-growth` сравнивает последний дневной bucket клиента со средним по предыдущим дневным bucket-ам за 7-дневное окно и показывает только положительный рост по total events, unique hosts или unique ports.

## Ошибки Xray и raw logs

Показать нормализованные ошибки:

```bash
xray-activity errors
xray-activity errors-summary 7
xray-activity errors-days 7 50
xray-activity errors-days 7 50 warning,error
xray-activity error-detail ERROR_ID
```

Срок хранения таблицы ошибок:

```bash
xray-activity error-retention
xray-activity error-retention 180
```

Raw-файлы `/var/log/xray/access.log` и `/var/log/xray/error.log` хранятся 180 дней по умолчанию. Ротация выполняется manager-owned systemd timer с точным `OnCalendar`, который генерируется из `XRAY_RAW_LOG_ROTATE_TIME` и `MANAGER_TIMEZONE`. По умолчанию timer получает расписание вроде `OnCalendar=*-*-* 03:00:00 Europe/Moscow`; если `MANAGER_TIMEZONE` пустой, используется server local time. В timer включён `Persistent=true`, поэтому пропущенный запуск после перезагрузки будет догнан systemd.

Показать настройки raw logs:

```bash
xray-activity raw-logs
```

Показать архивы raw `access.log` и `error.log`:

```bash
xray-activity raw-log-archives
```

Изменить сроки хранения:

```bash
xray-activity raw-log-retention access 180
xray-activity raw-log-retention error 180
```

Изменить время ротации:

```bash
xray-activity raw-log-rotate-time 03:00
```

Команда сразу пересобирает `/etc/systemd/system/xray-raw-log-rotate.service` и `/etc/systemd/system/xray-raw-log-rotate.timer`, выполняет `systemctl daemon-reload`, включает timer и перезапускает его. Если меняется `MANAGER_TIMEZONE` через `xray-client set-timezone`, timer тоже пересобирается автоматически.

Принудительно пересинхронизировать timer из текущих настроек:

```bash
xray-activity raw-log-timer-sync
```

Ротировать вручную:

```bash
xray-activity rotate-raw-logs
```

Перед ротацией менеджер сначала запускает activity/error ingest, чтобы дочитать текущие `access.log` и `error.log` в `manager.db`. Если этот pre-sync не прошёл, ротация останавливается до переименования файлов, а ошибка записывается в `xray_error_events`. После успешного pre-sync и ротации менеджер выполняет `systemctl try-restart xray.service`, чтобы Xray переоткрыл log-файлы. Если restart не прошёл, ошибка записывается в `xray_error_events` с `source=manager` и `component=xray-logrotate`.

В статусе `xray-activity raw-logs` строка `Backfill access range` показывает самый ранний и самый поздний timestamp, найденный в текущем и ротированных raw `access.log` файлах. Если timestamp-ов нет, backfill всё равно можно запустить, но команда не найдёт подходящих событий.

Отчёт по клиенту за последние 7 дней:

```bash
xray-activity client ИМЯ 7
```

Отчёт по подозрительной активности:

```bash
xray-activity suspicious 7
```

Подробно показать GeoIP-риски с временем подключения, IP/доменом и клиентом:

```bash
xray-activity geoip-risks 7
```

Показать состояние GeoIP bypass и события, которые прошли через bypass marker:

```bash
xray-activity bypass-status
xray-activity bypass-events 7
```

Отчёт `xray-activity client ИМЯ 7` показывает отдельную колонку `BYPASS` рядом с `RISKS`, чтобы было видно, что региональный трафик не только зафиксирован как GeoIP-событие, но и обработан серверным bypass route.

`xray-activity client` показывает дневную сводку по клиенту и колонку `RISKS`. Если у клиента несколько credentials, отчёт дополнительно выводит блок `Credentials` с событиями, host, портами, outbound, рисками, исключениями и top hosts по каждому credential; credentials без событий за выбранный период остаются в таблице с нулём. В колонку `RISKS` попадают метки отдельных событий:

- `smtp` - порты `25`, `465`, `587`, `2525`
- `admin-port` - административные/служебные порты `22`, `23`, `135`, `139`, `445`, `3389`, `5900`
- `blocked` или `torrent` - событие ушло в блокировку или похоже на torrent-событие
- `xray-geoip:CODE` - Xray routing направил событие через GeoIP warning outbound выбранного региона

`xray-activity suspicious` - это отдельная сводка по найденным поводам для внимания. Сейчас туда попадают `smtp`, `blocked/torrent`, `xray-geoip:CODE`, всплеск событий в rolling-окне, слишком много уникальных host за период или слишком много уникальных портов за период.

`xray-activity geoip-risks` сначала читает `activity_alert_events`, поэтому работает даже при выключенном detailed log. Если за период в alert-log нет GeoIP-строк, команда использует fallback на старые detailed `activity_events`, чтобы уже накопленная история оставалась доступной. В меню быстрый просмотр GeoIP/RU alert-log доступен через `Трафик и активность` -> `Предупреждения активности` -> `GeoIP/RU события`.

## Глобальные блокировки IP и доменов

Глобальный blocklist позволяет добавить домен, IP или CIDR-сеть в Xray routing rule `blocked`. Такой трафик не отправляется дальше по cascade/WARP/direct и выглядит для клиента как таймаут. Правила blocklist вставляются в `config.json` раньше `geoip-warning-*`, поэтому после добавления адрес больше не создаёт GeoIP warning-уведомление, а уходит в `blocked`.

Список хранится в `manager.db` в общей таблице. В строке сохраняются:

- адрес или домен;
- тип значения: domain, ip, cidr или mask;
- клиент, из GeoIP-событий которого адрес был добавлен;
- время добавления;
- комментарий в свободной форме;
- срок блокировки: бессрочно или до конкретного UTC-времени;
- последнее срабатывание блокировки, если оно уже было зафиксировано в access log.

Блокировка всегда глобальная: поле клиента нужно для истории и сортировки таблицы, но само Xray-правило срабатывает для всех клиентов.

Показать список блокировок:

```bash
xray-activity blocklist
```

Показать кандидатов из GeoIP RU-событий конкретного клиента за 7 дней:

```bash
xray-activity block-candidates ИМЯ_КЛИЕНТА 7 RU
```

Добавить блокировку вручную или из меню:

```bash
xray-activity block-add example.com ИМЯ_КЛИЕНТА forever "описание адреса"
xray-activity block-add 203.0.113.10 ИМЯ_КЛИЕНТА 30 "временная блокировка"
```

`DURATION` задаётся как `forever` для бессрочной блокировки или числом дней. После добавления команда обновляет `config.json`, проверяет конфиг через `/usr/local/bin/xray run -test -config /usr/local/etc/xray/config.json` и перезапускает Xray. Если новый конфиг не проходит проверку, менеджер восстанавливает backup.

Через меню основной сценарий такой:

```text
Маршрутизация -> Блокировки IP/доменов -> Добавить из GeoIP RU
```

Меню попросит выбрать клиента, период журнала, затем покажет таблицу IP/доменов, по которым у клиента были события `xray-geoip:RU`. После выбора нужно ввести комментарий и срок блокировки: бессрочно или количество дней.
Адреса и домены, которые уже активно блокируются, в этой таблице выбора не показываются.

Удалить блокировку:

```bash
xray-activity block-delete VALUE_OR_ID
```

Принудительно пересобрать Xray routing rules из текущей таблицы:

```bash
xray-activity block-sync
```

Обычная минутная синхронизация `xray-activity sync` тоже сверяет `config.json` с активным blocklist. Когда срок блокировки истёк, следующее выполнение sync убирает значение из Xray routing.

Статистика срабатываний:

```bash
xray-activity block-stats
```

Статистика хранит адрес/домен, список клиентов, которые обращались к заблокированному адресу, количество срабатываний по каждому клиенту, первое и последнее срабатывание. Счётчики обновляются при чтении access log: если событие пришло через outbound `blocked` и host совпал с активным blocklist, менеджер увеличивает счётчик для пары адрес + клиент.

Исключения suspicious позволяют скрыть известные безопасные домены, IP или сети из отчётов suspicious и подробных GeoIP-рисков. События и исключения читаются из `manager.db`. В клиентском отчёте совпадения с исключениями считаются в колонке `EXCEPTIONS`.

Показать исключения:

```bash
xray-activity exceptions
```

Добавить исключение вручную:

```bash
xray-activity exception-add '*.apple.com'
```

Поддерживаются домены, IP, CIDR-сети и wildcard-маски: `mask.icloud.com`, `*.apple.com`, `203.0.113.10`, `203.0.113.0/24`.

Показать кандидатов для добавления из подозрительной активности:

```bash
xray-activity exception-candidates 7
```

Удалить одно исключение:

```bash
xray-activity exception-delete '*.apple.com'
```

Удалить все исключения:

```bash
xray-activity exception-delete-all --yes
```

То же доступно через меню `Трафик и активность` -> `Предупреждения активности` -> `Настройки исключений`.

Порог suspicious по умолчанию:

```text
burst = 1000 events / 15 minutes
unique hosts = 500
unique ports = 20
```

Порог burst считается по rolling-окну, а не по календарному часу. Это снижает ложные срабатывания на обычном тяжёлом трафике вроде видеостриминга, но всё ещё ловит резкие всплески автоматизации.

Показать текущие лимиты suspicious:

```bash
xray-activity risk-limits
```

Изменить лимиты:

```bash
xray-activity risk-limits set 1000 15 500 20
```

Аргументы: `BURST_EVENTS BURST_WINDOW_MINUTES UNIQUE_HOSTS UNIQUE_PORTS`.
То же можно сделать через меню `Трафик и активность` -> `Настройки` -> `Настроить лимиты suspicious`.

Event-level риски пишутся в alert-log сразу при разборе `access.log`. Window-level риски `burst`, `unique-hosts` и `unique-ports` вычисляются по короткому состоянию в `manager.db`: burst использует rolling-окно, а unique-пороги считаются в дневном bucket выбранного manager timezone.

Экспорт отчёта по клиенту:

```bash
xray-activity export ИМЯ 2026-06-01 2026-06-30
```

Экспорт создаётся в `/root/xray_activity_exports`. Такие архивы содержат чувствительные метаданные и должны храниться как приватные данные.
Через меню `Трафик и активность` -> `Экспорт activity` -> `Экспорт отчёта по клиенту` архив создаётся на сервере, затем меню спрашивает `SSH target/user@host`, папку сохранения и показывает готовую команду `scp` для локального терминала.

Показать архивы экспорта на сервере:

```bash
xray-activity export-list
```

Удалить архив экспорта:

```bash
xray-activity export-delete /root/xray_activity_exports/ИМЯ_АРХИВА.tar.gz
```

Через меню `Трафик и активность` -> `Экспорт activity` -> `Удалить архив экспорта` архив выбирается из таблицы, после этого нужно подтвердить удаление.

Удалить все архивы экспорта:

```bash
xray-activity export-delete-all --yes
```

Через меню `Трафик и активность` -> `Экспорт activity` -> `Удалить все архивы экспорта` удаляются все `.tar.gz` архивы из `/root/xray_activity_exports` после подтверждения. Журнал активности и данные Xray не удаляются.

Показать команду скачивания уже созданного экспорта:

```bash
xray-activity download-command /root/xray_activity_exports/ИМЯ_АРХИВА.tar.gz root@SERVER_HOST ACTIVITY_EXPORT_DESTINATION
```

Детальные события журнала активности хранятся в `/usr/local/etc/xray/manager.db`. Срок хранения детальных событий по умолчанию 365 дней, после чего старые записи удаляются автоматически. Накопительная статистика трафика также хранится в `manager.db`.

Показать текущий срок хранения:

```bash
xray-activity retention
```

Изменить срок хранения, например на 180 дней:

```bash
xray-activity retention 180
```

После изменения старые события старше нового срока удаляются сразу. Архивы экспорта в `/root/xray_activity_exports` удаляются вручную и не зависят от `ACTIVITY_RETENTION_DAYS`.

GeoIP routing-предупреждения для проверки split tunneling включаются отдельно от парсера и по умолчанию отключены. Через меню `Маршрутизация` -> `GeoIP routing` -> `GeoIP routing: выбрать регион` можно выбрать регион из списка или найти другой регион поиском по названию из списка либо по коду из `geoip.dat`.

Меню добавляет в `config.json` правило Xray вида `geoip:CODE -> geoip-warning-CODE`. Маршрут трафика не меняется: warning outbound дублирует текущий активный `cascade-*`, `warp-out` или `direct`, но в access log появляется отдельная метка outbound. На время включённой GeoIP-проверки `routing.domainStrategy` переключается в `IPOnDemand`, чтобы доменные цели успевали пройти DNS/GeoIP-проверку до общего catch-all правила. При отключении GeoIP routing менеджер восстанавливает прежний `domainStrategy`. Парсер активности не сканирует IP самостоятельно, а только читает метку outbound и показывает риск `xray-geoip:CODE`.
