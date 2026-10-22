# Спецификация `/api/v2`

Версия службы 0.25.0, 22.10.2026. Этап 15.4 плана (`docs/TZ-finish-plan.md`, §8).

Здесь описаны **все** маршруты `/api/v2`: по каждому — метод, тело запроса, параметры, ответ, коды ошибок,
живой пример и правило ревизий. Старые маршруты (`/api/registry/*`, `/api/meters*`, `/api/plans*` …) сюда не входят, кроме
раздела 3 «Устаревшие маршруты v1», который фиксирует, что с ними произошло в 0.25.0.

**Откуда примеры.** Все JSON-примеры сняты с настоящей службы на демо-стенде (`tests/browser/demo_seed.py`: ввод, два щита, три нагрузки,
три счётчика, группа «Цех», план): это живые ответы, а не пересказ. Номера, время и значения условны. Длинные списки в примерах
сокращены до двух элементов.

**Держится в актуальном состоянии тестом.** `tests/test_step74_api_spec_sync.py` сверяет множество пар «метод + маршрут» из заголовков
этого документа с `app.url_map` в обе стороны: новый маршрут `/api/v2/*` без записи здесь, как и запись о несуществующем маршруте, валит тест.
Формат заголовка маршрута — ``#### `МЕТОД /api/v2/путь/{имя}` `` — менять нельзя.

## 1. Общие правила

### 1.1. Формат

* Запрос и ответ — JSON в UTF-8. Исключения: загрузка подложки плана (`multipart/form-data`, маршруты помечены) и сама подложка (`GET …/image`).
* Ответы идут с `Cache-Control: no-store` (кроме файла подложки) и `Access-Control-Allow-Origin: *`. **Аутентификации и проверки `Origin` нет** — это
  известный риск, он вне плана (решение R1) и записан в `docs/operations.md`.
* Идентификаторы — целые числа. Время в запросах и ответах — unix-секунды (целые), если не сказано иное; `as_of` — ISO-8601 в UTC;
  в `period` границы — строки с unix-секундами.
* **«Нет значения» — `null`, никогда не `0`.** `0` означает измеренный ноль. Клиент не должен превращать `null` в `0`
  (интерфейс показывает «—», тест A10).
* Булевы флаги в query передаются как `1`; ссылки на сущности — `{имя}` в пути.
* **Тело записи — плоский JSON.** Маршруты изменения конфигурации принимают и конверт `{"data": {…}}`, но тогда **`expected_revision` должен лежать внутри `data`**:
  поле того же имени на верхнем уровне конверта игнорируется (ответ `409 revision_conflict` «требуется expected_revision»). Расчётные маршруты
  (`metrics/query`, `overview/summary`, `reports/query`, `topology/nodes/{id}/balance`), а также `topology/validate`, `topology/publish` и все JSON-маршруты
  эмулятора, кроме `archive-points`, конверт не разбирают — только плоское тело.

### 1.2. Ошибки

Ошибка `/api/v2` — всегда JSON одной формы (envelope):

```json
{
  "code": "revision_conflict",
  "message": "ревизия конфигурации устарела: ожидалась 32, сейчас 33 — кто-то уже сохранил более новые изменения; перечитайте текущее состояние и повторите",
  "fields": ["expected_revision"],
  "ids": [1, 2],
  "path": []
}
```

| Поле | Тип | Смысл |
|---|---|---|
| `code` | строка | Машинный код ошибки (таблица ниже). По нему клиент ветвится. |
| `message` | строка | Сообщение на русском, пригодное для показа человеку. Не парсить. |
| `fields` | массив строк | Имена полей запроса, из-за которых отказ (есть не всегда). |
| `ids` | массив целых | Затронутые сущности: занявшие место, отсутствующие, конфликтующие (есть не всегда). |
| `path` | массив | Подробности отказа: нарушения схемы (`topology_conflict`), линии узла (`conflict`). Есть не всегда. |

При любой ошибке запись **не выполнена**: доменные изменения и новая ревизия фиксируются одной транзакцией, откат полный.

| Статус | `code` | Что означает |
|---|---|---|
| 400 | `bad_request` | Неверное тело, параметр или значение; нечего менять; не хватает обязательного поля. |
| 400 | `field_removed` | Поле удалено из API (сейчас — `is_input`, `fed_from_point_id` у точки): питание задаётся линиями схемы. |
| 400 | `reanchor_required` | Замена подложки с пропорциональным пересчётом невозможна — другое соотношение сторон. |
| 404 | `not_found` | Сущности (или ревизии) с таким идентификатором нет. `ids` — какие именно. |
| 409 | `revision_conflict` | `expected_revision` отсутствует или устарела (или `canvas_revision` плана, см. 1.3). |
| 409 | `double_counting` | Двойной учёт: прибор (его фаза) уже занят другой точкой, либо одна величина вошла в сумму дважды. |
| 409 | `topology_conflict` | Нарушены правила схемы: цикл, второй источник питания у узла, вид узла. |
| 409 | `already_metered` | Линию уже измеряет другая точка. |
| 409 | `cycle_conflict` | Перенос создал бы цикл в дереве (групп или мест). |
| 409 | `moved_to_v2` | Только `/api/registry/*`: запись через старый маршрут закрыта после поколения домена 2 (раздел 3). Тело — с теми же `code`/`message`, плюс `error` и `replacement`. |
| 409 | `category_conflict` | Категория дочерней группы не совпадает с категорией родителя. |
| 409 | `conflict` | Прочий конфликт состояния: уже состоит в группе, у узла есть действующие линии, предел числа приборов эмулятора, эмулятор выключен. |
| 500 | `internal` | Нарушен внутренний контракт результата расчёта; подробности — в журнале службы. |
| 500 | `server_error` | Сбой служебной операции (`admin/migrate-legacy`). |
| 503 | `service_unavailable` | Нужная часть службы не подключена (каталог планов, эмулятор). |

Не из envelope: неизвестный путь отвечает `404 {"error": "not found", "path": …}`, превышение размера тела — стандартной ошибкой
`{"error": …, "code": …}`, необработанный сбой — `500 {"error": "internal", "detail": …}`. Старые маршруты `/api/registry/*` после поколения 2 отвечают
`409` с тем же `code`/`message` и дополнительным полем `error` (раздел 3).

### 1.3. Протокол ревизий

Вся конфигурация (точки, места, группы, схема, границы баланса, свойства планов) защищена **одной глобальной ревизией**
`configuration_revision`: монотонно растущее число, которое увеличивает каждая успешная запись. Телеметрия в неё не входит.

1. **Прочитать текущую:** `GET /api/v2/revision` (или поле `configuration_revision` в ответе предыдущей записи).
2. **Создание** независимой сущности (`POST` точки, места, группы, узла, черновика линии, границы баланса) `expected_revision` **не требует**:
   создание не может затереть чужую правку. Ревизия всё равно продвигается, в ответе — новая `configuration_revision`.
3. **Изменение** (все остальные записывающие маршруты, помеченные в описании «`expected_revision` обязателен») принимает `expected_revision` — ревизию, которую клиент видел последней.
   Если она **устарела или не передана** — `409 revision_conflict`, ничего не записано. Клиент перечитывает состояние и повторяет.
   Число передаётся в теле (плоском или внутри `data`); для `DELETE …/members/{point_id}` — в query или в теле.
4. **Ответ записи** содержит новую `configuration_revision` (у `204` — заголовок `X-Configuration-Revision`).
5. Запрос «нечего менять» (`400`) ревизию не продвигает; провалившийся составной запрос не оставляет ничего — ни сущностей, ни ревизии.

Особые случаи:

| Маршрут | Как защищён |
|---|---|
| `POST /api/v2/topology/publish` | Поле называется **`expected_configuration_revision`**, смысл тот же. |
| `POST /api/v2/plans/{plan_id}/layout` | Не глобальной ревизией, а **`canvas_revision` плана**: в поле `expected_revision` передаётся `canvas_revision` из последнего чтения плана. |
| `POST/PATCH/DELETE …/plans/{plan_id}/items*`, `…/edges*`, `POST/DELETE /api/v2/plans*`, `…/image*` | Вне протокола: значок или фон можно править без `expected_revision`. Для пакетного сохранения с защитой от потери правок есть `…/layout`. |
| `/api/v2/emulator/*` (кроме `archive-points`) | Состояние эмулятора хранится отдельно от конфигурации. |
| `POST /api/v2/admin/migrate-legacy` | Защита — явное `confirm: true` и резервная копия БД. |

Ревизия — это счётчик защиты от потери правок, а не снимок: «как было на ревизии N» она не восстанавливает. Историю
схемы и состава ведёт журнал `GET /api/v2/change-log` (с 0.22.0, решение R7).

### 1.4. Время и периоды

Поля периода `from` и `to` в расчётных маршрутах (`metrics/query`, `overview/summary`, `reports/query`, `topology/nodes/{id}/balance`) и фильтры `from`/`to` журнала принимают **unix-секунды** (число) либо **строку-дату**: `ГГГГ-ММ-ДД`, `ГГГГ-ММ-ДД ЧЧ:ММ`, `ГГГГ-ММ-ДД ЧЧ:ММ:СС` (с пробелом или `T`). Период — полуинтервал `[from, to)`, `to` строго позже `from`; соседние сутки `[1 число, 2 число)` и `[2 число, 3 число)` не пересекаются, граничный час учитывается один раз.

**Часовой пояс.** Строка-дата читается в поясе `timezone` запроса (имя IANA, например `Europe/Moscow`; по умолчанию `UTC`), а не в поясе процесса службы: оператор имеет в виду сутки в своём календаре. Unix-секунды — это момент времени, пояс к ним отношения не имеет и остаётся подписью.

* `ГГГГ-ММ-ДД` — полночь этой даты в поясе `timezone`. Сутки в зоне с переходом на летнее время бывают 23 и 25 часов: в `Europe/Berlin` `2026-03-29` → `2026-03-30` — 23 часа, `2026-10-25` → `2026-10-26` — 25 часов.
* Несуществующее местное время (пропуск при переходе, `2026-03-29 02:30` в `Europe/Berlin`) читается по смещению *до* перехода; неоднозначное (повтор часа, `2026-10-25 02:30`) — по первому вхождению, то есть до перехода.
* Неизвестное имя пояса при строке-дате — `400`, `fields: ["timezone"]`. При unix-секундах имя не проверяется; если оно не распознано, подписи `*_local` строятся в UTC.
* Подписи в ответе: `period.from_local`, `period.to_local` — ISO 8601 со смещением пояса (`2026-03-29T00:00:00+01:00` → `2026-03-30T00:00:00+02:00`); `from` и `to` остаются unix-секундами строкой. То же у `compare_period` отчётов.
* Интерфейс всегда шлёт даты из полей выбора вместе с `timezone` браузера; по умолчанию — с 1-го числа текущего месяца по сегодня *в календаре браузера*.
* Командная строка (`consumption --from/--to`) и классические маршруты чтения вне `/api/v2` (`/api/meters/{id}/consumption`, `/api/summary/consumption`, `/api/reports/balance`, `/api/availability/summary` …) читают даты в поясе процесса службы: это локальные инструменты на контроллере.

### 1.5. Объекты

Поля, общие для нескольких маршрутов. Во всех сущностях `created_at`/`updated_at` — unix-секунды, `archived_at` — `null`, пока сущность не в архиве.

| Объект | Поля |
|---|---|
| **Точка учёта** | `id`, `code`, `name`, `description`, `installation_location_id`, `installation_note`, `enabled`, `archived_at`, `created_at`, `updated_at`; в карточке ещё `power_supply` (`is_input`, `fed_from_point_id`, `measured_edge_id`, `measured_edge_label`). |
| **Привязка** | `id`, `point_id`, `meter_source_id`, `channel_profile` (`total_3p`, `phase_l1`, `phase_l2`, `phase_l3`), `role` (`primary`, `check`), `valid_from`, `valid_to` (`null` — действует), `replacement_note`, `created_at`. |
| **Место** | `id`, `parent_id`, `kind`, `name`, `code`, `sort_order`, `archived_at`, `created_at`, `updated_at`. |
| **Узел схемы** | `id`, `code`, `name`, `kind` (`source`, `panel`, `junction`, `load`), `location_id`, `archived_at`, `created_at`, `updated_at`. |
| **Линия схемы** | `id`, `code`, `name`, `from_node_id`, `to_node_id`, `primary_point_id`, `phase_count`, `rated_current_a`, `cable_note`, `state` (`draft`, `published`), `valid_from`, `valid_to`, `archived_at`, `created_at`, `updated_at`. |
| **Группа** | `id`, `name`, `category`, `parent_id`, `color`, `created_at`. |
| **Членство** | `id`, `group_id`, `point_id`, `valid_from`, `valid_to`, `created_at`. |
| **План** | `id`, `name`, `plan_kind` (`floor`, `single_line`), `image_width`, `image_height`, `canvas_width`, `canvas_height`, `canvas_revision`, `is_default`, `created_at`, `updated_at`. |
| **Элемент плана** | `id`, `plan_id`, `kind`, `point_id`, `location_id`, `group_id`, `node_id`, `target_plan_id`, `geometry`, `coord_space`, `label`, `sort_order`. |
| **Вид линии на плане** | `id`, `plan_id`, `edge_id`, `from_item_id`, `to_item_id`, `waypoints`, `view_kind` (`structural`, `cable_route`), `confirmed_at`. |

### 1.6. Результат расчёта

`metrics/query`, `overview/summary`, `reports/query` и баланс узла возвращают один и тот же объект результата (контракт — `accounting_contract.py`),
один расчёт на все экраны, CSV и отчёты:

| Поле | Смысл |
|---|---|
| `metric`, `unit` | Что считается (`energy_import`, `imbalance`) и единица (`kWh`). |
| `mode` | `measured`, `sum`, `balance`, `comparison`. |
| `value` | Число — **только если данные покрывают весь ожидаемый состав**; иначе `null`. |
| `known_value` | Сумма известной части; без единой годной части — `null`, не `0`. |
| `availability` | `complete`, `partial` или `missing`. |
| `expected_count`, `valid_count`, `missing_ids` | Сколько частей ожидалось и сколько годных (часов для одной точки, точек для суммы и баланса), чего не хватает. |
| `quality_flags` | Открытый список причин неполноты: `gap`, `reset` (сброс накопителя внутри часа), `stale`, `estimated`, `channel_error`, `unsynchronised`, `missing_source`, `held`, `sparse` (редкие показания), `edge_approx`. |
| `structure_quality` | `verified`, `unverified` или `assumed_legacy` (схема до начала журнала). |
| `source` | `measured`, `calculated`, `estimated`. |
| `configuration_revision_id`, `configuration_revision_ids` | Ревизия конфигурации, по которой считали. |
| `as_of`, `period` | Когда посчитано и за какой период: `from`, `to` (unix-секунды строкой), `timezone` и подписи **`from_local`**, **`to_local`** — ISO 8601 со смещением пояса отчёта (у суток перехода часов видно, что смещение сменилось). |
| `explanation` | Состав расчёта: `point_id`; для суммы `included_point_ids`; для баланса `input_point_ids`, `output_point_ids`, `input_value`, `output_value`, `percentage`, `percentage_reason`. |

## 2. Маршруты

Всего 81 пар «метод + маршрут» в 10 группах. В заголовках ниже путь записан как `{имя}`; во Flask это `<int:имя>`.

### Указатель

| Метод | Маршрут | Группа | Что делает |
|---|---|---|---|
| GET | `/api/v2/revision` | Ревизия и журнал изменений | Текущая глобальная ревизия конфигурации — число, которое клиент подставляет в `expected_revision` следующей записи |
| GET | `/api/v2/change-log` | Ревизия и журнал изменений | Журнал изменений конфигурации, новые записи сверху |
| GET | `/api/v2/points` | Точки учёта и привязки приборов | Список точек учёта |
| POST | `/api/v2/points` | Точки учёта и привязки приборов | Создать точку учёта |
| GET | `/api/v2/points/{point_id}` | Точки учёта и привязки приборов | Одна точка учёта вместе с проекцией питания |
| PATCH | `/api/v2/points/{point_id}` | Точки учёта и привязки приборов | Изменить точку: имя, описание, место, включение или архивация |
| GET | `/api/v2/points/{point_id}/bindings` | Точки учёта и привязки приборов | История привязок точки к приборам (открытая — с `valid_to: null`) |
| POST | `/api/v2/points/{point_id}/bindings` | Точки учёта и привязки приборов | Открыть привязку точки к прибору (в первую очередь — первую) |
| POST | `/api/v2/points/{point_id}/replace-meter` | Точки учёта и привязки приборов | Атомарно заменить прибор точки: действующая основная привязка закрывается, новая открывается с момента `at` — одной ревизией |
| GET | `/api/v2/points/{point_id}/groups` | Точки учёта и привязки приборов | Членства точки в учётных группах (текущие и закрытые) |
| GET | `/api/v2/structure/points` | Точки учёта и привязки приборов | Данные для экрана «Структура»: все активные точки с местом, действующим прибором, признаками «размещена на плане», «ввод», «питается от» и список занятых приборов |
| GET | `/api/v2/snapshot` | Точки учёта и привязки приборов | Текущие значения точек из памяти службы (без обращений к истории): мощность, энергия, напряжения, токи, давность данных |
| GET | `/api/v2/validation` | Точки учёта и привязки приборов | Сводка незавершённой настройки: точки без прибора, места, группы, плана; узлы без линий; линии без измерения |
| GET | `/api/v2/locations` | Места | Список мест |
| POST | `/api/v2/locations` | Места | Создать место |
| GET | `/api/v2/locations/{location_id}` | Места | Одно место |
| PATCH | `/api/v2/locations/{location_id}` | Места | Переименовать, сменить код или родителя, архивировать место |
| GET | `/api/v2/groups` | Учётные группы | Список групп |
| POST | `/api/v2/groups` | Учётные группы | Создать группу |
| GET | `/api/v2/groups/{group_id}` | Учётные группы | Одна группа |
| PATCH | `/api/v2/groups/{group_id}` | Учётные группы | Перенести группу под другого родителя (`parent_id`) |
| GET | `/api/v2/groups/{group_id}/members` | Учётные группы | Состав группы (прямые участники) |
| POST | `/api/v2/groups/{group_id}/members` | Учётные группы | Добавить точку в группу с момента `valid_from` |
| DELETE | `/api/v2/groups/{group_id}/members/{point_id}` | Учётные группы | Вывести точку из группы (членство закрывается моментом `at`, история остаётся) |
| GET | `/api/v2/groups/{group_id}/effective-members` | Учётные группы | Эффективный состав: сама группа и все дочерние, точка один раз; `via` показывает, по какому пути она включена |
| GET | `/api/v2/topology/nodes` | Электрическая схема (узлы и линии) | Список узлов схемы |
| POST | `/api/v2/topology/nodes` | Электрическая схема (узлы и линии) | Создать узел |
| GET | `/api/v2/topology/nodes/{node_id}` | Электрическая схема (узлы и линии) | Один узел |
| PATCH | `/api/v2/topology/nodes/{node_id}` | Электрическая схема (узлы и линии) | Переименовать узел, сменить вид и место либо архивировать (`archived: true`) |
| GET | `/api/v2/topology/nodes/{node_id}/lines` | Электрическая схема (узлы и линии) | Линии узла (входящая и отходящие) с точкой учёта и прибором на каждой |
| POST | `/api/v2/topology/nodes/{node_id}/meters` | Электрическая схема (узлы и линии) | «+ Счётчик» в карточке узла: свободный прибор → точка учёта → привязка → «измеряет эту линию» — одной ревизией |
| POST | `/api/v2/topology/nodes/{node_id}/add-consumer` | Электрическая схема (узлы и линии) | Добавить нагрузку к узлу: создаются узел-нагрузка и опубликованная линия к нему |
| POST | `/api/v2/topology/nodes/{node_id}/balance` | Электрическая схема (узлы и линии) | Баланс одного узла (щита): вход против выходов за период |
| GET | `/api/v2/topology/edges` | Электрическая схема (узлы и линии) | Список линий: по умолчанию черновики |
| POST | `/api/v2/topology/edges` | Электрическая схема (узлы и линии) | Создать **черновик** линии |
| GET | `/api/v2/topology/edges/{edge_id}` | Электрическая схема (узлы и линии) | Одна линия |
| PATCH | `/api/v2/topology/edges/{edge_id}` | Электрическая схема (узлы и линии) | Поменять счётчик линии (`primary_point_id`), имя, номинальный ток, примечание — либо вывести линию из работы (`retire`) |
| POST | `/api/v2/topology/edges/connect` | Электрическая схема (узлы и линии) | Соединить два узла **сразу действующей** линией (без черновика) |
| POST | `/api/v2/topology/edges/{edge_id}/attach-new-point` | Электрическая схема (узлы и линии) | Поставить счётчик на неизмеренную линию: создать точку из прибора, привязать и назначить измеряющей — одной ревизией |
| POST | `/api/v2/topology/validate` | Электрическая схема (узлы и линии) | Проверить черновики линий на правила схемы, ничего не публикуя |
| POST | `/api/v2/topology/publish` | Электрическая схема (узлы и линии) | Опубликовать черновики линий: с `effective_from` они действуют |
| GET | `/api/v2/topology/edge-loads` | Электрическая схема (узлы и линии) | Загрузка линий током от номинала (`load_percent`) по текущим значениям точек |
| GET | `/api/v2/balance-scopes` | Границы баланса | Список границ баланса |
| POST | `/api/v2/balance-scopes` | Границы баланса | Создать границу баланса |
| GET | `/api/v2/balance-scopes/{scope_id}` | Границы баланса | Одна граница с действующим составом |
| PATCH | `/api/v2/balance-scopes/{scope_id}` | Границы баланса | Изменить имя, описание или состав |
| POST | `/api/v2/metrics/query` | Расчёты и отчёты | Один показатель по точкам или границе баланса за период |
| POST | `/api/v2/overview/summary` | Расчёты и отчёты | Сводка для «Обзора»: итог объекта (только через назначенные вводы), небаланс отдельной строкой, ветви сети, группы |
| POST | `/api/v2/reports/query` | Расчёты и отчёты | Строки отчёта в одном из четырёх срезов; тот же расчёт, что у «Обзора» и `metrics/query` |
| GET | `/api/v2/plans` | Планы | Список планов |
| POST | `/api/v2/plans` | Планы | Создать план: с подложкой или пустой холст |
| GET | `/api/v2/plans/{plan_id}` | Планы | План с элементами и видами линий |
| PATCH | `/api/v2/plans/{plan_id}` | Планы | Переименовать план или сделать его «планом по умолчанию» |
| DELETE | `/api/v2/plans/{plan_id}` | Планы | Удалить план (только его вид: точки, узлы и линии схемы остаются) |
| GET | `/api/v2/plans/{plan_id}/image` | Планы | Файл подложки (картинка, не JSON) |
| POST | `/api/v2/plans/{plan_id}/image` | Планы | Заменить подложку |
| POST | `/api/v2/plans/{plan_id}/image/preview` | Планы | Предпросмотр замены подложки: ничего не пишет |
| GET | `/api/v2/plans/{plan_id}/image-versions` | Планы | Версии подложки плана |
| POST | `/api/v2/plans/{plan_id}/image/restore` | Планы | Вернуть прежнюю подложку |
| GET | `/api/v2/plans/{plan_id}/items` | Планы | Элементы плана |
| POST | `/api/v2/plans/{plan_id}/items` | Планы | Разместить элемент на плане |
| PATCH | `/api/v2/plans/{plan_id}/items/{item_id}` | Планы | Сдвинуть элемент: сменить геометрию |
| DELETE | `/api/v2/plans/{plan_id}/items/{item_id}` | Планы | Снять элемент с плана (сущность схемы остаётся) |
| GET | `/api/v2/plans/{plan_id}/edges` | Планы | Виды линий на плане |
| POST | `/api/v2/plans/{plan_id}/edges` | Планы | Нарисовать линию схемы на плане между двумя элементами |
| DELETE | `/api/v2/plans/{plan_id}/edges/{view_id}` | Планы | Убрать вид линии с плана (сама линия схемы остаётся) |
| POST | `/api/v2/plans/{plan_id}/layout` | Планы | Атомарно сохранить пакет правок плана (элементы и виды линий) с оптимистичной блокировкой по `canvas_revision` |
| GET | `/api/v2/migration/status` | Перенос данных и служебные маршруты | Состояние миграции: версия схемы БД, поколения домена, карта переноса, число приборов и связей, ещё не перенесённых |
| GET | `/api/v2/migration/legacy-links` | Перенос данных и служебные маршруты | Связи плана старой модели (зона → зона) и статус их переноса в линии схемы |
| POST | `/api/v2/migration/legacy-links/confirm` | Перенос данных и служебные маршруты | Подтвердить перенос связей пачкой: либо все, либо ни одной |
| POST | `/api/v2/admin/migrate-legacy` | Перенос данных и служебные маршруты | Перенести все строки `meters`/`meter_groups` в точки, приборы, привязки и членство групп |
| GET | `/api/v2/emulator` | Эмулятор счётчиков | Полное состояние эмулятора: включён ли, приборы, журнал действий, состояние пробы истории, точки, привязанные к приборам эмулятора |
| GET | `/api/v2/emulator/summary` | Эмулятор счётчиков | Лёгкая сводка для баннера «Обзора»: берётся из памяти, к БД не обращается |
| POST | `/api/v2/emulator/enabled` | Эмулятор счётчиков | Включить или выключить эмулятор |
| POST | `/api/v2/emulator/devices` | Эмулятор счётчиков | Добавить виртуальный прибор |
| POST | `/api/v2/emulator/devices/preset` | Эмулятор счётчиков | Добавить типовой набор: три нагрузки с разными фазами и ввод над ними (всего 4 прибора) |
| PATCH | `/api/v2/emulator/devices/{n}` | Эмулятор счётчиков | Изменить виртуальный прибор |
| DELETE | `/api/v2/emulator/devices/{n}` | Эмулятор счётчиков | Удалить виртуальный прибор (его каналы стираются из MQTT, из `sources` вводов он убирается) |
| POST | `/api/v2/emulator/devices/{n}/fault` | Эмулятор счётчиков | Вызвать неисправность прибора |
| POST | `/api/v2/emulator/probe-history` | Эмулятор счётчиков | Проверить, пишет ли `wb-mqtt-db` историю канала эмулятора (одним запросом) |
| POST | `/api/v2/emulator/archive-points` | Эмулятор счётчиков | Архивировать точки, привязанные к приборам эмулятора, одной ревизией (история сохраняется, приборы освобождаются) |

### 2.1. Ревизия и журнал изменений

Точка отсчёта для записи и журнал изменений (только чтение).

#### `GET /api/v2/revision`

Текущая глобальная ревизия конфигурации — число, которое клиент подставляет в `expected_revision` следующей записи.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` `{"configuration_revision": N}`. `0` — в этой базе ещё не было ни одной защищённой записи.

**Пример.** `/api/v2/revision`

Ответ `200`:

```json
{
  "configuration_revision": 17
}
```

#### `GET /api/v2/change-log`

Журнал изменений конфигурации, новые записи сверху. Только чтение; журнал ведёт слой записи БД (0.22.0), история топологии есть только с этого выпуска.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| entity_type | строка | нет | Тип сущности (`metering_point`, `electrical_edge`, `group`, `plan_layout` и др.); неизвестный — `400`. |
| entity_id | целое | нет | Идентификатор сущности. |
| action | строка | нет | `create`, `update`, `close` и т. п. |
| from | целое или дата | нет | Нижняя граница по времени записи (unix-секунды или дата; дату читают в поясе `timezone`). |
| to | целое или дата | нет | Верхняя граница по времени записи. |
| timezone | строка | нет | Часовой пояс IANA для строк-дат в `from`/`to`, по умолчанию `UTC`; неизвестное имя при строке-дате — `400`, поле `timezone`. К unix-секундам пояс отношения не имеет. |
| limit | целое ≥ 1 | нет | Размер страницы, по умолчанию 100, не более 500. |
| before_id | целое | нет | Курсор: только записи старше этого `id` (берётся из `next_before_id`). |
| related | `1` | нет | С записями подчинённых сущностей — история карточки. |

**Ответ.** `200` `{items, next_before_id, limit, journal_started_at}`. Элемент: `id`, `entity_type`, `entity_id`, `action`, `old_value`, `new_value`, `operation` (метод и шаблон маршрута), `origin` (`web`, `legacy-api` …), `client`, `revision_id`, `effective_from`, `recorded_at`. `next_before_id` — `null`, когда страниц больше нет.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Неизвестный `entity_type`, нецелое `entity_id`/`before_id`/`limit`, `limit` < 1, нечитаемая дата. |

**Пример.** `/api/v2/change-log?limit=2`

Ответ `200`:

```json
{
  "items": [
    {
      "id": 42,
      "entity_type": "plan_layout",
      "entity_id": 1,
      "action": "update",
      "old_value": null,
      "new_value": {
        "edge_views": {
          "added": 1,
          "removed": 0,
          "updated": 0
        },
        "items": {
          "added": 0,
          "removed": 0,
          "updated": 0
        }
      },
      "operation": "POST /api/v2/plans/<int:plan_id>/edges",
      "origin": "web",
      "client": "127.0.0.1",
      "revision_id": null,
      "effective_from": 1792658667,
      "recorded_at": 1792658667
    },
    {
      "id": 41,
      "entity_type": "plan_layout",
      "entity_id": 1,
      "action": "update",
      "old_value": null,
      "new_value": {
        "edge_views": {
          "added": 1,
          "removed": 0,
          "updated": 0
        },
        "items": {
          "added": 0,
          "removed": 0,
          "updated": 0
        }
      },
      "operation": "POST /api/v2/plans/<int:plan_id>/edges",
      "origin": "web",
      "client": "127.0.0.1",
      "revision_id": null,
      "effective_from": 1792658667,
      "recorded_at": 1792658667
    }
  ],
  "next_before_id": 41,
  "limit": 2,
  "journal_started_at": 1792658666
}
```

**Пример: после записей.** `/api/v2/change-log?limit=3&entity_type=metering_point`

Ответ `200`:

```json
{
  "items": [
    {
      "id": 77,
      "entity_type": "metering_point",
      "entity_id": 6,
      "action": "create",
      "old_value": null,
      "new_value": {
        "archived_at": null,
        "code": "pv3-m-3-1792658668049310",
        "created_at": 1792658668,
        "description": null,
        "enabled": 1,
        "id": 6,
        "installation_location_id": null,
        "installation_note": null,
        "name": "dev-n2",
        "updated_at": 1792658668
      },
      "operation": "POST /api/v2/topology/nodes/<int:node_id>/meters",
      "origin": "web",
      "client": "127.0.0.1",
      "revision_id": 38,
      "effective_from": 1792658668,
      "recorded_at": 1792658668
    },
    {
      "id": 72,
      "entity_type": "metering_point",
      "entity_id": 5,
      "action": "create",
      "old_value": null,
      "new_value": {
        "archived_at": null,
        "code": "pv3-att-8-1792658668046",
        "created_at": 1792658668,
        "description": null,
        "enabled": 1,
        "id": 5,
        "installation_location_id": null,
        "installation_note": null,
        "name": "Вентиляция",
        "updated_at": 1792658668
      },
      "operation": "POST /api/v2/topology/edges/<int:edge_id>/attach-new-point",
      "origin": "web",
      "client": "127.0.0.1",
      "revision_id": 37,
      "effective_from": 1792658668,
      "recorded_at": 1792658668
    }
  ],
  "next_before_id": 48,
  "limit": 3,
  "journal_started_at": 1792658667
}
```

_Списки в примере сокращены до двух элементов._

**Пример: bad_request.** `/api/v2/change-log?limit=abc`

Ответ `400`:

```json
{
  "code": "bad_request",
  "message": "limit должен быть целым числом",
  "fields": [
    "limit"
  ]
}
```

### 2.2. Точки учёта и привязки приборов

Точка учёта — то, что измеряется; прибор (`meter_source`) привязывается к ней интервалом времени. «Питание» точки (`power_supply`) — только проекция опубликованной схемы, отдельного поля в точке нет.

#### `GET /api/v2/points`

Список точек учёта.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| include_archived | `1` | нет | Показать и архивные (`archived_at` не `null`). |

**Ответ.** `200` массив точек (поля — в «Общих объектах», без `power_supply`).

**Пример.** `/api/v2/points`

Ответ `200`:

```json
[
  {
    "id": 1,
    "code": "pt-in",
    "name": "Ввод",
    "description": null,
    "installation_location_id": null,
    "installation_note": null,
    "enabled": true,
    "archived_at": null,
    "created_at": 1792658666,
    "updated_at": 1792658666
  },
  {
    "id": 2,
    "code": "pt-light",
    "name": "Освещение",
    "description": null,
    "installation_location_id": null,
    "installation_note": null,
    "enabled": true,
    "archived_at": null,
    "created_at": 1792658666,
    "updated_at": 1792658666
  }
]
```

_Списки в примере сокращены до двух элементов._

#### `POST /api/v2/points`

Создать точку учёта. Прибор к ней привязывается отдельно (`POST …/bindings`).

**Ревизия.** Создаёт запись и продвигает ревизию, но `expected_revision` **не требует** (создание новой сущности не может затереть чужую правку). В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| code | строка | да | Уникальный код точки. |
| name | строка | да | Название. |
| description | строка | нет | Описание. |
| installation_location_id | целое | нет | Место установки (см. `/api/v2/locations`). |
| installation_note | строка | нет | Пометка к месту установки. |

**Ответ.** `201` созданная точка и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Пустой/повторяющийся `code`, пустое имя, неизвестное место. |

**Пример.** `/api/v2/points`

Тело запроса:

```json
{
  "code": "pt-new",
  "name": "Компрессор",
  "description": "Склад, щит ЩР-2"
}
```

Ответ `201`:

```json
{
  "id": 4,
  "code": "pt-new",
  "name": "Компрессор",
  "description": "Склад, щит ЩР-2",
  "installation_location_id": null,
  "installation_note": null,
  "enabled": true,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 20
}
```

**Пример: bad_request.** `/api/v2/points`

Тело запроса:

```json
{
  "name": "Без кода"
}
```

Ответ `400`:

```json
{
  "code": "bad_request",
  "message": "код не может быть пустым",
  "fields": [
    "code",
    "name"
  ]
}
```

#### `GET /api/v2/points/{point_id}`

Одна точка учёта вместе с проекцией питания.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` точка и `power_supply`: `is_input` (точка измеряет входящую линию источника), `fed_from_point_id`, `measured_edge_id`, `measured_edge_label`. Проекция читается из опубликованной схемы.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Точки нет. |

**Пример.** `/api/v2/points/2`

Ответ `200`:

```json
{
  "id": 2,
  "code": "pt-light",
  "name": "Освещение",
  "description": null,
  "installation_location_id": null,
  "installation_note": null,
  "enabled": true,
  "archived_at": null,
  "created_at": 1792658666,
  "updated_at": 1792658666,
  "power_supply": {
    "is_input": false,
    "fed_from_point_id": 1,
    "measured_edge_id": 3,
    "measured_edge_label": "ЩР-1 → Освещение"
  }
}
```

**Пример: not_found.** `/api/v2/points/999999`

Ответ `404`:

```json
{
  "code": "not_found",
  "message": "Точка 999999 не найдена",
  "ids": [
    999999
  ]
}
```

#### `PATCH /api/v2/points/{point_id}`

Изменить точку: имя, описание, место, включение или архивация. Должно быть передано хотя бы одно поле.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| name | строка | нет | Новое имя. |
| description | строка | нет | Новое описание. |
| installation_note | строка | нет | Новая пометка. |
| installation_location_id | целое или null | нет | Новое место; `null` снимает. |
| enabled | булево | нет | Включить или выключить точку. |
| archived | `true` | нет | Архивировать. Одновременно закрываются открытые привязки (прибор освобождается) и точка снимается с линий (`primary_point_id` → `null`); история сохраняется. Разархивации нет. |
| is_input / fed_from_point_id | — | нет | **Удалены** (0.22.x): питание задаётся линиями схемы. Любое из них → `400 field_removed`, тело целиком отклоняется. |

**Ответ.** `200` точка с `power_supply` и `configuration_revision`; при архивации ещё `released`: `{closed_bindings, edge_ids}` — сколько привязок закрыто и какие линии освобождены.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | field_removed | Присланы `is_input` или `fed_from_point_id`. |
| 400 | bad_request | Нет ни одного изменяемого поля; нарушено ограничение значения. |
| 404 | not_found | Точки нет. |
| 409 | revision_conflict | `expected_revision` устарела или не передана. |

**Пример.** `/api/v2/points/4`

Тело запроса:

```json
{
  "name": "Компрессор К-1",
  "installation_location_id": 1,
  "expected_revision": 20
}
```

Ответ `200`:

```json
{
  "id": 4,
  "code": "pt-new",
  "name": "Компрессор К-1",
  "description": "Склад, щит ЩР-2",
  "installation_location_id": 1,
  "installation_note": null,
  "enabled": true,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "power_supply": {
    "is_input": false,
    "fed_from_point_id": null,
    "measured_edge_id": null,
    "measured_edge_label": null
  },
  "configuration_revision": 21
}
```

**Пример: field_removed.** `/api/v2/points/4`

Тело запроса:

```json
{
  "is_input": true,
  "expected_revision": 21
}
```

Ответ `400`:

```json
{
  "code": "field_removed",
  "message": "Питание задаётся линиями на «Плане v3»",
  "fields": [
    "is_input",
    "fed_from_point_id"
  ],
  "ids": [
    4
  ]
}
```

#### `GET /api/v2/points/{point_id}/bindings`

История привязок точки к приборам (открытая — с `valid_to: null`).

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` массив привязок: `id`, `point_id`, `meter_source_id`, `channel_profile`, `role`, `valid_from`, `valid_to`, `replacement_note`, `created_at`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Точки нет. |

**Пример.** `/api/v2/points/2/bindings`

Ответ `200`:

```json
[
  {
    "id": 2,
    "point_id": 2,
    "meter_source_id": 2,
    "channel_profile": "total_3p",
    "role": "primary",
    "valid_from": 0,
    "valid_to": null,
    "replacement_note": null,
    "created_at": 1792658667
  }
]
```

**Пример: после замены.** `/api/v2/points/4/bindings`

Ответ `200`:

```json
[
  {
    "id": 4,
    "point_id": 4,
    "meter_source_id": 4,
    "channel_profile": "total_3p",
    "role": "primary",
    "valid_from": 1792658668,
    "valid_to": 1792658728,
    "replacement_note": null,
    "created_at": 1792658668
  },
  {
    "id": 5,
    "point_id": 4,
    "meter_source_id": 5,
    "channel_profile": "total_3p",
    "role": "primary",
    "valid_from": 1792658728,
    "valid_to": null,
    "replacement_note": "Заменён после поверки",
    "created_at": 1792658668
  }
]
```

#### `POST /api/v2/points/{point_id}/bindings`

Открыть привязку точки к прибору (в первую очередь — первую). Прибор задаётся готовым источником либо адресом нового.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| meter_source_id | целое | один из двух | Существующий источник прибора. |
| new_meter | объект | один из двух | `{device_id, controller_key?, display_name?}` — адрес прибора как в MQTT; недостающие `meter` и источник заводятся тем же запросом (повтор безопасен). |
| channel_profile | строка | нет | `total_3p` (по умолчанию) или `phase_l1`/`phase_l2`/`phase_l3` — какую часть прибора измеряет точка. |
| role | строка | нет | `primary` (по умолчанию) или `check` (контрольный прибор — другой, чем основной). |
| valid_from / at | целое | нет | Начало действия привязки (unix-секунды), по умолчанию «сейчас». |

Прибор считается занятым, пока на нём есть действующая привязка к другой точке (для пофазных профилей — занята только своя фаза).

**Ответ.** `201` привязка, `configuration_revision` и `meter_source_id` использованного источника.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Не указан ни `meter_source_id`, ни `new_meter`; неверный профиль/роль/`device_id`. |
| 404 | not_found | Точки нет. |
| 409 | double_counting | Прибор (или его фаза) уже занят другой точкой, либо у точки уже есть основной прибор в этом интервале. В `message` — понятный русский текст, кто занял. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример.** `/api/v2/points/4/bindings`

Тело запроса:

```json
{
  "new_meter": {
    "controller_key": "wb-map3e_",
    "device_id": "dev-new",
    "display_name": "Новый прибор"
  },
  "channel_profile": "total_3p",
  "expected_revision": 21
}
```

Ответ `201`:

```json
{
  "id": 4,
  "point_id": 4,
  "meter_source_id": 4,
  "channel_profile": "total_3p",
  "role": "primary",
  "valid_from": 1792658668,
  "valid_to": null,
  "replacement_note": null,
  "created_at": 1792658668,
  "configuration_revision": 22
}
```

**Пример: double_counting.** `/api/v2/points/2/bindings`

Тело запроса:

```json
{
  "new_meter": {
    "controller_key": "wb-map3e_",
    "device_id": "dev-new"
  },
  "expected_revision": 22
}
```

Ответ `409`:

```json
{
  "code": "double_counting",
  "message": "Прибор «dev-new» уже занят точкой учёта «Компрессор К-1». Сначала уберите у неё счётчик, затем ставьте прибор заново. У этой точки учёта уже стоит счётчик. Чтобы поставить другой, замените прибор у точки.",
  "ids": [
    2
  ]
}
```

#### `POST /api/v2/points/{point_id}/replace-meter`

Атомарно заменить прибор точки: действующая основная привязка закрывается, новая открывается с момента `at` — одной ревизией.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| meter_source_id | целое | один из двух | Существующий источник. |
| new_meter | объект | один из двух | `{device_id, controller_key?, display_name?}` — как в `…/bindings`. |
| at | целое | нет | Момент замены (unix-секунды); должен быть позже начала текущей привязки. |
| channel_profile | строка | нет | Профиль новой привязки. |
| replacement_note | строка | нет | Причина замены. |

**Ответ.** `200` новая привязка, `configuration_revision`, `meter_source_id`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет адреса нового прибора; нет действующей основной привязки; `at` не позже начала текущей привязки. |
| 409 | double_counting | Новый прибор занят другой точкой. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример.** `/api/v2/points/4/replace-meter`

Тело запроса:

```json
{
  "new_meter": {
    "controller_key": "wb-map3e_",
    "device_id": "dev-repl",
    "display_name": "Прибор на замену"
  },
  "replacement_note": "Заменён после поверки",
  "at": 1792658728,
  "expected_revision": 22
}
```

Ответ `200`:

```json
{
  "id": 5,
  "point_id": 4,
  "meter_source_id": 5,
  "channel_profile": "total_3p",
  "role": "primary",
  "valid_from": 1792658728,
  "valid_to": null,
  "replacement_note": "Заменён после поверки",
  "created_at": 1792658668,
  "configuration_revision": 23
}
```

#### `GET /api/v2/points/{point_id}/groups`

Членства точки в учётных группах (текущие и закрытые).

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` массив членств: `id`, `group_id`, `point_id`, `valid_from`, `valid_to`, `created_at`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Точки нет. |

**Пример.** `/api/v2/points/2/groups`

Ответ `200`:

```json
[
  {
    "id": 1,
    "group_id": 1,
    "point_id": 2,
    "valid_from": 0,
    "valid_to": null,
    "created_at": 1792658667
  }
]
```

#### `GET /api/v2/structure/points`

Данные для экрана «Структура»: все активные точки с местом, действующим прибором, признаками «размещена на плане», «ввод», «питается от» и список занятых приборов.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| q | строка | нет | Поиск без учёта регистра по коду, имени, MQTT ID прибора, серийному номеру, пути места, названиям групп и питающему узлу. |

**Ответ.** `200` `{points, occupied_devices}`. `points[]`: `point_id`, `code`, `name`, `enabled`, `location_id`, `location_path`, `bound`, `meter_device_id`, `meter_controller_key`, `meter_serial`, `is_emulator`, `channel_profile`, `meter_phase`, `placed_on_plan`, `is_input`, `fed_from_point_id`, `fed_from_point_name`, `measured_edge_id`, `measured_edge_label`. `occupied_devices[]`: `device_id`, `controller_key`, `is_emulator`, держатель (`point_id`, `point_name`, `archived`), `profiles`, `busy_phases`, `free_phases`, `fully_busy`.

**Пример.** `/api/v2/structure/points`

Ответ `200`:

```json
{
  "points": [
    {
      "point_id": 1,
      "code": "pt-in",
      "name": "Ввод",
      "enabled": true,
      "location_id": null,
      "location_path": null,
      "bound": true,
      "meter_device_id": "dev-in",
      "meter_controller_key": "wb8-main",
      "meter_serial": null,
      "is_emulator": false,
      "channel_profile": "total_3p",
      "meter_phase": null,
      "placed_on_plan": true,
      "is_input": true,
      "fed_from_point_id": null,
      "fed_from_point_name": null,
      "measured_edge_id": 1,
      "measured_edge_label": "Ввод → ЩР-1"
    },
    {
      "point_id": 2,
      "code": "pt-light",
      "name": "Освещение",
      "enabled": true,
      "location_id": null,
      "location_path": null,
      "bound": true,
      "meter_device_id": "dev-light",
      "meter_controller_key": "wb8-main",
      "meter_serial": null,
      "is_emulator": false,
      "channel_profile": "total_3p",
      "meter_phase": null,
      "placed_on_plan": true,
      "is_input": false,
      "fed_from_point_id": 1,
      "fed_from_point_name": "Ввод",
      "measured_edge_id": 3,
      "measured_edge_label": "ЩР-1 → Освещение"
    }
  ],
  "occupied_devices": [
    {
      "device_id": "dev-in",
      "controller_key": "wb8-main",
      "is_emulator": false,
      "point_id": 1,
      "point_name": "Ввод",
      "archived": false,
      "profiles": [
        "total_3p"
      ],
      "busy_phases": {
        "L1": {
          "point_id": 1,
          "point_name": "Ввод",
          "archived": false
        },
        "L2": {
          "point_id": 1,
          "point_name": "Ввод",
          "archived": false
        },
        "L3": {
          "point_id": 1,
          "point_name": "Ввод",
          "archived": false
        }
      },
      "free_phases": [],
      "fully_busy": true
    },
    {
      "device_id": "dev-light",
      "controller_key": "wb8-main",
      "is_emulator": false,
      "point_id": 2,
      "point_name": "Освещение",
      "archived": false,
      "profiles": [
        "total_3p"
      ],
      "busy_phases": {
        "L1": {
          "point_id": 2,
          "point_name": "Освещение",
          "archived": false
        },
        "L2": {
          "point_id": 2,
          "point_name": "Освещение",
          "archived": false
        },
        "L3": {
          "point_id": 2,
          "point_name": "Освещение",
          "archived": false
        }
      },
      "free_phases": [],
      "fully_busy": true
    }
  ]
}
```

_Списки в примере сокращены до двух элементов._

#### `GET /api/v2/snapshot`

Текущие значения точек из памяти службы (без обращений к истории): мощность, энергия, напряжения, токи, давность данных.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| point_ids | список через запятую | нет | Только эти точки; по умолчанию все неархивные. Несуществующая — `404`. |

**Ответ.** `200` `{as_of, points}`. Элемент: `point_id`, `code`, `name`, `enabled`, `binding_status` (`bound`/`unbound`), `is_emulator`, `device_id` (**новое в 0.25.0**: MQTT ID действующего прибора, `null` без привязки; поле добавлено, прежние не менялись), `channel_profile`, `device_status`, `device_status_reason`, `power_w`, `energy_total_kwh`, `voltage_v`/`current_a` (по фазам `L1`–`L3`), `frequency_hz`, `last_update_age_s`, `last_measurement_age_s`. Нет значения — `null`, а не `0`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | `point_ids` — не список целых. |
| 404 | not_found | Среди `point_ids` есть несуществующая точка (`ids` — список). |

**Пример.** `/api/v2/snapshot`

Ответ `200`:

```json
{
  "as_of": "2026-10-22T08:44:27Z",
  "points": [
    {
      "point_id": 1,
      "code": "pt-in",
      "name": "Ввод",
      "enabled": true,
      "binding_status": "bound",
      "is_emulator": false,
      "device_id": "dev-in",
      "channel_profile": "total_3p",
      "device_status": "ok",
      "device_status_reason": "",
      "power_w": 4800.0,
      "energy_total_kwh": 15210.4,
      "voltage_v": null,
      "current_a": null,
      "frequency_hz": null,
      "last_update_age_s": 0.23595523834228516,
      "last_measurement_age_s": 0.23595571517944336
    },
    {
      "point_id": 2,
      "code": "pt-light",
      "name": "Освещение",
      "enabled": true,
      "binding_status": "bound",
      "is_emulator": false,
      "device_id": "dev-light",
      "channel_profile": "total_3p",
      "device_status": "ok",
      "device_status_reason": "",
      "power_w": 760.0,
      "energy_total_kwh": 3210.9,
      "voltage_v": null,
      "current_a": null,
      "frequency_hz": null,
      "last_update_age_s": 0.2359914779663086,
      "last_measurement_age_s": 0.2359917163848877
    }
  ]
}
```

_Списки в примере сокращены до двух элементов._

#### `GET /api/v2/validation`

Сводка незавершённой настройки: точки без прибора, места, группы, плана; узлы без линий; линии без измерения.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` `{as_of, points_without_meter, points_without_location, points_without_group, points_without_plan, nodes_without_edges, edges_without_measurement, issues, total_issues}`. `issues[]` — те же проблемы одним списком, упорядоченным по важности (`kind`, `entity_type`, `entity_id`, `label`, `code`). Точка ввода не считается проблемой «без группы».

**Пример.** `/api/v2/validation`

Ответ `200`:

```json
{
  "as_of": "2026-10-22T08:44:27Z",
  "points_without_meter": [],
  "points_without_location": [
    {
      "point_id": 1,
      "code": "pt-in",
      "name": "Ввод"
    },
    {
      "point_id": 2,
      "code": "pt-light",
      "name": "Освещение"
    }
  ],
  "points_without_group": [],
  "points_without_plan": [],
  "nodes_without_edges": [],
  "edges_without_measurement": [
    {
      "edge_id": 2,
      "code": "pv3-e-1-3-1792658666990544",
      "name": null,
      "from_node_id": 1,
      "to_node_id": 3
    },
    {
      "edge_id": 5,
      "code": "pv3-e-2-6-1792658666994380",
      "name": null,
      "from_node_id": 2,
      "to_node_id": 6
    }
  ],
  "issues": [
    {
      "kind": "edge_without_measurement",
      "entity_type": "edge",
      "entity_id": 2,
      "label": "pv3-e-1-3-1792658666990544",
      "code": "pv3-e-1-3-1792658666990544"
    },
    {
      "kind": "edge_without_measurement",
      "entity_type": "edge",
      "entity_id": 5,
      "label": "pv3-e-2-6-1792658666994380",
      "code": "pv3-e-2-6-1792658666994380"
    }
  ],
  "total_issues": 5
}
```

_Списки в примере сокращены до двух элементов._

### 2.3. Места

Дерево мест (объект → здание → этаж → помещение → зона). В наборе v2 места в интерфейсе не используются (решение R11), но маршруты и данные остаются.

#### `GET /api/v2/locations`

Список мест.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| include_archived | `1` | нет | Показать и архивные. |

**Ответ.** `200` массив мест: `id`, `parent_id`, `kind`, `name`, `code`, `sort_order`, `archived_at`, `created_at`, `updated_at`.

**Пример.** `/api/v2/locations`

Ответ `200`:

```json
[]
```

**Пример.** `/api/v2/locations`

Ответ `200`:

```json
[
  {
    "id": 1,
    "parent_id": null,
    "kind": "room",
    "name": "Цех №1",
    "code": null,
    "sort_order": 0,
    "archived_at": null,
    "created_at": 1792658668,
    "updated_at": 1792658668
  }
]
```

#### `POST /api/v2/locations`

Создать место.

**Ревизия.** Создаёт запись и продвигает ревизию, но `expected_revision` **не требует** (создание новой сущности не может затереть чужую правку). В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| name | строка | да | Название. |
| kind | строка | да | `object`, `building`, `floor`, `room`, `zone` или `installation_point`. |
| parent_id | целое | нет | Родитель. |
| code | строка | нет | Код. |
| sort_order | целое | нет | Порядок среди соседей, по умолчанию 0. |

**Ответ.** `201` место и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Пустое имя, неверный `kind`, неизвестный родитель, глубина дерева больше 32. |

**Пример.** `/api/v2/locations`

Тело запроса:

```json
{
  "name": "Цех 1",
  "kind": "room"
}
```

Ответ `201`:

```json
{
  "id": 1,
  "parent_id": null,
  "kind": "room",
  "name": "Цех 1",
  "code": null,
  "sort_order": 0,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 18
}
```

**Пример: bad_request.** `/api/v2/locations`

Тело запроса:

```json
{
  "kind": "room"
}
```

Ответ `400`:

```json
{
  "code": "bad_request",
  "message": "имя не может быть пустым",
  "fields": [
    "name",
    "kind"
  ]
}
```

#### `GET /api/v2/locations/{location_id}`

Одно место.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` место.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Места нет. |

**Пример.** `/api/v2/locations/1`

Ответ `200`:

```json
{
  "id": 1,
  "parent_id": null,
  "kind": "room",
  "name": "Цех №1",
  "code": null,
  "sort_order": 0,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668
}
```

#### `PATCH /api/v2/locations/{location_id}`

Переименовать, сменить код или родителя, архивировать место.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| name | строка | нет | Новое имя. |
| code | строка | нет | Новый код. |
| parent_id | целое или null | нет | Новый родитель. |
| archived | `true` | нет | Архивировать. |

**Ответ.** `200` место и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нечего менять; значение не прошло проверку. |
| 404 | not_found | Места нет. |
| 409 | cycle_conflict | Перенос создал бы цикл в дереве. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример.** `/api/v2/locations/1`

Тело запроса:

```json
{
  "name": "Цех №1",
  "expected_revision": 18
}
```

Ответ `200`:

```json
{
  "id": 1,
  "parent_id": null,
  "kind": "room",
  "name": "Цех №1",
  "code": null,
  "sort_order": 0,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 19
}
```

**Пример: revision_conflict.** `/api/v2/locations/1`

Тело запроса:

```json
{
  "name": "Цех",
  "expected_revision": 18
}
```

Ответ `409`:

```json
{
  "code": "revision_conflict",
  "message": "ревизия конфигурации устарела: ожидалась 18, сейчас 19 — кто-то уже сохранил более новые изменения; перечитайте текущее состояние и повторите",
  "fields": [
    "expected_revision"
  ],
  "ids": [
    1
  ]
}
```

### 2.4. Учётные группы

Дерево групп с версионируемым составом: точка может состоять в нескольких группах, членство имеет интервал `valid_from`–`valid_to`. Удаления групп нет (история составов сохраняется); меняется только родитель.

#### `GET /api/v2/groups`

Список групп.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| parent_id | целое или `null` | нет | Только прямые дети этой группы; `null` — корневые. |

**Ответ.** `200` массив групп: `id`, `name`, `category`, `parent_id`, `color`, `created_at`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | `parent_id` — не целое и не `null`. |

**Пример.** `/api/v2/groups`

Ответ `200`:

```json
[
  {
    "id": 1,
    "name": "Цех",
    "category": null,
    "parent_id": null,
    "color": "#2f9e8f",
    "created_at": 1792658667
  }
]
```

#### `POST /api/v2/groups`

Создать группу.

**Ревизия.** Создаёт запись и продвигает ревизию, но `expected_revision` **не требует** (создание новой сущности не может затереть чужую правку). В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| name | строка | да | Название. |
| category | строка | нет | Категория (свободная строка): ребёнок обязан иметь ту же категорию, что родитель, если обе заданы. |
| parent_id | целое | нет | Родитель. |
| color | строка | нет | Цвет `#rrggbb`; по умолчанию подбирается. |

**Ответ.** `201` группа и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Пустое или повторяющееся имя, неизвестный родитель. |
| 409 | category_conflict | Категория ребёнка не совпадает с категорией родителя. |
| 409 | cycle_conflict | Родитель создал бы цикл. |

**Пример.** `/api/v2/groups`

Тело запроса:

```json
{
  "name": "Участок А",
  "category": "area"
}
```

Ответ `201`:

```json
{
  "id": 2,
  "name": "Участок А",
  "category": "area",
  "parent_id": null,
  "color": "#3b82f6",
  "created_at": 1792658668,
  "configuration_revision": 24
}
```

**Пример.** `/api/v2/groups`

Тело запроса:

```json
{
  "name": "Подгруппа",
  "category": "area",
  "parent_id": 2
}
```

Ответ `201`:

```json
{
  "id": 3,
  "name": "Подгруппа",
  "category": "area",
  "parent_id": 2,
  "color": "#f59e0b",
  "created_at": 1792658668,
  "configuration_revision": 25
}
```

#### `GET /api/v2/groups/{group_id}`

Одна группа.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` группа.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Группы нет. |

**Пример.** `/api/v2/groups/1`

Ответ `200`:

```json
{
  "id": 1,
  "name": "Цех",
  "category": null,
  "parent_id": null,
  "color": "#2f9e8f",
  "created_at": 1792658667
}
```

#### `PATCH /api/v2/groups/{group_id}`

Перенести группу под другого родителя (`parent_id`). Других полей PATCH не принимает: переименования и удаления группы нет.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| parent_id | целое или null | да | Новый родитель; `null` — в корень. |

**Ответ.** `200` группа и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет `parent_id`. |
| 404 | not_found | Группы нет. |
| 409 | cycle_conflict | Перенос создал бы цикл (в примере — группа под своей же дочерней). |
| 409 | category_conflict | Категории родителя и ребёнка различаются. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример: цикл не возникает: родитель — другая группа.** `/api/v2/groups/2`

Тело запроса:

```json
{
  "parent_id": 1,
  "expected_revision": 25
}
```

Ответ `200`:

```json
{
  "id": 2,
  "name": "Участок А",
  "category": "area",
  "parent_id": 1,
  "color": "#3b82f6",
  "created_at": 1792658668,
  "configuration_revision": 26
}
```

**Пример: cycle_conflict.** `/api/v2/groups/1`

Тело запроса:

```json
{
  "parent_id": 2,
  "expected_revision": 26
}
```

Ответ `409`:

```json
{
  "code": "cycle_conflict",
  "message": "Перенос создал бы цикл в дереве групп",
  "ids": [
    1
  ]
}
```

#### `GET /api/v2/groups/{group_id}/members`

Состав группы (прямые участники).

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| include_closed | `1` | нет | С закрытыми членствами (`valid_to` не `null`). |

**Ответ.** `200` массив членств: `id`, `group_id`, `point_id`, `valid_from`, `valid_to`, `created_at`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Группы нет. |

**Пример.** `/api/v2/groups/1/members`

Ответ `200`:

```json
[
  {
    "id": 1,
    "group_id": 1,
    "point_id": 2,
    "valid_from": 0,
    "valid_to": null,
    "created_at": 1792658667
  },
  {
    "id": 2,
    "group_id": 1,
    "point_id": 3,
    "valid_from": 0,
    "valid_to": null,
    "created_at": 1792658667
  }
]
```

#### `POST /api/v2/groups/{group_id}/members`

Добавить точку в группу с момента `valid_from`.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| point_id | целое | да | Точка. |
| valid_from | целое | нет | Начало членства (unix-секунды), по умолчанию «сейчас». |

**Ответ.** `201` членство и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет `point_id`; значение не прошло проверку. |
| 404 | not_found | Группы или точки нет. |
| 409 | conflict | Точка уже состоит в этой группе. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример.** `/api/v2/groups/2/members`

Тело запроса:

```json
{
  "point_id": 1,
  "expected_revision": 26
}
```

Ответ `201`:

```json
{
  "id": 3,
  "group_id": 2,
  "point_id": 1,
  "valid_from": 1792658668,
  "valid_to": null,
  "created_at": 1792658668,
  "configuration_revision": 27
}
```

**Пример: conflict (уже состоит).** `/api/v2/groups/2/members`

Тело запроса:

```json
{
  "point_id": 1,
  "expected_revision": 27
}
```

Ответ `409`:

```json
{
  "code": "conflict",
  "message": "Точка 1 уже состоит в группе 2 (открытое членство)",
  "ids": [
    2,
    1
  ]
}
```

#### `DELETE /api/v2/groups/{group_id}/members/{point_id}`

Вывести точку из группы (членство закрывается моментом `at`, история остаётся).

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | Можно и в теле запроса. |
| at | целое | нет | Момент исключения (unix-секунды), по умолчанию «сейчас»; должен быть позже начала членства. |

**Ответ.** `204`, тело пустое; новая ревизия — в заголовке `X-Configuration-Revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | `at` — не целое или не позже начала членства. |
| 404 | not_found | Точка не состоит в группе. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример.** `/api/v2/groups/2/members/1?expected_revision=27&at=1792658728`

Ответ `204`, тело пустое; новая ревизия — в заголовке `X-Configuration-Revision`.

#### `GET /api/v2/groups/{group_id}/effective-members`

Эффективный состав: сама группа и все дочерние, точка один раз; `via` показывает, по какому пути она включена.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| at | целое | нет | Момент времени (unix-секунды); по умолчанию «сейчас». |

**Ответ.** `200` `{group_id, as_of, points: [{point_id, code, name, via}]}`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | `at` — не целое. |
| 404 | not_found | Группы нет. |

**Пример.** `/api/v2/groups/1/effective-members`

Ответ `200`:

```json
{
  "group_id": 1,
  "as_of": 1792658667,
  "points": [
    {
      "point_id": 2,
      "code": "pt-light",
      "name": "Освещение",
      "via": [
        1
      ]
    },
    {
      "point_id": 3,
      "code": "pt-sock",
      "name": "Розетки",
      "via": [
        1
      ]
    }
  ]
}
```

### 2.5. Электрическая схема (узлы и линии)

Схема — дерево: **узел** (`source` — ввод, `panel` — щит, `junction` — ответвление, `load` — нагрузка) и направленные **линии** между узлами. Точка учёта измеряет линию (`primary_point_id`). Линия бывает `draft` (черновик), `published` (действует с `valid_from`) и закрытой (`valid_to` задан). Правила леса — один источник питания у узла, без циклов — проверяются при подключении и публикации и дают `409 topology_conflict`. «План v3» работает через составные операции (`connect`, `add-consumer`, `meters`, `attach-new-point`): каждая — одна ревизия и целиком либо ничего.

#### `GET /api/v2/topology/nodes`

Список узлов схемы.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| include_archived | `1` | нет | Показать и архивные. |

**Ответ.** `200` массив узлов: `id`, `code`, `name`, `kind`, `location_id`, `archived_at`, `created_at`, `updated_at`.

**Пример.** `/api/v2/topology/nodes`

Ответ `200`:

```json
[
  {
    "id": 4,
    "code": "pv3-load-1792658666991869",
    "name": "Освещение",
    "kind": "load",
    "location_id": null,
    "archived_at": null,
    "created_at": 1792658666,
    "updated_at": 1792658666
  },
  {
    "id": 5,
    "code": "pv3-load-1792658666993105",
    "name": "Розетки",
    "kind": "load",
    "location_id": null,
    "archived_at": null,
    "created_at": 1792658666,
    "updated_at": 1792658666
  }
]
```

_Списки в примере сокращены до двух элементов._

#### `POST /api/v2/topology/nodes`

Создать узел.

**Ревизия.** Создаёт запись и продвигает ревизию, но `expected_revision` **не требует** (создание новой сущности не может затереть чужую правку). В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| code | строка | да | Уникальный код. |
| name | строка | да | Название. |
| kind | строка | да | `source`, `panel`, `junction` или `load`. |
| location_id | целое | нет | Место. |

**Ответ.** `201` узел и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Пустой/повторяющийся код, пустое имя, неверный `kind`. |

**Пример.** `/api/v2/topology/nodes`

Тело запроса:

```json
{
  "code": "shr3",
  "name": "ЩР-3",
  "kind": "panel"
}
```

Ответ `201`:

```json
{
  "id": 7,
  "code": "shr3",
  "name": "ЩР-3",
  "kind": "panel",
  "location_id": null,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 29
}
```

**Пример.** `/api/v2/topology/nodes`

Тело запроса:

```json
{
  "code": "shr4",
  "name": "ЩР-4",
  "kind": "panel"
}
```

Ответ `201`:

```json
{
  "id": 8,
  "code": "shr4",
  "name": "ЩР-4",
  "kind": "panel",
  "location_id": null,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 34
}
```

#### `GET /api/v2/topology/nodes/{node_id}`

Один узел.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` узел.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Узла нет. |

**Пример.** `/api/v2/topology/nodes/2`

Ответ `200`:

```json
{
  "id": 2,
  "code": "shr1",
  "name": "ЩР-1",
  "kind": "panel",
  "location_id": null,
  "archived_at": null,
  "created_at": 1792658666,
  "updated_at": 1792658666
}
```

#### `PATCH /api/v2/topology/nodes/{node_id}`

Переименовать узел, сменить вид и место либо архивировать (`archived: true`). Архивация узла с действующими линиями отказывает, если не задано `retire_edges`.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| name | строка | нет | Новое имя. |
| kind | строка | нет | Новый вид. В `source` нельзя, если в узел уже входит линия; в `load` — если из него выходят линии (`409 topology_conflict`). |
| location_id | целое или null | нет | Новое место. |
| location_text | строка | нет | Место по названию: найдётся существующее или создастся новое. |
| archived | `true` | нет | Архивировать узел; его значки снимаются с планов. |
| retire_edges | булево | нет | Вместе с `archived`: вывести из работы все действующие линии узла. Без него при наличии линий — `409 conflict` и список линий в `path`. |

**Ответ.** `200` узел и `configuration_revision`; при `retire_edges` ещё `retired_edge_ids`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нечего менять; значение не прошло проверку. |
| 404 | not_found | Узла нет. |
| 409 | conflict | Архивация узла с действующими линиями без `retire_edges` (`path` — линии: `edge_id`, `label`, концы). |
| 409 | topology_conflict | Смена вида нарушает правила схемы. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример.** `/api/v2/topology/nodes/7`

Тело запроса:

```json
{
  "name": "ЩР-3 (склад)",
  "location_id": 1,
  "expected_revision": 29
}
```

Ответ `200`:

```json
{
  "id": 7,
  "code": "shr3",
  "name": "ЩР-3 (склад)",
  "kind": "panel",
  "location_id": 1,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 30
}
```

#### `GET /api/v2/topology/nodes/{node_id}/lines`

Линии узла (входящая и отходящие) с точкой учёта и прибором на каждой.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` `{lines: [...]}`. Элемент: `edge_id`, `direction` (`in`/`out`), `other_node_id`, `other_node_name`, `code`, `name`, `primary_point_id`/`_code`/`_name`, `phase_count`, `rated_current_a`, `cable_note`, `meter_device_id`, `channel_profile`, `phase`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Узла нет. |

**Пример.** `/api/v2/topology/nodes/2/lines`

Ответ `200`:

```json
{
  "lines": [
    {
      "edge_id": 1,
      "direction": "in",
      "other_node_id": 1,
      "other_node_name": "Ввод",
      "code": "pv3-e-1-2-1792658666989207",
      "name": null,
      "primary_point_id": 1,
      "primary_point_code": "pt-in",
      "primary_point_name": "Ввод",
      "phase_count": null,
      "rated_current_a": null,
      "cable_note": null,
      "meter_device_id": "dev-in",
      "channel_profile": "total_3p",
      "phase": null
    },
    {
      "edge_id": 3,
      "direction": "out",
      "other_node_id": 4,
      "other_node_name": "Освещение",
      "code": "pv3-e-2-4-1792658666991951",
      "name": null,
      "primary_point_id": 2,
      "primary_point_code": "pt-light",
      "primary_point_name": "Освещение",
      "phase_count": null,
      "rated_current_a": null,
      "cable_note": null,
      "meter_device_id": "dev-light",
      "channel_profile": "total_3p",
      "phase": null
    }
  ]
}
```

_Списки в примере сокращены до двух элементов._

**Пример.** `/api/v2/topology/nodes/3/lines`

Ответ `200`:

```json
{
  "lines": [
    {
      "edge_id": 2,
      "direction": "in",
      "other_node_id": 1,
      "other_node_name": "Ввод",
      "code": "pv3-e-1-3-1792658667770163",
      "name": null,
      "primary_point_id": null,
      "primary_point_code": null,
      "primary_point_name": null,
      "phase_count": null,
      "rated_current_a": null,
      "cable_note": null,
      "meter_device_id": null,
      "channel_profile": null,
      "phase": null
    },
    {
      "edge_id": 8,
      "direction": "out",
      "other_node_id": 9,
      "other_node_name": "Вентиляция",
      "code": "pv3-e-3-9-1792658668045624",
      "name": null,
      "primary_point_id": 5,
      "primary_point_code": "pv3-att-8-1792658668046",
      "primary_point_name": "Вентиляция",
      "phase_count": null,
      "rated_current_a": 16.0,
      "cable_note": null,
      "meter_device_id": "dev-att",
      "channel_profile": "total_3p",
      "phase": null
    }
  ]
}
```

_Списки в примере сокращены до двух элементов._

#### `POST /api/v2/topology/nodes/{node_id}/meters`

«+ Счётчик» в карточке узла: свободный прибор → точка учёта → привязка → «измеряет эту линию» — одной ревизией. Недостающие линия и нагрузка создаются тут же.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| device_id | строка | да | MQTT ID прибора. |
| measures | строка | да (кроме `mode=phases`) | `input` — входящая линия узла (если её нет, создаётся от `fed_from_node_id`); `outgoing` — отходящая. Допустимое зависит от вида узла: ввод — только `outgoing`, нагрузка — только `input`, щит и ответвление — оба. |
| line_id | целое | нет | `outgoing`: существующая линия. |
| to_node_id | целое | нет | `outgoing`: существующий узел-получатель (линия создаётся, если её не было). |
| new_load_name | строка | нет | `outgoing`: создать новую нагрузку с таким именем и линию к ней. |
| fed_from_node_id | целое | нет | `input`: откуда питается узел, если входящей линии ещё нет. |
| name | строка | нет | Имя точки; по умолчанию — имя прибора. |
| channel_profile | строка | нет | `total_3p` (по умолчанию) или `phase_l1`/`l2`/`l3`. |
| at | целое | нет | Начало привязки (unix-секунды). |
| mode / phases | `"phases"` / массив | нет | «По фазам» (`measures=outgoing`): `[{phase: "L1", line_id \| to_node_id \| new_load_name, name?}, …, {phase: "L3", skip: true}]` — каждая используемая фаза получает свою линию (`phase_count = 1`), точку и привязку `phase_lN`. |

**Ответ.** `201` `{point, edge, node, created_edge, created_node, meter_source_id, measures, configuration_revision}`; `node` — новая нагрузка или `null`. В режиме «по фазам» — `{point: null, edge: null, node: null, created_edge, created_node, meter_source_id, measures, mode: "phases", phases: [{phase, point, edge, node, created_edge, created_node}], configuration_revision}`. `200` с `attached: "already"` — этот же прибор уже стоит на этой линии, ничего не записано.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Не выбран прибор; недопустимое `measures` для вида узла; нет пофазных счётчиков энергии в MQTT; пустой список фаз; одна линия на две фазы. |
| 404 | not_found | Узла (или линии, узла-получателя) нет. |
| 409 | double_counting | Прибор (или фаза) уже занят другой точкой. |
| 409 | already_metered | Линия уже измеряется другой точкой. |
| 409 | topology_conflict | Правила леса: цикл, второй источник питания. |
| 409 | revision_conflict | `expected_revision` устарела. |

При любом отказе не создано ничего — ни линии, ни нагрузки, ни точки, ни привязки, ни ревизии.

**Пример.** `/api/v2/topology/nodes/3/meters`

Тело запроса:

```json
{
  "device_id": "dev-n2",
  "measures": "outgoing",
  "new_load_name": "Насос",
  "expected_revision": 37
}
```

Ответ `201`:

```json
{
  "point": {
    "id": 6,
    "code": "pv3-m-3-1792658668049310",
    "name": "dev-n2",
    "description": null,
    "installation_location_id": null,
    "installation_note": null,
    "enabled": true,
    "archived_at": null,
    "created_at": 1792658668,
    "updated_at": 1792658668
  },
  "edge": {
    "id": 9,
    "code": "pv3-e-3-10-1792658668049126",
    "name": null,
    "from_node_id": 3,
    "to_node_id": 10,
    "primary_point_id": 6,
    "phase_count": null,
    "rated_current_a": null,
    "cable_note": null,
    "state": "published",
    "valid_from": 1792658668,
    "valid_to": null,
    "archived_at": null,
    "created_at": 1792658668,
    "updated_at": 1792658668
  },
  "node": {
    "id": 10,
    "code": "pv3-load-1792658668049035",
    "name": "Насос",
    "kind": "load",
    "location_id": null,
    "archived_at": null,
    "created_at": 1792658668,
    "updated_at": 1792658668
  },
  "created_edge": true,
  "created_node": true,
  "meter_source_id": 7,
  "measures": "outgoing",
  "configuration_revision": 38
}
```

#### `POST /api/v2/topology/nodes/{node_id}/add-consumer`

Добавить нагрузку к узлу: создаются узел-нагрузка и опубликованная линия к нему.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| name | строка | да | Название нагрузки. |
| rated_current_a | число | нет | Номинальный ток линии, А. |
| cable_note | строка | нет | Кабель/автомат. |

**Ответ.** `201` `{node, edge, configuration_revision}`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Пустое имя; значение не прошло проверку. |
| 409 | topology_conflict | Узел нельзя питать так (например, он сам нагрузка). |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример.** `/api/v2/topology/nodes/3/add-consumer`

Тело запроса:

```json
{
  "name": "Вентиляция",
  "rated_current_a": 16,
  "expected_revision": 35
}
```

Ответ `201`:

```json
{
  "node": {
    "id": 9,
    "code": "pv3-load-1792658668045473",
    "name": "Вентиляция",
    "kind": "load",
    "location_id": null,
    "archived_at": null,
    "created_at": 1792658668,
    "updated_at": 1792658668
  },
  "edge": {
    "id": 8,
    "code": "pv3-e-3-9-1792658668045624",
    "name": null,
    "from_node_id": 3,
    "to_node_id": 9,
    "primary_point_id": null,
    "phase_count": null,
    "rated_current_a": 16.0,
    "cable_note": null,
    "state": "published",
    "valid_from": 1792658668,
    "valid_to": null,
    "archived_at": null,
    "created_at": 1792658668,
    "updated_at": 1792658668
  },
  "configuration_revision": 36
}
```

#### `POST /api/v2/topology/nodes/{node_id}/balance`

Баланс одного узла (щита): вход против выходов за период.

**Ревизия.** Расчёт, ничего не пишет. Весь расчёт идёт в одном снимке БД (A43): конкурентная запись не попадает в уже начатый запрос.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| from | число или строка | да | Начало периода (см. «Время и периоды»). |
| to | число или строка | да | Конец периода, строго позже `from`. |
| timezone | строка | нет | Часовой пояс IANA (по умолчанию `UTC`): в нём читаются строки-даты в `from`/`to`, по нему строятся часовые границы и подписи `period.from_local`/`period.to_local`. Неизвестное имя при строке-дате — `400`, поле `timezone`. |
| structure_mode | строка | нет | `current` (по умолчанию) — по текущей схеме; `as_was` — по схеме, как она была на каждом интервале (история с 0.22.0). |
| configuration_revision_id | целое | нет | Пометить результат ревизией (должна существовать). |

**Ответ.** `200` результат расчёта (см. «Результат расчёта») с `metric = "imbalance"`, `mode = "balance"` и полями узла: `node_id`, `input`, `outputs`, `unmetered_branches`, `boundary_coverage`, `unavailable_reason`, `structure_mode`, `assumed_legacy`, `journal_started_at`, `structure_intervals`. Если баланс недоступен — `value: null`, известные части всё равно возвращаются.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Не разобрано `from`/`to`; `to` не позже `from`; неизвестный `timezone` при строке-дате; неверный `structure_mode`/`configuration_revision_id`. |
| 404 | not_found | Узла или ревизии нет. |
| 500 | internal | Нарушен контракт результата (ошибка службы, в журнал пишется подробность). |

**Пример.** `/api/v2/topology/nodes/2/balance`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow"
}
```

Ответ `200`:

```json
{
  "metric": "imbalance",
  "unit": "kWh",
  "mode": "balance",
  "value": 1610.08,
  "known_value": 1610.08,
  "availability": "complete",
  "quality_flags": [],
  "structure_quality": "verified",
  "source": "calculated",
  "expected_count": 3,
  "valid_count": 3,
  "missing_ids": [],
  "configuration_revision_id": 41,
  "configuration_revision_ids": [
    41
  ],
  "as_of": "2026-10-22T08:44:28.095108Z",
  "period": {
    "from": "1790812800",
    "to": "1792656000",
    "timezone": "Europe/Moscow",
    "from_local": "2026-10-01T03:00:00+03:00",
    "to_local": "2026-10-22T11:00:00+03:00"
  },
  "explanation": {
    "input_point_ids": [
      1
    ],
    "output_point_ids": [
      2,
      3
    ],
    "percentage": 46.77,
    "percentage_reason": null,
    "input_value": 3442.24,
    "output_value": 1832.16
  },
  "node_id": 2,
  "input": {
    "edge_id": 1,
    "point_id": 1,
    "name": "ЩР-1",
    "result": {
      "metric": "energy_import",
      "unit": "kWh",
      "mode": "measured",
      "value": 3442.24,
      "known_value": 3442.24,
      "availability": "complete",
      "quality_flags": [],
      "structure_quality": "verified",
      "source": "measured",
      "expected_count": 512,
      "valid_count": 512,
      "missing_ids": [],
      "configuration_revision_id": 41,
      "configuration_revision_ids": [
        41
      ],
      "as_of": "2026-10-22T08:44:28.095061Z",
      "period": {
        "from": "1790812800",
        "to": "1792656000",
        "timezone": "Europe/Moscow",
        "from_local": "2026-10-01T03:00:00+03:00",
        "to_local": "2026-10-22T11:00:00+03:00"
      },
      "explanation": {
        "point_id": 1
      }
    }
  },
  "outputs": [
    {
      "edge_id": 3,
      "point_id": 2,
      "to_node_id": 4,
      "name": "Освещение",
      "result": {
        "metric": "energy_import",
        "unit": "kWh",
        "mode": "measured",
        "value": 499.68,
        "known_value": 499.68,
        "availability": "complete",
        "quality_flags": [],
        "structure_quality": "verified",
        "source": "measured",
        "expected_count": 512,
        "valid_count": 512,
        "missing_ids": [],
        "configuration_revision_id": 41,
        "configuration_revision_ids": [
          41
        ],
        "as_of": "2026-10-22T08:44:28.095090Z",
        "period": {
          "from": "1790812800",
          "to": "1792656000",
          "timezone": "Europe/Moscow",
          "from_local": "2026-10-01T03:00:00+03:00",
          "to_local": "2026-10-22T11:00:00+03:00"
        },
        "explanation": {
          "point_id": 2
        }
      }
    },
    {
      "edge_id": 4,
      "point_id": 3,
      "to_node_id": 5,
      "name": "Розетки",
      "result": {
        "metric": "energy_import",
        "unit": "kWh",
        "mode": "measured",
        "value": 1332.48,
        "known_value": 1332.48,
        "availability": "complete",
        "quality_flags": [],
        "structure_quality": "verified",
        "source": "measured",
        "expected_count": 512,
        "valid_count": 512,
        "missing_ids": [],
        "configuration_revision_id": 41,
        "configuration_revision_ids": [
          41
        ],
        "as_of": "2026-10-22T08:44:28.095098Z",
        "period": {
          "from": "1790812800",
          "to": "1792656000",
          "timezone": "Europe/Moscow",
          "from_local": "2026-10-01T03:00:00+03:00",
          "to_local": "2026-10-22T11:00:00+03:00"
        },
        "explanation": {
          "point_id": 3
        }
      }
    }
  ],
  "unmetered_branches": [
    {
      "edge_id": 5,
      "to_node_id": 6,
      "name": "Станок"
    }
  ],
  "boundary_coverage": "has_unmetered_branches",
  "unavailable_reason": null,
  "structure_mode": "current",
  "assumed_legacy": false,
  "journal_started_at": 1792658667,
  "structure_intervals": [
    {
      "from": 1790812800,
      "to": 1792656000,
      "assumed_legacy": false
    }
  ]
}
```

#### `GET /api/v2/topology/edges`

Список линий: по умолчанию черновики.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| state | строка | нет | `draft` (по умолчанию) или `published` — действующие линии. |

**Ответ.** `200` массив линий: `id`, `code`, `name`, `from_node_id`, `to_node_id`, `primary_point_id`, `phase_count`, `rated_current_a`, `cable_note`, `state`, `valid_from`, `valid_to`, `archived_at`, `created_at`, `updated_at`.

**Пример: черновики (по умолчанию); примера с опубликованными см. ниже.** `/api/v2/topology/edges`

Ответ `200`:

```json
[]
```

**Пример: state=published.** `/api/v2/topology/edges?state=published`

Ответ `200`:

```json
[
  {
    "id": 1,
    "code": "pv3-e-1-2-1792658667768215",
    "name": null,
    "from_node_id": 1,
    "to_node_id": 2,
    "primary_point_id": 1,
    "phase_count": null,
    "rated_current_a": null,
    "cable_note": null,
    "state": "published",
    "valid_from": 1792658667,
    "valid_to": null,
    "archived_at": null,
    "created_at": 1792658667,
    "updated_at": 1792658667
  },
  {
    "id": 2,
    "code": "pv3-e-1-3-1792658667770163",
    "name": null,
    "from_node_id": 1,
    "to_node_id": 3,
    "primary_point_id": null,
    "phase_count": null,
    "rated_current_a": null,
    "cable_note": null,
    "state": "published",
    "valid_from": 1792658667,
    "valid_to": null,
    "archived_at": null,
    "created_at": 1792658667,
    "updated_at": 1792658667
  }
]
```

_Списки в примере сокращены до двух элементов._

#### `POST /api/v2/topology/edges`

Создать **черновик** линии. Действующей она становится после `POST /api/v2/topology/publish`.

**Ревизия.** Создаёт запись и продвигает ревизию, но `expected_revision` **не требует** (создание новой сущности не может затереть чужую правку). В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| from_node_id | целое | да | Откуда. |
| to_node_id | целое | да | Куда. |
| code | строка | нет | Код. |
| name | строка | нет | Название. |
| primary_point_id | целое | нет | Точка, измеряющая линию. |
| phase_count | целое | нет | Число фаз (1 или 3). |
| rated_current_a | число | нет | Номинальный ток, А. |
| cable_note | строка | нет | Кабель/автомат. |

**Ответ.** `201` линия (`state: "draft"`) и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Неизвестные узлы, линия на себя, неверные числа. |

**Пример: черновик связи.** `/api/v2/topology/edges`

Тело запроса:

```json
{
  "from_node_id": 1,
  "to_node_id": 7,
  "name": "Л-ЩР3"
}
```

Ответ `201`:

```json
{
  "id": 6,
  "code": null,
  "name": "Л-ЩР3",
  "from_node_id": 1,
  "to_node_id": 7,
  "primary_point_id": null,
  "phase_count": null,
  "rated_current_a": null,
  "cable_note": null,
  "state": "draft",
  "valid_from": null,
  "valid_to": null,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 31
}
```

#### `GET /api/v2/topology/edges/{edge_id}`

Одна линия.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` линия.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Линии нет. |

**Пример.** `/api/v2/topology/edges/1`

Ответ `200`:

```json
{
  "id": 1,
  "code": "pv3-e-1-2-1792658666989207",
  "name": null,
  "from_node_id": 1,
  "to_node_id": 2,
  "primary_point_id": 1,
  "phase_count": null,
  "rated_current_a": null,
  "cable_note": null,
  "state": "published",
  "valid_from": 1792658666,
  "valid_to": null,
  "archived_at": null,
  "created_at": 1792658666,
  "updated_at": 1792658666
}
```

#### `PATCH /api/v2/topology/edges/{edge_id}`

Поменять счётчик линии (`primary_point_id`), имя, номинальный ток, примечание — либо вывести линию из работы (`retire`).

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| primary_point_id | целое или null | нет | Точка, измеряющая линию; `null` снимает счётчик. |
| name | строка | нет | Название. |
| rated_current_a | число | нет | Номинальный ток, А. |
| cable_note | строка | нет | Кабель/автомат. |
| retire | `true` | нет | Вывести линию из работы с момента `at`; её виды на планах удаляются. |
| at | целое | нет | С `retire`: момент вывода (unix-секунды). |

**Ответ.** `200` линия и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нечего менять; точка уже измеряет другую линию; линия не действует. |
| 404 | not_found | Линии нет. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример.** `/api/v2/topology/edges/6`

Тело запроса:

```json
{
  "rated_current_a": 63,
  "cable_note": "ВВГ 4x16",
  "expected_revision": 31
}
```

Ответ `200`:

```json
{
  "id": 6,
  "code": null,
  "name": "Л-ЩР3",
  "from_node_id": 1,
  "to_node_id": 7,
  "primary_point_id": null,
  "phase_count": null,
  "rated_current_a": 63.0,
  "cable_note": "ВВГ 4x16",
  "state": "draft",
  "valid_from": null,
  "valid_to": null,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 32
}
```

**Пример: вывод линии из работы.** `/api/v2/topology/edges/6`

Тело запроса:

```json
{
  "retire": true,
  "expected_revision": 38
}
```

Ответ `200`:

```json
{
  "id": 6,
  "code": null,
  "name": "Л-ЩР3",
  "from_node_id": 1,
  "to_node_id": 7,
  "primary_point_id": null,
  "phase_count": null,
  "rated_current_a": 63.0,
  "cable_note": "ВВГ 4x16",
  "state": "published",
  "valid_from": 1792658668,
  "valid_to": 1792658668,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 39
}
```

#### `POST /api/v2/topology/edges/connect`

Соединить два узла **сразу действующей** линией (без черновика).

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| from_node_id | целое | да | Откуда. |
| to_node_id | целое | да | Куда. |
| name / code / cable_note | строка | нет | Подписи линии. |
| rated_current_a | число | нет | Номинальный ток, А. |
| phase_count | целое | нет | Число фаз. |
| primary_point_id | целое | нет | Точка, измеряющая линию. |

**Ответ.** `201` линия (`state: "published"`) и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет `from_node_id`/`to_node_id`; значение не прошло проверку. |
| 409 | topology_conflict | У получателя уже есть источник питания, цикл, ввод не может получать питание. `message` называет узлы по именам. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример: успешное подключение.** `/api/v2/topology/edges/connect`

Тело запроса:

```json
{
  "from_node_id": 1,
  "to_node_id": 8,
  "name": "Л-ЩР4",
  "rated_current_a": 40,
  "expected_revision": 34
}
```

Ответ `201`:

```json
{
  "id": 7,
  "code": "pv3-e-1-8-1792658668044356",
  "name": "Л-ЩР4",
  "from_node_id": 1,
  "to_node_id": 8,
  "primary_point_id": null,
  "phase_count": null,
  "rated_current_a": 40.0,
  "cable_note": null,
  "state": "published",
  "valid_from": 1792658668,
  "valid_to": null,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 35
}
```

**Пример: topology_conflict (второй источник питания).** `/api/v2/topology/edges/connect`

Тело запроса:

```json
{
  "from_node_id": 3,
  "to_node_id": 7,
  "name": "Резерв",
  "expected_revision": 33
}
```

Ответ `409`:

```json
{
  "code": "topology_conflict",
  "message": "«ЩР-3 (склад)» уже получает питание от «Ввод» — у узла может быть только один источник; чтобы подключить «ЩР-2», сначала уберите действующую линию от «Ввод»",
  "ids": [
    3,
    7
  ]
}
```

#### `POST /api/v2/topology/edges/{edge_id}/attach-new-point`

Поставить счётчик на неизмеренную линию: создать точку из прибора, привязать и назначить измеряющей — одной ревизией. Повтор безопасен.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| device_id | строка | да | MQTT ID прибора. |
| name | строка | нет | Имя точки; по умолчанию — имя узла-получателя. |
| code | строка | нет | Код точки; по умолчанию генерируется. |
| channel_profile | строка | нет | `total_3p` (по умолчанию) или `phase_lN`. |
| at | целое | нет | Начало привязки. |

**Ответ.** `201` линия с `configuration_revision`, `point` и `meter_source_id`. Если этот прибор уже измеряет эту линию — `200` с `attached: "already"` и `point_id`, ничего не записано.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет `device_id` или он неверен. |
| 404 | not_found | Линии нет. |
| 409 | already_metered | Линию уже измеряет другая точка — меняют её явным `PATCH …/edges/{id}`. |
| 409 | double_counting | Прибор занят другой точкой. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример.** `/api/v2/topology/edges/8/attach-new-point`

Тело запроса:

```json
{
  "device_id": "dev-att",
  "name": "Вентиляция",
  "expected_revision": 36
}
```

Ответ `201`:

```json
{
  "id": 8,
  "code": "pv3-e-3-9-1792658668045624",
  "name": null,
  "from_node_id": 3,
  "to_node_id": 9,
  "primary_point_id": 5,
  "phase_count": null,
  "rated_current_a": 16.0,
  "cable_note": null,
  "state": "published",
  "valid_from": 1792658668,
  "valid_to": null,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 37,
  "point": {
    "id": 5,
    "code": "pv3-att-8-1792658668046",
    "name": "Вентиляция",
    "description": null,
    "installation_location_id": null,
    "installation_note": null,
    "enabled": true,
    "archived_at": null,
    "created_at": 1792658668,
    "updated_at": 1792658668
  },
  "meter_source_id": 6
}
```

**Пример: already_metered.** `/api/v2/topology/edges/8/attach-new-point`

Тело запроса:

```json
{
  "device_id": "dev-light",
  "expected_revision": 37
}
```

Ответ `409`:

```json
{
  "code": "already_metered",
  "message": "Линия 8 уже измеряется другой точкой (5) — сначала снимите её явно, прежде чем ставить новую через эту ручку",
  "ids": [
    8,
    5
  ]
}
```

#### `POST /api/v2/topology/validate`

Проверить черновики линий на правила схемы, ничего не публикуя.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| edge_ids | массив целых | нет | Какие черновики проверить; пусто — проверяется действующая схема как есть. |

**Ответ.** `200` `{ok, violations: [{kind, message, node_ids, edge_ids}]}`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Неизвестная линия или линия не черновик. |

**Пример.** `/api/v2/topology/validate`

Тело запроса:

```json
{
  "edge_ids": [
    6
  ]
}
```

Ответ `200`:

```json
{
  "ok": true,
  "violations": []
}
```

#### `POST /api/v2/topology/publish`

Опубликовать черновики линий: с `effective_from` они действуют. Действующая линия, приходящая в тот же узел, закрывается этим же моментом (так узел переподключают). Ошибка любой линии откатывает всё.

**Ревизия.** Протокол ревизий: поле называется **`expected_configuration_revision`** (обязательно; смысл тот же, что у `expected_revision`). Устаревшее или отсутствующее — `409 revision_conflict` с `fields: ["expected_configuration_revision"]`, схема не меняется.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_configuration_revision | целое | да | Текущая ревизия конфигурации. |
| edge_ids | массив целых | да | Публикуемые черновики. Допустим и синоним `draft_id` (число или массив). |
| effective_from | целое | нет | Момент начала действия (unix-секунды), по умолчанию «сейчас». |

**Ответ.** `200` `{edges: […опубликованные линии], configuration_revision}`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Неизвестная линия или линия не черновик. |
| 409 | topology_conflict | Публикация нарушила бы правила схемы; `path` — нарушения (`kind`, `message`, `node_ids`, `edge_ids`). |
| 409 | revision_conflict | `expected_configuration_revision` устарела. |

**Пример.** `/api/v2/topology/publish`

Тело запроса:

```json
{
  "edge_ids": [
    6
  ],
  "expected_configuration_revision": 32
}
```

Ответ `200`:

```json
{
  "edges": [
    {
      "id": 6,
      "code": null,
      "name": "Л-ЩР3",
      "from_node_id": 1,
      "to_node_id": 7,
      "primary_point_id": null,
      "phase_count": null,
      "rated_current_a": 63.0,
      "cable_note": "ВВГ 4x16",
      "state": "published",
      "valid_from": 1792658668,
      "valid_to": null,
      "archived_at": null,
      "created_at": 1792658668,
      "updated_at": 1792658668
    }
  ],
  "configuration_revision": 33
}
```

**Пример: revision_conflict.** `/api/v2/topology/publish`

Тело запроса:

```json
{
  "edge_ids": [
    6
  ],
  "expected_configuration_revision": 32
}
```

Ответ `409`:

```json
{
  "code": "revision_conflict",
  "message": "ревизия конфигурации устарела: ожидалась 32, сейчас 33 — кто-то уже сохранил более новые изменения; перечитайте текущее состояние и повторите",
  "fields": [
    "expected_configuration_revision"
  ]
}
```

#### `GET /api/v2/topology/edge-loads`

Загрузка линий током от номинала (`load_percent`) по текущим значениям точек. Линии без номинала или без счётчика — с причиной, почему процента нет.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| edge_ids | список через запятую | нет | Только эти линии; по умолчанию все действующие и черновики. |

**Ответ.** `200` `{as_of, edges: [{edge_id, primary_point_id, rated_current_a, current_a, phase_currents, power_w, missing_phases, load_percent, load_unavailable_reason, load_unavailable_text}]}`. Нет данных — `null`, не `0`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | `edge_ids` — не список целых. |
| 404 | not_found | Среди `edge_ids` есть несуществующая линия. |

**Пример.** `/api/v2/topology/edge-loads`

Ответ `200`:

```json
{
  "as_of": "2026-10-22T08:44:27Z",
  "edges": [
    {
      "rated_current_a": null,
      "current_a": null,
      "phase_currents": null,
      "power_w": 4800.0,
      "missing_phases": [],
      "load_percent": null,
      "load_unavailable_reason": "no_rating",
      "load_unavailable_text": "не указан номинальный ток линии",
      "edge_id": 1,
      "primary_point_id": 1
    },
    {
      "rated_current_a": null,
      "current_a": null,
      "phase_currents": null,
      "power_w": null,
      "missing_phases": [],
      "load_percent": null,
      "load_unavailable_reason": "no_rating",
      "load_unavailable_text": "не указан номинальный ток линии",
      "edge_id": 2,
      "primary_point_id": null
    }
  ]
}
```

_Списки в примере сокращены до двух элементов._

### 2.6. Границы баланса

Именованный набор точек «вход» и «выход»; состав версионируется (старые участники закрываются, новые открываются).

#### `GET /api/v2/balance-scopes`

Список границ баланса.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` массив: `id`, `name`, `description`, `created_at`, `updated_at`, `archived_at`.

**Пример.** `/api/v2/balance-scopes`

Ответ `200`:

```json
[]
```

#### `POST /api/v2/balance-scopes`

Создать границу баланса.

**Ревизия.** Создаёт запись и продвигает ревизию, но `expected_revision` **не требует** (создание новой сущности не может затереть чужую правку). В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| name | строка | да | Название. |
| description | строка | нет | Описание. |
| input_point_ids | массив целых | нет | Точки-входы. |
| output_point_ids | массив целых | нет | Точки-выходы. |

**Ответ.** `201` граница и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Пустое имя. |

**Пример.** `/api/v2/balance-scopes`

Тело запроса:

```json
{
  "name": "Освещение + розетки",
  "input_point_ids": [
    1
  ],
  "output_point_ids": [
    2,
    3
  ]
}
```

Ответ `201`:

```json
{
  "id": 1,
  "name": "Освещение + розетки",
  "description": null,
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 40
}
```

#### `GET /api/v2/balance-scopes/{scope_id}`

Одна граница с действующим составом.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` граница и `input_point_ids`, `output_point_ids`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Границы нет. |

**Пример.** `/api/v2/balance-scopes/1`

Ответ `200`:

```json
{
  "id": 1,
  "name": "Освещение + розетки",
  "description": "Контрольная граница",
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "input_point_ids": [
    1
  ],
  "output_point_ids": [
    2,
    3
  ]
}
```

#### `PATCH /api/v2/balance-scopes/{scope_id}`

Изменить имя, описание или состав. Если передан список — он заменяет сторону целиком.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| name | строка | нет | Новое имя. |
| description | строка | нет | Новое описание. |
| input_point_ids | массив целых | нет | Новый состав входов. |
| output_point_ids | массив целых | нет | Новый состав выходов. |

**Ответ.** `200` граница с составом и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нечего менять. |
| 404 | not_found | Границы нет. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример.** `/api/v2/balance-scopes/1`

Тело запроса:

```json
{
  "description": "Контрольная граница",
  "expected_revision": 40
}
```

Ответ `200`:

```json
{
  "id": 1,
  "name": "Освещение + розетки",
  "description": "Контрольная граница",
  "archived_at": null,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "input_point_ids": [
    1
  ],
  "output_point_ids": [
    2,
    3
  ],
  "configuration_revision": 41
}
```

### 2.7. Расчёты и отчёты

Все три маршрута считают по локальным часовым итогам (`aggregates`) и конфигурации, не обращаясь к `wb-mqtt-db`; весь расчёт идёт в одном снимке БД. Формат результата — «Результат расчёта» ниже; «нет данных» всегда `null`, а не `0`.

#### `POST /api/v2/metrics/query`

Один показатель по точкам или границе баланса за период.

**Ревизия.** Расчёт, ничего не пишет. Весь расчёт идёт в одном снимке БД (A43): конкурентная запись не попадает в уже начатый запрос.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| mode | строка | да | `measured` (ровно одна точка), `sum` (сумма точек), `balance` (граница баланса), `comparison` (по каждой точке отдельно). |
| point_ids | массив целых | для measured/sum/comparison | Точки. `measured` требует ровно одну. |
| scope | целое | для balance | Идентификатор границы баланса. |
| from | число или строка | да | Начало периода: unix-секунды либо дата (см. «Время и периоды»). |
| to | число или строка | да | Конец периода, строго позже `from`. |
| timezone | строка | нет | Часовой пояс IANA (по умолчанию `UTC`): в нём читаются строки-даты в `from`/`to`, по нему строятся часовые границы и подписи `period.from_local`/`period.to_local`. Неизвестное имя при строке-дате — `400`, поле `timezone`. |
| configuration_revision_id | целое | нет | Пометить результат ревизией (должна существовать; иначе `404`). Без неё ставится текущая, зафиксированная в начале запроса. |

**Ответ.** `200` результат расчёта; для `comparison` — объект `{"<point_id>": результат, …}`. 

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Неизвестный `mode`; не хватает `point_ids`/`scope`; не разобрано `from`/`to`; `to` не позже `from`. |
| 404 | not_found | Нет такой ревизии конфигурации. |
| 409 | double_counting | Одна и та же физическая величина вошла в сумму дважды. |
| 500 | internal | Нарушен контракт результата. |

**Пример: mode=measured.** `/api/v2/metrics/query`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow",
  "mode": "measured",
  "point_ids": [
    2
  ]
}
```

Ответ `200`:

```json
{
  "metric": "energy_import",
  "unit": "kWh",
  "mode": "measured",
  "value": 499.68,
  "known_value": 499.68,
  "availability": "complete",
  "quality_flags": [],
  "structure_quality": "verified",
  "source": "measured",
  "expected_count": 512,
  "valid_count": 512,
  "missing_ids": [],
  "configuration_revision_id": 41,
  "configuration_revision_ids": [
    41
  ],
  "as_of": "2026-10-22T08:44:28.057234Z",
  "period": {
    "from": "1790812800",
    "to": "1792656000",
    "timezone": "Europe/Moscow",
    "from_local": "2026-10-01T03:00:00+03:00",
    "to_local": "2026-10-22T11:00:00+03:00"
  },
  "explanation": {
    "point_id": 2
  }
}
```

**Пример: mode=sum.** `/api/v2/metrics/query`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow",
  "mode": "sum",
  "point_ids": [
    2,
    3
  ]
}
```

Ответ `200`:

```json
{
  "metric": "energy_import",
  "unit": "kWh",
  "mode": "sum",
  "value": 1832.16,
  "known_value": 1832.16,
  "availability": "complete",
  "quality_flags": [],
  "structure_quality": "verified",
  "source": "calculated",
  "expected_count": 2,
  "valid_count": 2,
  "missing_ids": [],
  "configuration_revision_id": 41,
  "configuration_revision_ids": [
    41
  ],
  "as_of": "2026-10-22T08:44:28.065042Z",
  "period": {
    "from": "1790812800",
    "to": "1792656000",
    "timezone": "Europe/Moscow",
    "from_local": "2026-10-01T03:00:00+03:00",
    "to_local": "2026-10-22T11:00:00+03:00"
  },
  "explanation": {
    "included_point_ids": [
      2,
      3
    ]
  }
}
```

**Пример: mode=balance.** `/api/v2/metrics/query`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow",
  "mode": "balance",
  "scope": 1
}
```

Ответ `200`:

```json
{
  "metric": "imbalance",
  "unit": "kWh",
  "mode": "balance",
  "value": 1610.08,
  "known_value": 1610.08,
  "availability": "complete",
  "quality_flags": [],
  "structure_quality": "verified",
  "source": "calculated",
  "expected_count": 3,
  "valid_count": 3,
  "missing_ids": [],
  "configuration_revision_id": 41,
  "configuration_revision_ids": [
    41
  ],
  "as_of": "2026-10-22T08:44:28.071529Z",
  "period": {
    "from": "1790812800",
    "to": "1792656000",
    "timezone": "Europe/Moscow",
    "from_local": "2026-10-01T03:00:00+03:00",
    "to_local": "2026-10-22T11:00:00+03:00"
  },
  "explanation": {
    "input_point_ids": [
      1
    ],
    "output_point_ids": [
      2,
      3
    ],
    "percentage": 46.77,
    "percentage_reason": null,
    "input_value": 3442.24,
    "output_value": 1832.16
  }
}
```

**Пример: mode=comparison.** `/api/v2/metrics/query`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow",
  "mode": "comparison",
  "point_ids": [
    2,
    3
  ]
}
```

Ответ `200`:

```json
{
  "2": {
    "metric": "energy_import",
    "unit": "kWh",
    "mode": "measured",
    "value": 499.68,
    "known_value": 499.68,
    "availability": "complete",
    "quality_flags": [],
    "structure_quality": "verified",
    "source": "measured",
    "expected_count": 512,
    "valid_count": 512,
    "missing_ids": [],
    "configuration_revision_id": 41,
    "configuration_revision_ids": [
      41
    ],
    "as_of": "2026-10-22T08:44:28.075835Z",
    "period": {
      "from": "1790812800",
      "to": "1792656000",
      "timezone": "Europe/Moscow",
      "from_local": "2026-10-01T03:00:00+03:00",
      "to_local": "2026-10-22T11:00:00+03:00"
    },
    "explanation": {
      "point_id": 2
    }
  },
  "3": {
    "metric": "energy_import",
    "unit": "kWh",
    "mode": "measured",
    "value": 1332.48,
    "known_value": 1332.48,
    "availability": "complete",
    "quality_flags": [],
    "structure_quality": "verified",
    "source": "measured",
    "expected_count": 512,
    "valid_count": 512,
    "missing_ids": [],
    "configuration_revision_id": 41,
    "configuration_revision_ids": [
      41
    ],
    "as_of": "2026-10-22T08:44:28.075869Z",
    "period": {
      "from": "1790812800",
      "to": "1792656000",
      "timezone": "Europe/Moscow",
      "from_local": "2026-10-01T03:00:00+03:00",
      "to_local": "2026-10-22T11:00:00+03:00"
    },
    "explanation": {
      "point_id": 3
    }
  }
}
```

**Пример: bad_request.** `/api/v2/metrics/query`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow",
  "mode": "nope"
}
```

Ответ `400`:

```json
{
  "code": "bad_request",
  "message": "неизвестный mode='nope', допустимые: measured|sum|balance|comparison",
  "fields": [
    "mode"
  ]
}
```

#### `POST /api/v2/overview/summary`

Сводка для «Обзора»: итог объекта (только через назначенные вводы), небаланс отдельной строкой, ветви сети, группы.

**Ревизия.** Расчёт, ничего не пишет. Весь расчёт идёт в одном снимке БД (A43): конкурентная запись не попадает в уже начатый запрос.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| from | число или строка | да | Начало периода: unix-секунды либо дата (см. «Время и периоды»). |
| to | число или строка | да | Конец периода, строго позже `from`. |
| timezone | строка | нет | Часовой пояс IANA (по умолчанию `UTC`): в нём читаются строки-даты в `from`/`to`, по нему строятся часовые границы и подписи `period.from_local`/`period.to_local`. Неизвестное имя при строке-дате — `400`, поле `timezone`. |
| configuration_revision_id | целое | нет | Пометить результат ревизией (должна существовать; иначе `404`). Без неё ставится текущая, зафиксированная в начале запроса. |
| structure_mode | строка | нет | `current` (по умолчанию) — по текущей схеме; `as_was` — по схеме, как она была на каждом интервале периода (история топологии с 0.22.0; до неё — `assumed_legacy`). |

**Ответ.** `200` `{configuration_revision_id, as_of, period, structure_mode, assumed_legacy, journal_started_at, structure_intervals, object_input_point_ids, object_has_emulator, has_emulator, object_total, object_total_unavailable_reason, imbalance_value, imbalance_percent, imbalance_percent_reason, imbalance, boundary_coverage, network_branches, unmetered_branches, unmetered_inputs, branches, groups_note, ungrouped_point_ids}`. `object_total` — результат расчёта по вводам; `network_branches[]` — отходящие линии ввода с результатом и долей от объекта; `unmetered_*` — линии без счётчика (по ним итог неполон); `branches[]` — верхние учётные группы (для сравнения со схемой). Недоступное значение — `null` и причина в соседнем `*_reason`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Не разобрано `from`/`to`; `to` не позже `from`; неизвестный `timezone` при строке-дате; неверный `structure_mode`/`configuration_revision_id`. |
| 404 | not_found | Нет такой ревизии конфигурации. |
| 500 | internal | Нарушен контракт результата или алгоритм баланса. |

**Пример.** `/api/v2/overview/summary`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow"
}
```

Ответ `200`:

```json
{
  "configuration_revision_id": 41,
  "as_of": "2026-10-22T08:44:28.088560Z",
  "period": {
    "from": "1790812800",
    "to": "1792656000",
    "timezone": "Europe/Moscow",
    "from_local": "2026-10-01T03:00:00+03:00",
    "to_local": "2026-10-22T11:00:00+03:00"
  },
  "structure_mode": "current",
  "assumed_legacy": false,
  "journal_started_at": 1792658667,
  "structure_intervals": [
    {
      "from": 1790812800,
      "to": 1792656000,
      "assumed_legacy": false
    }
  ],
  "object_input_point_ids": [
    1
  ],
  "object_has_emulator": false,
  "has_emulator": false,
  "object_total": {
    "metric": "energy_import",
    "unit": "kWh",
    "mode": "sum",
    "value": null,
    "known_value": 3442.24,
    "availability": "partial",
    "quality_flags": [],
    "structure_quality": "verified",
    "source": "calculated",
    "expected_count": 3,
    "valid_count": 1,
    "missing_ids": [],
    "configuration_revision_id": 41,
    "configuration_revision_ids": [
      41
    ],
    "as_of": "2026-10-22T08:44:28.082664Z",
    "period": {
      "from": "1790812800",
      "to": "1792656000",
      "timezone": "Europe/Moscow",
      "from_local": "2026-10-01T03:00:00+03:00",
      "to_local": "2026-10-22T11:00:00+03:00"
    },
    "explanation": {
      "included_point_ids": [
        1
      ]
    }
  },
  "object_total_unavailable_reason": "unmetered_input",
  "imbalance_value": null,
  "imbalance_percent": null,
  "imbalance_percent_reason": "object_total_incomplete",
  "imbalance": null,
  "boundary_coverage": "has_unmetered_branches",
  "network_branches": [
    {
      "edge_id": 3,
      "point_id": 2,
      "name": "Освещение",
      "result": {
        "metric": "energy_import",
        "unit": "kWh",
        "mode": "measured",
        "value": 499.68,
        "known_value": 499.68,
        "availability": "complete",
        "quality_flags": [],
        "structure_quality": "verified",
        "source": "measured",
        "expected_count": 512,
        "valid_count": 512,
        "missing_ids": [],
        "configuration_revision_id": 41,
        "configuration_revision_ids": [
          41
        ],
        "as_of": "2026-10-22T08:44:28.082676Z",
        "period": {
          "from": "1790812800",
          "to": "1792656000",
          "timezone": "Europe/Moscow",
          "from_local": "2026-10-01T03:00:00+03:00",
          "to_local": "2026-10-22T11:00:00+03:00"
        },
        "explanation": {
          "point_id": 2
        }
      },
      "percentage_of_object": null,
      "percentage_of_object_reason": "no_data",
      "has_emulator": false
    },
    {
      "edge_id": 4,
      "point_id": 3,
      "name": "Розетки",
      "result": {
        "metric": "energy_import",
        "unit": "kWh",
        "mode": "measured",
        "value": 1332.48,
        "known_value": 1332.48,
        "availability": "complete",
        "quality_flags": [],
        "structure_quality": "verified",
        "source": "measured",
        "expected_count": 512,
        "valid_count": 512,
        "missing_ids": [],
        "configuration_revision_id": 41,
        "configuration_revision_ids": [
          41
        ],
        "as_of": "2026-10-22T08:44:28.082698Z",
        "period": {
          "from": "1790812800",
          "to": "1792656000",
          "timezone": "Europe/Moscow",
          "from_local": "2026-10-01T03:00:00+03:00",
          "to_local": "2026-10-22T11:00:00+03:00"
        },
        "explanation": {
          "point_id": 3
        }
      },
      "percentage_of_object": null,
      "percentage_of_object_reason": "no_data",
      "has_emulator": false
    }
  ],
  "unmetered_branches": [
    {
      "edge_id": 5,
      "name": "Станок"
    }
  ],
  "unmetered_inputs": [
    {
      "edge_id": 2,
      "from_node_name": "Ввод",
      "to_node_name": "ЩР-2"
    },
    {
      "edge_id": 7,
      "from_node_name": "Ввод",
      "to_node_name": "ЩР-4"
    }
  ],
  "branches": [
    {
      "group_id": 1,
      "name": "Цех",
      "category": null,
      "member_point_ids": [
        1,
        2,
        3
      ],
      "mode": "comparison",
      "conflict_reason": "точка 2 находится ниже точки 1 в электрической сети (путь от узла 2 до узла 4) — суммирование даст двойной счёт; варианты: оставить только 1, сравнить точки по отдельности, либо посчитать небаланс между ними",
      "points": {
        "1": {
          "metric": "energy_import",
          "unit": "kWh",
          "mode": "measured",
          "value": 3442.24,
          "known_value": 3442.24,
          "availability": "complete",
          "quality_flags": [],
          "structure_quality": "verified",
          "source": "measured",
          "expected_count": 512,
          "valid_count": 512,
          "missing_ids": [],
          "configuration_revision_id": 41,
          "configuration_revision_ids": [
            41
          ],
          "as_of": "2026-10-22T08:44:28.088341Z",
          "period": {
            "from": "1790812800",
            "to": "1792656000",
            "timezone": "Europe/Moscow",
            "from_local": "2026-10-01T03:00:00+03:00",
            "to_local": "2026-10-22T11:00:00+03:00"
          },
          "explanation": {
            "point_id": 1
          }
        },
        "2": {
          "metric": "energy_import",
          "unit": "kWh",
          "mode": "measured",
          "value": 499.68,
          "known_value": 499.68,
          "availability": "complete",
          "quality_flags": [],
          "structure_quality": "verified",
          "source": "measured",
          "expected_count": 512,
          "valid_count": 512,
          "missing_ids": [],
          "configuration_revision_id": 41,
          "configuration_revision_ids": [
            41
          ],
          "as_of": "2026-10-22T08:44:28.088369Z",
          "period": {
            "from": "1790812800",
            "to": "1792656000",
            "timezone": "Europe/Moscow",
            "from_local": "2026-10-01T03:00:00+03:00",
            "to_local": "2026-10-22T11:00:00+03:00"
          },
          "explanation": {
            "point_id": 2
          }
        },
        "3": {
          "metric": "energy_import",
          "unit": "kWh",
          "mode": "measured",
          "value": 1332.48,
          "known_value": 1332.48,
          "availability": "complete",
          "quality_flags": [],
          "structure_quality": "verified",
          "source": "measured",
          "expected_count": 512,
          "valid_count": 512,
          "missing_ids": [],
          "configuration_revision_id": 41,
          "configuration_revision_ids": [
            41
          ],
          "as_of": "2026-10-22T08:44:28.088377Z",
          "period": {
            "from": "1790812800",
            "to": "1792656000",
            "timezone": "Europe/Moscow",
            "from_local": "2026-10-01T03:00:00+03:00",
            "to_local": "2026-10-22T11:00:00+03:00"
          },
          "explanation": {
            "point_id": 3
          }
        }
      },
      "has_emulator": false
    }
  ],
  "groups_note": "распределение по учётным группам; группы могут пересекаться, в небалансе не участвуют",
  "ungrouped_point_ids": [
    4,
    5,
    6
  ]
}
```

#### `POST /api/v2/reports/query`

Строки отчёта в одном из четырёх срезов; тот же расчёт, что у «Обзора» и `metrics/query`.

**Ревизия.** Расчёт, ничего не пишет. Весь расчёт идёт в одном снимке БД (A43): конкурентная запись не попадает в уже начатый запрос.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| dimension | строка | да | `point`, `branch`, `group` или `balance_scope`. |
| scope_ids | массив целых | для point/group/balance_scope | Точки, группы или границы баланса. Для `branch` игнорируется: берутся все ветви верхнего уровня. |
| composition_mode | строка | нет | Состав групп: `as_was` (по умолчанию, как было на каждом интервале) или `current`. Вместе с `structure_mode=as_was` `current` недопустим. |
| compare | объект | нет | `{from, to}` — второй период; в ответе появляется `compare_period`, а различия состава и замены приборов раскрываются явно. |
| from | число или строка | да | Начало периода: unix-секунды либо дата (см. «Время и периоды»). |
| to | число или строка | да | Конец периода, строго позже `from`. |
| timezone | строка | нет | Часовой пояс IANA (по умолчанию `UTC`): в нём читаются строки-даты в `from`/`to`, по нему строятся часовые границы и подписи `period.from_local`/`period.to_local`. Неизвестное имя при строке-дате — `400`, поле `timezone`. |
| configuration_revision_id | целое | нет | Пометить результат ревизией (должна существовать; иначе `404`). Без неё ставится текущая, зафиксированная в начале запроса. |
| structure_mode | строка | нет | `current` (по умолчанию) — по текущей схеме; `as_was` — по схеме, как она была на каждом интервале периода (история топологии с 0.22.0; до неё — `assumed_legacy`). |

**Ответ.** `200` `{configuration_revision_id, as_of, dimension, composition_mode, has_emulator, structure_mode, assumed_legacy, journal_started_at, structure_intervals, period, rows, compare_period?}`. Строка: `dimension`, `id`, `name`, `member_point_ids`, `result` (результат расчёта), `conflict_reason`, `structure_confirmed`, `has_emulator`; для `branch` и `group` — поля состава.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Неверные `dimension`/`composition_mode`/`structure_mode`/`compare`; нет `scope_ids`; период не разобран или `to` не позже `from`; `as_was` просит интервалы мельче допустимых. |
| 404 | not_found | Нет такой ревизии, группы, точки или границы (`ids`). |
| 500 | internal | Нарушен контракт результата. |

**Пример: dimension=point.** `/api/v2/reports/query`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow",
  "dimension": "point",
  "scope_ids": [
    2,
    3
  ]
}
```

Ответ `200`:

```json
{
  "configuration_revision_id": 41,
  "as_of": "2026-10-22T08:44:28.111812Z",
  "dimension": "point",
  "composition_mode": "as_was",
  "has_emulator": false,
  "structure_mode": "current",
  "assumed_legacy": false,
  "journal_started_at": 1792658667,
  "structure_intervals": [
    {
      "from": 1790812800,
      "to": 1792656000,
      "assumed_legacy": false
    }
  ],
  "period": {
    "from": "1790812800",
    "to": "1792656000",
    "timezone": "Europe/Moscow",
    "from_local": "2026-10-01T03:00:00+03:00",
    "to_local": "2026-10-22T11:00:00+03:00"
  },
  "rows": [
    {
      "dimension": "point",
      "id": 2,
      "name": "Освещение",
      "member_point_ids": [
        2
      ],
      "result": {
        "metric": "energy_import",
        "unit": "kWh",
        "mode": "measured",
        "value": 499.68,
        "known_value": 499.68,
        "availability": "complete",
        "quality_flags": [],
        "structure_quality": "verified",
        "source": "measured",
        "expected_count": 512,
        "valid_count": 512,
        "missing_ids": [],
        "configuration_revision_id": 41,
        "configuration_revision_ids": [
          41
        ],
        "as_of": "2026-10-22T08:44:28.097676Z",
        "period": {
          "from": "1790812800",
          "to": "1792656000",
          "timezone": "Europe/Moscow",
          "from_local": "2026-10-01T03:00:00+03:00",
          "to_local": "2026-10-22T11:00:00+03:00"
        },
        "explanation": {
          "point_id": 2
        }
      },
      "conflict_reason": null,
      "structure_confirmed": true,
      "has_emulator": false
    },
    {
      "dimension": "point",
      "id": 3,
      "name": "Розетки",
      "member_point_ids": [
        3
      ],
      "result": {
        "metric": "energy_import",
        "unit": "kWh",
        "mode": "measured",
        "value": 1332.48,
        "known_value": 1332.48,
        "availability": "complete",
        "quality_flags": [],
        "structure_quality": "verified",
        "source": "measured",
        "expected_count": 512,
        "valid_count": 512,
        "missing_ids": [],
        "configuration_revision_id": 41,
        "configuration_revision_ids": [
          41
        ],
        "as_of": "2026-10-22T08:44:28.111729Z",
        "period": {
          "from": "1790812800",
          "to": "1792656000",
          "timezone": "Europe/Moscow",
          "from_local": "2026-10-01T03:00:00+03:00",
          "to_local": "2026-10-22T11:00:00+03:00"
        },
        "explanation": {
          "point_id": 3
        }
      },
      "conflict_reason": null,
      "structure_confirmed": true,
      "has_emulator": false
    }
  ]
}
```

**Пример: dimension=group.** `/api/v2/reports/query`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow",
  "dimension": "group",
  "scope_ids": [
    1
  ],
  "composition_mode": "as_was"
}
```

Ответ `200`:

```json
{
  "configuration_revision_id": 41,
  "as_of": "2026-10-22T08:44:28.116617Z",
  "dimension": "group",
  "composition_mode": "as_was",
  "has_emulator": false,
  "structure_mode": "current",
  "assumed_legacy": false,
  "journal_started_at": 1792658667,
  "structure_intervals": [
    {
      "from": 1790812800,
      "to": 1792656000,
      "assumed_legacy": false
    }
  ],
  "period": {
    "from": "1790812800",
    "to": "1792656000",
    "timezone": "Europe/Moscow",
    "from_local": "2026-10-01T03:00:00+03:00",
    "to_local": "2026-10-22T11:00:00+03:00"
  },
  "rows": [
    {
      "dimension": "group",
      "id": 1,
      "name": "Цех",
      "member_point_ids": [
        2,
        3
      ],
      "result": {
        "metric": "energy_import",
        "unit": "kWh",
        "mode": "sum",
        "value": 1832.16,
        "known_value": 1832.16,
        "availability": "complete",
        "quality_flags": [],
        "structure_quality": "verified",
        "source": "calculated",
        "expected_count": 2,
        "valid_count": 2,
        "missing_ids": [],
        "configuration_revision_id": 41,
        "configuration_revision_ids": [
          41
        ],
        "as_of": "2026-10-22T08:44:28.116528Z",
        "period": {
          "from": "1790812800",
          "to": "1792656000",
          "timezone": "Europe/Moscow",
          "from_local": "2026-10-01T03:00:00+03:00",
          "to_local": "2026-10-22T11:00:00+03:00"
        },
        "explanation": {
          "included_point_ids": [
            2,
            3
          ]
        }
      },
      "conflict_reason": null,
      "structure_confirmed": true,
      "has_emulator": false
    }
  ]
}
```

**Пример: dimension=branch.** `/api/v2/reports/query`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow",
  "dimension": "branch"
}
```

Ответ `200`:

```json
{
  "configuration_revision_id": 41,
  "as_of": "2026-10-22T08:44:28.123316Z",
  "dimension": "branch",
  "composition_mode": "as_was",
  "has_emulator": false,
  "structure_mode": "current",
  "assumed_legacy": false,
  "journal_started_at": 1792658667,
  "structure_intervals": [
    {
      "from": 1790812800,
      "to": 1792656000,
      "assumed_legacy": false
    }
  ],
  "period": {
    "from": "1790812800",
    "to": "1792656000",
    "timezone": "Europe/Moscow",
    "from_local": "2026-10-01T03:00:00+03:00",
    "to_local": "2026-10-22T11:00:00+03:00"
  },
  "rows": [
    {
      "dimension": "branch",
      "id": 3,
      "name": "Освещение",
      "member_point_ids": [
        2
      ],
      "result": {
        "metric": "energy_import",
        "unit": "kWh",
        "mode": "sum",
        "value": 499.68,
        "known_value": 499.68,
        "availability": "complete",
        "quality_flags": [],
        "structure_quality": "verified",
        "source": "calculated",
        "expected_count": 1,
        "valid_count": 1,
        "missing_ids": [],
        "configuration_revision_id": 41,
        "configuration_revision_ids": [
          41
        ],
        "as_of": "2026-10-22T08:44:28.123201Z",
        "period": {
          "from": "1790812800",
          "to": "1792656000",
          "timezone": "Europe/Moscow",
          "from_local": "2026-10-01T03:00:00+03:00",
          "to_local": "2026-10-22T11:00:00+03:00"
        },
        "explanation": {
          "included_point_ids": [
            2
          ]
        }
      },
      "conflict_reason": null,
      "structure_confirmed": true,
      "has_emulator": false
    },
    {
      "dimension": "branch",
      "id": 4,
      "name": "Розетки",
      "member_point_ids": [
        3
      ],
      "result": {
        "metric": "energy_import",
        "unit": "kWh",
        "mode": "sum",
        "value": 1332.48,
        "known_value": 1332.48,
        "availability": "complete",
        "quality_flags": [],
        "structure_quality": "verified",
        "source": "calculated",
        "expected_count": 1,
        "valid_count": 1,
        "missing_ids": [],
        "configuration_revision_id": 41,
        "configuration_revision_ids": [
          41
        ],
        "as_of": "2026-10-22T08:44:28.123272Z",
        "period": {
          "from": "1790812800",
          "to": "1792656000",
          "timezone": "Europe/Moscow",
          "from_local": "2026-10-01T03:00:00+03:00",
          "to_local": "2026-10-22T11:00:00+03:00"
        },
        "explanation": {
          "included_point_ids": [
            3
          ]
        }
      },
      "conflict_reason": null,
      "structure_confirmed": true,
      "has_emulator": false
    }
  ]
}
```

**Пример: dimension=balance_scope.** `/api/v2/reports/query`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow",
  "dimension": "balance_scope",
  "scope_ids": [
    1
  ]
}
```

Ответ `200`:

```json
{
  "configuration_revision_id": 41,
  "as_of": "2026-10-22T08:44:28.129353Z",
  "dimension": "balance_scope",
  "composition_mode": "as_was",
  "has_emulator": false,
  "structure_mode": "current",
  "assumed_legacy": false,
  "journal_started_at": 1792658667,
  "structure_intervals": [
    {
      "from": 1790812800,
      "to": 1792656000,
      "assumed_legacy": false
    }
  ],
  "period": {
    "from": "1790812800",
    "to": "1792656000",
    "timezone": "Europe/Moscow",
    "from_local": "2026-10-01T03:00:00+03:00",
    "to_local": "2026-10-22T11:00:00+03:00"
  },
  "rows": [
    {
      "dimension": "balance_scope",
      "id": 1,
      "name": "Освещение + розетки",
      "member_point_ids": [
        1,
        2,
        3
      ],
      "result": {
        "metric": "imbalance",
        "unit": "kWh",
        "mode": "balance",
        "value": 1610.08,
        "known_value": 1610.08,
        "availability": "complete",
        "quality_flags": [],
        "structure_quality": "verified",
        "source": "calculated",
        "expected_count": 3,
        "valid_count": 3,
        "missing_ids": [],
        "configuration_revision_id": 41,
        "configuration_revision_ids": [
          41
        ],
        "as_of": "2026-10-22T08:44:28.129297Z",
        "period": {
          "from": "1790812800",
          "to": "1792656000",
          "timezone": "Europe/Moscow",
          "from_local": "2026-10-01T03:00:00+03:00",
          "to_local": "2026-10-22T11:00:00+03:00"
        },
        "explanation": {
          "input_point_ids": [
            1
          ],
          "output_point_ids": [
            2,
            3
          ],
          "percentage": 46.77,
          "percentage_reason": null,
          "input_value": 3442.24,
          "output_value": 1832.16
        }
      },
      "conflict_reason": null,
      "structure_confirmed": true,
      "has_emulator": false
    }
  ]
}
```

**Пример: bad_request.** `/api/v2/reports/query`

Тело запроса:

```json
{
  "from": 1790812800,
  "to": 1792656000,
  "timezone": "Europe/Moscow",
  "dimension": "zone"
}
```

Ответ `400`:

```json
{
  "code": "bad_request",
  "message": "dimension='zone' — допустимые: point|branch|group|balance_scope",
  "fields": [
    "dimension"
  ]
}
```

### 2.8. Планы

План — подложка (картинка до 25 МБ, PNG/JPEG, длинная сторона до 8192 px) либо пустой холст однолинейной схемы, на которых размещаются **элементы** (значки точек, узлов, групп, мест) и **виды линий** (как линия схемы нарисована на этом плане). Координаты — `{x, y}` в пикселях (`coord_space`: `image_px_xy_v2`, `canvas_xy_v2`; `legacy_leaflet_yx_v1` — только чтение старых планов). Фон версионируется, прежний можно вернуть. Элементы и виды линий **вне протокола глобальных ревизий**; пакетное сохранение защищается `canvas_revision` плана.

#### `GET /api/v2/plans`

Список планов.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| plan_kind | строка | нет | `floor` (по подложке) или `single_line` (холст). |

**Ответ.** `200` массив планов: `id`, `name`, `plan_kind`, `image_width`, `image_height`, `canvas_width`, `canvas_height`, `canvas_revision`, `is_default`, `created_at`, `updated_at`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 503 | service_unavailable | Каталог планов не настроен (в службе — только в тестовых стендах). |

**Пример.** `/api/v2/plans`

Ответ `200`:

```json
[
  {
    "id": 1,
    "name": "Схема цеха",
    "plan_kind": "single_line",
    "image_width": null,
    "image_height": null,
    "canvas_width": 2000,
    "canvas_height": 1200,
    "canvas_revision": 1,
    "is_default": true,
    "created_at": 1792658667,
    "updated_at": 1792658667
  }
]
```

#### `POST /api/v2/plans`

Создать план: с подложкой или пустой холст.

**Ревизия.** Вне протокола глобальных ревизий; `expected_revision` не нужен.

**Тело запроса** (multipart/form-data):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| name | строка | да | Название, до 200 символов. |
| plan_kind | строка | нет | `floor` (по умолчанию) или `single_line`. |
| file | файл | для floor | Подложка PNG или JPEG до 25 МБ, длинная сторона до 8192 px. Для `single_line` не нужна. |
| canvas_width / canvas_height | целые | для single_line | Логические размеры пустого холста. |

**Ответ.** `201` план.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Пустое имя; неверный вид; слишком большой или повреждённый файл; нецелые размеры холста. |
| 503 | service_unavailable | Каталог планов не настроен (в службе — только в тестовых стендах). |

**Пример.** `/api/v2/plans`

Тело: multipart: file, name, plan_kind

Ответ `201`:

```json
{
  "id": 2,
  "name": "Склад",
  "plan_kind": "floor",
  "image_width": 200,
  "image_height": 100,
  "canvas_width": null,
  "canvas_height": null,
  "canvas_revision": 1,
  "is_default": false,
  "created_at": 1792658668,
  "updated_at": 1792658668
}
```

#### `GET /api/v2/plans/{plan_id}`

План с элементами и видами линий.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` план и массивы `items` (элементы: `id`, `plan_id`, `kind`, ссылки `point_id`/`location_id`/`group_id`/`node_id`/`target_plan_id`, `geometry`, `coord_space`, `label`, `sort_order`) и `edges` (виды линий: `id`, `plan_id`, `edge_id`, `from_item_id`, `to_item_id`, `waypoints`, `view_kind`, `confirmed_at`).

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Плана нет. |

**Пример.** `/api/v2/plans/1`

Ответ `200`:

```json
{
  "id": 1,
  "name": "Схема цеха",
  "plan_kind": "single_line",
  "image_width": null,
  "image_height": null,
  "canvas_width": 2000,
  "canvas_height": 1200,
  "canvas_revision": 1,
  "is_default": true,
  "created_at": 1792658667,
  "updated_at": 1792658667,
  "items": [
    {
      "id": 1,
      "plan_id": 1,
      "kind": "node",
      "point_id": null,
      "location_id": null,
      "group_id": null,
      "node_id": 1,
      "target_plan_id": null,
      "geometry": {
        "x": 240,
        "y": 200
      },
      "coord_space": "canvas_xy_v2",
      "label": "Ввод",
      "sort_order": 0
    },
    {
      "id": 2,
      "plan_id": 1,
      "kind": "node",
      "point_id": null,
      "location_id": null,
      "group_id": null,
      "node_id": 2,
      "target_plan_id": null,
      "geometry": {
        "x": 620,
        "y": 200
      },
      "coord_space": "canvas_xy_v2",
      "label": "ЩР-1",
      "sort_order": 0
    }
  ],
  "edges": [
    {
      "id": 1,
      "plan_id": 1,
      "edge_id": 1,
      "from_item_id": 1,
      "to_item_id": 2,
      "waypoints": null,
      "view_kind": "structural",
      "confirmed_at": null
    },
    {
      "id": 2,
      "plan_id": 1,
      "edge_id": 2,
      "from_item_id": 1,
      "to_item_id": 3,
      "waypoints": null,
      "view_kind": "structural",
      "confirmed_at": null
    }
  ]
}
```

_Списки в примере сокращены до двух элементов._

#### `PATCH /api/v2/plans/{plan_id}`

Переименовать план или сделать его «планом по умолчанию». Вид, размеры и фон не меняются.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| name | строка | нет | Новое имя. |
| is_default | булево | нет | План по умолчанию (у остальных признак снимается). |

**Ответ.** `200` план и `configuration_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нечего менять; присланы неизменяемые поля (`plan_kind`, размеры, фон …); неверный тип значения; пустое имя. |
| 404 | not_found | Плана нет. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример.** `/api/v2/plans/2`

Тело запроса:

```json
{
  "name": "Склад (1 этаж)",
  "is_default": false,
  "expected_revision": 41
}
```

Ответ `200`:

```json
{
  "id": 2,
  "name": "Склад (1 этаж)",
  "plan_kind": "floor",
  "image_width": 200,
  "image_height": 100,
  "canvas_width": null,
  "canvas_height": null,
  "canvas_revision": 1,
  "is_default": false,
  "created_at": 1792658668,
  "updated_at": 1792658668,
  "configuration_revision": 42
}
```

#### `DELETE /api/v2/plans/{plan_id}`

Удалить план (только его вид: точки, узлы и линии схемы остаются).

**Ревизия.** Вне протокола глобальных ревизий.

**Ответ.** `200` `{ok: true, id}`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Плана нет. |

**Пример.** `/api/v2/plans/2`

Ответ `200`:

```json
{
  "ok": true,
  "id": 2
}
```

#### `GET /api/v2/plans/{plan_id}/image`

Файл подложки (картинка, не JSON).

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| version | целое | нет | Номер версии фона из `image-versions`; по умолчанию текущий. |

**Ответ.** `200` изображение (`image/png` или `image/jpeg`), `Cache-Control: private, max-age=3600`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Плана нет, у плана нет подложки, версии нет или файл потерян. |
| 503 | service_unavailable | Каталог планов не настроен. |

**Пример.** `/api/v2/plans/2/image`

Ответ `200`: image/png, 33 байт.

**Пример.** `/api/v2/plans/1/image`

Ответ `404`:

```json
{
  "code": "not_found",
  "message": "У плана 1 нет фонового изображения",
  "ids": [
    1
  ]
}
```

#### `POST /api/v2/plans/{plan_id}/image`

Заменить подложку. Прежний файл не удаляется — версию можно вернуть.

**Ревизия.** Вне протокола глобальных ревизий.

**Тело запроса** (multipart/form-data):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| file | файл | да | Новая подложка PNG/JPEG. |
| mode | строка | нет | `keep` (по умолчанию) — значки остаются на прежних пикселях; `scale` — при том же соотношении сторон значки пересчитываются пропорционально. |

**Ответ.** `200` план.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет файла; неверный `mode`; файл не разобран или слишком велик. |
| 400 | reanchor_required | `mode=scale`, но соотношение сторон другое — оставьте значки (`keep`) и перепривязайте вручную. |
| 404 | not_found | Плана нет. |
| 503 | service_unavailable | Каталог планов не настроен. |

**Пример.** `/api/v2/plans/2/image`

Тело: multipart: file, mode

Ответ `200`:

```json
{
  "id": 2,
  "name": "Склад (1 этаж)",
  "plan_kind": "floor",
  "image_width": 400,
  "image_height": 200,
  "canvas_width": null,
  "canvas_height": null,
  "canvas_revision": 3,
  "is_default": false,
  "created_at": 1792658668,
  "updated_at": 1792658668
}
```

#### `POST /api/v2/plans/{plan_id}/image/preview`

Предпросмотр замены подложки: ничего не пишет.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Тело запроса** (multipart/form-data):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| file | файл | да | Новая подложка. |

**Ответ.** `200` `{compat, old, new, items_total, items_outside_if_kept, …}`; `compat`: `same`, `scalable` (то же соотношение сторон) или `reanchor` (нужна ручная перепривязка).

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет файла или он не разобран. |
| 404 | not_found | Плана нет. |

**Пример.** `/api/v2/plans/2/image/preview`

Тело: multipart: file

Ответ `200`:

```json
{
  "compat": "scalable",
  "scale_x": 2.0,
  "scale_y": 2.0,
  "old": {
    "width": 200,
    "height": 100
  },
  "new": {
    "width": 400,
    "height": 200
  },
  "items_total": 2,
  "items_outside_if_kept": 0
}
```

#### `GET /api/v2/plans/{plan_id}/image-versions`

Версии подложки плана.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` массив версий фона, новые сверху: `id`, `image_width`, `image_height`, `created_at`, `is_current`; у неактуальных ещё описание перехода к текущей подложке (`compat`, `scale_x`, `scale_y`, `old`, `new`, `items_total`, `items_outside_if_kept`). Пустой массив — подложку не загружали.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Плана нет. |

**Пример.** `/api/v2/plans/1/image-versions`

Ответ `200`:

```json
[]
```

**Пример.** `/api/v2/plans/2/image-versions`

Ответ `200`:

```json
[
  {
    "id": 2,
    "image_width": 400,
    "image_height": 200,
    "created_at": 1792658668,
    "is_current": true
  },
  {
    "id": 1,
    "image_width": 200,
    "image_height": 100,
    "created_at": 1792658668,
    "is_current": false,
    "compat": "scalable",
    "scale_x": 0.5,
    "scale_y": 0.5,
    "old": {
      "width": 400,
      "height": 200
    },
    "new": {
      "width": 200,
      "height": 100
    },
    "items_total": 2,
    "items_outside_if_kept": 1
  }
]
```

#### `POST /api/v2/plans/{plan_id}/image/restore`

Вернуть прежнюю подложку.

**Ревизия.** Вне протокола глобальных ревизий.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| version_id | целое | да | Версия из `image-versions`. |
| mode | строка | нет | `keep` (по умолчанию) или `scale` — как при замене. |

**Ответ.** `200` план.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | `version_id` не число; неверный `mode`. |
| 400 | reanchor_required | `mode=scale` при другом соотношении сторон. |
| 404 | not_found | Плана или версии нет. |

**Пример.** `/api/v2/plans/2/image/restore`

Тело запроса:

```json
{
  "version_id": 2,
  "mode": "scale"
}
```

Ответ `200`:

```json
{
  "id": 2,
  "name": "Склад (1 этаж)",
  "plan_kind": "floor",
  "image_width": 400,
  "image_height": 200,
  "canvas_width": null,
  "canvas_height": null,
  "canvas_revision": 4,
  "is_default": false,
  "created_at": 1792658668,
  "updated_at": 1792658668
}
```

#### `GET /api/v2/plans/{plan_id}/items`

Элементы плана.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` массив элементов (поля — в `GET /api/v2/plans/{plan_id}`).

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Плана нет. |

**Пример.** `/api/v2/plans/1/items`

Ответ `200`:

```json
[
  {
    "id": 1,
    "plan_id": 1,
    "kind": "node",
    "point_id": null,
    "location_id": null,
    "group_id": null,
    "node_id": 1,
    "target_plan_id": null,
    "geometry": {
      "x": 240,
      "y": 200
    },
    "coord_space": "canvas_xy_v2",
    "label": "Ввод",
    "sort_order": 0
  },
  {
    "id": 2,
    "plan_id": 1,
    "kind": "node",
    "point_id": null,
    "location_id": null,
    "group_id": null,
    "node_id": 2,
    "target_plan_id": null,
    "geometry": {
      "x": 620,
      "y": 200
    },
    "coord_space": "canvas_xy_v2",
    "label": "ЩР-1",
    "sort_order": 0
  }
]
```

_Списки в примере сокращены до двух элементов._

#### `POST /api/v2/plans/{plan_id}/items`

Разместить элемент на плане.

**Ревизия.** Вне протокола глобальных ревизий; для пакетных правок с защитой от потери — `POST …/layout`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| kind | строка | да | `point`, `location`, `group`, `node`, `port` или `annotation`. |
| geometry | объект или массив | да | `{x, y}`; для `location` и `group` допустим и полигон `[[x, y], …]`. |
| coord_space | строка | да | `image_px_xy_v2` или `canvas_xy_v2` (для холста). |
| point_id / location_id / group_id / node_id / target_plan_id | целое | по `kind` | Ссылка на сущность — ровно та, что соответствует `kind`. |
| label | строка | нет | Подпись. |
| sort_order | целое | нет | Порядок отрисовки. |

**Ответ.** `201` элемент.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Неверный `kind`/`coord_space`; нет нужной ссылки; координаты вне плана; пустая геометрия. |
| 404 | not_found | Плана нет. |

**Пример.** `/api/v2/plans/2/items`

Тело запроса:

```json
{
  "kind": "point",
  "point_id": 2,
  "geometry": {
    "x": 60,
    "y": 40
  },
  "coord_space": "image_px_xy_v2",
  "label": "Свет"
}
```

Ответ `201`:

```json
{
  "id": 7,
  "plan_id": 2,
  "kind": "point",
  "point_id": 2,
  "location_id": null,
  "group_id": null,
  "node_id": null,
  "target_plan_id": null,
  "geometry": {
    "x": 60,
    "y": 40
  },
  "coord_space": "image_px_xy_v2",
  "label": "Свет",
  "sort_order": 0
}
```

**Пример.** `/api/v2/plans/2/items`

Тело запроса:

```json
{
  "kind": "point",
  "point_id": 3,
  "geometry": {
    "x": 120,
    "y": 40
  },
  "coord_space": "image_px_xy_v2"
}
```

Ответ `201`:

```json
{
  "id": 8,
  "plan_id": 2,
  "kind": "point",
  "point_id": 3,
  "location_id": null,
  "group_id": null,
  "node_id": null,
  "target_plan_id": null,
  "geometry": {
    "x": 120,
    "y": 40
  },
  "coord_space": "image_px_xy_v2",
  "label": null,
  "sort_order": 0
}
```

**Пример: bad_request.** `/api/v2/plans/2/items`

Тело запроса:

```json
{
  "kind": "point"
}
```

Ответ `400`:

```json
{
  "code": "bad_request",
  "message": "kind='point' требует соответствующую ссылку"
}
```

#### `PATCH /api/v2/plans/{plan_id}/items/{item_id}`

Сдвинуть элемент: сменить геометрию.

**Ревизия.** Вне протокола глобальных ревизий.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| geometry | объект или массив | да | Новая геометрия. |
| coord_space | строка | нет | Система координат; по умолчанию прежняя. |

**Ответ.** `200` элемент.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет `geometry`; координаты неверны или вне плана. |
| 404 | not_found | Плана или элемента на этом плане нет. |

**Пример.** `/api/v2/plans/2/items/7`

Тело запроса:

```json
{
  "geometry": {
    "x": 70,
    "y": 45
  },
  "coord_space": "image_px_xy_v2"
}
```

Ответ `200`:

```json
{
  "id": 7,
  "plan_id": 2,
  "kind": "point",
  "point_id": 2,
  "location_id": null,
  "group_id": null,
  "node_id": null,
  "target_plan_id": null,
  "geometry": {
    "x": 70,
    "y": 45
  },
  "coord_space": "image_px_xy_v2",
  "label": "Свет",
  "sort_order": 0
}
```

#### `DELETE /api/v2/plans/{plan_id}/items/{item_id}`

Снять элемент с плана (сущность схемы остаётся).

**Ревизия.** Вне протокола глобальных ревизий.

**Параметры запроса (query):**

| Параметр | Тип | Обяз. | Смысл |
|---|---|---|---|
| edge_views | `remove` | нет | Заодно удалить виды линий, привязанные к элементу. |

**Ответ.** `200` `{ok: true, id}`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Плана или элемента на этом плане нет. |

**Пример.** `/api/v2/plans/2/items/7?edge_views=remove`

Ответ `200`:

```json
{
  "ok": true,
  "id": 7
}
```

#### `GET /api/v2/plans/{plan_id}/edges`

Виды линий на плане.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` массив: `id`, `plan_id`, `edge_id`, `from_item_id`, `to_item_id`, `waypoints`, `view_kind`, `confirmed_at`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Плана нет. |

**Пример.** `/api/v2/plans/1/edges`

Ответ `200`:

```json
[
  {
    "id": 1,
    "plan_id": 1,
    "edge_id": 1,
    "from_item_id": 1,
    "to_item_id": 2,
    "waypoints": null,
    "view_kind": "structural",
    "confirmed_at": null
  },
  {
    "id": 2,
    "plan_id": 1,
    "edge_id": 2,
    "from_item_id": 1,
    "to_item_id": 3,
    "waypoints": null,
    "view_kind": "structural",
    "confirmed_at": null
  }
]
```

_Списки в примере сокращены до двух элементов._

#### `POST /api/v2/plans/{plan_id}/edges`

Нарисовать линию схемы на плане между двумя элементами.

**Ревизия.** Вне протокола глобальных ревизий.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| edge_id | целое | да | Линия схемы. |
| from_item_id / to_item_id | целое | да | Элементы плана на концах (должны соответствовать концам линии). |
| view_kind | строка | нет | `structural` (по умолчанию) или `cable_route`. |
| waypoints | массив пар | нет | Изломы трассы `[[x, y], …]` в системе координат элементов. |

**Ответ.** `201` вид линии.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет `edge_id`; элементы не соответствуют концам линии; неверные изломы. |
| 404 | not_found | Плана нет. |

**Пример.** `/api/v2/plans/1/edges`

Тело запроса:

```json
{
  "edge_id": 5,
  "from_item_id": 2,
  "to_item_id": 6,
  "view_kind": "structural",
  "waypoints": [
    [
      800,
      350
    ]
  ]
}
```

Ответ `201`:

```json
{
  "id": 6,
  "plan_id": 1,
  "edge_id": 5,
  "from_item_id": 2,
  "to_item_id": 6,
  "waypoints": [
    [
      800,
      350
    ]
  ],
  "view_kind": "structural",
  "confirmed_at": null
}
```

#### `DELETE /api/v2/plans/{plan_id}/edges/{view_id}`

Убрать вид линии с плана (сама линия схемы остаётся).

**Ревизия.** Вне протокола глобальных ревизий.

**Ответ.** `200` `{ok: true, id}`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Плана или вида на этом плане нет. |

**Пример.** `/api/v2/plans/1/edges/6`

Ответ `200`:

```json
{
  "ok": true,
  "id": 6
}
```

#### `POST /api/v2/plans/{plan_id}/layout`

Атомарно сохранить пакет правок плана (элементы и виды линий) с оптимистичной блокировкой по `canvas_revision`.

**Ревизия.** Защита — **`canvas_revision` плана**, а не глобальная ревизия: поле `expected_revision` здесь обязательно и означает значение `canvas_revision`, которое клиент видел. Расхождение — `409 revision_conflict`, пакет не применяется. Успех увеличивает `canvas_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | `canvas_revision` плана из последнего чтения. Можно внутри `{"data": {…}}`. |
| item_ops | массив | нет | Правки элементов: `{op: "remove", id}`; изменение `{id, geometry, coord_space?, label?, sort_order?}`; создание `{kind, geometry, coord_space?, point_id\|location_id\|group_id\|node_id\|target_plan_id, label?, sort_order?}`. |
| edge_view_ops | массив | нет | Правки видов линий: `{op: "remove", id}` или создание/обновление `{edge_id, from_item_id, to_item_id, view_kind?, waypoints?}`; `from_item_id`/`to_item_id` можно задать ссылкой `"$N"` на N-й элемент создаваемого пакета. |

**Ответ.** `200` план с новым `canvas_revision`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет `expected_revision`; операция неверна (вид, геометрия, ссылки). |
| 404 | not_found | Плана нет. |
| 409 | revision_conflict | `canvas_revision` плана изменилась: кто-то уже сохранил (в `message` — ожидалась и текущая). |

**Пример.** `/api/v2/plans/2/layout`

Тело запроса:

```json
{
  "expected_revision": 1,
  "item_ops": [
    {
      "id": 7,
      "geometry": {
        "x": 80,
        "y": 50
      },
      "coord_space": "image_px_xy_v2",
      "label": "Свет"
    },
    {
      "kind": "location",
      "location_id": 1,
      "geometry": [
        [
          10,
          10
        ],
        [
          90,
          10
        ]
      ],
      "coord_space": "image_px_xy_v2",
      "label": "Цех"
    }
  ],
  "edge_view_ops": []
}
```

Ответ `200`:

```json
{
  "id": 2,
  "name": "Склад (1 этаж)",
  "plan_kind": "floor",
  "image_width": 200,
  "image_height": 100,
  "canvas_width": null,
  "canvas_height": null,
  "canvas_revision": 2,
  "is_default": false,
  "created_at": 1792658668,
  "updated_at": 1792658668
}
```

**Пример: revision_conflict (canvas_revision устарела).** `/api/v2/plans/2/layout`

Тело запроса:

```json
{
  "expected_revision": 1,
  "item_ops": [],
  "edge_view_ops": []
}
```

Ответ `409`:

```json
{
  "code": "revision_conflict",
  "message": "План id=2: ожидалась ревизия 1, сейчас 2 — его уже сохранил кто-то ещё",
  "fields": [
    "expected_revision"
  ],
  "ids": [
    2
  ]
}
```

### 2.9. Перенос данных и служебные маршруты

Остаток переноса старой модели (`meters`/`meter_groups`, зоны и связи плана v1) в точки и схему. Данные текущего контроллера тестовые (R9), поэтому в интерфейсе v2 перенос не показывается; маршруты остаются для баз со старыми данными.

#### `GET /api/v2/migration/status`

Состояние миграции: версия схемы БД, поколения домена, карта переноса, число приборов и связей, ещё не перенесённых.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` `{schema_version, code_generation, domain_model_generation, minimum_reader_generation, model_v2_first_write_revision, migration_map, legacy_meters: {total, migrated, not_migrated}, legacy_links: {total, confirmed, unconfirmed}}`. `domain_model_generation >= 2` — старые записывающие маршруты `/api/registry/*` закрыты (см. «Устаревшие маршруты v1»).

**Пример.** `/api/v2/migration/status`

Ответ `200`:

```json
{
  "schema_version": 6,
  "code_generation": 3,
  "domain_model_generation": 2,
  "minimum_reader_generation": 2,
  "model_v2_first_write_revision": "1792658666",
  "migration_map": [],
  "legacy_meters": {
    "total": 3,
    "migrated": 0,
    "not_migrated": 3
  },
  "legacy_links": {
    "total": 0,
    "confirmed": 0,
    "unconfirmed": 0
  }
}
```

#### `GET /api/v2/migration/legacy-links`

Связи плана старой модели (зона → зона) и статус их переноса в линии схемы.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` `{links: [{plan_link_id, plan_id, plan_name, label, source_meter_id, rated_current_a, from_zone, to_zone, migration_status, migrated_edge_id}]}`; `migration_status`: `pending` или `migrated`.

**Пример.** `/api/v2/migration/legacy-links`

Ответ `200`:

```json
{
  "links": []
}
```

#### `POST /api/v2/migration/legacy-links/confirm`

Подтвердить перенос связей пачкой: либо все, либо ни одной.

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| confirmations | непустой массив | да | Решения по связям (формат элемента — `migration_wizard_service.confirm_links`). |

**Ответ.** `200` `{configuration_revision, results}`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Пустой или неверный список; ошибка в любом элементе откатывает всю пачку. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример: пустая пачка.** `/api/v2/migration/legacy-links/confirm`

Тело запроса:

```json
{
  "confirmations": [],
  "expected_revision": 42
}
```

Ответ `400`:

```json
{
  "code": "bad_request",
  "message": "confirmations должен быть непустым списком"
}
```

#### `POST /api/v2/admin/migrate-legacy`

Перенести все строки `meters`/`meter_groups` в точки, приборы, привязки и членство групп. Идемпотентно; перед переносом снимается резервная копия БД.

**Ревизия.** Вне протокола ревизий; защита — явное подтверждение.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| confirm | `true` | да | Без него — отказ: перенос меняет данные. |

**Ответ.** `200` `{backup_path, …отчёт переноса}`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет `confirm: true`. |
| 500 | server_error | Не удалось сделать резервную копию (перенос не запущен) или сам перенос упал (можно повторить; копия — в `backup_path` сообщения). |

**Пример: без confirm.** `/api/v2/admin/migrate-legacy`

Тело запроса:

```json
{}
```

Ответ `400`:

```json
{
  "code": "bad_request",
  "message": "требуется подтверждение: {\"confirm\": true} — перенос меняет данные на контроллере (бэкап делается автоматически, но подтверждение обязательно)",
  "fields": [
    "confirm"
  ]
}
```

### 2.10. Эмулятор счётчиков

Виртуальные счётчики WB-MAP3E внутри службы (`wb-map3e` по виду, MQTT ID вида `emu-…`) для проверки схемы без железа; управляются в «Настройках» набора v2. Без подключённого эмулятора (тестовые стенды) чтение отвечает «недоступен» (`available: false`), а все изменения — `503 service_unavailable`. Лимиты: до 12 приборов, до 100 кВт на фазу. Эмулятор не включает и не выключает себя сам.

#### `GET /api/v2/emulator`

Полное состояние эмулятора: включён ли, приборы, журнал действий, состояние пробы истории, точки, привязанные к приборам эмулятора.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` `{available, enabled, enabled_at, device_count, devices, history: {state, checked_at, message}, journal, banner, bound_points, limits: {max_devices, max_kw}}`. Прибор: `n`, `name`, `device_id`, `role` (`load`/`input`), `kw` (три числа — мощность L1–L3, кВт), `profile` (`flat`/`daily`), `pct` (небаланс ввода, %), `sources` (номера нагрузок ввода), состояние неисправностей. `history.state`: `pending`, `ok`, `missing`, `error`.

**Пример: эмулятор не подключён к службе.** `/api/v2/emulator`

Ответ `200`:

```json
{
  "available": false,
  "enabled": false,
  "enabled_at": null,
  "device_count": 0,
  "devices": [],
  "history": {
    "state": "pending",
    "checked_at": null,
    "message": ""
  },
  "journal": [],
  "banner": "",
  "bound_points": [],
  "limits": {
    "max_devices": 12,
    "max_kw": 100.0
  }
}
```

**Пример: включён, с приборами.** `/api/v2/emulator`

Ответ `200`:

```json
{
  "enabled": true,
  "enabled_at": 1728000000,
  "device_count": 4,
  "devices": [
    {
      "n": 2,
      "device_id": "emu-map3e-2",
      "name": "Эмулятор 2: нагрузка A",
      "serial": "EMU-2-0",
      "kw": [
        1.2,
        0.4,
        0.8
      ],
      "profile": "daily",
      "role": "load",
      "sources": [],
      "pct": 0.0,
      "power_w": [
        364.8,
        117.6,
        243.6
      ],
      "power_total_w": 726.1,
      "energy_kwh": [
        0.0,
        0.0,
        0.0
      ],
      "energy_total_kwh": 0.0,
      "fault": "loss",
      "loss_until": 1728001800,
      "sparse_until": null,
      "is_emulator": true
    },
    {
      "n": 3,
      "device_id": "emu-map3e-3",
      "name": "Эмулятор 3: нагрузка B",
      "serial": "EMU-3-0",
      "kw": [
        0.3,
        2.0,
        0.5
      ],
      "profile": "daily",
      "role": "load",
      "sources": [],
      "pct": 0.0,
      "power_w": [
        87.5,
        620.2,
        146.1
      ],
      "power_total_w": 853.8,
      "energy_kwh": [
        0.0,
        0.0,
        0.0
      ],
      "energy_total_kwh": 0.0,
      "fault": "ok",
      "loss_until": null,
      "sparse_until": null,
      "is_emulator": true
    }
  ],
  "history": {
    "state": "pending",
    "checked_at": null,
    "message": ""
  },
  "journal": [
    {
      "ts": 1728000000,
      "kind": "device_removed",
      "device_id": "emu-map3e-1",
      "text": "Удалён прибор «Эмулятор 1» (emu-map3e-1)"
    },
    {
      "ts": 1728000000,
      "kind": "loss",
      "device_id": "emu-map3e-2",
      "text": "Потеря связи с «Эмулятор 2: нагрузка A» на 30 мин (meta/error = r)"
    }
  ],
  "banner": "Включён эмулятор: 4 виртуальных прибора — цифры с ними не настоящие",
  "limits": {
    "max_devices": 12,
    "max_kw": 100.0
  },
  "available": true,
  "bound_points": []
}
```

_Списки в примере сокращены до двух элементов._

#### `GET /api/v2/emulator/summary`

Лёгкая сводка для баннера «Обзора»: берётся из памяти, к БД не обращается.

**Ревизия.** Только чтение. Ревизию конфигурации не трогает.

**Ответ.** `200` `{enabled, device_count, banner, history_state, history_message}`.

**Пример.** `/api/v2/emulator/summary`

Ответ `200`:

```json
{
  "enabled": false,
  "device_count": 0,
  "banner": "",
  "history_state": null,
  "history_message": ""
}
```

**Пример: эмулятор выключен.** `/api/v2/emulator/summary`

Ответ `200`:

```json
{
  "enabled": false,
  "device_count": 0,
  "banner": "",
  "history_state": "pending",
  "history_message": ""
}
```

#### `POST /api/v2/emulator/enabled`

Включить или выключить эмулятор. Повтор текущего состояния безопасен.

**Ревизия.** Вне протокола ревизий конфигурации (состояние эмулятора хранится отдельно).

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| enabled | булево | да | `true` или `false`. |

**Ответ.** `200` состояние эмулятора (как `GET /api/v2/emulator`, без `bound_points`).

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | `enabled` — не булево. |
| 503 | service_unavailable | Эмулятор не подключён. |

**Пример.** `/api/v2/emulator/enabled`

Тело запроса:

```json
{
  "enabled": true
}
```

Ответ `200`:

```json
{
  "enabled": true,
  "enabled_at": 1728000000,
  "device_count": 0,
  "devices": [],
  "history": {
    "state": "pending",
    "checked_at": null,
    "message": ""
  },
  "journal": [
    {
      "ts": 1728000000,
      "kind": "enabled",
      "device_id": null,
      "text": "Эмулятор включён"
    }
  ],
  "banner": "Включён эмулятор: 0 виртуальных приборов — цифры с ними не настоящие",
  "limits": {
    "max_devices": 12,
    "max_kw": 100.0
  }
}
```

**Пример.** `/api/v2/emulator/enabled`

Тело запроса:

```json
{
  "enabled": false
}
```

Ответ `200`:

```json
{
  "enabled": false,
  "enabled_at": 1728000000,
  "device_count": 4,
  "devices": [
    {
      "n": 2,
      "device_id": "emu-map3e-2",
      "name": "Эмулятор 2: нагрузка A",
      "serial": "EMU-2-0",
      "kw": [
        1.2,
        0.4,
        0.8
      ],
      "profile": "daily",
      "role": "load",
      "sources": [],
      "pct": 0.0,
      "power_w": [
        364.8,
        117.6,
        243.6
      ],
      "power_total_w": 726.1,
      "energy_kwh": [
        0.0,
        0.0,
        0.0
      ],
      "energy_total_kwh": 0.0,
      "fault": "ok",
      "loss_until": null,
      "sparse_until": null,
      "is_emulator": true
    },
    {
      "n": 3,
      "device_id": "emu-map3e-3",
      "name": "Эмулятор 3: нагрузка B",
      "serial": "EMU-3-0",
      "kw": [
        0.3,
        2.0,
        0.5
      ],
      "profile": "daily",
      "role": "load",
      "sources": [],
      "pct": 0.0,
      "power_w": [
        87.5,
        620.2,
        146.1
      ],
      "power_total_w": 853.8,
      "energy_kwh": [
        0.0,
        0.0,
        0.0
      ],
      "energy_total_kwh": 0.0,
      "fault": "ok",
      "loss_until": null,
      "sparse_until": null,
      "is_emulator": true
    }
  ],
  "history": {
    "state": "pending",
    "checked_at": null,
    "message": ""
  },
  "journal": [
    {
      "ts": 1728000000,
      "kind": "disabled",
      "device_id": null,
      "text": "Эмулятор выключен: публикация остановлена, retained-топики стёрты"
    },
    {
      "ts": 1728000000,
      "kind": "device_removed",
      "device_id": "emu-map3e-1",
      "text": "Удалён прибор «Эмулятор 1» (emu-map3e-1)"
    }
  ],
  "banner": "",
  "limits": {
    "max_devices": 12,
    "max_kw": 100.0
  }
}
```

_Списки в примере сокращены до двух элементов._

**Пример: bad_request.** `/api/v2/emulator/enabled`

Тело запроса:

```json
{
  "enabled": "yes"
}
```

Ответ `400`:

```json
{
  "code": "bad_request",
  "message": "enabled: true или false",
  "fields": [
    "enabled"
  ]
}
```

#### `POST /api/v2/emulator/devices`

Добавить виртуальный прибор.

**Ревизия.** Вне протокола ревизий конфигурации.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| name | строка | нет | Имя; по умолчанию «Эмулятор N». |
| role | строка | нет | `load` (по умолчанию) или `input`. |
| kw | массив из 3 чисел | для load | Мощность фаз L1, L2, L3, кВт. |
| profile | строка | нет | `daily` (по умолчанию, суточная волна) или `flat`. |
| sources | массив целых | для input | Номера нагрузок, которые ввод суммирует. |
| pct | число | нет | Небаланс ввода: добавка к сумме нагрузок, % (от −50 до 200). |

**Ответ.** `201` прибор.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Неверные поля (нет `kw` у нагрузки, нет `sources` у ввода, `pct` вне диапазона …). |
| 409 | conflict | Достигнут предел 12 приборов. |
| 503 | service_unavailable | Эмулятор не подключён. |

**Пример.** `/api/v2/emulator/devices`

Тело запроса:

```json
{
  "kw": [
    1,
    2,
    3
  ]
}
```

Ответ `201`:

```json
{
  "n": 1,
  "device_id": "emu-map3e-1",
  "name": "Эмулятор 1",
  "serial": "EMU-1-0",
  "kw": [
    1.0,
    2.0,
    3.0
  ],
  "profile": "daily",
  "role": "load",
  "sources": [],
  "pct": 0.0,
  "power_w": [
    310.1,
    583.6,
    928.6
  ],
  "power_total_w": 1822.2,
  "energy_kwh": [
    0.0,
    0.0,
    0.0
  ],
  "energy_total_kwh": 0.0,
  "fault": "ok",
  "loss_until": null,
  "sparse_until": null,
  "is_emulator": true
}
```

#### `POST /api/v2/emulator/devices/preset`

Добавить типовой набор: три нагрузки с разными фазами и ввод над ними (всего 4 прибора).

**Ревизия.** Вне протокола ревизий конфигурации.

**Ответ.** `201` массив из четырёх приборов.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 409 | conflict | Не хватает места (всего не больше 12 приборов). |
| 503 | service_unavailable | Эмулятор не подключён. |

**Пример.** `/api/v2/emulator/devices/preset`

Тело запроса:

```json
{}
```

Ответ `201`:

```json
[
  {
    "n": 2,
    "device_id": "emu-map3e-2",
    "name": "Эмулятор 2: нагрузка A",
    "serial": "EMU-2-0",
    "kw": [
      1.2,
      0.4,
      0.8
    ],
    "profile": "daily",
    "role": "load",
    "sources": [],
    "pct": 0.0,
    "power_w": [
      364.8,
      117.6,
      243.6
    ],
    "power_total_w": 726.1,
    "energy_kwh": [
      0.0,
      0.0,
      0.0
    ],
    "energy_total_kwh": 0.0,
    "fault": "ok",
    "loss_until": null,
    "sparse_until": null,
    "is_emulator": true
  },
  {
    "n": 3,
    "device_id": "emu-map3e-3",
    "name": "Эмулятор 3: нагрузка B",
    "serial": "EMU-3-0",
    "kw": [
      0.3,
      2.0,
      0.5
    ],
    "profile": "daily",
    "role": "load",
    "sources": [],
    "pct": 0.0,
    "power_w": [
      87.5,
      620.2,
      146.1
    ],
    "power_total_w": 853.8,
    "energy_kwh": [
      0.0,
      0.0,
      0.0
    ],
    "energy_total_kwh": 0.0,
    "fault": "ok",
    "loss_until": null,
    "sparse_until": null,
    "is_emulator": true
  }
]
```

_Списки в примере сокращены до двух элементов._

#### `PATCH /api/v2/emulator/devices/{n}`

Изменить виртуальный прибор. Нужно хотя бы одно поле.

**Ревизия.** Вне протокола ревизий конфигурации.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| name / kw / profile / role / sources / pct | — | хотя бы одно | Как при добавлении. Смена роли на `load` требует `kw`; нагрузку, питающую ввод, нельзя сделать вводом. |

**Ответ.** `200` прибор.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нечего менять или значение неверно. |
| 404 | not_found | Прибора с таким номером нет. |
| 409 | conflict | Прибор питает ввод — сначала уберите его из `sources`. |
| 503 | service_unavailable | Эмулятор не подключён. |

**Пример.** `/api/v2/emulator/devices/1`

Тело запроса:

```json
{
  "kw": [
    2,
    2,
    2
  ]
}
```

Ответ `200`:

```json
{
  "n": 1,
  "device_id": "emu-map3e-1",
  "name": "Эмулятор 1",
  "serial": "EMU-1-0",
  "kw": [
    2.0,
    2.0,
    2.0
  ],
  "profile": "daily",
  "role": "load",
  "sources": [],
  "pct": 0.0,
  "power_w": [
    620.1,
    583.6,
    619.0
  ],
  "power_total_w": 1822.7,
  "energy_kwh": [
    0.0,
    0.0,
    0.0
  ],
  "energy_total_kwh": 0.0,
  "fault": "ok",
  "loss_until": null,
  "sparse_until": null,
  "is_emulator": true
}
```

**Пример: not_found.** `/api/v2/emulator/devices/99`

Тело запроса:

```json
{
  "pct": 1
}
```

Ответ `404`:

```json
{
  "code": "not_found",
  "message": "прибора 99 нет"
}
```

#### `DELETE /api/v2/emulator/devices/{n}`

Удалить виртуальный прибор (его каналы стираются из MQTT, из `sources` вводов он убирается).

**Ревизия.** Вне протокола ревизий конфигурации.

**Ответ.** `200` `{removed: n}`.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 404 | not_found | Прибора с таким номером нет. |
| 503 | service_unavailable | Эмулятор не подключён. |

**Пример.** `/api/v2/emulator/devices/1`

Ответ `200`:

```json
{
  "removed": 1
}
```

#### `POST /api/v2/emulator/devices/{n}/fault`

Вызвать неисправность прибора. Действие пишется в журнал эмулятора.

**Ревизия.** Вне протокола ревизий конфигурации.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| kind | строка | да | `reset` — обнулить показания; `replace` — замена счётчика (новый серийный номер, показания с нуля); `loss` — потеря связи на `minutes` минут; `sparse` — редкие показания на `hours` часов. |
| minutes | целое | для loss | От 1 до 1440. |
| hours | целое | для sparse | От 1 до 72. |

**Ответ.** `200` `{device, journal}` — прибор и последние 10 записей журнала.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Неизвестный `kind`; `minutes`/`hours` вне диапазона. |
| 404 | not_found | Прибора с таким номером нет. |
| 409 | conflict | Эмулятор выключен: неисправности вызываются только на включённом. |
| 503 | service_unavailable | Эмулятор не подключён. |

**Пример.** `/api/v2/emulator/devices/2/fault`

Тело запроса:

```json
{
  "kind": "reset"
}
```

Ответ `200`:

```json
{
  "device": {
    "n": 2,
    "device_id": "emu-map3e-2",
    "name": "Эмулятор 2: нагрузка A",
    "serial": "EMU-2-0",
    "kw": [
      1.2,
      0.4,
      0.8
    ],
    "profile": "daily",
    "role": "load",
    "sources": [],
    "pct": 0.0,
    "power_w": [
      364.8,
      117.6,
      243.6
    ],
    "power_total_w": 726.1,
    "energy_kwh": [
      0.0,
      0.0,
      0.0
    ],
    "energy_total_kwh": 0.0,
    "fault": "ok",
    "loss_until": null,
    "sparse_until": null,
    "is_emulator": true
  },
  "journal": [
    {
      "ts": 1728000000,
      "kind": "reset",
      "device_id": "emu-map3e-2",
      "text": "Сброс счётчика «Эмулятор 2: нагрузка A»: показания обнулены"
    },
    {
      "ts": 1728000000,
      "kind": "device_added",
      "device_id": "emu-map3e-5",
      "text": "Добавлен прибор «Эмулятор 5: ввод» (emu-map3e-5)"
    }
  ]
}
```

_Списки в примере сокращены до двух элементов._

**Пример: kind=loss.** `/api/v2/emulator/devices/2/fault`

Тело запроса:

```json
{
  "kind": "loss",
  "minutes": 30
}
```

Ответ `200`:

```json
{
  "device": {
    "n": 2,
    "device_id": "emu-map3e-2",
    "name": "Эмулятор 2: нагрузка A",
    "serial": "EMU-2-0",
    "kw": [
      1.2,
      0.4,
      0.8
    ],
    "profile": "daily",
    "role": "load",
    "sources": [],
    "pct": 0.0,
    "power_w": [
      364.8,
      117.6,
      243.6
    ],
    "power_total_w": 726.1,
    "energy_kwh": [
      0.0,
      0.0,
      0.0
    ],
    "energy_total_kwh": 0.0,
    "fault": "loss",
    "loss_until": 1728001800,
    "sparse_until": null,
    "is_emulator": true
  },
  "journal": [
    {
      "ts": 1728000000,
      "kind": "loss",
      "device_id": "emu-map3e-2",
      "text": "Потеря связи с «Эмулятор 2: нагрузка A» на 30 мин (meta/error = r)"
    },
    {
      "ts": 1728000000,
      "kind": "reset",
      "device_id": "emu-map3e-2",
      "text": "Сброс счётчика «Эмулятор 2: нагрузка A»: показания обнулены"
    }
  ]
}
```

_Списки в примере сокращены до двух элементов._

#### `POST /api/v2/emulator/probe-history`

Проверить, пишет ли `wb-mqtt-db` историю канала эмулятора (одним запросом).

**Ревизия.** Вне протокола ревизий конфигурации.

**Ответ.** `200` `{state, checked_at, message}`: `ok` — точки истории есть, `missing` — нет (предупреждение), `error` — ошибка самого запроса.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 409 | conflict | Проба истории не подключена или нет приборов. |
| 503 | service_unavailable | Эмулятор не подключён. |

**Пример.** `/api/v2/emulator/probe-history`

Тело запроса:

```json
{}
```

Ответ `409`:

```json
{
  "code": "conflict",
  "message": "проба истории не подключена"
}
```

#### `POST /api/v2/emulator/archive-points`

Архивировать точки, привязанные к приборам эмулятора, одной ревизией (история сохраняется, приборы освобождаются).

**Ревизия.** Меняет конфигурацию через протокол ревизий: **`expected_revision` обязателен** (целое, текущее значение из `GET /api/v2/revision` или из последнего ответа). Устаревшее или отсутствующее значение — `409 revision_conflict`, ничего не записано. В ответе — новая `configuration_revision`.

**Тело запроса** (JSON):

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| expected_revision | целое | да | См. «Протокол ревизий». |
| confirm | `true` | да | Подтверждение: без него — отказ. |

**Ответ.** `200` `{archived, points, configuration_revision}`. Нечего архивировать — `{archived: 0, points: []}` без новой ревизии.

**Ошибки:**

| Статус | `code` | Когда |
|---|---|---|
| 400 | bad_request | Нет `confirm: true`. |
| 409 | revision_conflict | `expected_revision` устарела. |

**Пример: нечего архивировать.** `/api/v2/emulator/archive-points`

Тело запроса:

```json
{
  "confirm": true,
  "expected_revision": 0
}
```

Ответ `200`:

```json
{
  "archived": 0,
  "points": []
}
```

**Пример: bad_request (нет confirm).** `/api/v2/emulator/archive-points`

Тело запроса:

```json
{
  "expected_revision": 0
}
```

Ответ `400`:

```json
{
  "code": "bad_request",
  "message": "требуется подтверждение: {\"confirm\": true} — точки эмулятора уйдут в архив (история сохранится)",
  "fields": [
    "confirm"
  ]
}
```

## 3. Устаревшие маршруты v1

Решение R2: классический интерфейс убирается, учёт ведётся по новой модели. С 0.25.0 (этап 15.2) старые **записывающие** маршруты реестра закрыты.

**Условие.** Как только база перешла на поколение домена 2 (`domain_model_generation >= 2` в `GET /api/v2/migration/status`; поколение объявляет первая
запись через домен-маршрут `/api/v2` — точка, место, группа, элемент плана, привязка …, база без ключа считается поколением 1), любая запись
`POST`/`PATCH`/`PUT`/`DELETE` под `/api/registry/`:

* отвечает **`409`** с `code = "moved_to_v2"`, русским `message`, подсказкой нового маршрута в `replacement` и тем же текстом в `error`
  (его показывает классический интерфейс);
* **не доходит до обработчика**: в БД ничего не меняется, в журнал изменений ничего не пишется.

До поколения 2 (свежая или ещё не перенесённая база) поведение прежнее — запись работает. Правило общее: **любой новый записывающий маршрут под `/api/registry/` закрыт
по умолчанию.**

| Метод | Маршрут v1 | Что делать вместо | Маршрут v2 |
|---|---|---|---|
| POST | `/api/registry/meters` | Счётчики заводятся в интерфейсе v2: «Структура» или «План v3» → «+ Счётчик». | `POST /api/v2/points, затем POST /api/v2/points/{id}/bindings (new_meter)` |
| PATCH | `/api/registry/meters/{device_id}/role` | Роль счётчика задаётся его местом в схеме: ввод щита или отходящая линия. | `PATCH /api/v2/topology/edges/{id}` |
| PATCH | `/api/registry/meters/{device_id}` | Описание точки меняется в «Структуре» (карточка точки), состав учётной группы — там же. | `PATCH /api/v2/points/{id}; POST /api/v2/groups/{id}/members` |
| DELETE | `/api/registry/meters/{device_id}` | Счётчик не удаляется: точку архивируют в «Структуре» (история остаётся). | `PATCH /api/v2/points/{id} с archived=true` |
| POST | `/api/registry/groups` | Учётные группы создаются в интерфейсе v2: «План v3» → «＋ Зона» → «＋ Новая группа». | `POST /api/v2/groups` |
| PATCH | `/api/registry/groups/{group_id}` | Состав учётной группы меняется в карточке точки («Структура»); переименование и объединение групп в v2 не реализованы. | `POST /api/v2/groups/{id}/members; DELETE /api/v2/groups/{id}/members/{point_id}` |
| DELETE | `/api/registry/groups/{group_id}` | Учётные группы в v2 не удаляются (история составов сохраняется); точку можно вывести из группы в карточке точки. | `DELETE /api/v2/groups/{id}/members/{point_id}` |

Пример ответа (`POST /api/registry/groups` на базе поколения 2):

```json
{
  "error": "Запись через старый маршрут /api/registry/groups закрыта: данные ведутся в новой модели учёта. Учётные группы создаются в интерфейсе v2: «План v3» → «＋ Зона» → «＋ Новая группа». Маршрут v2: POST /api/v2/groups.",
  "code": "moved_to_v2",
  "message": "Запись через старый маршрут /api/registry/groups закрыта: данные ведутся в новой модели учёта. Учётные группы создаются в интерфейсе v2: «План v3» → «＋ Зона» → «＋ Новая группа». Маршрут v2: POST /api/v2/groups.",
  "replacement": "POST /api/v2/groups",
  "fields": []
}
```

**Чтение остаётся на один переходный цикл и помечено устаревшим (deprecated):**

| Маршрут | Заменяет |
|---|---|
| `GET /api/registry/meters`, `GET /api/registry/meters/{device_id}` | Точки учёта и приборы: `GET /api/v2/points`, `GET /api/v2/structure/points`, `GET /api/v2/snapshot`. |
| `GET /api/registry/groups` | `GET /api/v2/groups`, `GET /api/v2/groups/{group_id}/members`. |

Эти три маршрута удалят вместе с кодом классического интерфейса отдельной чисткой (решение R5, после месяца работы на контроллере). Остальные маршруты классики
(`/api/meters*`, `/api/plans*`, `/api/summary/consumption`, `/api/reports/balance`, `/api/status` …) этой спецификацией не покрываются и не менялись.

**Командная строка.** Пишущие команды `python -m wb_energy_meter` на базе поколения 2 завершаются кодом **3** (`EXIT_MOVED_TO_V2`) и сообщением в stderr
с указанием, что делать в интерфейсе v2:
`meter add`, `meter rename`, `meter group`, `meter role`, `meter enable`, `meter disable`, `meter remove`, `group remove`, `scan --add-all`.
Диагностические команды работают как раньше: `meter list`, `meter show`, `group list`, `scan` без `--add-all`, `db`, `config`, `consumption`, `history`, `aggregates`.
Код 3 отличается от 1 («не найдено или отменено»), 2 («ошибка») и 130 (Ctrl+C).

## 4. Не реализовано осознанно

**Фоновые задания `metrics/jobs` и ответ `202` (решение R8).** Долгие расчёты с опросом результата в `/api/v2` не делаются, и это сознательно:

* ни один расчётный запрос `/api/v2` не ждёт MQTT-RPC: расчёт идёт по локальным часовым итогам (`aggregates`) и конфигурации, а `api_v2.py`, `accounting_service.py`,
  `overview_service.py` и `reports_service.py` не обращаются к `wb_db_client` (проверено поиском по коду 22.10.2026);
* месячный отчёт на ~100 точках укладывается в 500 мс (~365 мс в фоновом прогоне, `docs/load-test-2026-09.md`); худший случай — залп с другими запросами —
  измеряется отдельно (A40, `docs/acceptance-matrix.md`);
* задание в очереди добавило бы состояние, которое нужно хранить, отменять и показывать, ради ответа, который и так приходит быстрее секунды.

Единственный `/api/v2`-запрос, который ходит в `wb-mqtt-db`, — `POST /api/v2/emulator/probe-history`: ручная проверка одного канала эмулятора, один запрос
с таймаутом (по умолчанию 10 с), не расчёт и не вызывается автоматически.

Вне плана также (большое ТЗ, «Отложить»): аутентификация и права (R1), кольцевые сети, тарифы и биллинг, алерты, подписки на изменения.
Если расчёт когда-нибудь станет тяжелее порога, ему потребуется отдельное решение — сначала измерение (`scripts/loadtest/`), потом задания.

## 5. Что изменилось в 0.25.0

Одно изменение поведения (строки-даты, A44) и добавления; существующие поля не менялись.

| Где | Что |
|---|---|
| `POST /api/v2/metrics/query`, `overview/summary`, `reports/query`, `topology/nodes/{id}/balance`, `GET /change-log` | **Изменение поведения (A44).** Строка-дата в `from`/`to` читается в часовом поясе `timezone` запроса (раньше — в поясе процесса службы, а `timezone` был только подписью). Для клиентов, которые присылают `timezone` (интерфейс делает это всегда), границы теперь совпадают с календарём оператора; сутки перехода часов длятся 23 и 25 часов. Без `timezone` строка читается как UTC. Unix-секунды не затронуты. В ответах `period` и `compare_period` добавлены `from_local`/`to_local` (раздел 1.4). |
| `GET /api/v2/snapshot` | Новое поле точки **`device_id`** — MQTT ID действующего прибора (`null` без привязки). Нужно, чтобы «Обзор» показывал строку «Точка учёта» в карточке прибора (П13). |
| `GET /api/status` (вне `/api/v2`) | Новое поле прибора **`power_units`** — единица канала `Total P` из meta прибора (`"W"`, `"kW"` …) или `null`. Карточки «Приборы» переводят ватты в киловатты только при явном `W`/`Вт`. |
| `GET /` | Адреса `/static/…` в отданной странице получают `?v=<версия>`, а в `<head>` появляется `<meta name="wbem-build">` — страница сравнивает его с версией службы и после обновления показывает плашку «Служба обновлена… перезагрузите страницу» (П14). Корень отдаётся без кэша. |
| `/api/registry/*` (запись) | Закрыта после поколения домена 2: `409 moved_to_v2` (раздел 3). |
| CLI | Пишущие команды на базе поколения 2 — код выхода 3 (раздел 3). |

## 6. Как поддерживать документ

1. Добавили маршрут `/api/v2/…` — добавьте в этот документ заголовок ``#### `МЕТОД /api/v2/путь/{имя}` `` и строку в «Указатель»; без этого `test_step74_api_spec_sync.py` упадёт.
2. Поменяли тело, ответ или коды ошибок — поправьте описание и пример. Пример снимают с демо-стенда (`tests/browser/demo_seed.py`) живым запросом; руками не пишут.
3. Новый код ошибки — строка в таблице 1.2.
