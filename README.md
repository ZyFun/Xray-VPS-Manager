# Xray VPS Manager

Xray VPS Manager - интерактивный менеджер VPS с Xray VLESS Reality, дополнительными VLESS XHTTP/TLS-подключениями через Caddy и Trojan TLS/WebSocket-подключениями через Caddy. Основной сценарий работы проходит через `xray-menu`: в одном меню можно управлять клиентами, VLESS/Trojan-подключениями, каскадами, WARP, трафиком, журналом активности, резервными копиями, обновлениями Xray, SSH-безопасностью и Telegram-ботом.

CLI-команды остаются доступными для автоматизации и ручного запуска отдельных операций, но пользовательский вход по умолчанию - это меню.

## Быстрый старт

Быстрая установка последнего релиза на новый сервер без вопросов, со значениями по умолчанию:

```bash
apt update && apt install -y curl ca-certificates
curl -fsSL https://github.com/ZyFun/Xray-VPS-Manager/releases/latest/download/bootstrap.sh | bash
```

Интерактивная установка с вопросами по базовым настройкам:

```bash
apt update && apt install -y curl ca-certificates
curl -fsSL -o /tmp/xray-bootstrap.sh https://github.com/ZyFun/Xray-VPS-Manager/releases/latest/download/bootstrap.sh
bash /tmp/xray-bootstrap.sh
```

В интерактивном режиме можно выбрать начальный протокол для стартового клиента: `vless`, `trojan` или `both`. Быстрая установка через pipe остаётся VLESS Reality по умолчанию.

После установки открой меню:

```bash
xray-menu
```

Стартовая ссылка выводится в конце установки и сохраняется в `/root/xray-reality-client.txt`. Если при установке выбран `both`, файл содержит и `VLESS_CLIENT_URI`, и `TROJAN_CLIENT_URI`. Повторно вывести ссылку стартового клиента можно командой:

```bash
xray-client link starter
```

`install.sh` предназначен только для новой установки. Для обновления уже настроенного сервера используй `xray-manager-update`, чтобы не пересоздать рабочий Xray-конфиг.

## Возможности

- установка и обновление Xray Core из официальных релизов XTLS/Xray-core;
- создание одного или нескольких VLESS Reality-подключений с transport `tcp`, `grpc` или `xhttp`, TLS-терминированных XHTTP-подключений через Caddy, Trojan TLS/WebSocket-подключений через Caddy как compatibility/DPI-bypass режим и управление Caddy/TLS site configs из SSH-меню и Telegram-админки;
- управление клиентами с несколькими credentials, VLESS/Trojan-ссылками, переносом одиночных VLESS credentials между VLESS-подключениями, сроками доступа, статусом оплаты и traffic limits;
- постоянная SQLite-база `manager.db` для клиентов, трафика, активности, глобальных блокировок, Telegram-настроек, подписок и оплаты;
- статистика трафика через локальный Xray API, online/offline-статус, суммарная месячная сводка и история по часам/дням за 6 месяцев;
- каскадные outbound-серверы, выбор маршрута для отдельного клиента, GeoIP warning rules для проверки split tunneling и глобальная блокировка доменов/IP через `blocked`;
- WARP как Xray `wireguard` outbound без изменения системного default route;
- журнал активности по метаданным access log без чтения содержимого HTTPS, сообщений, файлов или тела запросов: detailed log можно включать для всех или выбранных клиентов, alert-log и лёгкие счётчики продолжают работать отдельно;
- Telegram-бот для клиентских подписок, актуальных ссылок, повторной выдачи ссылок владельцем, статуса, трафика, напоминаний об оплате с учётом месячной аренды сервера и годовой аренды домена, а также ограниченной админ-панели владельца;
- резервные копии `config.json`, переносимого `server.env`, консистентного SQLite snapshot `manager.db` и Caddy site configs с pre-restore backup перед восстановлением, плюс отдельные backup/restore операции для Caddy config-only и файлов сайта из меню Caddy/TLS;
- диагностика сервера, проверка Xray config, timers, SQLite, routing, blocklist/torrent-блокировок и SSH password login.

## Документация

- [Установка](docs/installation.md)
- [Состав проекта](docs/project-structure.md)
- [Меню](docs/menu.md)
- [Подключения](docs/connections.md)
- [Клиенты](docs/clients.md)
- [Журнал активности](docs/activity.md)
- [Telegram бот](docs/telegram.md)
- [Часовой пояс и лимиты трафика](docs/timezone-and-traffic.md)
- [Каскад и WARP](docs/cascade-and-warp.md)
- [Обновления и диагностика](docs/updates.md)
- [Безопасность SSH](docs/security.md)
- [Резервные копии и данные](docs/backups-and-data.md)
- [Схема базы данных](docs/database-schema.md)

## Основные команды

```bash
xray-menu                     # интерактивное меню
xray-manager-update --check   # проверить обновление менеджера
xray-manager-update --update  # обновить менеджер до latest release
xray-update --check           # проверить обновление Xray Core
xray-test                     # диагностика сервера
xray-backup create            # создать резервную копию данных
xray-set-bypass list          # показать GeoIP bypass routes
```

## Важные файлы на сервере

```text
/root/xray_server                         исходная папка установщика
/usr/local/sbin                           локальные команды менеджера
/usr/local/lib/xray-vps-manager           установленный Python-пакет менеджера
/usr/local/etc/xray/config.json           основной Xray config
/usr/local/etc/xray/server.env            переносимые параметры сервера
/usr/local/etc/xray/manager.db            SQLite-база менеджера
/etc/caddy/Caddyfile                      основной Caddy config для TLS site configs
/etc/caddy/conf.d                         TLS site configs Caddy
/root/xray_backups                        резервные копии данных и Caddy config
/root/xray_caddy_backups                  резервные копии Caddy config
/root/xray_caddy_site_backups             резервные копии файлов сайта Caddy
/usr/local/lib/xray-vps-manager-backups   резервные копии менеджера
```

## Обновление

Обновить сам менеджер из последнего GitHub Release:

```bash
xray-manager-update --update
```

Обновить Xray Core и geo assets:

```bash
xray-update --check
xray-update --update
```

Подробности: [Обновления и диагностика](docs/updates.md).
