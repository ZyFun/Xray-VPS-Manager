# Подключения

[← README](../README.md)


Показать доступные Reality-подключения:

```bash
xray-client connection-list
```

Создать дополнительное подключение:

```bash
xray-client add-connection ИМЯ PORT REALITY_SNI FINGERPRINT
xray-client add-connection ИМЯ PORT REALITY_SNI FINGERPRINT grpc --grpc-service-name vless-grpc
xray-client add-connection ИМЯ PORT REALITY_SNI FINGERPRINT xhttp --xhttp-path /vless-xhttp --xhttp-mode auto
```

Пример:

```bash
xray-client add-connection backup443 8443 www.microsoft.com chrome
```

Новое подключение создаёт отдельный VLESS Reality inbound с собственным портом, SNI, DEST, fingerprint и transport.
`REALITY_DEST` создаётся автоматически как `REALITY_SNI:443`.
Transport по умолчанию - `tcp`; для него используется Vision flow `xtls-rprx-vision`. Для `grpc` и `xhttp` flow в клиентский config и VLESS-ссылку не добавляется.
При добавлении клиента через меню, если подключений больше одного, появится выбор подключения.

Сменить transport существующего подключения:

```bash
xray-client connection-transport ИМЯ_ИЛИ_TAG tcp
xray-client connection-transport ИМЯ_ИЛИ_TAG grpc --grpc-service-name vless-grpc
xray-client connection-transport ИМЯ_ИЛИ_TAG xhttp --xhttp-path /vless-xhttp --xhttp-mode auto
```

После смены transport нужно выдать клиентам новые VLESS-ссылки через `xray-client link ИМЯ` или Telegram-кнопку получения актуальной ссылки.

Удалить подключение вместе со всеми клиентами в нём:

```bash
xray-client remove-connection ИМЯ_ИЛИ_TAG
```

Удаление убирает Reality inbound из `config.json`, запись подключения из `manager.db`, всех клиентов этого подключения и их историю трафика.
Последнее Reality-подключение удалить нельзя.

