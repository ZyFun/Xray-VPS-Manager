# Состав проекта

[← README](../README.md)


```text
bootstrap.sh
install.sh
install-caddy-download-proxy.sh
pyproject.toml
caddy-menu
xray-vps-manager
xray-menu
xray-client
xray-set-cascade
xray-traffic-sync
xray-update
xray-backup
xray-test
xray-manager-update
xray-activity
xray-telegram
xray-warp
xray_vps_manager/
  cli.py
  runner.py
  commands/
  core/
  clients/
  traffic/
  activity/
  xray/
  telegram/
README.md
```

Корневые команды - это тонкие совместимые точки входа для ручного запуска и автоматизации. `install-caddy-download-proxy.sh` и `caddy-menu` относятся к отдельному Caddy-only сценарию второго download endpoint и не ставят основной Xray manager. Основной код находится в Python-пакете `xray_vps_manager`: команды в `commands`, общая инфраструктура в `core`, логика клиентов в `clients`, трафик в `traffic`, активность в `activity`, работа с Xray config в `xray`, Telegram-бот в `telegram`, SQLite-слой в `db`.
