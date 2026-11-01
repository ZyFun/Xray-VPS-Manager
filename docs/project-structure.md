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
xray-set-bypass
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
  system/
README.md
```

Корневые команды - это тонкие совместимые точки входа для ручного запуска и автоматизации. `install-caddy-download-proxy.sh` и `caddy-menu` относятся к отдельному Caddy-only сценарию второго download endpoint и не ставят основной Xray manager. Основной код находится в Python-пакете `xray_vps_manager`: команды в `commands`, общая инфраструктура в `core`, логика клиентов в `clients`, трафик в `traffic`, активность в `activity`, работа с Xray config в `xray`, Telegram-бот в `telegram`, SQLite-слой в `db`, системные сценарии (интерпретатор команд, генерация и доставка systemd units) в `system`.

`runner.py` задаёт общую обработку ошибок, проверку root и вход в команды; `commands/arguments.py` проверяет форму argv до рабочих операций. Контракт описан в [CLI](cli.md). Версия во время выполнения берётся из `xray_vps_manager.__version__`; тест сверяет её с меню и `pyproject.toml`.

`core.settings` задаёт единые чтение, нормализацию и сериализацию [server.env](backups-and-data.md#формат-serverenv), включая переносимую копию в бэкапе. `core.server_env` сохраняет совместимые имена функций. `update_env` выполняет точечное изменение под `manager.lock`, `write_env` — полную запись, `env_text` формирует текст без записи файла.

## Локальные тесты и CI

Текущий код требует Python 3.12+. Полный набор из корня репозитория:

```bash
python3.12 -m unittest discover -s tests -t .
```

`tests` — импортируемый пакет; работают также обычный discovery с `-s tests` и изолированный запуск `python3.12 -I -S -B -m unittest discover -s tests -t .`. Добавлять каталог `tests` в `PYTHONPATH` не требуется. Тесты используют временные файлы, подмены процессов и HTTP; отдельные регрессии создают локальные процессы и привязывают loopback-порты.

GitHub Actions (`.github/workflows/tests.yml`) запускает unittest на Python 3.12 и 3.13 для pull request и push в `dev`/`main`. Новых Python-зависимостей нет. Превращение DeprecationWarning в ошибки пока не включено.

## Архитектурные проверки

`tests/test_architecture.py` проверяет импорты между слоями; прежний allow-list содержит 12 пар и может только сокращаться. Отдельный `tests/fixtures/architecture-markers.json` фиксирует 340 существующих AST-вхождений операций и рабочих путей. Новое вхождение ломает тест, устранённое требует удаления записи из baseline. Номер строки не участвует в идентичности: учитываются модуль, функция/класс, категория, AST и число повторений.

Проверяются `print`, `input`, `sys.exit`, `SystemExit`, `open`, импорты и обращения к `subprocess`/`sqlite3`, строки с `/usr/local/` вне `core.paths`. Разрешения заданы по роли модуля: вывод в интерфейсах и `core.terminal`, ввод-вывод в явно перечисленной инфраструктуре. `subprocess.CalledProcessError`, `TimeoutExpired`, `CompletedProcess` и импорты, используемые только для этих имён, разрешены. Настоящие docstring не считаются рабочими путями; строки шаблонов units учитываются. Это статическая проверка перечисленных маркеров, а не полное определение всех способов ввода-вывода Python.
