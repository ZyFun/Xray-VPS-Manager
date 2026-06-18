# Резервные копии и данные

[← README](../README.md)

## Важные Файлы На Сервере

```text
/usr/local/etc/xray/config.json          основной конфиг Xray
/usr/local/etc/xray/manager.db           основная SQLite-база клиентов, трафика, активности, blocklist, Telegram и настроек оплаты
/usr/local/etc/xray/server.env           параметры подключения, имя сервера, порт, SNI, DEST, fingerprint, timezone
/usr/local/etc/xray/warp                 локальный WARP account/profile для Xray WireGuard outbound
/usr/local/sbin/xray-client              управление клиентами
/usr/local/sbin/xray-menu                интерактивное меню
/usr/local/sbin/xray-set-cascade         управление каскадом
/usr/local/sbin/xray-warp                управление WARP outbound
/usr/local/sbin/xray-traffic-sync        сохранение статистики
/usr/local/sbin/xray-update              обновление и откат Xray
/usr/local/sbin/xray-manager-update      обновление и откат Xray VPS Manager
/usr/local/sbin/xray-backup              резервное копирование и восстановление данных
/usr/local/sbin/xray-test                безопасная диагностика сервера
/usr/local/sbin/xray-activity            журнал активности и отчёты по метаданным
/usr/local/sbin/xray-telegram            Telegram-уведомления о GeoIP-событиях
/usr/local/sbin/xray-vps-manager         единая CLI-точка входа в команды менеджера
/usr/local/lib/xray-vps-manager          Python-пакет менеджера
/root/xray_server/bootstrap.sh           установка чистого сервера из GitHub Releases
/root/xray-reality-client.txt            стартовая ссылка
/root/xray_backups                       архивы резервных копий данных
/root/xray_activity_exports              экспортированные отчёты активности
/usr/local/lib/xray-vps-manager-backups  архивы отката менеджера
/etc/ssh/sshd_config.d/00-xray-vps-manager.conf  managed-настройка SSH password login
/etc/systemd/system/xray-telegram-poller.service  быстрые ответы Telegram-бота через long polling
/etc/systemd/system/xray-client-expire.timer  ежедневное отключение клиентов с истёкшим сроком
```


## Резервные Копии Данных

Архив данных создаётся командой:

```bash
xray-backup create
```

В архив входят:

```text
/usr/local/etc/xray/config.json
/usr/local/etc/xray/server.env
/usr/local/etc/xray/manager.db
```

Архивы хранятся на сервере в `/root/xray_backups`.
Архив содержит Reality private key, UUID клиентов, SQLite-базу менеджера, статистику трафика, журнал активности, глобальные блокировки, исключения suspicious и token Telegram-бота, поэтому его нужно хранить как приватный секрет.

`server.env` сохраняется как переносимая конфигурация. Host-specific значения, например `SERVER_ADDR` и `SECURITY_AUDIT_LAST_RUN`, в новый архив не записываются. При восстановлении `xray-backup restore` сохраняет текущий `SERVER_ADDR` нового сервера, чтобы новые VLESS-ссылки генерировались с актуальным адресом.

Показать бэкапы на сервере:

```bash
xray-backup list
```

Удалить бэкап с сервера:

```bash
xray-backup delete /root/xray_backups/ИМЯ_АРХИВА.tar.gz
```

Через меню `Резервные копии` -> `Удалить бэкап` архив выбирается из таблицы, после этого нужно подтвердить удаление.
Удаление бэкапа не меняет текущие данные Xray.

Создать бэкап и скачать на компьютер, с которого выполняется подключение:

```bash
xray-backup create
scp root@SERVER_HOST:/root/xray_backups/ИМЯ_АРХИВА.tar.gz BACKUP_DESTINATION/
```

Через меню `Резервные копии` -> `Создать бэкап и показать команду скачивания` сервер создаст архив, спросит путь для сохранения и покажет готовую команду `scp`.
В меню можно выбрать систему компьютера, где будет выполняться `scp`, чтобы подставить подходящую папку загрузок по умолчанию.
Эту команду нужно выполнить в терминале на компьютере, с которого выполняется подключение.

Загрузить архив обратно на сервер перед восстановлением:

```bash
scp BACKUP_ARCHIVE_PATH root@SERVER_HOST:/root/xray_backups/
```

Восстановить данные из архива, который уже лежит на сервере:

```bash
xray-backup restore /root/xray_backups/ИМЯ_АРХИВА.tar.gz
```

Перед восстановлением менеджер автоматически создаёт pre-restore бэкап текущего состояния.
Если на сервере уже есть `/usr/local/etc/xray/manager.db`, дополнительно создаётся отдельная pre-restore копия SQLite-базы в каталоге резервных копий.
После восстановления менеджер проверяет `config.json`, перезапускает Xray и включает timers.
Если архив переносится на сервер с новым IP или доменом, сначала установи менеджер на новом сервере, затем восстанови архив. `SERVER_ADDR` из нового `server.env` будет сохранён, а старый адрес из архива не перезапишет новый.


## SQLite Данные

SQLite-база менеджера хранится в:

```text
/usr/local/etc/xray/manager.db
```

`install.sh` создаёт эту базу сразу. Клиенты, Reality-подключения, трафик, журнал активности, исключения suspicious, Telegram-настройки, подписки, флаг личных уведомлений активности и настройки оплаты хранятся в `manager.db`.

Подробная схема таблиц и индексов описана отдельно: [Схема базы данных](database-schema.md).

Проверить состояние базы:

```bash
xray-vps-manager sqlite status
```

Эта команда показывает путь к базе, версию схемы, результат `PRAGMA quick_check`, готовность SQLite и количество строк в основных таблицах, включая activity blocklist и hit-счётчики. То же доступно через меню `Настройки Xray` -> `Обновление Xray`:

```text
SQLite: статус базы
```


## Автоматические Технические Бэкапы

Перед постоянными изменениями Xray config менеджер создаёт технический бэкап текущего конфига.

Бэкапы конфига лежат рядом с основным конфигом:

```bash
/usr/local/etc/xray/config.json.bak.*
```

Бэкапы предыдущих версий Xray лежат здесь:

```bash
/usr/local/lib/xray-backups
```

Если новый конфиг не проходит проверку, менеджер восстанавливает предыдущий конфиг и перезапускает Xray.
