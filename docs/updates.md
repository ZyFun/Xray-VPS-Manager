# Обновления и диагностика

[← README](../README.md)

## Обновление Xray

Проверить только доступность обновления Xray:

```bash
xray-update --check
```

Эта команда атомарная: она проверяет установленную версию, official latest и не запускает диагностику сервера.

Проверить совместимость latest-версии Xray с текущим `config.json` без установки:

```bash
xray-update --test-latest
```

Обновить Xray из официального GitHub Releases:

```bash
xray-update --update
```

Если установленная версия уже совпадает с official latest или новее, команда выведет зелёное сообщение, что обновление не требуется.
Перед обновлением и после установки новой версии `xray-update --update` запускает `xray-test`. Если диагностика или обновление не проходит, команда выводит красное сообщение с причиной.
Перед заменой бинарника создаётся backup предыдущей версии в `/usr/local/lib/xray-backups`.

Обновить `geoip.dat` и `geosite.dat` из official latest Xray Release:

```bash
xray-update --update-assets
```

Скачать более свежие `geoip.dat` и `geosite.dat` из Loyalsoldier fresh rules:

```bash
xray-update --update-assets loyalsoldier
```

Скачать только `geoip.dat` из v2fly geoip source:

```bash
xray-update --update-assets v2fly
```

Все варианты проверяют новые `.dat` с текущим `config.json`, заменяют только изменившиеся файлы и перезапускают Xray. Если проверка не проходит или Xray после перезапуска не остаётся активным либо не открывает порты inbound, старые `.dat` восстанавливаются. Вариант `v2fly` обновляет только `geoip.dat`, а текущий `geosite.dat` не трогает.
Эти действия доступны через меню `Обновления` -> `Geo assets`.

Показать сохранённые бэкапы:

```bash
xray-update --backups
```

Откатить Xray к последней сохранённой предыдущей версии:

```bash
xray-update --rollback
```

Откатить к конкретному бэкапу:

```bash
xray-update --rollback ИМЯ_БЭКАПА
```

При обновлении, откате и обновлении assets каждый перезапуск Xray проверяется: менеджер в течение 3 секунд проверяет, что `xray.service` остаётся активным, а TCP-порты inbound из `config.json` принимают соединения. Если Xray падает после перезапуска, обновление считается неуспешным и откатывается на предыдущую версию или прежние assets.

## Обновление Менеджера

Код меню, клиентских команд, Telegram-бота и Python-пакета менеджера обновляется отдельно от Xray Core. Для этого используется `xray-manager-update`, который скачивает архив конкретного GitHub Release из репозитория `ZyFun/Xray-VPS-Manager`.

Проверить latest release:

```bash
xray-manager-update --check
```

Обновиться до latest release:

```bash
xray-manager-update --update
```

Обновиться до конкретного тега:

```bash
xray-manager-update --update v1.0.1
```

Показать бэкапы менеджера:

```bash
xray-manager-update --backups
```

Откатиться к последнему backup:

```bash
xray-manager-update --rollback
```

Откатиться к конкретному backup:

```bash
xray-manager-update --rollback ИМЯ_БЭКАПА
```

Переустановить тот же тег или явно поставить тег старее текущей версии:

```bash
xray-manager-update --update v2.2.0 --force
```

Команда обновляет только файлы менеджера:

```text
/root/xray_server
/usr/local/sbin/xray-*
/usr/local/lib/xray-vps-manager/xray_vps_manager
```

Она не запускает `install.sh` и не трогает runtime-данные:

```text
/usr/local/etc/xray/config.json
/usr/local/etc/xray/server.env (кроме ключа MANAGER_PYTHON)
/usr/local/etc/xray/manager.db
Reality keys
clients
traffic history
Telegram settings
```

В `server.env` обновление и откат меняют только `MANAGER_PYTHON` — путь к интерпретатору команд менеджера (см. [Интерпретатор Python](#интерпретатор-python)).

Перед заменой файлов создаётся backup в `/usr/local/lib/xray-vps-manager-backups`. После update или rollback менеджер пересобирает raw-log rotation units через `xray-activity raw-log-timer-sync`, затем выполняет `systemctl daemon-reload` и `try-restart` для manager-owned units: `xray-traffic-sync.timer`, `xray-raw-log-rotate.timer`, `xray-client-expire.timer`, `xray-traffic-sync.service`, `xray-raw-log-rotate.service`, `xray-client-expire.service` и `xray-telegram-poller.service`. Xray Core при этом не перезапускается, чтобы не рвать клиентские соединения без необходимости.

Пакет `/usr/local/lib/xray-vps-manager/xray_vps_manager` заменяется не поверх, а через соседний каталог. Новая копия собирается рядом как `xray_vps_manager.new`; права, владелец и чистка служебных `._*` файлов применяются к ней до замены. Затем текущий каталог переименовывается в `xray_vps_manager.old`, новый — на его место, и `.old` удаляется. Под рабочим путём никогда не бывает наполовину скопированного пакета: там лежит либо полный старый, либо полный новый. Между двумя переименованиями рабочего пути на долю миллисекунды нет, и прогон таймера, совпавший с этим моментом, может завершиться ошибкой импорта. Если копирование в `.new` не удалось, старый пакет остаётся нетронутым; если не удалось второе переименование, старый каталог возвращается на место. Если прошлый запуск прервался между переименованиями и рабочего каталога нет, следующее обновление или откат сначала возвращает `xray_vps_manager.old` на место, а остатки `xray_vps_manager.new` удаляет. После успешного `--update` или `--rollback` в `/usr/local/lib/xray-vps-manager` остаётся только `xray_vps_manager`. Так же пакет заменяется при `--rollback` и при автоматическом откате неудачного обновления.

Если процесс обновления или отката убит именно между двумя переименованиями (например, `kill -9`, нехватка памяти или потеря питания), рабочего каталога пакета нет, и команды из `/usr/local/sbin` завершаются ошибкой `ModuleNotFoundError: No module named 'xray_vps_manager'`. В этом случае запусти откат копией команды из `/root/xray_server`: она берёт пакет из своей папки и не зависит от рабочего каталога.

```bash
/root/xray_server/xray-manager-update --rollback
```

Откат сначала вернёт `xray_vps_manager.old` на место, затем восстановит версию из последнего backup менеджера.

`--update` и `--rollback` выполняются по одному: на всё время обновления или отката, включая `xray-test`, они берут блокировку `/usr/local/etc/xray/manager-update.lock`. Если другое обновление или откат уже идёт, команда сразу завершается без изменений с кодом `1` и сообщением `Другое обновление или откат менеджера уже выполняется: …` с данными о процессе, который держит блокировку. Блокировка отдельная от `manager.lock`, поэтому обновление не задерживает команды клиентов и шаги минутного таймера: `manager.lock` берётся только на короткую запись `MANAGER_PYTHON` в `server.env`.

После обновления проверяется запуск `xray-vps-manager --help`, `xray-manager-update --help` и, если не передан `--no-test`, выполняется `xray-test`. Если проверка не проходит, менеджер пытается восстановить предыдущую версию из backup.

Эти действия доступны через меню `Обновления` -> `Менеджер` в `xray-menu`.

### Интерпретатор Python

Команды в бандле начинаются с `#!/usr/bin/env python3`. При `--update`, `--rollback` и автоматическом откате неудачного обновления `xray-manager-update` записывает в первую строку каждой команды `/usr/local/sbin/xray-*` абсолютный путь к Python 3.12+ и заменяет каждую команду целиком одним переименованием, без промежуточного состояния. Интерпретатор выбирается так же, как при установке (см. [Python 3.12](installation.md#python-312)):

1. `MANAGER_PYTHON` из `server.env`;
2. `python3.13` из `PATH`;
3. `python3.12` из `PATH`;
4. системный `python3`, если он сам 3.12 или новее, — по версионному пути, например `/usr/bin/python3.14`.

Подходящий интерпретатор — исполняемый Python 3.12+, у которого импортируются модули `sqlite3`, `ssl`, `zlib`, `lzma` и `zoneinfo`. Каждый пропущенный кандидат выводится строкой `WARN: <путь> пропущен: <причина>`, например `WARN: MANAGER_PYTHON=… пропущен: файл не найден или не исполняемый.` или `… пропущен: у Python 3.12.7 нет модулей sqlite3.`, и выбор продолжается. Поэтому Python, собранный без dev-пакетов, не попадёт в первую строку команд, а автоматический откат неудачного обновления не закрепит его в `MANAGER_PYTHON`.

Выбранный путь записывается в `server.env` как `MANAGER_PYTHON`, если он изменился; отсутствующий `server.env` команда не создаёт. Файл перечитывается и записывается под блокировкой `/usr/local/etc/xray/manager.lock` (ожидание до 60 секунд), чтобы не затереть ключи, которые в это время сохраняют команды маршрутизации, меню или `xray-backup restore`. Если блокировка занята дольше, выводится `WARN: MANAGER_PYTHON не записан в …: Другая операция менеджера ещё выполняется: …`, обновление продолжается, а ключ запишет следующее обновление. Поставленный позже `python3.13` не сменит интерпретатор сам: сохранённый `MANAGER_PYTHON` выбирается первым, пока он рабочий.

Откат не копирует первую строку из backup: содержимое команд восстанавливается, а shebang выставляется тем же выбором. Поэтому backup, созданный до перехода на абсолютный путь (с `#!/usr/bin/env python3`), или backup с путём к уже удалённому интерпретатору после отката тоже получает рабочий абсолютный путь. Код старых версий из backup совместим с Python 3.9 и работает на 3.12.

Если на сервере нет подходящего Python 3.12+, обновление не прерывается: команды остаются с `#!/usr/bin/env python3`, `MANAGER_PYTHON` не меняется, а вывод содержит строки `пропущен` по найденным кандидатам и `WARN: Python >= 3.12 с модулями sqlite3, ssl, zlib, lzma, zoneinfo не найден: …`. `xray-test` будет показывать `WARN` проверки `Helper interpreter`, пока не появится подходящий интерпретатор. Поставь Python 3.12 рядом с системным (см. [Python 3.12](installation.md#python-312)) и повтори обновление командой `xray-manager-update --update --force`. Код менеджера пока совместим с Python 3.9, но мажорная версия 3.0.0 поднимет минимальную версию до 3.12.

Первое обновление на версию с абсолютным shebang (2.2.0) выполняет ещё установленная старая версия: она копирует команды как есть, поэтому после обновления первая строка остаётся `#!/usr/bin/env python3`, а `xray-test` показывает `WARN` проверки `Helper interpreter`. Один раз переустанови тот же релиз — теперь его выполнит уже новый код:

```bash
xray-manager-update --update --force
head -1 /usr/local/sbin/xray-*
grep MANAGER_PYTHON /usr/local/etc/xray/server.env
xray-test
```

Все строки `head -1` должны показывать абсолютный путь вида `#!/usr/bin/python3.12`, а `xray-test` — не показывать `WARN` проверки `Helper interpreter`.

Если интерпретатор из первой строки удалён (например, после обновления ОС), команды из `/usr/local/sbin` перестают запускаться с ошибкой `bad interpreter`. Запусти переустановку, явно указав интерпретатор: команда выберет рабочий Python по порядку выше и перепишет первые строки.

```bash
python3 /usr/local/sbin/xray-manager-update --update --force
```


## Проверка Сервиса

Прогнать все безопасные тесты сервера:

```bash
xray-test
```

`xray-test` проверяет Xray, config.json, Reality-подключения и локальные порты, согласованность клиентов SQLite со всеми активными managed inbounds включая VLESS TLS/Caddy и Trojan TLS/WebSocket через Caddy, duplicate active client names между VLESS и Trojan credentials, TLS certificate diagnostics для direct TLS cert/key и managed Caddy sites, Stats API, SQLite-базу менеджера (включая режим журнала WAL, см. [SQLite данные](backups-and-data.md#sqlite-данные)), `server.env`, таймзону, служебные команды, timers, сервис сбора трафика, torrent-правило, глобальный blocklist routing, каскадную конфигурацию и GeoIP bypass routes.
Проверка duplicate active client names warning-level: нормальная модель `один клиент -> несколько credentials` проходит, а проблемы выводятся только когда активный VLESS/Trojan дубль не сопоставляется с `client_credentials` в SQLite или повторно использует один и тот же active email.
Проверка сервиса сбора трафика (`Traffic sync service`) читает итоговый список команд через `systemctl show --property=ExecStart xray-traffic-sync.service` — с учётом drop-in, без комментариев — и требует все семь шагов минутного таймера (см. ниже); если какого-то шага нет, она падает с `missing service command(s)`. Отдельная warning-level проверка `Traffic sync step independence` по тому же списку выводит `WARN` со списком шагов без префикса `-` (`ignore_errors=no`), а также посторонних команд без `-`, после которых в списке идут другие команды: такой unit остался от старой установки или переопределён drop-in, и ошибка одной команды в нём останавливает остальные.
TLS certificate diagnostics проверяет абсолютные пути direct TLS `certificateFile`/`keyFile`, базовые права файлов, срок действия сертификата, соответствие сертификата `SNI`/домену, наличие Caddy site config для managed TLS/Caddy-подключений, upstream local port, route path и live TLS handshake к `DOMAIN:PUBLIC_PORT`. Эта проверка warning-level: проблемы видны в выводе, но временный DNS/ACME/network сбой не блокирует весь `xray-test`.
Обычный запуск пропускает полный физический проход `PRAGMA quick_check` по SQLite-файлу и deep Caddy endpoint probes, потому что они могут занимать больше времени и зависеть от публичного DNS/TLS. Для глубокой проверки используйте:

```bash
xray-test --all
```

`xray-test --all` дополнительно проверяет Caddy endpoint для managed TLS-подключений. Для Trojan/WebSocket он отправляет пробный WebSocket upgrade на `WS_PATH` и ожидает ответ `101 Switching Protocols`; для TLS/XHTTP проверяет, что route не уходит в Caddy fallback JSON/HTML и upstream не возвращает `5xx`. Пустой `404` от Xray на обычный probe считается допустимым ответом неподходящего запроса к XHTTP endpoint. Также `--all` выводит понятные warning по deprecated Trojan и WebSocket transport: это не hard fail, а напоминание, что Trojan/WebSocket используется как compatibility/DPI-bypass режим и должен иметь отдельный план миграции, когда появится подходящая замена.

Проверка `Helper interpreter` warning-level: все команды `/usr/local/sbin/xray-*` должны начинаться с одного и того же абсолютного пути к Python 3.12+. `WARN` выводится, если первая строка — `#!/usr/bin/env python3`, если интерпретатор из неё не запускается, старше 3.12 или без модулей `sqlite3`, `ssl`, `zlib`, `lzma`, `zoneinfo` (`lacks modules: …`), а также если у команд разные первые строки. Проверки `Helper script syntax` и `Manager package` компилируют команды и установленный Python-пакет (`py_compile`) тем же интерпретатором, что указан в первой строке команд.
При проверке установленного Python-пакета служебные `._*` файлы не считаются исходниками менеджера.
Глубокий сетевой тест каскада остаётся отдельной командой `xray-set-cascade --test` или `xray-set-cascade test NAME`, потому что он временно меняет конфиг и перезапускает Xray.

Посмотреть статус Xray:

```bash
systemctl status xray --no-pager
```

Проверить, активен ли Xray:

```bash
systemctl is-active xray
```

Проверить конфиг Xray:

```bash
/usr/local/bin/xray run -test -config /usr/local/etc/xray/config.json
```

Перезапустить Xray:

```bash
systemctl restart xray
```

Посмотреть последние логи:

```bash
journalctl -u xray -n 80 --no-pager
```

Посмотреть открытые порты:

```bash
ss -tulpn
```

Проверить timer автоотключения клиентов:

```bash
systemctl status xray-client-expire.timer --no-pager
```

## Минутный таймер

`xray-traffic-sync.timer` раз в минуту запускает `xray-traffic-sync.service`. Сервис по порядку выполняет семь шагов:

1. `xray-traffic-sync --quiet` — сохраняет счётчики трафика;
2. `xray-activity sync --quiet` — читает журнал активности;
3. `xray-telegram notify-geoip --quiet` — GeoIP-уведомления;
4. `xray-client enforce-limits --quiet` — лимиты трафика;
5. `xray-client expire-due --quiet` — отключение клиентов с истёкшим сроком;
6. `xray-telegram notify-expiry --quiet` — напоминания о сроке доступа, после `expire-due`;
7. `xray-telegram notify-daily-summary --quiet` — ежедневная сводка.

Каждый шаг записан как `ExecStart=-…`, поэтому шаги независимы: ошибка одного шага не останавливает следующие. Например, пока Xray перезапускается и Stats API недоступен, `xray-traffic-sync` завершается с кодом 1, а лимиты, сроки доступа и напоминания всё равно выполняются в ту же минуту.

Из-за префикса `-` systemd не помечает `xray-traffic-sync.service` как `failed`, когда падает один из шагов. Ошибка видна только в журнале сервиса — строкой вида `status=1/FAILURE (ignoring)` рядом с выводом шага:

```bash
journalctl -u xray-traffic-sync.service --since "-1h"
```

Состояние `systemctl status xray-traffic-sync.service` и `systemctl --failed` для этого сервиса больше не показывает сбой отдельного шага.

Unit-файл пишет только `install.sh`. `xray-manager-update` выполняет `daemon-reload` и `try-restart`, но unit не переписывает, поэтому на сервере, установленном до этого изменения, шаги остаются зависимыми, а `xray-test` показывает `WARN` проверки `Traffic sync step independence`. Чтобы обновить unit вручную:

```bash
cp -a /etc/systemd/system/xray-traffic-sync.service /root/xray-traffic-sync.service.bak
sed -i 's|^ExecStart=/usr/local/sbin/|ExecStart=-/usr/local/sbin/|' /etc/systemd/system/xray-traffic-sync.service
systemctl daemon-reload
systemctl cat xray-traffic-sync.service
xray-test
```

В выводе `systemctl cat` все семь строк должны начинаться с `ExecStart=-/usr/local/sbin/`, а `xray-test` — не показывать `WARN` проверки `Traffic sync step independence`. Повторный запуск `install.sh` на настроенном сервере для этого не нужен: он перегенерирует ключи и конфиг.
