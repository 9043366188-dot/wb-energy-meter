# wb-energy-meter

Сервис учёта электроэнергии для контроллеров Wiren Board со счётчиками
WB-MAP3E (и совместимыми). Подключается к штатным сервисам контроллера
(`mosquitto`, `wb-mqtt-db`), не дублирует телеметрию, не требует
дополнительного железа.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-0.8.0-blue)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-Wiren%20Board%20%7C%20Linux-lightgrey)

---

## Содержание

- [Зачем](#зачем)
- [Возможности](#возможности)
- [Требования](#требования)
- [Установка](#установка)
- [Использование](#использование)
- [Веб-интерфейс](#веб-интерфейс)
- [HTTP API](#http-api)
- [CLI](#cli)
- [Архитектура](#архитектура)
- [Конфигурация](#конфигурация)
- [Разработка](#разработка)
- [Дорожная карта](#дорожная-карта)
- [Лицензия](#лицензия)

---

## Зачем

Wiren Board даёт «сырые» данные счётчиков через MQTT — мгновенные значения
напряжений, токов, мощности, накопительной энергии. Хочется поверх этого
получить **систему учёта**: понятный реестр, расход за периоды, статусы,
группировку по щиткам, отчёты. И всё это — на самом контроллере, без
внешних серверов и облаков.

Этот проект — такой слой поверх штатного Wiren Board.

---

## Возможности

### Мониторинг и статусы

- Автоматическая сборка состояния счётчиков из MQTT (WB Conventions).
- Движок статусов с двумя уровнями: **«нет связи»** (нет любых сообщений)
  и **«нет нагрузки»** (счётчик отвечает, но U/I = 0). Счётчики
  WB-MAP3E fw2 работают в event-driven режиме — Uptime-канал используется
  как «пульс» для подтверждения живости без нагрузки.
- История переходов статусов: точное время начала и конца каждого
  инцидента недоступности.

### Учёт расхода

- Расчёт расхода за любой период через MQTT-RPC к `wb-mqtt-db` (история
  не дублируется в нашу БД).
- Почасовые агрегаты (`period_aggregates`) — быстрый расчёт за месяц
  без тяжёлых RPC-запросов.
- Качественные флаги: `ok / edge_approx / gap / reset / no_data`.

### Управление

- Реестр счётчиков в SQLite — имена, группы (зоны), роли (ввод /
  потребитель / прочее), комментарии.
- **Зоны** — группировка счётчиков по щитам или помещениям.
- **Роли** — разделение вводных и потребительских счётчиков для
  расчёта баланса.
- CLI и **веб-интерфейс** для управления без ручного редактирования
  конфигов.

### Веб-интерфейс

Доступен по адресу `http://<IP>:8080/`. Четыре раздела:

**Дашборд** — сводные плитки (счётчики, мощность, энергия, MQTT),
карточки счётчиков с цветовым статусом, мощностью, энергией,
напряжениями по фазам, аптаймом и временем последнего обновления.
Карточки сгруппированы по зонам с цветными маркерами.
Автообновление раз в 5 секунд.

**Расход** — выбор периода (пресеты + произвольные даты), таблица
расхода, почасовой SVG-график.

**Настройки** — управление счётчиками (добавить/переименовать/удалить/
назначить роль/зону/комментарий) и зонами без CLI и SSH. Зона
назначается выбором из списка (не свободным текстом), доступно массовое
назначение зоны сразу нескольким счётчикам чекбоксами. У каждой зоны —
свой цвет, который можно сменить.

**Отчёты** — пять видов:

| Отчёт | Что показывает |
|---|---|
| Ведомость расхода | Расход по всем счётчикам за период с группировкой по зонам |
| Профиль нагрузки | Почасовой график выбранного счётчика с пиком |
| Сравнение периодов | Два периода рядом: Δ кВт·ч и Δ % для каждого счётчика |
| Доступность | % времени на связи, список инцидентов с датами |
| Баланс | Ввод минус потребители = небаланс в кВт·ч и % |

Все отчёты выгружаются в CSV (открывается в Excel, BOM для кириллицы).

**Карточка счётчика** — параметры переведены на русский, разбиты по
категориям (Основные / Напряжение / Ток / Мощность / Энергия /
Качество сети / Служебные / Прочее), при наведении — подсказка. Клик по
числовому параметру открывает график истории значений с выбором
периода (Час / Сутки / Неделя / Месяц) и выгрузкой CSV.

### API и интеграции

- REST API на Flask — все данные доступны программно.
- CLI-утилита `wb-energy-meter-cli`.
- systemd-сервис с автозапуском и автоперезапуском.

---

## Требования

- Wiren Board 8.x (Debian Bullseye/Bookworm, aarch64/armv7).
- Python 3.9+ (на WB уже есть).
- `python3-flask`, `python3-paho-mqtt`, `python3-yaml` — из apt,
  устанавливаются автоматически.
- Запущенные `mosquitto` и `wb-mqtt-db` (штатные сервисы WB).
- ~10 МБ свободного места на eMMC.

---

## Установка

### Способ 1. Windows (рекомендуется)

1. Скачайте `Install-WbEnergyMeter.cmd` и `Install-WbEnergyMeter.ps1`
   из репозитория, положите в одну папку.
2. Двойной клик по `Install-WbEnergyMeter.cmd`.
3. Введите IP-адрес Wiren Board (например, `192.168.0.101`).

Установщик:
- Создаёт SSH-ключ на вашем компьютере (один раз).
- Загружает ключ на WB (один раз, с паролем по умолчанию `wirenboard`).
- Скачивает код с GitHub прямо на WB и запускает установщик.
- После первой установки — пароль WB можно сменить: ключ продолжит
  работать.

### Способ 2. Напрямую на WB (по SSH)

```bash
curl -fsSL https://raw.githubusercontent.com/9043366188-dot/wb-energy-meter/main/scripts/install-from-github.sh | bash
```

### Обновление

Повторите установку — миграции БД применятся автоматически, бэкап
`state.db` будет сделан автоматически.

### Проверка

```bash
systemctl status wb-energy-meter
curl -s http://127.0.0.1:8080/api/status | python3 -m json.tool
```

---

## Использование

### Добавить счётчики

Проще всего — через веб-интерфейс: **Настройки → Сканировать**.
Или через CLI:

```bash
wb-energy-meter-cli scan              # посмотреть что найдено в MQTT
wb-energy-meter-cli scan --add-all    # добавить всё в реестр
```

### Назначить роли и зоны

```bash
wb-energy-meter-cli meter rename wb-map3e_16 --name "Ввод 1"
wb-energy-meter-cli meter group  wb-map3e_16 --group "Главный щит"
wb-energy-meter-cli meter role   wb-map3e_16 --role input
```

Или через веб: **Настройки → ✏ Изменить**.

### Посмотреть расход

```bash
wb-energy-meter-cli consumption wb-map3e_16 --period today
wb-energy-meter-cli consumption wb-map3e_16 --period last_month
wb-energy-meter-cli consumption-summary --period this_month
```

### Почасовые агрегаты

```bash
wb-energy-meter-cli aggregates status
wb-energy-meter-cli aggregates show wb-map3e_16 --period last_7d
wb-energy-meter-cli aggregates catchup --days 90   # пересчитать историю
```

---

## Веб-интерфейс

Откройте в браузере: **http://\<IP WB\>:8080/**

> Требует доступ к CDN `cdn.jsdelivr.net` (Alpine.js). Если браузер
> без интернета — API и CLI работают независимо.

### Первый запуск

1. **Настройки → Сканировать** — находит счётчики в MQTT.
2. Нажмите **+ Добавить** рядом с найденным счётчиком, введите имя.
3. На вкладке «Настройки → Зоны» создайте зоны (щиты, помещения).
4. В настройках счётчика назначьте зону и роль (Ввод / Потребитель).
5. Перейдите на Дашборд — карточки сгруппированы по зонам.
6. Отчёты → Баланс покажет небаланс как только наберутся данные.

---

## HTTP API

Сервис слушает на `0.0.0.0:8080`. Все ответы в JSON (UTF-8). CORS открыт.
Документация: `http://<IP>:8080/api/docs`.

| Метод | URL | Описание |
|---|---|---|
| GET | `/health` | Проверка живости |
| GET | `/api/status` | Версия, MQTT, список счётчиков со статусами |
| GET | `/api/meters` | Список зарегистрированных счётчиков |
| GET | `/api/meters/unregistered` | Счётчики в MQTT, не в реестре |
| GET | `/api/meters/<id>` | Детали: все каналы, статус, значения |
| GET | `/api/meters/<id>/consumption?period=today` | Расход за период |
| GET | `/api/meters/<id>/hourly?period=last_7d` | Почасовые агрегаты |
| GET | `/api/meters/<id>/history-info` | Каналы в `wb-mqtt-db` |
| GET | `/api/meters/<id>/channel-history?control=...&period=...` | История значений одного параметра (для графика) |
| GET | `/api/meters/<id>/availability?period=last_30d` | Доступность |
| GET | `/api/summary/consumption?period=this_month` | Расход по всем |
| GET | `/api/availability/summary?period=last_30d` | Доступность по всем |
| GET | `/api/reports/balance?period=this_month` | Баланс (ввод − потребители) |
| GET | `/api/aggregates/status` | Статистика агрегатов |
| GET | `/api/channels/dictionary` | Словарь каналов: русские названия, единицы, подсказки, категории |
| POST | `/api/registry/meters` | Добавить счётчик |
| PATCH | `/api/registry/meters/<id>` | Переименовать / сменить зону (`group:""` или `null` — снять) / комментарий |
| PATCH | `/api/registry/meters/<id>/role` | Изменить роль |
| DELETE | `/api/registry/meters/<id>` | Удалить счётчик |
| GET | `/api/registry/groups` | Список зон (с цветом) |
| POST | `/api/registry/groups` | Создать зону (`name`, необязательно `color`) |
| PATCH | `/api/registry/groups/<id>` | Переименовать и/или сменить цвет зоны. Конфликт имени (без `merge:true`) → 409 |
| DELETE | `/api/registry/groups/<id>` | Удалить зону |

**Периоды:** `today`, `yesterday`, `this_month`, `last_month`,
`last_24h`, `last_7d`, `last_30d` или `?from=YYYY-MM-DD&to=YYYY-MM-DD`.
Для графика истории параметра на фронте дополнительно используется
клиентский пресет «Час» — как произвольный диапазон `from`/`to` за
последний час (в списке пресетов API его нет, чтобы не расширять
`periods.py`).

---

## CLI

```
wb-energy-meter-cli <команда> [опции]

meter list                             список счётчиков в реестре
meter add <device_id> --name <имя>     добавить
meter show <device_id>                 детали
meter rename <device_id> --name <имя>  переименовать
meter group <device_id> --group <зона> назначить зону
meter role <device_id> --role <роль>   роль: input / consumer / other
meter remove <device_id>               удалить
scan [--add-all]                       найти новые в MQTT
consumption <device_id> --period ...   расход
consumption-summary --period ...       расход по всем
history-info <device_id>               каналы в wb-mqtt-db
history-show <device_id> <канал>       точки истории
aggregates status                      статистика агрегатов
aggregates show <device_id>            почасовые данные
aggregates catchup [--days N]          догнать пропущенные часы
db status                              состояние БД
```

---

## Архитектура

```
WB-MAP3E (Modbus TCP/RS-485)
       ↓ opрос
  wb-mqtt-serial
       ↓ publish
   mosquitto (MQTT)
       ↓ subscribe
  wb-energy-meter
  ├── mqtt_client.py   — парсер WB Conventions, реестр в памяти
  ├── status.py        — классификация статусов, запись переходов
  ├── alert_repo.py    — история инцидентов (alert_events)
  ├── aggregator.py    — почасовые агрегаты + catch-up
  ├── consumption.py   — расчёт расхода (агрегаты + RPC хвосты)
  ├── api.py           — HTTP API (Flask)
  ├── static/          — веб-интерфейс (Alpine.js)
  └── db.py            — SQLite, миграции

  ↕ MQTT-RPC
  wb-mqtt-db           — история каналов (штатная)
```

**Принципы:**
- Не дублировать телеметрию — история живёт в `wb-mqtt-db`.
- Не писать в чужие БД — только через публичный MQTT-RPC.
- Не блокировать MQTT-колбэки — тяжёлая работа в фоновые потоки.
- Миграции только вперёд — никогда не правим старые SQL-файлы.

---

## Конфигурация

Файл: `/etc/wb-energy-meter.conf`. Пример — в
`scripts/wb-energy-meter.conf.example`.

```yaml
mqtt:
  host: 127.0.0.1
  port: 1883

http:
  host: 0.0.0.0
  port: 8080

device_prefix: "wb-map3e_"

status:
  # Для WB-MAP3E fw2 (event-driven) с каналом Uptime рекомендуется 600 с
  no_connection_timeout_s: 600
  undervoltage_v: 198.0
  overvoltage_v: 253.0
  phase_lost_v: 150.0
  freq_min_hz: 49.0
  freq_max_hz: 51.0

aggregator:
  enabled: true
  catchup_days: 90
  max_catchup_duration_s: 300

log_file: /var/log/wb-energy-meter/wb-energy-meter.log
```

> **Про Uptime.** WB-MAP3E fw2 работает в event-driven режиме — данные
> приходят только при изменении значений. Без нагрузки (U/I = 0) счётчик
> молчит часами. Включите канал **Uptime** в Device Manager WB (HW Info
> → Uptime → in queue order) — он растёт каждые 2 секунды и служит
> «пульсом» для подтверждения живости. После этого сервис корректно
> различает «нет связи» и «нет нагрузки».

---

## Разработка

### Локальный запуск

```bash
git clone https://github.com/9043366188-dot/wb-energy-meter.git
cd wb-energy-meter
pip install paho-mqtt pyyaml flask

# В отдельном терминале:
mosquitto -p 1883 -v

# Демон:
python -m wb_energy_meter.main \
  --config scripts/wb-energy-meter.conf.example \
  --db-path /tmp/state.db
```

### Тесты

```bash
python tests/test_step3_consumption.py   # юнит
python tests/test_step3_e2e.py           # e2e (нужен mosquitto)
python tests/test_step4_aggregator.py    # юнит агрегаторов
python tests/test_step5_flask_api.py     # юнит Flask API
python tests/test_step6_webui.py         # юнит веб-UI + баланс HTML-тегов
python tests/test_step8_groups.py        # юнит групп/зон (A1-A5)
python tests/test_step8_channels.py      # юнит словаря каналов
```

CI запускается на GitHub Actions (Python 3.9–3.12).

### Структура репозитория

```
wb-energy-meter/
├── wb_energy_meter/
│   ├── __init__.py            # версия
│   ├── alert_repo.py          # история инцидентов
│   ├── aggregates_repo.py     # CRUD period_aggregates
│   ├── aggregator.py          # почасовые агрегаты
│   ├── api.py                 # HTTP API (Flask)
│   ├── background.py          # фоновые задачи
│   ├── channels.py            # словарь каналов: label/units/hint/category
│   ├── cli.py                 # CLI
│   ├── config.py              # YAML-конфиг
│   ├── consumption.py         # расчёт расхода
│   ├── db.py                  # SQLite + миграции
│   ├── logger.py              # логирование
│   ├── main.py                # точка входа
│   ├── migrations/            # .sql миграции
│   │   ├── 001_initial_schema.sql
│   │   ├── 002_aggregator_indexes.sql
│   │   └── 003_group_name_normalized.sql
│   ├── model.py               # доменные модели
│   ├── mqtt_client.py         # MQTT + WB Conventions
│   ├── periods.py             # стандартные периоды
│   ├── repo.py                # CRUD БД
│   ├── static/
│   │   └── index.html         # SPA (Alpine.js)
│   ├── status.py              # классификатор статусов
│   └── wb_db_client.py        # MQTT-RPC к wb-mqtt-db
├── scripts/
│   ├── install.sh
│   ├── install-from-github.sh
│   ├── uninstall.sh
│   └── wb-energy-meter.conf.example
├── tests/
├── .github/workflows/ci.yml
├── Install-WbEnergyMeter.cmd     # Windows-установщик
├── Install-WbEnergyMeter.ps1
├── pyproject.toml
├── README.md
├── CHANGELOG.md
└── LICENSE
```

---

## Дорожная карта

| Шаг | Версия | Что добавляется | Статус |
|---:|:---:|---|:---:|
| 1 | 0.1.0 | MQTT, статусы, HTTP API stub | ✅ |
| 2 | 0.2.0 | SQLite, миграции, CLI, репозитории | ✅ |
| 3 | 0.3.0 | MQTT-RPC к wb-mqtt-db, расчёт расхода | ✅ |
| 4 | 0.4.0 | Воркер почасовых агрегатов + catch-up | ✅ |
| 5 | 0.5.0 | Переход на Flask | ✅ |
| 6 | 0.6.0 | Веб-интерфейс (Alpine.js SPA) | ✅ |
| 7 | 0.7.0 | Настройки в UI, зоны, роли, отчёты, история доступности | ✅ |
| 8 | 0.8.0 | Починка групп, график истории параметра, русификация карточки счётчика | ✅ |
| 9 | 0.9.0 | Двух-тарифный учёт (день/ночь) | ⏳ |
| 10 | 0.10.0 | Алерты + SMTP уведомления | ⏳ |
| 11 | 0.11.0 | Excel-отчёты + автоматическая рассылка | ⏳ |
| 12 | 1.0.0 | .deb-пакет, финальная документация | ⏳ |

---

## Лицензия

[MIT](LICENSE) — используйте свободно.
