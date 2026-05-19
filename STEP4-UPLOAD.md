# Шаг 4 — загрузка в GitHub

Инструкция по апгрейду репозитория с **v0.3.0 → v0.4.0**.
В архиве **16 файлов**: 5 новых + 11 изменённых.

## Что внутри

```
step4-pack/
├── new-files/                  ← НОВЫЕ файлы, нужно ДОБАВИТЬ
│   ├── wb_energy_meter/
│   │   ├── aggregator.py
│   │   ├── aggregates_repo.py
│   │   └── migrations/
│   │       └── 002_aggregator_indexes.sql
│   └── tests/
│       ├── test_step4_aggregator.py
│       └── test_step4_daemon.py
└── changed-files/              ← ИЗМЕНЁННЫЕ файлы, нужно ЗАМЕНИТЬ
    ├── .github/workflows/ci.yml
    ├── CHANGELOG.md
    ├── README.md
    ├── pyproject.toml
    ├── scripts/wb-energy-meter.conf.example
    └── wb_energy_meter/
        ├── __init__.py        ← версия 0.3.0 → 0.4.0
        ├── api.py
        ├── cli.py
        ├── config.py
        ├── consumption.py
        └── main.py
```

Папки `new-files/` и `changed-files/` нужны только для разделения здесь —
**в GitHub их класть НЕ нужно**. В репо все файлы лежат на тех же местах,
где они есть в архиве (`wb_energy_meter/aggregator.py` идёт прямо в
папку `wb_energy_meter/` репозитория, и так далее).

## Способ 1 — через GitHub Web (без git)

Самый простой, занимает 5-7 минут.

### A. Загрузка НОВЫХ файлов

**A1. Файлы в `wb_energy_meter/`** (`aggregator.py` и `aggregates_repo.py`).

1. Откройте https://github.com/9043366188-dot/wb-energy-meter/tree/main/wb_energy_meter
2. Нажмите **Add file** → **Upload files**
3. Перетащите оба файла из `new-files/wb_energy_meter/`:
   - `aggregator.py`
   - `aggregates_repo.py`
4. Внизу страницы введите commit message: `feat(step4): add Aggregator and AggregateRepo`
5. Нажмите **Commit changes**

**A2. Миграция 002.**

1. Откройте https://github.com/9043366188-dot/wb-energy-meter/tree/main/wb_energy_meter/migrations
2. **Add file** → **Upload files**
3. Перетащите `new-files/wb_energy_meter/migrations/002_aggregator_indexes.sql`
4. Commit message: `feat(step4): migration 002 - aggregator indexes`
5. **Commit changes**

**A3. Тесты Шага 4.**

1. Откройте https://github.com/9043366188-dot/wb-energy-meter/tree/main/tests
2. **Add file** → **Upload files**
3. Перетащите оба файла из `new-files/tests/`:
   - `test_step4_aggregator.py`
   - `test_step4_daemon.py`
4. Commit message: `test(step4): unit + e2e tests for aggregator`
5. **Commit changes**

### Б. Замена ИЗМЕНЁННЫХ файлов

Для каждого файла из `changed-files/` нужно:

1. Открыть текущий файл в репозитории по соответствующему URL ниже.
2. Нажать иконку **карандаша** (Edit this file) сверху справа.
3. Выделить **всё содержимое** (`Ctrl+A`) и удалить.
4. Скопировать **всё содержимое** новой версии и вставить.
5. Внизу — commit message и нажать **Commit changes**.

**Список файлов и прямых URL:**

| # | Файл в репо | URL для редактирования |
|---|---|---|
| 1 | `wb_energy_meter/__init__.py` | https://github.com/9043366188-dot/wb-energy-meter/edit/main/wb_energy_meter/__init__.py |
| 2 | `wb_energy_meter/main.py` | https://github.com/9043366188-dot/wb-energy-meter/edit/main/wb_energy_meter/main.py |
| 3 | `wb_energy_meter/config.py` | https://github.com/9043366188-dot/wb-energy-meter/edit/main/wb_energy_meter/config.py |
| 4 | `wb_energy_meter/consumption.py` | https://github.com/9043366188-dot/wb-energy-meter/edit/main/wb_energy_meter/consumption.py |
| 5 | `wb_energy_meter/api.py` | https://github.com/9043366188-dot/wb-energy-meter/edit/main/wb_energy_meter/api.py |
| 6 | `wb_energy_meter/cli.py` | https://github.com/9043366188-dot/wb-energy-meter/edit/main/wb_energy_meter/cli.py |
| 7 | `scripts/wb-energy-meter.conf.example` | https://github.com/9043366188-dot/wb-energy-meter/edit/main/scripts/wb-energy-meter.conf.example |
| 8 | `pyproject.toml` | https://github.com/9043366188-dot/wb-energy-meter/edit/main/pyproject.toml |
| 9 | `README.md` | https://github.com/9043366188-dot/wb-energy-meter/edit/main/README.md |
| 10 | `CHANGELOG.md` | https://github.com/9043366188-dot/wb-energy-meter/edit/main/CHANGELOG.md |
| 11 | `.github/workflows/ci.yml` | https://github.com/9043366188-dot/wb-energy-meter/edit/main/.github/workflows/ci.yml |

Suggested commit message для всех: `feat(step4): wire up Aggregator + hybrid consumption (v0.4.0)`

### В. Проверка

После всех загрузок:

1. Откройте https://github.com/9043366188-dot/wb-energy-meter/blob/main/wb_energy_meter/__init__.py
   — должно быть `__version__ = "0.4.0"`.
2. Откройте https://github.com/9043366188-dot/wb-energy-meter/tree/main/wb_energy_meter
   — должны быть файлы `aggregator.py` и `aggregates_repo.py`.
3. Откройте https://github.com/9043366188-dot/wb-energy-meter/actions
   — должны быть новые запуски CI. Если они зелёные — всё ОК.

### Г. Установка на WB

Двойной клик по `Install-WbEnergyMeter.cmd` в `C:\WB\`. Он скачает свежий
main с GitHub (теперь там 0.4.0), применит миграцию 002 поверх вашей БД,
обновит код и перезапустит сервис.

В выводе ожидаем:

```
wb-energy-meter 0.4.0 — запуск
Применяю миграцию 002_aggregator_indexes ...
Миграция 002_aggregator_indexes применена
Aggregator запущен (catchup_days=90, max_catchup=300s)
Расчёт расхода: гибридный (агрегаты + RPC)
```

И в `/api/status`: `"version": "0.4.0"`.

## Способ 2 — через git (если он установлен)

```bash
cd /path/to/local/clone/of/wb-energy-meter
git pull

# Распаковать архив (адаптируйте путь к скачанному pack)
tar -xzf /path/to/wb-energy-meter-step4-pack.tar.gz -C /tmp

# Скопировать новые файлы
cp -r /tmp/step4-pack/new-files/. .

# Скопировать (перезаписать) изменённые файлы
cp -r /tmp/step4-pack/changed-files/. .

# Проверить, что версия обновилась
grep version wb_energy_meter/__init__.py
# Должно быть: __version__ = "0.4.0"

# Зафиксировать и запушить
git add .
git status                                  # посмотрите список
git commit -m "feat: step 4 — hourly aggregator (v0.4.0)

- Aggregator with hourly worker + catch-up + patcher
- Hybrid consumption: aggregates + RPC tails
- Migration 002: idx_aggr_meter_period
- New CLI: aggregates status/show/recompute/catchup
- New API: /api/meters/<id>/hourly, /api/aggregates/status"
git push
```

## Что после загрузки

После того как GitHub Actions покажет зелёный CI:

1. **Создать Release** (опционально, но красиво).
   - https://github.com/9043366188-dot/wb-energy-meter/releases/new
   - Choose a tag: `v0.4.0` (Create new tag).
   - Title: `v0.4.0 — Hourly aggregator`.
   - Description: скопировать раздел `[0.4.0]` из `CHANGELOG.md`.
   - Publish release.

2. **Установить на WB** — двойной клик по `Install-WbEnergyMeter.cmd`.

## Если что-то пошло не так

**CI красный после загрузки** — откройте Actions, посмотрите какой тест
упал. Скриншот пришлите мне, разберём.

**`Apply migration 002` пишет ошибку в журнале на WB** — пришлите
`journalctl -u wb-energy-meter -n 100`. Скорее всего ничего страшного,
поправим.

**Версия после установки всё ещё 0.3.0** — значит install.sh взял
кешированный код. На WB выполните:
```
rm -rf /tmp/wb-energy-meter-install-*
systemctl restart wb-energy-meter
```
И запустите установщик заново.
