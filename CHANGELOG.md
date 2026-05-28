# Changelog

Все значимые изменения проекта описаны в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/),
проект придерживается [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Запланировано

- Шаг 8: двух-тарифный учёт (день/ночь) на основе часовых агрегатов.
- Шаг 9: алерты + SMTP уведомления.

## [0.7.0] — 2026-05-28

### Добавлено

- **Настройки в веб-интерфейсе** (вкладка «Настройки») — управление
  счётчиками и зонами без CLI и SSH:
  - Сканирование MQTT: находит новые устройства и предлагает добавить.
  - Добавление, переименование, удаление счётчиков.
  - Inline-редактирование имени, зоны, комментария, роли прямо в таблице.
  - Управление зонами: создать, переименовать, удалить.
  - Autocomplete по существующим зонам при вводе.

- **Зоны** — группировка счётчиков по щитам/помещениям:
  - На дашборде карточки сгруппированы по зонам с цветными маркерами
    и сводкой мощности по зоне.
  - Цвет зоны детерминирован из её названия (всегда одинаковый).

- **Роли** — разделение вводных и потребительских счётчиков:
  - Три роли: `input` (Ввод), `consumer` (Потребитель), `other` (Прочее).
  - Бейдж **ВВОД** на карточке дашборда для вводных счётчиков.
  - Используются в отчёте «Баланс».

- **Комментарии** (`notes`) к счётчикам — отображаются на карточке
  (курсивом под зоной) и в таблице настроек.

- **История статусов** (`alert_repo.py`) — автоматическая запись переходов:
  - При смене статуса на `no_connection`/`device_error` — открывается
    запись с `started_at`.
  - При восстановлении — запись закрывается с `ended_at`.
  - Из пар started_at/ended_at вычисляются точные интервалы недоступности.

- **Пять отчётов** (вкладка «Отчёты»):
  1. **Ведомость расхода** — таблица по всем счётчикам за период,
     сгруппированная по зонам, итог по каждой зоне и общий итог.
  2. **Профиль нагрузки** — почасовой SVG-график выбранного счётчика,
     метрики (итого, пик, среднее, часов с данными), таблица по часам.
  3. **Сравнение периодов** — два периода рядом, Δ кВт·ч и Δ % с
     цветовой индикацией роста/снижения.
  4. **Доступность** — % времени на связи, суммарное время недоступности,
     число инцидентов; клик раскрывает список с датами от/до каждого.
  5. **Баланс** — ввод минус потребители = небаланс в кВт·ч и %. Три
     сводных карточки + детализация по каждой группе. Если вводных
     счётчиков нет — подсказка как их назначить.
  - Все отчёты выгружаются в **CSV** (UTF-8 BOM, открывается в Excel).

- **Аптайм счётчика** на карточке дашборда — если канал Uptime (s)
  включён в Device Manager WB.

- **Цветное время последнего обновления** на карточке: 🟢 < 2 мин,
  🟡 < 10 мин, 🔴 > 10 мин.

### Изменено

- Дашборд: карточки сгруппированы по зонам вместо плоского списка.
- `status.py`: два раздельных таймаута — `no_connection` (нет любых
  сообщений) и `no_measurement` (связь есть, но U/I = 0). Статус
  `no_measurement` теперь корректно отображается как «Нет нагрузки».
- `model.py`: добавлен `last_measurement_ts`, `MEASUREMENT_CHANNELS`,
  канал `Uptime` в `MAIN_CHANNELS`.
- `mqtt_client.py`: обновляет `last_measurement_ts` только для
  измерительных каналов (U/I/P/F) выше порога.
- `config.py`: дефолт `no_connection_timeout_s` поднят 300 → 600 с.
- `main.py`: `groups_repo` и `alert_repo` передаются в `ApiServer`
  и `StatusEngine`.

### Новые API-эндпоинты

```
GET  /api/meters/<id>/availability?period=...
GET  /api/availability/summary?period=...
GET  /api/reports/balance?period=...
GET  /api/meters/unregistered
GET  /api/registry/meters
GET  /api/registry/meters/<id>
POST /api/registry/meters
PATCH /api/registry/meters/<id>
PATCH /api/registry/meters/<id>/role
DELETE /api/registry/meters/<id>
GET  /api/registry/groups
POST /api/registry/groups
PATCH /api/registry/groups/<id>
DELETE /api/registry/groups/<id>
```

## [0.6.0] — 2026-05-22

### Добавлено

- Веб-интерфейс (SPA на Alpine.js): дашборд, расход, графики, детали.
- Переключатель светлой/тёмной темы.

## [0.5.0] — 2026-05-21

### Изменено

- HTTP API переведён с `http.server` на Flask (`python3-flask` из apt).
- Страница `/api/docs` с описанием эндпоинтов.

## [0.4.0] — 2026-05-18

### Добавлено

- Воркер почасовых агрегатов с catch-up и patcher.
- Гибридный расчёт расхода (агрегаты + RPC хвосты).
- CLI: `aggregates status/show/recompute/catchup`.
- API: `/api/meters/<id>/hourly`, `/api/aggregates/status`.

## [0.3.0] — 2026-05-12

### Добавлено

- MQTT-RPC к `wb-mqtt-db`, расчёт расхода за периоды.
- Флаги качества данных.
- CLI: `consumption`, `consumption-summary`, `history-info`, `history-show`.

## [0.2.0] — 2026-05-05

### Добавлено

- SQLite-хранилище с миграциями.
- Репозитории (MeterRepo, GroupRepo, KvRepo).
- CLI: `meter`, `group`, `scan`, `db`.

## [0.1.0] — 2026-04-20

### Добавлено

- MQTT-клиент с парсером WB Conventions.
- Движок статусов.
- HTTP API stub.
- Реестр счётчиков из YAML.
- systemd-юнит.

[Unreleased]: https://github.com/9043366188-dot/wb-energy-meter/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/9043366188-dot/wb-energy-meter/releases/tag/v0.7.0
[0.6.0]: https://github.com/9043366188-dot/wb-energy-meter/releases/tag/v0.6.0
[0.5.0]: https://github.com/9043366188-dot/wb-energy-meter/releases/tag/v0.5.0
[0.4.0]: https://github.com/9043366188-dot/wb-energy-meter/releases/tag/v0.4.0
[0.3.0]: https://github.com/9043366188-dot/wb-energy-meter/releases/tag/v0.3.0
[0.2.0]: https://github.com/9043366188-dot/wb-energy-meter/releases/tag/v0.2.0
[0.1.0]: https://github.com/9043366188-dot/wb-energy-meter/releases/tag/v0.1.0
