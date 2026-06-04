# xray_server

Единый комплект для установки Xray-сервера с нуля.

При каждом запуске `install.sh` создаётся новый конфиг:

- новый VLESS Reality
- новый UUID
- новый Reality key
- новый shortId
- стартовый клиент `starter`
- новая ссылка в `/root/xray-reality-client.txt`
- локальная статистика трафика через Xray API на `127.0.0.1:10085`
- сохранение накопительной статистики в `/usr/local/etc/xray/traffic.json`

Сохранённого `config.json` в папке нет. Каскадный outbound тоже заранее не сохранён.

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

Xray скачивается только из официального источника:

```text
https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip
```

Используется последняя стабильная версия из GitHub Releases.

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

## После установки

Стартовая ссылка:

```bash
cat /root/xray-reality-client.txt
```

Повторно вывести ссылку стартового клиента:

```bash
xray-client link starter
```

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

Список клиентов:

```bash
xray-client list
```

В списке показываются статус, дата добавления и трафик по клиенту:

```text
NAME     STATUS    IN       OUT      TOTAL    CREATED
starter  enabled    0.00GB   0.00GB   0.00GB  2026-06-03T21:01:34Z
```

`IN` - трафик от клиента к серверу, `OUT` - трафик от сервера к клиенту.
Накопительная статистика сохраняется в `/usr/local/etc/xray/traffic.json` и переживает перезапуск Xray или сервера.
`xray-traffic-sync.timer` сохраняет счётчики раз в минуту, а `xray.service` пытается сохранить их перед штатной остановкой.
Статистика начинает считаться после включения Xray stats и не восстанавливает трафик, который прошёл раньше.

Добавить или заменить каскад позже:

```bash
xray-set-cascade
```

Проверить каскад:

```bash
xray-set-cascade --test
```

Открыть интерактивное меню управления:

```bash
xray-menu
```

Через меню можно менять `PORT`, `REALITY_SNI`, `FINGERPRINT`, управлять клиентами и каскадом.
После смены `FINGERPRINT` перезапуск Xray не нужен, но клиентам нужно заново выдать ссылку.
