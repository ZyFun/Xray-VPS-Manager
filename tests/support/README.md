# Sandbox для контрактных тестов

`Sandbox` создаёт временный сервер с `Paths.under(root)`, config, env и SQLite
схемы 8. Используется только последовательно в одном процессе: контекст меняет
модульные зависимости и возвращает их при выходе, в том числе после ошибки.

```python
from support.sandbox import Sandbox

with Sandbox() as box:
    result = box.run_cli("sqlite", "status")
    assert result.exit_code == 0
    assert "clients: 1" in result.stdout
    rows = box.db_rows("clients", "client_credentials")
```

`run_cli` и `run_menu(["0"])` возвращают `stdout`, `stderr`, `exit_code`.
Они выполняют настоящую точку входа в текущем процессе. Неожиданные ошибки
остаются исключениями теста. Меню принимает строки ответов, включая пустые.

Внешние команды нужно задавать явно, с точными аргументами:

```python
box.processes.script({
    ("/usr/local/bin/xray", "version"): box.processes.ok("Xray 26.1.1\n"),
    ("systemctl", "restart", "xray"): box.processes.result(1, stderr="failed\n"),
})
result = box.run_menu(["0"])
assert box.processes.calls == [("/usr/local/bin/xray", "version")]
```

`shutil.which` и захваченные `which=shutil.which` не читают `PATH` хоста.
По умолчанию возвращается `None`; доступные программы задаются отдельно:
`box.processes.executables["systemctl"] = "/usr/bin/systemctl"`.
Это только ответ поиска, файл не создаётся; вызов найденной программы тоже
нужно задать через `script`. Аргументы `mode` и `path` не включают поиск на хосте.

Один ответ повторяется; список ответов расходуется по порядку. Исключение
(например, `subprocess.TimeoutExpired`) можно задать вместо ответа. Журнал
`calls` хранит argv всех попыток, включая незаданные. Поддерживаются `run`,
`call`, `check_call`, `check_output`, текст/байты, PIPE и обычный вывод.
Незаданная команда, shell, прямой Popen или exec завершаются `UnexpectedCommand`.
Все запрещённые попытки сохраняются в `processes.unexpected`. Если продуктовый
код перехватил исключение, выход из `Sandbox` всё равно поднимает `AssertionError`
со списком попыток после восстановления зависимостей. Уже выходящее из блока
исключение сохраняется. При проверке самого запрета размещайте `assertRaises`
снаружи контекста `Sandbox`, чтобы проверить также ошибку выхода.
Незапланированные HTTP-запросы через urlopen, соединения через create_connection,
DNS через getaddrinfo и создание socket (включая bind-пробы портов)
также запрещены и записываются в `unexpected`. Успешные сетевые проверки
требуют отдельной явной подмены на уровне сценария.
Это фасад зависимостей для доверенных тестов, не изолятор
произвольного кода: новые внешние механизмы требуют отдельного шва.

После VIREI-103 время задаётся через `core.clock.frozen`; для немигрированного
кода фасад подменяет старые пути, defaults (включая `__wrapped__`),
классы datetime/date и захваченные runner-параметры. После VIREI-104 процессы
подключены через `core.process.fake`, HTTP — через `core.http.fake`. `LEGACY_PATH_ATTRS` —
явный реестр рабочих модульных констант, в том числе списков и производных путей.
Защитные списки отдельно перечислены в `LEGACY_GUARD_PATH_ATTRS`: для них
сохраняются исходные и перенесённые пути, а также их значения после `resolve()`.
Каждый вход сверяет полноту обоих реестров. Старые имена не должны появляться в сценарных
тестах. Удаляйте записи из реестра при миграции потребителей на `paths()`.
Часы `core.clock.now()` и legacy datetime/date заморожены; таймауты
на time/monotonic сохраняют ход. `clock.frozen(moment)` можно вложить в Sandbox
для следующего момента одного сценария: `now`, `today` и штампы
используют этот момент. Legacy datetime/date и SQLite остаются на исходном
времени своего Sandbox; уже открытые соединения не меняют часы при входе
во вложенный Sandbox. Выход восстанавливает прежние часы.
Локальная зона сервера берётся из `tz`; `server_local_timezone()` сохраняет
снимок смещения (контракт XR-REV-065), не исправляя DST в рефакторинге.
`LEGACY_CACHE_ATTRS` перечисляет сбрасываемые кэши (сейчас список GeoIP-кодов).
При входе они получают `None`; выход восстанавливает исходный объект,
в том числе при вложенных Sandbox и ошибках.
Trojan-пароли воспроизводимы, UUID/shortId/ключи задаются ответами FakeProcesses.
Подмена root-проверки и владельца файла не заменяет тестов прав в core/fs.

**Ограничение файловых литералов.** Подмена модульных констант не меняет пути,
созданные внутри функций. До их переноса на `paths()` от файлов хоста зависят:

- `commands.backup.sync_traffic`: наличие `/usr/local/sbin/xray-traffic-sync`
  и `/usr/local/sbin/xray-activity`;
- `commands.update.preflight` и `run_diagnostics`: таймеры в `/etc/systemd/system`
  и `/usr/local/sbin/xray-test`;
- `telegram.admin.run_server_test_text` и `create_backup_text`: helper-файлы
  `/usr/local/sbin/xray-test` и `/usr/local/sbin/xray-backup`;
- `commands.menu_security_actions.sshd_binary`: `/usr/sbin/sshd` и
  `/usr/local/sbin/sshd` независимо от подмены `which`;
- `xray.caddy.site_root_candidates`: `/usr/share/caddy`, `/var/www`,
  `/var/www/html`, `/srv/www`.

Не записывайте эти сценарии как переносимые golden без отдельной подмены
соответствующих файловых проверок. Литералы в argv перехватываются
`FakeProcesses`, но наличие файлов само по себе не подменяется.
Полный инвентарь строк внутри функций, начинающихся с `/`, находится в
`tests/fixtures/paths/legacy-function-literals.json`: модуль, функция, строка,
число вхождений. `test_sandbox_path_inventory.py` сверяет его через AST,
исключая канонический `core/paths.py`. Инвентарь консервативно содержит также
разделители, URL-пути и команды Telegram; это контроль изменений, а не
подмена файловой системы. При переносе литерала удаляйте его запись;
новые записи требуют проверки причины, а не автоматического обновления списка.

Фикстуры находятся в `tests/fixtures/{config,env,db,units}`. Параметры
`config`, `env`, `db` принимают путь относительно `tests` либо абсолютный;
`db=None` оставляет пустую схему. SQL загружается после штатной инициализации.
Пути журналов в config перенаправляются под root, MANAGER_TIMEZONE в env —
в выбранный `tz`. Существующие эталоны units сохраняются без изменений.
`db_rows` сортирует строки по первичному ключу, а без него — по всем колонкам.
Соединения, открытые через `sqlite3.connect` внутри Sandbox, используют
замороженное время в `strftime(format, 'now')`, включая DEFAULT и UPDATE.
Другие значения форматирует штатный SQLite. Это подмена используемой в проекте
формы с двумя аргументами; другие функции часов SQLite не замораживаются.
Закрывайте соединения до выхода из Sandbox; соединения, открытые до входа,
не меняются. Вложенные контексты имеют независимые часы.

`assert_golden("область/сценарий", {"stdout.txt": result.stdout, ...})`
сравнивает файлы в `tests/golden`, заменяя временный корень на `<ROOT>`.
По умолчанию отсутствующий файл или отличие — ошибка. Для создания/обновления:

```sh
UPDATE_GOLDEN=1 python3.12 -m unittest discover -s tests -p 'test_sandbox.py'
```

После обновления проверьте diff всех golden-файлов и повторите тест без флага.
Изменять существующие характеризационные ожидания в задаче рефакторинга нельзя.

## Переходные контракты настроек и времени (R1.2)

`core.settings` — канон `read_env`, `env_value`, `write_env`, `update_env`.
Без `path` путь определяется через `paths()` при вызове. `update_env` берёт
реентерабельный `manager_lock`, читает файл, заполняет отсутствующие ключи
из `defaults`, удаляет `remove`, затем применяет `changes`. Существующее пустое
значение не заменяется значением по умолчанию. Параметр `ordered_keys`
сохраняет старые порядки записи.
`write_env` полностью заменяет содержимое; restore и старые адаптеры полных
снимков продолжают использовать его. Не заменяйте снимок на merge без анализа
удаляемых ключей и обязательных пустых значений.

Старые имена `core.server_env` и `core.time` остаются адаптерами. Парсинг env
не нормализует ключи, export и хвостовые комментарии (XR-REV-069). Писатели
traffic и отметки аудита передают точечные изменения; восстановление,
Caddy env-файлы, activity, клиентские/Reality и routing-снимки сохраняют полную запись.
Activity оставляет совместимый адаптер: его CLI уже держит `activity.lock`,
а cleanup по контракту не берёт `manager.lock`. Переход на точечные записи
требует отдельного согласования порядка блокировок этих сценариев.

`core.clock.manager_timezone` возвращает `ManagerTimezone`; параметр `invalid`
сохраняет три прежние политики (`local`, `utc`, `raise`). Подписи и преобразование
исключений остаются у совместимых интерфейсов. Единая политика — отдельная B-задача.
`clients.runtime.parse_time` использует общий ISO-парсер с сохранением исходного
смещения и значимых пробелов. Activity/Telegram обрезают микросекунды у `utc_now`,
а `core.time.utc_now` сохраняет их; форматирование штампов оставлено прежним.

## Процессы, systemd и HTTP (R1.3)

`core.process.run` захватывает текстовые stdout/stderr, поддерживает `input_text`,
`timeout`, `env` и `check`. При `check=True` ошибка является одновременно
`ExternalCommandError` и `CalledProcessError`; старый `run_capture` сохраняет
прежний контракт. `run_stream` возвращает код завершения и выводит оба потока
в терминал, поэтому применяется только в интерфейсах. В Sandbox он не запускает
настоящий Popen. `processes.requests` дополняет журнал argv параметрами запуска.
Ответом `script` также может быть callable `(argv, **kwargs) → ProcessResult`
или `CompletedProcess` для сценария, состояние которого меняется между вызовами.

`core.process.fake(handler)` возвращает журнал argv. Callable-handler принимает
argv и возвращает `CompletedProcess`; `None` означает неизвестную команду и
ошибку без запуска процесса. Объект с методом `run(argv, **kwargs)`, например
`FakeProcesses`, получает также параметры запуска. Вложенные контексты процессов
и HTTP восстанавливают внешний обработчик даже после исключения.

HTTP задаётся отдельно, без патчей `commands.*`:

```python
from xray_vps_manager.core.http import http_json

with Sandbox() as box:
    url = "https://example.invalid/release"
    box.http.script({url: box.http.json({"tag_name": "v1.0.0"})})
    assert http_json(url) == {"tag_name": "v1.0.0"}
    assert box.http.calls == [(url, {"User-agent": "xray-vps-manager"}, 20)]
```

Ответ — байты (для проверок декодирования), `http.json(value)` или исключение.
Список ответов расходуется по порядку; неизвестный URL записывается в
`http.unexpected` и приводит к ошибке выхода, даже если сценарий её перехватил.
Подмена применяется и к оставшимся прямым `urlopen`, включая загрузку архива
manager-update. JSON-клиент сохраняет исключения urllib и декодирования;
интерфейсы сохраняют собственную обработку, User-Agent и таймауты 20/20/30 с.

`core.systemd` владеет единственным `MANAGER_UNITS` и проверкой рестарта.
Старые импорты из `core.process` остаются совместимыми алиасами. Сохранены окно
3 с, retry/reset-failed, проверки портов, результат `CompletedProcess` и
`SystemdUnitStartError`, наследующий `CalledProcessError`.

По уточнению пользователя при разборе 104-R02 новый `writers_paused` останавливает
все переданные units, включая oneshot в `activating`, и запускает только ранее
активные в `finally`, в том числе после частично неудачной остановки. Это закрывает
запуск service таймером между проверкой и остановкой. Legacy restore/cleanup
сохраняют `active_units(WRITER_RESTART_UNITS)` → `stop_writer_units()` →
`restart_units(selected)`: остановку всех семи писателей и восстановление только
ранее активных таймеров/poller, без повторного запуска минутных service.
Эти контракты нельзя взаимозаменять без отдельного изменения поведения.
Ошибки stop/start нового `writers_paused` являются `ExternalCommandError`
с командой, кодом завершения и выводом. Legacy-helper'ы сохраняют `RuntimeError`.
`support.restore_sandbox.RestoreSandbox` подключает общий Sandbox и канонический
фейк процессов, а старые подмены шагов restore держит внутри переходного фасада
до переноса самого сценария backup. Ожидания XR-REV-012 и ExecStop сохранены.
