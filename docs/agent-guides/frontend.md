# Карта проекта и правила фронтенда

Читайте этот файл при изменении HTML, JavaScript, статики, API-проверки фронтенд-файлов или браузерных сценариев.

## Карта файлов

| Область | Файлы |
|---|---|
| Демон и CLI | **wb_energy_meter/main.py**, **wb_energy_meter/cli.py** |
| HTTP API и конфигурация | **wb_energy_meter/api.py**, **wb_energy_meter/config.py** |
| MQTT и история Wiren Board | **wb_energy_meter/mqtt_client.py**, **wb_energy_meter/wb_db_client.py** |
| SQLite и предметные записи | **wb_energy_meter/db.py**, **wb_energy_meter/repo.py**, **wb_energy_meter/*_repo.py** |
| Статусы и доступность | **wb_energy_meter/status.py**, **wb_energy_meter/alert_repo.py** |
| Каналы, фоновые задачи, модели и журналы | **wb_energy_meter/channels.py**, **wb_energy_meter/background.py**, **wb_energy_meter/model.py**, **wb_energy_meter/logger.py** |
| Самообновление и настройки WB serial | **wb_energy_meter/updater.py**, **wb_energy_meter/wb_serial_config.py** |
| Метаданные изображений и геометрия плана | **wb_energy_meter/image_meta.py**, **wb_energy_meter/plan_geo.py**, **wb_energy_meter/plan_repo.py** |
| Проверка статических ресурсов | **api.py::build_selfcheck_result()**, маршрут **/api/selfcheck** |
| Расход и агрегаты | **wb_energy_meter/consumption.py**, **wb_energy_meter/periods.py**, **wb_energy_meter/aggregator.py**, **wb_energy_meter/aggregates_repo.py** |
| Веб-разметка и сборщик Alpine | **wb_energy_meter/static/index.html** |
| Части веб-интерфейса | **wb_energy_meter/static/js/** |
| Локальные зависимости интерфейса | **wb_energy_meter/static/vendor/** |
| SQL-схема | **wb_energy_meter/migrations/*.sql** |
| Скриптовые проверки | **tests/**, включая **tests/browser/** |
| Установка и самообновление | **scripts/** |

Веб-интерфейс использует Alpine.js и не собирается отдельным frontend-сборщиком.
**index.html** содержит HTML, загрузочную заглушку **div#boot-error** и функцию
**app()**. Файлы **static/js/*.js** регистрируют части в **window.WBEM.parts**;
**app()** объединяет их в объект Alpine. Конфликты имён попадают в
**window.WBEM.conflicts**, чтобы загрузочная заглушка могла показать ошибку
вместо белого экрана.

## Экран → файл

| Экран или назначение | Файл |
|---|---|
| Общие хелперы и жизненный цикл (init, refresh, тема, статус), общий тик опроса и «ленты» запросов (`feed*`) | **static/js/core.js** |
| Адаптивный каркас: брейкпоинты, выдвижная панель карточки, Escape и возврат фокуса, клавиатура в списках (`layoutRowKey`) | **static/js/layout.js** |
| Дашборд | **static/js/dashboard.js** |
| Потребление | **static/js/consumption.js** |
| Настройки и CRUD зон/групп | **static/js/settings.js** |
| Массовое назначение зоны | **static/js/zones.js** |
| План v1 | **static/js/plan-v1.js** |
| План v2 | **static/js/plan-v2.js** |
| План v3 | **static/js/plan-v3.js** |
| Обзор v2 | **static/js/overview-v2.js** |
| Отчёты v2 | **static/js/reports-v2.js** |
| Структура (поиск, карточки точки/линии/группы, история, загрузка списков) | **static/js/structure.js** |
| Эмулятор счётчиков: раздел «Настроек» v2 (приборы, сбои, журнал, архивация точек), плашка «Обзора», подтверждения | **static/js/emulator.js** |
| Схема: дерево «Структуры», «Не на схеме», «+ Счётчик» в карточке элемента, подтверждаемые действия (архивировать, убрать, вывести, удалить план) | **static/js/scheme.js** |
| Однолинейная схема на «Плане v3» (`plan_kind='single_line'`): значки и подписи, ортогональные трассы, P/загрузка линий, баланс щита, автораскладка и отмена, ручная трасса, новая схема, печать | **static/js/single-line.js** |
| Чистые функции раскладки и трасс схемы без DOM (`window.SchemeLayout`; проверяются под `node`, tests/test_step69) | **static/js/scheme-layout.js** |

## Инварианты фронтенда

- **Пересчёт координат плана — одна копия.** `_planXyToLatLng`,
  `_planLatLngToXy`, `_planEffectiveDims` лежат в core.js; «План v2», «План v3» и
  схема вызывают их, своих копий не заводят.
- **Новый экран — отдельный файл в static/js/.** Общие хелперы, которые
  используют два или больше экранов, размещайте в core.js. Разметка всех
  экранов остаётся в index.html.
- **Сборщик сохраняет геттеры.** app() переносит дескрипторы свойств через
  Object.defineProperty и Object.getOwnPropertyDescriptor. Обычное
  присваивание превращает геттеры в значения и ломает реактивность Alpine.
- **Учитывайте общую область имён classic scripts.** Если переменная или
  функция экрана нужна другой части или браузерной проверке, объявляйте её
  на верхнем уровне вне IIFE. Например, tests/browser/test_b02_planv3_clean.py
  обращается к _planV3Map через page.evaluate().
- **Сохраняйте порядок загрузки скриптов.** Сначала vendor, затем core.js,
  остальные части без defer и type="module", затем Alpine.js с defer.
  Не добавляйте ?v=... к src: /api/selfcheck проверяет путь буквально и
  сочтёт такой файл отсутствующим (tests/test_step44_ui_parts.py).
- **Заглушка вместо белого экрана обязана работать без Alpine.** Не добавляйте
  в div#boot-error Alpine-директивы или CSS-переменные темы: при отказе Alpine
  они недоступны. Ошибки загрузки скриптов и стилей ловятся capture-обработчиком
  error; таймер отдельно показывает заглушку, если скрипт загрузился, но
  упал внутри. Успешный init() снимает таймер и скрывает блок.
- **HTML &lt;template&gt; должен быть сбалансирован.** Незакрытый тег делает
  последующую разметку и скрипты инертными, часто без ошибки в консоли.
  Используйте проверку tests/test_step6_webui.py::test_index_html_tag_balance.
  При собственной проверке парсер должен учитывать кавычки: символы < и >
  внутри значения атрибута — не границы тега.
- **Карточка элемента схемы — в index.html дважды, дословно.** `x-teleport`
  принимает только литеральный селектор, поэтому блок между
  `<!-- NODE-CARD:BEGIN` и `<!-- NODE-CARD:END -->` лежит и в «Структуре», и
  в «Плане v3». Правьте обе копии одинаково: идентичность проверяет
  tests/test_step61_scheme_ui.py. «Структура» — `x-if`, «План v3» — `x-show`,
  поэтому в браузерных тестах берите `[data-testid=node-card]:visible`.
- **Подписи типов элементов схемы — только из `NODE_KINDS` (core.js).** В
  разметке `<select>` строится по `nodeKinds`, подпись — `nodeKindLabel(kind)`.
  Слово «узел» названием типа не служит.
- **Формы и окна для `x-if` — не `null`.** Alpine перечитывает вложенные
  выражения до того, как уберёт блок, и `x.field` на `null` пишет в консоль
  «Cannot read properties of null». `uiConfirm` и `nodeMeterForm` — всегда
  объекты, открытость — `uiConfirmOpen` / `nodeMeterOpen`; чтения
  `structInspector` в карточках — через `((structInspector||{}).data||{})`.
  Так же результат отчёта: внутри `x-if="!rv2Loading && rv2Result"` читайте
  `rv2R()` (`rv2Result || {}`), а не `rv2Result.` — эффекты внутри `x-if`
  вычисляются и после того, как `rv2Result` стал `null` (при смене среза или
  повторном запуске), и консоль заполняется ошибками. Проверяет
  tests/browser/test_b11_flaky_regressions.py (раздел 4) и консольный контроль
  репетиции пилота (tests/pilot/rehearsal.py).
- **`<select>` с `x-for`-вариантами** — `:selected` на `<option>`, иначе
  `x-model` до появления вариантов показывает первый из них.
- **Запросы экрана — через «ленты» `core.js`, таймеров не заводить.**
  `feedStart(key, {live, skipIfBusy})` обесценивает предыдущий запрос того же
  ключа и отменяет его (`AbortController`); после `await` проверяйте
  `feedLive(token)`, иначе запоздавший ответ перетрёт новый (A33). Периодический
  опрос — только через общий `_pollTick` (один `setInterval` 5 с; скрытая
  вкладка не опрашивает, при возврате — одно обновление); периодические запросы
  — `skipIfBusy`, чтобы на каждый тип запроса приходился один незавершённый.
  Новый `setInterval` для экрана — нарушение §10: `test_b15_polling.py`
  считает живые таймеры. `init()` идемпотентен (`_initDone`): Alpine вызывает
  его сам, и ещё есть `x-init="init()"`. Состояния экрана — loading / empty /
  ready / partial / error и «устарело, обновлено ЧЧ:ММ» (`feedStale*`), а не
  пустой экран; при обрыве прежние данные остаются.
- **Адаптивность: брейкпоинты в двух местах.** `@media` в `index.html` (блок
  «адаптивность») и `MQ_NARROW`/`MQ_PHONE` в `layout.js` — ≥ 1280, 768–1279,
  < 768 — меняйте вместе. Состояние карточки одно на всех ширинах
  (`structInspector`); панель только показывает его иначе. У страницы нет
  горизонтального скролла: широкие таблицы — внутри `.tbl-scroll`.
  `viewport` у классического набора — `width=1100`, как раньше; у набора v2 —
  `device-width` (переключает `layout.js`). Проверяют `test_b14_responsive.py`,
  `test_b16_keyboard.py`.
- **Доступность — часть кнопки, а не отдельная правка.** У кнопки-значка
  `aria-label`; строка списка, которую можно выбрать, — `tabindex="0"`,
  `role="button"`, `data-kb-row`; видимый фокус даёт `:focus-visible`; цвета —
  только токены темы (`--txt3`, `--accent`, `--line-ctl`…): контраст пар токенов
  проверяет `tests/test_contrast_tokens.py`, нарушение чинится в токене.
- **Браузерные тесты: ждать состояния, не времени.** `tests/browser/waits.py`
  (`wait_map_v3_ready`, `wait_card_open`, `wait_card_closed`, `close_card`) вместо
  `wait_for_timeout` там, где ждёте карту или карточку; `fetch_tools.FETCH_WRAP`
  (через `open_browser(init_script=…)`) — обрыв, задержка и журнал запросов,
  журнал `setInterval`; `demo_seed.seed_demo` — одна демо-схема для снимков и
  сценариев; `shots.py` — снимки экранов. Серию на медленном процессоре
  запускайте с `WBEM_BROWSER_CPU_THROTTLE=4`. Правка
  `docs/browser-checklist-plan-v3.md` и правка `test_b17_checklist_plan_v3.py` —
  в одном PR: тест идёт по шагам файла в том же порядке.
- **Leaflet и вкладки на &lt;template x-if&gt;.** Вкладка карты использует x-show,
  не x-if: последнее уничтожает контейнер, на который ссылается L.Map. Храните
  карту вне реактивных данных Alpine, задавайте контейнеру явную высоту и
  вызывайте invalidateSize() после показа скрытой карты. Внутри контейнера
  карты меняйте слои и popup через Leaflet API, а не Alpine-директивами;
  обновляйте их на месте, чтобы периодические обновления не закрывали открытый
  popup.
  Подгонка под размер контейнера — `planV3InvalidateMap()` (с троттлингом
  150 мс; зум и открытая карточка не сбрасываются). Перетаскивание значка
  включайте по событию `add` слоя (`layer.on('add', …enableLayerDrag)`): Geoman
  включает его только у слоя, уже лежащего на карте. Узлы держите выше линий,
  зоны — ниже, иначе линия перехватывает клик по значку.
- **/health не видит белый экран — /api/selfcheck видит.** /health подтверждает
  состояние сервиса, но не загрузку Alpine и других файлов. Самопроверка
  проверяет ссылки из index.html непосредственно на диске; её HTTP-код
  всегда 200, результат нужно читать в поле ok. Автообновление учитывает
  ok: false; 404 допустим при откате на версию без этого endpoint.

- **Новые вложенные каталоги static включайте в package-data.** Маска
  setuptools static/* не рекурсивна; без отдельной записи файлы могут
  отсутствовать в собранном пакете, хотя сборка завершилась успешно. Проверяйте
  состав wheel при изменении такой упаковки (tests/test_step45_package_data.py).
Переключение вкладок обычно использует tab == '...'; у старой вкладки
«План» отдельное поведение. Для ручного пользовательского прохода по «Плану
v3» используйте **docs/browser-checklist-plan-v3.md**.