# wb-energy-meter

Сервис учёта электроэнергии для контроллеров Wiren Board со счётчиками
WB-MAP3E (и совместимыми). Подключается к штатным сервисам контроллера
(`mosquitto`, `wb-mqtt-db`), не дублирует телеметрию и не требует
дополнительного железа.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-0.3.0-blue)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-Wiren%20Board%20%7C%20Linux-lightgrey)

> **Статус проекта.** Активная разработка. Версия 0.3.0 решает базовые
> задачи (реестр счётчиков, статус, расчёт расхода через wb-mqtt-db RPC).
> Веб-интерфейс, двух-тарифный учёт, алерты и Excel-отчёты — в
> [дорожной карте](#дорожная-карта).

---

## Содержание

- [Зачем](#зачем)
- [Возможности](#возможности)
- [Требования](#требования)
- [Установка](#установка)
- [Использование](#использование)
- [HTTP API](#http-api)
- [CLI](#cli)
- [Архитектура](#архитектура)
- [Конфигурация](#конфигурация)
- [Разработка](#разработка)
- [FAQ](#faq)
- [Дорожная карта](#дорожная-карта)
- [Лицензия](#лицензия)

---

## Зачем

Wiren Board даёт «сырые» данные счётчиков через MQTT — мгновенные значения
напряжений, токов, мощности, накопительной активной энергии. Хочется поверх
этого получить **систему учёта**: с понятным реестром счётчиков, расчётом
расхода за периоды (день / месяц / год), статусом «работает или сломан»,
группировкой по щиткам или зонам. И всё это — на самом контроллере, без
внешних серверов и облаков.

Этот проект — такой слой поверх штатного Wiren Board.

## Возможности

**Уже работает (версия 0.3.0):**

- Автоматическая сборка состояния всех счётчиков из MQTT (`/devices/+/...`),
  с поддержкой WB Conventions (метаданные канала: тип, единицы, точность).
- Реестр счётчиков с именами, группами, ролями (ввод/потребитель), в
  собственной SQLite (`/mnt/data/var/lib/wb-energy-meter/state.db`).
- Движок статусов: `ok / no_connection / no_measurement / incomplete /
  warning / device_error / unknown`. Различает «нет связи» и «связь есть,
  но нет нагрузки».
- Авто-определение серийного номера из MQTT и синхронизация в БД.
- Расчёт расхода за период через MQTT-RPC к `wb-mqtt-db` (без дублирования
  истории!). Флаги качества: `ok / edge_approx / gap / reset / no_data /
  stale`.
- HTTP API (`/api/status`, `/api/meters`, `/api/meters/<id>/consumption`,
  `/api/summary/consumption`, `/api/meters/<id>/history-info`).
- CLI с командами `meter list/add/show/...`, `scan`, `consumption`,
  `consumption-summary`, `history-info`, `history-show`, `db status`.
- Миграции БД с автоматическим бэкапом перед апгрейдом.
- systemd-юнит с автозапуском и автоперезапуском.

**В работе / планируется:** см. [дорожную карту](#дорожная-карта).

## Требования

- Wiren Board 8.x с Debian Bullseye/Bookworm (или любой Linux ARM/x86 с
  работающим `mosquitto`).
- Python 3.9 или новее (на Wiren Board 8.x по умолчанию `python3.11`).
- Запущенные `mosquitto` и `wb-mqtt-db` (штатные сервисы WB).
- ~5 МБ свободного места на eMMC.

## Установка

### Способ 1. Из архива (на контроллере, по SSH)

```bash
# На вашем компьютере:
git clone https://github.com/YOURUSER/wb-energy-meter.git
cd wb-energy-meter
tar -czf /tmp/wb-energy-meter.tar.gz \
  --exclude='__pycache__' --exclude='.git' --exclude='tests' \
  --exclude='.github' --exclude='*.tar.gz' \
  -C .. wb-energy-meter

# Загрузка на контроллер:
scp /tmp/wb-energy-meter.tar.gz root@<IP_WB>:/tmp/
ssh root@<IP_WB>

# На контроллере:
cd /tmp
tar -xzf wb-energy-meter.tar.gz
cd wb-energy-meter
bash scripts/install.sh
```

### Способ 2. С Windows

В папке с архивом запустите `Install-WbEnergyMeter.cmd` — он сам всё
скопирует и установит (см. документацию по установщику в проекте).

### Проверка после установки

```bash
systemctl status wb-energy-meter
curl -s http://127.0.0.1:8080/api/status | python3 -m json.tool
wb-energy-meter-cli meter list
```

### Обновление

Просто повторите установку поверх — миграции БД накатятся автоматически,
перед апгрейдом будет сделан бэкап `state.db.backup-<timestamp>`.

### Удаление

```bash
bash scripts/uninstall.sh
```

## Использование

### Добавить счётчики в реестр

Самый простой способ — авто-сканирование MQTT:

```bash
wb-energy-meter-cli scan              # посмотреть, что найдётся
wb-energy-meter-cli scan --add-all    # добавить всё новое в реестр
```

Дальше отредактируйте имена и группы:

```bash
wb-energy-meter-cli meter rename wb-map3e_17 --name "Ввод 1"
wb-energy-meter-cli meter group  wb-map3e_17 --group "Главный щит"
wb-energy-meter-cli meter role   wb-map3e_17 --role input
```

### Посмотреть расход

```bash
wb-energy-meter-cli consumption wb-map3e_17 --period today
wb-energy-meter-cli consumption wb-map3e_17 --period last_month
wb-energy-meter-cli consumption wb-map3e_17 --from "2026-04-01" --to "2026-05-01"

# Сводный отчёт по всем счётчикам:
wb-energy-meter-cli consumption-summary --period this_month
```

### Проверить, что в истории есть данные

```bash
wb-energy-meter-cli history-info wb-map3e_17
wb-energy-meter-cli history-show wb-map3e_17 "Total AP energy" --period last_7d
```

## HTTP API

Сервис слушает по умолчанию на `0.0.0.0:8080`. Ответы в JSON, кодировка
UTF-8, CORS разрешён.

| Метод | URL | Описание |
|---|---|---|
| GET | `/health` | Проверка жизни сервиса |
| GET | `/api/status` | Сводка: версия, MQTT, реестр, статусы |
| GET | `/api/meters` | Полный реестр счётчиков |
| GET | `/api/meters/<device_id>` | Подробности по одному счётчику |
| GET | `/api/meters/<device_id>/consumption?period=today` | Расход |
| GET | `/api/meters/<device_id>/history-info` | Какие каналы в `wb-mqtt-db` |
| GET | `/api/summary/consumption?period=this_month` | Расход по всем |

Поддерживаемые периоды: `today`, `yesterday`, `this_month`, `last_month`,
`last_24h`, `last_7d`, `last_30d`, или произвольный через `?from=YYYY-MM-DD&to=YYYY-MM-DD`.

Пример ответа `/api/meters/wb-map3e_16/consumption?period=today`:

```json
{
  "device_id": "wb-map3e_16",
  "display_name": "Тестовый счётчик 1",
  "group": "Тестовая зона",
  "period": {
    "label": "today",
    "description": "Сегодня (с 00:00)",
    "from": "2026-05-04 00:00:00",
    "to":   "2026-05-04 14:30:15",
    "duration_s": 52215
  },
  "consumption_kwh": 12.345,
  "ap_energy_start": 0.29159,
  "ap_energy_end":   12.63659,
  "samples_in_period": 142,
  "quality": "ok",
  "warnings": []
}
```

## CLI

Полная справка: `wb-energy-meter-cli --help` и `wb-energy-meter-cli <команда> --help`.

```
meter list / add / rename / group / role / enable / disable / remove / show
group list / remove
scan [--duration N] [--add-all]
db   status / vacuum
config show / validate

consumption          <device_id> [--period ... | --from ... --to ...] [--json]
consumption-summary             [--period ... | --from ... --to ...]
history-info         [<device_id>]
history-show         <device_id> <channel> [--period ...] [--limit N] [--all]
```

## Архитектура

```
                    ┌─────────────────────┐
   Modbus  ────────►│  mosquitto (1883)   │◄────── wb-mqtt-db
   counters         │  /devices/+/...     │        (history sqlite)
                    │  /rpc/v1/...        │
                    └─────────┬───────────┘
                              │
                              │ MQTT + MQTT-RPC
                              ▼
                    ┌─────────────────────┐
                    │  wb-energy-meter    │
                    │  ───────────────    │
                    │  • MQTT client      │  ← парсит WB Conventions
                    │  • Status engine    │  ← классификатор
                    │  • SQLite registry  │  ← /mnt/data/var/lib/...
                    │  • RPC client       │  ← к wb-mqtt-db
                    │  • Consumption svc  │  ← дельты Total AP energy
                    │  • HTTP API (8080)  │
                    └─────────────────────┘
```

**Ключевой принцип:** проект **не дублирует** телеметрию. Сырые значения
живут в `wb-mqtt-db` (штатном сервисе Wiren Board), наша БД хранит только
реестр и (с Шага 4) предрассчитанные агрегаты — то, что дёшево пересчитать
из исходника.

Размер собственной БД на 10 счётчиков за год: ~5 МБ.

## Конфигурация

Конфиг в `/etc/wb-energy-meter.conf` (YAML). Пример — в
`scripts/wb-energy-meter.conf.example`. Основные параметры:

```yaml
mqtt:
  host: 127.0.0.1
  port: 1883
http:
  host: 0.0.0.0
  port: 8080
device_prefix: "wb-map3e_"     # только эти устройства попадают в реестр
status:
  no_connection_timeout_s: 300
  undervoltage_v: 198
  overvoltage_v: 253
  ...
log_file: /var/log/wb-energy-meter/wb-energy-meter.log
```

Начиная с версии 0.2.0, реестр счётчиков хранится в **БД**, а не в YAML.
YAML-секция `meters:` используется только при первом запуске для импорта.
После — управление реестром через `wb-energy-meter-cli meter ...`.

## Разработка

### Локальный запуск

Можно запустить демон против локального брокера mosquitto и пощупать всё на
своём компьютере, без WB:

```bash
git clone https://github.com/YOURUSER/wb-energy-meter.git
cd wb-energy-meter
pip install paho-mqtt pyyaml

# Свой mosquitto в другом терминале:
mosquitto -p 1883 -v

# Демон:
python -m wb_energy_meter.main \
  --config scripts/wb-energy-meter.conf.example \
  --db-path /tmp/state.db
```

### Запуск тестов

```bash
# Юнит-тесты (без MQTT):
python tests/test_step3_consumption.py

# E2E-тесты (нужен запущенный mosquitto):
python tests/test_step3_e2e.py
python tests/test_step3_daemon.py
```

### Структура репозитория

```
wb-energy-meter/
├── wb_energy_meter/           # пакет Python
│   ├── __init__.py
│   ├── api.py                 # HTTP API (http.server, до Шага 5)
│   ├── background.py          # фоновые задачи
│   ├── cli.py                 # CLI-утилита
│   ├── config.py              # загрузка YAML
│   ├── consumption.py         # расчёт расхода
│   ├── db.py                  # SQLite + миграции
│   ├── logger.py              # настройка логов
│   ├── main.py                # точка входа демона
│   ├── migrations/            # .sql миграции схемы
│   ├── model.py               # доменные модели
│   ├── mqtt_client.py         # MQTT-клиент + парсер WB Conventions
│   ├── periods.py             # стандартные периоды (today, ...)
│   ├── repo.py                # CRUD над БД
│   ├── status.py              # классификатор статусов
│   └── wb_db_client.py        # клиент MQTT-RPC к wb-mqtt-db
├── scripts/
│   ├── install.sh             # установщик для Linux/WB
│   ├── uninstall.sh
│   └── wb-energy-meter.conf.example
├── tests/                     # тесты
├── .github/
│   └── workflows/ci.yml       # GitHub Actions
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── CONTRIBUTING.md
└── LICENSE
```

Подробности — в [CONTRIBUTING.md](CONTRIBUTING.md).

## FAQ

**Q: А почему не InfluxDB / Prometheus / Grafana?**

A: На контроллере и так уже работает `wb-mqtt-db`, который пишет историю в
SQLite. Ставить ещё одну time-series БД ради тех же данных — дублирование
с риском перегрузить eMMC. Текущий подход (читать историю через MQTT-RPC у
штатного сервиса) экономнее.

**Q: Сколько счётчиков потянет?**

A: Архитектура рассчитана на 10–50 счётчиков на одном контроллере. Не
проверено на больших инсталляциях; узким местом будет скорее `wb-mqtt-db`,
чем наш код.

**Q: А если контроллер перезагрузится?**

A: Сервис стартует автоматически (`systemctl enable wb-energy-meter`).
Реестр живёт в SQLite, не теряется. История значений — в `wb-mqtt-db`,
тоже не теряется. После перезагрузки расчёт расхода работает «как до
перезагрузки», точки за время простоя могут отсутствовать (это пометится
флагом качества `gap`).

**Q: Можно ли заменить счётчик и не сломать историю?**

A: По дизайну — да. Серийный номер хранится в реестре, при появлении
другого серийника в MQTT он будет автоматически обновлён. Подробная
обработка замены счётчика (с очисткой исторических данных по device_id)
запланирована на более поздние шаги.

**Q: Безопасность?**

A: На текущей стадии — никакой аутентификации в API. Предполагается, что
контроллер находится в доверенной локальной сети, или доступ ограничен на
сетевом уровне (VPN, firewall). Аутентификация и роли — в дорожной карте.

## Дорожная карта

| Шаг | Версия | Что добавляется | Статус |
|---:|:---:|---|:---:|
| 1 | 0.1.0 | MQTT, статусы, HTTP API stub, реестр в YAML | ✅ |
| 2 | 0.2.0 | SQLite, миграции, CLI, репозитории | ✅ |
| 3 | 0.3.0 | MQTT-RPC к wb-mqtt-db, расчёт расхода | ✅ |
| 4 | 0.4.0 | Воркер почасовых агрегатов + докатыватель | 🚧 |
| 5 | 0.5.0 | Переход на FastAPI | ⏳ |
| 6 | 0.6.0 | Веб-интерфейс (SPA на Alpine.js) | ⏳ |
| 7 | 0.7.0 | Двух-тарифный учёт | ⏳ |
| 8 | 0.8.0 | Алерты + SMTP + snooze | ⏳ |
| 9 | 0.9.0 | Excel-отчёты + автоматическая рассылка | ⏳ |
| 10 | 1.0.0 | .deb-пакет, документация, релиз | ⏳ |

## Лицензия

[MIT](LICENSE). Используйте, изменяйте, распространяйте — главное сохраните
файл лицензии.

---

Если проект полезен — звезда на GitHub помогает другим его найти. Баги и
предложения — в [Issues](https://github.com/YOURUSER/wb-energy-meter/issues).
