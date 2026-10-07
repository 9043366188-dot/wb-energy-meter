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
| Общие хелперы и жизненный цикл (init, refresh, тема, статус) | **static/js/core.js** |
| Дашборд | **static/js/dashboard.js** |
| Потребление | **static/js/consumption.js** |
| Настройки и CRUD зон/групп | **static/js/settings.js** |
| Массовое назначение зоны | **static/js/zones.js** |
| План v1 | **static/js/plan-v1.js** |
| План v2 | **static/js/plan-v2.js** |
| План v3 | **static/js/plan-v3.js** |
| Обзор v2 | **static/js/overview-v2.js** |
| Отчёты v2 | **static/js/reports-v2.js** |
| Структура | **static/js/structure.js** |

## Инварианты фронтенда

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
- **Leaflet и вкладки на &lt;template x-if&gt;.** Вкладка карты использует x-show,
  не x-if: последнее уничтожает контейнер, на который ссылается L.Map. Храните
  карту вне реактивных данных Alpine, задавайте контейнеру явную высоту и
  вызывайте invalidateSize() после показа скрытой карты. Внутри контейнера
  карты меняйте слои и popup через Leaflet API, а не Alpine-директивами;
  обновляйте их на месте, чтобы периодические обновления не закрывали открытый
  popup.
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