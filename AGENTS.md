# AGENTS.md — wb-energy-meter

Контекст проекта для агентов (Codex читает `AGENTS.md`, Claude Code — `CLAUDE.md`,
который является ссылкой на этот файл). Держите оба в синхроне: правьте только `AGENTS.md`.

## Что это

`wb-energy-meter` — сервис учёта электроэнергии для контроллеров **Wiren Board 8.x**
со счётчиками **WB-MAP3E**. Читает данные из MQTT-инфраструктуры WirenBoard,
складывает в SQLite, считает расход и агрегаты, отдаёт веб-интерфейс и REST API.

- Язык: Python (`>=3.9`), пакет `wb_energy_meter`
- Текущая версия: **0.8.0** (`wb_energy_meter/__init__.py` + `pyproject.toml`)
- Репозиторий: https://github.com/9043366188-dot/wb-energy-meter
- Целевое железо: Wiren Board 8.x, WB-MAP3E
- Тестовый контроллер: `192.168.10.212`

## Структура

```
wb_energy_meter/
  main.py            точка входа демона (console_script: wb-energy-meter)
  cli.py             CLI (console_script: wb-energy-meter-cli)
  api.py             Flask-приложение, все REST-эндпоинты
  mqtt_client.py     подписка на MQTT WirenBoard
  wb_db_client.py    доступ к БД WirenBoard
  db.py, repo.py     SQLite: подключение и репозиторий
  aggregates_repo.py, aggregator.py   почасовые/периодные агрегаты
  consumption.py, periods.py          расчёт расхода по периодам
  alert_repo.py, status.py            статусы и доступность счётчиков
  channels.py         словарь каналов: русские названия, единицы, подсказки, категории
  background.py      фоновые задачи (в т.ч. периодическая ресинхронизация зон)
  config.py          чтение /etc/wb-energy-meter.conf
  model.py, logger.py
  migrations/*.sql   схема БД (применяются по порядку)
  static/index.html  весь веб-интерфейс: один файл, Alpine.js с CDN
tests/               автономные скрипты, запускаются как `python tests/<file>.py`
scripts/             install.sh, install-from-github.sh, uninstall.sh, пример конфига
```

Веб-интерфейс — **один файл** `wb_energy_meter/static/index.html` на Alpine.js
(подключается с CDN, сборки нет). Вкладки переключаются через `tab=='...'`.

## Команды

```bash
pip install "paho-mqtt>=1.6,<3" "pyyaml>=5.4" "flask>=1.1"
python -m compileall -q wb_energy_meter      # синтаксическая проверка
python tests/test_step3_consumption.py       # тесты запускаются по одному
python tests/test_step4_aggregator.py
python tests/test_step5_flask_api.py
python tests/test_step6_webui.py             # включает проверку баланса HTML-тегов
python tests/test_step8_groups.py            # группы/зоны, регрессия A1-A5
python tests/test_step8_channels.py          # словарь каналов
```

Тесты — не pytest, а самостоятельные скрипты; часть e2e-тестов требует локального
mosquitto на `127.0.0.1:1883` с `allow_anonymous true`.

CI (`.github/workflows/ci.yml`) гоняет матрицу Python 3.9–3.12 на ubuntu-latest.

## Правила и грабли

- **paho-mqtt 2.x**: при QoS=1 `wait_for_publish()` виснет навсегда без `loop_start()`.
  В тестовых публикаторах всегда вызывайте `pub.loop_start()` сразу после `pub.connect()`.
- **CI и зависимости**: рантайм-зависимости (в частности `flask`) должны быть явно
  перечислены в шаге `pip install` в `ci.yml`, наличия в `pyproject.toml` недостаточно.
- **HTML `<template>`**: незакрытый `<template>` заставляет Chrome считать весь
  последующий HTML инертным фрагментом — скрипты не выполняются, Alpine не стартует,
  получается белый экран **без единой ошибки в консоли**. При правках `index.html`
  проверяйте баланс тегов (снять `<style>`/`<script>`, затем посчитать стек тегов).
  Автоматическая проверка — `tests/test_step6_webui.py::test_index_html_tag_balance`.
  **Важно:** наивный regex `<[^>]*>` для поиска тегов ломается на строках вида
  `:class="r.delta>0?'a':'b'"` — литеральные `<`/`>` внутри значений атрибутов
  нужно пропускать (учитывать открытые кавычки), иначе проверка даёт ложные
  срабатывания или пропускает реальные ошибки.
- **Группа счётчика живёт в двух местах**: в SQLite (`meters.group_id`) и в
  in-memory `MeterRegistry` (`MeterState.group`). Второе заполняется один раз
  при старте демона (`main.py`) — без явной синхронизации любое изменение
  группы через API не долетает до `/api/status` до перезапуска. Синхронизация
  сделана на двух уровнях: push в конце обработчиков `api.py`
  (`_sync_registry_groups()`) и периодический опрос в `background.py`
  (`BackgroundTasks._sync_groups()`). При добавлении новых мест, где меняется
  привязка счётчика к зоне, не забывайте вызвать синхронизацию.
- **`COLLATE NOCASE` и кириллица**: SQLite сворачивает регистр в `COLLATE NOCASE`
  только для ASCII A-Z — «Цех1» и «ЦЕХ1» считаются разными строками. Для
  регистронезависимой уникальности с кириллицей нужна казефолд-нормализация на
  стороне Python (`str.casefold()`), а не SQL-коллация. См. `repo.py::_norm_name`,
  колонку `meter_groups.name_norm` (миграция 003) и `db.py::_py_casefold`
  (SQL-функция `py_casefold()` для миграций).
- **Установщик под Windows** (`Install-WbEnergyMeter.ps1` / `.cmd`):
  - запускать **от обычного пользователя**, не от администратора;
  - ключи генерировать без парольной фразы: `ssh-keygen -t ed25519 -N ""`;
  - OpenSSH под Windows молча не подписывает ключом, у которого в ACL есть
    унаследованные права или доступ шире текущего пользователя — снимать наследование
    через `icacls` и давать полный доступ только текущему пользователю;
  - PowerShell 5.1 не знает `Join-String`;
  - у старых сборок `plink` нет `-hostkey "*"` — сначала кэшировать ключ через
    `echo y | plink`.
- Версию бампать **в двух местах**: `wb_energy_meter/__init__.py` и `pyproject.toml`.
  Изменения фиксировать в `CHANGELOG.md`.

## Что в работе

- Незакрытый `<template>` в `static/index.html`, из-за которого раньше был
  белый экран на `192.168.10.212` — **исправлено** (коммит «Index Fix»,
  до v0.8.0). Баланс тегов теперь проверяется автоматически, см. выше.
- `Install-WbEnergyMeter.cmd` и `Install-WbEnergyMeter.ps1` в корне репозитория
  по-прежнему отсутствуют — README на них ссылается, но файлов нет ни в
  рабочем каталоге, ни где-либо ещё в дереве. Не выдумывать их содержимое,
  добавить, когда появятся у пользователя.
- Следующий шаг по дорожной карте — двух-тарифный учёт (день/ночь) на основе
  часовых агрегатов (см. `CHANGELOG.md` → Unreleased → Запланировано).

## Стиль ответов

Пользователь — инженер-энергетик, читает и пишет по-русски. Отвечать по-русски,
кратко и по делу, без лишних преамбул.
