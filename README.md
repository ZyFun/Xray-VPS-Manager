# xray_server

Единый комплект для установки и обслуживания Xray VLESS + Reality сервера с нуля.

Локальный источник проекта:

```bash
./xray_server
```

Текущий рабочий серверный alias:

```bash
root@SERVER_HOST
```

При каждом запуске `install.sh` создаётся новый конфиг:

- новый VLESS Reality inbound
- новый UUID
- новый Reality key
- новый shortId
- стартовый клиент `starter`
- новая ссылка в `/root/xray-reality-client.txt`
- локальная статистика трафика через Xray API на `127.0.0.1:10085`
- сохранение накопительной статистики в `/usr/local/etc/xray/traffic.json`

Сохранённого `config.json` в папке нет. Каскадный outbound тоже заранее не сохранён.

## Что Внутри

```text
install.sh
xray-menu
xray-client
xray-set-cascade
xray-traffic-sync
xray-update
README.md
```

## Установка

С локального компьютера:

```bash
scp -r ./xray_server root@SERVER_HOST:/root/
```

На сервере:

```bash
ssh root@SERVER_HOST
cd /root/xray_server
bash install.sh
```

Если сервер будет другим, замени `root@SERVER_HOST` в командах `scp` и `ssh`.

Xray скачивается только из официального источника Xray:

```text
https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip
```

Используется последняя стабильная версия из GitHub Releases. Если доступен digest-файл, установщик проверяет SHA256.

Перед установкой Xray можно изменить базовые параметры:

```text
PORT
REALITY_SNI
CLIENT_NAME
FINGERPRINT
```

Нажми Enter на любом вопросе, чтобы оставить значение по умолчанию.

Значения по умолчанию:

```text
PORT=443
REALITY_SNI=www.microsoft.com
CLIENT_NAME=starter
FINGERPRINT=chrome
```

`REALITY_DEST` создаётся автоматически из SNI и порта:

```text
REALITY_DEST=REALITY_SNI:PORT
```

`FINGERPRINT` задаёт маскировку браузера/uTLS в клиентской ссылке `vless://`.
Доступные варианты: `chrome`, `firefox`, `safari`, `ios`, `android`, `edge`, `360`, `qq`, `random`, `randomized`.

Во время установки появится вопрос:

```text
Add cascade upstream VLESS link now? [y/N]:
```

Нажми Enter или введи `n`, чтобы продолжить без каскада.

Введи `y`, чтобы сразу вставить ссылку исходящего/root-сервера вида `vless://...`.

## После Установки

Стартовая ссылка выводится в конце установки и сохраняется здесь:

```bash
/root/xray-reality-client.txt
```

Повторно вывести ссылку стартового клиента:

```bash
xray-client link starter
```

## Меню

Открыть интерактивное меню:

```bash
xray-menu
```

В шапке меню выводятся установленная версия Xray и дата последнего обновления меню:

```text
+-------------------------------------+
| Xray Menu                           |
+--------------+----------------------+
| Xray Version | 26.3.27              |
| Menu Updated | 2026-06-04 09:33 UTC |
+--------------+----------------------+
```

Пункты меню выводятся таблицей. После выбора пункта вывод команды отделяется визуальным блоком:

```text
==================== Показать клиентов =====================

... вывод команды ...

============================================================
```

Через меню можно менять `PORT`, `REALITY_SNI`, `FINGERPRINT`, управлять клиентами, каскадом, обновлением и откатом Xray.
При обновлении `PORT` или `REALITY_SNI` значение `REALITY_DEST` пересчитывается автоматически.
После смены `FINGERPRINT` перезапуск Xray не нужен, но клиентам нужно заново выдать ссылку.

## Клиенты

Добавить клиента:

```bash
xray-client add ИМЯ
```

Временно отключить клиента без удаления:

```bash
xray-client disable ИМЯ
```

Включить клиента обратно с той же ссылкой:

```bash
xray-client enable ИМЯ
```

Полностью удалить клиента:

```bash
xray-client remove ИМЯ
```

Повторно вывести ссылку клиента:

```bash
xray-client link ИМЯ
```

Список клиентов:

```bash
xray-client list
```

В списке показываются статус, online/offline, последнее подключение, дата добавления и трафик по клиенту:

```text
+------------+---------+---------+----------+----------+----------+----------------------+----------------------+
| NAME       | STATUS  | ONLINE  | IN       | OUT      | TOTAL    | LAST ONLINE          | CREATED              |
+------------+---------+---------+----------+----------+----------+----------------------+----------------------+
| starter    | enabled | offline | 0.00KB   | 0.00KB   | 0.00KB   | never                | 2026-06-03T22:37:11Z |
| phone  | enabled | online  | 245.20MB | 292.36MB | 537.56MB | 2026-06-04 09:33 UTC | 2026-06-03T22:38:51Z |
+------------+---------+---------+----------+----------+----------+----------------------+----------------------+
```

`IN` - трафик от клиента к серверу, `OUT` - трафик от сервера к клиенту.
Значения показываются в удобных единицах: `KB`, `MB` или `GB`.
`ONLINE` показывает `online`, если за последние 5 минут было зафиксировано принятое подключение или прирост трафика.
`LAST ONLINE` - последнее время, когда Xray зафиксировал подключение клиента к серверу.

Накопительная статистика сохраняется в `/usr/local/etc/xray/traffic.json` и переживает перезапуск Xray или сервера.
`xray-traffic-sync.timer` сохраняет счётчики раз в минуту, а `xray.service` пытается сохранить их перед штатной остановкой.
Статистика начинает считаться после включения Xray stats и не восстанавливает трафик, который прошёл раньше.

## Каскад

Добавить или заменить каскад:

```bash
xray-set-cascade
```

Проверить каскад с самого сервера:

```bash
xray-set-cascade --test
```

Отключить каскад:

```bash
xray-set-cascade --disable
```

## Обновление Xray

Проверить текущие настройки, конфиг, сервис, локальную статистику и совместимость latest-версии Xray с текущим `config.json`:

```bash
xray-update --check
```

Обновить Xray из официального GitHub Releases:

```bash
xray-update --update
```

Если установленная версия уже совпадает с official latest или новее, скрипт выведет зелёное сообщение, что обновление не требуется.
Если проверка или обновление не проходит, скрипт выводит красное сообщение с причиной.
Перед заменой бинарника создаётся backup предыдущей версии в `/usr/local/lib/xray-backups`.

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

## Проверка Сервиса

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

## Важные Файлы На Сервере

```text
/usr/local/etc/xray/config.json          основной конфиг Xray
/usr/local/etc/xray/clients.json         база клиентов
/usr/local/etc/xray/server.env           адрес сервера, порт, SNI, DEST, fingerprint
/usr/local/etc/xray/traffic.json         накопительная статистика трафика
/usr/local/sbin/xray-client              управление клиентами
/usr/local/sbin/xray-menu                интерактивное меню
/usr/local/sbin/xray-set-cascade         управление каскадом
/usr/local/sbin/xray-traffic-sync        сохранение статистики
/usr/local/sbin/xray-update              обновление и откат Xray
/root/xray-reality-client.txt            стартовая ссылка
```

## Бэкапы

Скрипты создают бэкап конфига перед постоянными изменениями.

Бэкапы конфига лежат рядом с основным конфигом:

```bash
/usr/local/etc/xray/config.json.bak.*
```

Бэкапы предыдущих версий Xray лежат здесь:

```bash
/usr/local/lib/xray-backups
```

Если новый конфиг не проходит проверку, скрипт восстанавливает предыдущий конфиг и перезапускает Xray.
