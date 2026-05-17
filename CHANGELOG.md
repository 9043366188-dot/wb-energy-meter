# Changelog

Все значимые изменения проекта описаны в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/),
проект придерживается [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Запланировано

- Шаг 4: воркер почасовых агрегатов (`period_aggregates`) с догоном
  пропущенных часов после перезагрузки.

## [0.3.0] — 2026-05-04

### Добавлено

- MQTT-RPC клиент к `wb-mqtt-db` (`get_channels`, `get_values`) с
  тайм-аутами, обработкой ошибок (`RpcRemoteError`, `RpcTimeout`,
  `RpcConnectError`) и корректным порядком подписки до публикации
  запроса.
- Расчёт расхода электроэнергии за период по дельте `Total AP energy`
  с обработкой граничных случаев (`edge_approx`), внутренних пропусков
  (`gap`), сброса/замены счётчика (`reset`), отсутствия данных
  (`no_data`) и «молчащего» счётчика (`stale`).
- Стандартные периоды: `today`, `yesterday`, `this_month`, `last_month`,
  `last_24h`, `last_7d`, `last_30d`, плюс произвольный по `from`/`to`.
  Все периоды считаются по локальной timezone.
- HTTP endpoint'ы:
  - `GET /api/meters/<id>/consumption?period=...|from=...&to=...`
  - `GET /api/meters/<id>/history-info`
  - `GET /api/summary/consumption?period=...`
- CLI-команды:
  - `consumption <device_id> [--period|--from/--to] [--json]`
  - `consumption-summary [--period|--from/--to]`
  - `history-info [device_id]`
  - `history-show <device_id> <channel> [--period|--from/--to] [--limit] [--all]`

### Изменено

- Версия пакета: `0.2.0` → `0.3.0`.
- `ApiServer` принимает опциональные `wb_db_client` и
  `consumption_service`; если не переданы — соответствующие эндпоинты
  возвращают `503`.

## [0.2.0] — 2026-04

### Добавлено

- Собственная SQLite-БД в `/mnt/data/var/lib/wb-energy-meter/state.db`.
- Движок миграций со схемой `schema_migrations`, миграция
  `001_initial_schema` (таблицы `meters`, `meter_groups`,
  `period_aggregates`, `alert_events`, `snoozes`, `kv`).
- Репозитории `MeterRepo`, `GroupRepo`, `KvRepo` с валидацией имён
  (макс. 200 символов, без управляющих символов) и device_id
  (`[A-Za-z0-9._-]+`).
- CLI-утилита `wb-energy-meter-cli` с командами:
  - `meter list / add / rename / group / role / enable / disable / remove / show`
  - `group list / remove`
  - `scan [--add-all]` — поиск счётчиков в MQTT
  - `db status / vacuum`
  - `config show / validate`
- Автоматический одноразовый импорт реестра из YAML в БД при первом
  запуске обновлённого демона.
- Фоновая синхронизация серийных номеров из MQTT в БД (раз в минуту).
- Бэкап БД перед миграцией, ротация до 5 последних бэкапов.

### Изменено

- Реестр счётчиков теперь хранится в БД, а не в YAML. YAML-секция
  `meters:` используется как seed при первой установке.
- `install.sh`: создаёт data-директорию, ставит CLI-launcher в
  `/usr/bin/wb-energy-meter-cli`.

## [0.1.0] — 2026-04

### Добавлено

- MQTT-клиент с подпиской на `/devices/+/...` и парсером WB
  Conventions (включая `meta/title` с локализацией, `meta/precision`,
  типы каналов: voltage / current / power / power_consumption / text).
- 55 каналов WB-MAP3E корректно распознаются.
- In-memory реестр счётчиков (`MeterRegistry`, `MeterState`,
  `ControlState`).
- Движок статусов: `OK / NO_CONNECTION / NO_MEASUREMENT /
  INCOMPLETE / WARNING / DEVICE_ERROR / UNKNOWN`. Различает
  «связь есть, но нагрузки нет» от настоящего обрыва.
- HTTP API (stdlib `http.server`):
  - `GET /health`
  - `GET /api/status` — сводка
  - `GET /api/meters` — список
  - `GET /api/meters/<device_id>` — детали
- YAML-конфиг `/etc/wb-energy-meter.conf` со встроенной валидацией.
- Установщик `scripts/install.sh` (Linux) и
  `Install-WbEnergyMeter.cmd/ps1` (Windows).
- systemd-юнит с автозапуском и автоперезапуском.

[Unreleased]: https://github.com/YOURUSER/wb-energy-meter/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/YOURUSER/wb-energy-meter/releases/tag/v0.3.0
[0.2.0]: https://github.com/YOURUSER/wb-energy-meter/releases/tag/v0.2.0
[0.1.0]: https://github.com/YOURUSER/wb-energy-meter/releases/tag/v0.1.0
