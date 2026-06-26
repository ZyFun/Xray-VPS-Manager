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

Все варианты проверяют новые `.dat` с текущим `config.json`, заменяют только изменившиеся файлы и перезапускают Xray. Если проверка или перезапуск не проходит, старые `.dat` восстанавливаются. Вариант `v2fly` обновляет только `geoip.dat`, а текущий `geosite.dat` не трогает.
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
xray-manager-update --update v1.0.0 --force
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
/usr/local/etc/xray/server.env
/usr/local/etc/xray/manager.db
Reality keys
clients
traffic history
Telegram settings
```

Перед заменой файлов создаётся backup в `/usr/local/lib/xray-vps-manager-backups`. После update или rollback менеджер пересобирает raw-log rotation units через `xray-activity raw-log-timer-sync`, затем выполняет `systemctl daemon-reload` и `try-restart` для manager-owned units: `xray-traffic-sync.timer`, `xray-raw-log-rotate.timer`, `xray-client-expire.timer`, `xray-traffic-sync.service`, `xray-raw-log-rotate.service`, `xray-client-expire.service` и `xray-telegram-poller.service`. Xray Core при этом не перезапускается, чтобы не рвать клиентские соединения без необходимости.

После обновления проверяется запуск `xray-vps-manager --help`, `xray-manager-update --help` и, если не передан `--no-test`, выполняется `xray-test`. Если проверка не проходит, менеджер пытается восстановить предыдущую версию из backup.

Эти действия доступны через меню `Обновления` -> `Менеджер` в `xray-menu`.


## Проверка Сервиса

Прогнать все безопасные тесты сервера:

```bash
xray-test
```

`xray-test` проверяет Xray, config.json, Reality-подключения и локальные порты, согласованность клиентов SQLite со всеми активными managed inbounds включая VLESS TLS/Caddy и Trojan TLS/WebSocket через Caddy, duplicate active client names между VLESS и Trojan credentials, TLS certificate diagnostics для direct TLS cert/key и managed Caddy sites, Stats API, SQLite-базу менеджера, `server.env`, таймзону, служебные команды, timers, сервис сбора трафика, torrent-правило, глобальный blocklist routing, каскадную конфигурацию и GeoIP bypass routes.
Проверка duplicate active client names warning-level: нормальная модель `один клиент -> несколько credentials` проходит, а проблемы выводятся только когда активный VLESS/Trojan дубль не сопоставляется с `client_credentials` в SQLite или повторно использует один и тот же active email.
TLS certificate diagnostics проверяет абсолютные пути direct TLS `certificateFile`/`keyFile`, базовые права файлов, срок действия сертификата, соответствие сертификата `SNI`/домену, наличие Caddy site config для managed TLS/Caddy-подключений, upstream local port, route path и live TLS handshake к `DOMAIN:PUBLIC_PORT`. Эта проверка warning-level: проблемы видны в выводе, но временный DNS/ACME/network сбой не блокирует весь `xray-test`.
Обычный запуск пропускает полный физический проход `PRAGMA quick_check` по SQLite-файлу и deep Caddy endpoint probes, потому что они могут занимать больше времени и зависеть от публичного DNS/TLS. Для глубокой проверки используйте:

```bash
xray-test --all
```

`xray-test --all` дополнительно проверяет Caddy endpoint для managed TLS-подключений. Для Trojan/WebSocket он отправляет пробный WebSocket upgrade на `WS_PATH` и ожидает ответ `101 Switching Protocols`; для TLS/XHTTP проверяет, что route не уходит в Caddy fallback JSON/HTML и upstream не возвращает `5xx`. Пустой `404` от Xray на обычный probe считается допустимым ответом неподходящего запроса к XHTTP endpoint. Также `--all` выводит понятные warning по deprecated Trojan и WebSocket transport: это не hard fail, а напоминание, что Trojan/WebSocket используется как compatibility/DPI-bypass режим и должен иметь отдельный план миграции, когда появится подходящая замена.

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
