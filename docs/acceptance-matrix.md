# Матрица приёмки A01–A45

Составлена 10.10.2026 в партии 11 (этап 11.2, версия 0.21.0) по разделу 13
`docs/TZ-metering-architecture-dashboard.md`. Назначение: для каждого из
45 сценариев приёмки сказать честно, чем он подтверждён и чего не хватает.
Раньше сценарии упоминались в тестах только в комментариях; эта таблица
первая сводит их в одном месте.

## Как читать

| Знак | Смысл |
|---|---|
| ✅ | Есть тест (или несколько), который проверяет **ожидаемый результат из §13** целиком; функцию теста я открыл и увидел сверку значений |
| 🟡 | Подтверждена часть ожидаемого результата; в колонке «Пробел» указано, какая именно часть не проверена, и в какую партию она уходит |
| ❌ | Теста нет, либо тест показывает, что ожидаемого поведения в коде нет. Для найденных ошибок кода это отдельно перечислено в разделе «Найденные ошибки» |
| вне объёма | Не применимо к текущему этапу (в этой версии таких нет) |

Свидетельство даётся как `файл::функция`. Метка вида `A04` в комментарии
теста ничего не доказывает: ✅ ставится только когда в теле функции есть
проверка нужного значения. Тесты 3.9 и 3.11 прогоняются одним набором
(`tests/run_all.py`), поэтому отдельной колонки для версий Python нет.

## Итог

| Статус | Количество | ID |
|---|---|---|
| ✅ | 32 | A01, A02, A03, A04, A05, A06, A07, A08, A09, A11, A12, A13, A14, A15, A16, A17, A18, A19, A21, A22, A23, A24, A25, A28, A33, A34, A35, A36, A38, A39, A43, A45 |
| 🟡 | 8 | A10, A20, A27, A37, A40, A41, A42, A44 |
| ❌ | 5 | A26, A29, A30, A31, A32 |

В партии 11 закрыты тестами `tests/test_step49_acceptance_gaps.py`: A01,
A04 (объяснение пути перекрытия), A05, A06, A14 (суммарный расход и флаги),
A15, A25, A38 (вторая половина), A39, A44 (границы суток). Для A39 найдена
и исправлена ошибка кода (см. ниже).

В партии 12 (0.22.0) закрыты A07, A13, A14, A16, A21, A23 и остаток A39
(сеть): тесты `test_step55_topology_as_was.py`, `test_step56_aggregates_a13_a14.py`,
`test_step57_retained_a16_daemon.py`, `test_step58_a07_unverified_overview.py`.
A13 и A16 были найденными ошибками кода; решения Кира от 10.10.2026
реализованы, строки ниже обновлены.

В партии 13 (0.23.0) закрыты A02, A33, A34, A35, A36 и A45: браузерные сценарии
`test_b14_responsive.py`, `test_b15_polling.py`, `test_b16_keyboard.py`,
`test_b20_states.py`, `test_b21_matrix_gaps.py`, тесты `test_step32_structure_search.py`
и `test_contrast_tokens.py`. По ходу найдены и исправлены ошибки интерфейса (список —
в `CHANGELOG.md`, 0.23.0); ошибок расчёта партия 13 не нашла.

## Таблица

| ID | Сценарий (кратко) | Статус | Свидетельство | Пробел → партия |
|---|---|---|---|---|
| A01 | Одиночный прибор без групп, планов и сети | ✅ | `test_step49_acceptance_gaps.py::test_a01_single_meter_without_groups_plans_or_topology` — точка 7.5 кВт·ч: `metrics/query` measured = 7.5 (complete), отчёт по точке = 7.5, список групп и планов пуст (200), Обзор отвечает 200 без итога объекта | — |
| A02 | Одна точка в трёх отношениях (место / питание / группа); поиск находит её | ✅ | `test_step32_structure_search.py::test_search_by_name_code_mqtt_serial_and_path` — поиск по имени, коду, MQTT ID, серийнику и пути находит свою точку и не находит чужую; `::test_location_path_reflects_full_hierarchy` — путь места; `test_step32_structure_search.py::test_a02_search_finds_point_by_group_and_feed_node` (партия 13) — одна точка в группе «Котельный контур», с местом «Котельная» и линией от узла «Трансформаторная»: поиск находит её по названию группы, по имени узла питания (узел-получатель и закрытое членство ложных совпадений не дают) и по прежним полям; `test_b21_matrix_gaps.py` — в настоящем браузере первый же поиск после загрузки страницы показывает результаты, «цех» находит обе точки группы, «щр-1» — точки, питающиеся от него | — |
| A03 | Ввод A=100, B=60, C=30, D=20 внутри B | ✅ | `test_step27_overview_summary.py::test_a03_object_total_via_input_not_sum_of_all_meters` (итог 100, B+C=90, небаланс 10, D не задвоен); `test_step40_topology_balance.py::test_t1_object_and_node_balance_with_nested_unbranched_consumer`; `test_step17_accounting_service.py::test_a03_sum_b_c_not_d_double_counted` | — |
| A04 | Явная сумма A+B или B+D | ✅ | `test_step49_acceptance_gaps.py::test_a04_overlap_409_explains_path_and_comparison_allowed` — A+B и B+D → 409 `double_counting`, в тексте «путь от узла … до узла …»; C+D = 50 разрешено; сравнение A=100, B=60 без общего итога; `test_step17_accounting_service.py::test_a04_sum_a_d_rejected_overlap`; `test_step18_api_v2.py::test_metrics_query_sum_overlap_409`; `test_step27_overview_summary.py::test_a04_branch_sum_overlap_via_group_falls_back_to_comparison` (Обзор переходит в режим сравнения) | — |
| A05 | D включена в группу и в её дочернюю группу | ✅ | `test_step49_acceptance_gaps.py::test_a05_point_in_group_and_child_group_counted_once_with_provenance` — Объект{D=20}, дочерняя Цех{D, E=5}: отчёт по Объекту = 25, не 45; `via(D) = [Объект, Цех]`, `via(E) = [Цех]`; `test_step24_group_repo_v2.py::test_resolve_effective_members_dedup_and_provenance`; `test_step17_accounting_service.py::test_a04_sum_same_point_measures_same_node_rejected` (дубликат id в сумме учтён один раз) | — |
| A06 | Две группы пересекаются по точкам | ✅ | `test_step49_acceptance_gaps.py::test_a06_overlapping_groups_rows_comparable_total_by_union` — G1{a=10,b=20}=30, G2{b=20,c=5}=25 (строки без конфликта); итог по объединению {a,b,c} = 35, а не 30+25=55; повтор точки b в запросе учтён один раз | — |
| A07 | Состав есть, топология неизвестна | ✅ | `test_step17_accounting_service.py::test_unverified_topology_marks_structure_quality` (ядро: `structure_quality=unverified`, значение 12 помечено неподтверждённым); `test_step58_a07_unverified_overview.py::test_no_topology_no_confirmed_total_no_percentages` — сети нет: итога объекта нет, сумма группы 12 с `unverified` и без доли, строка отчёта `structure_confirmed=false`; `::test_network_exists_group_outside_it_gets_no_percentage` — итог 100 (verified), группа в сети 60.0 %, группа вне сети (10) без процента | — |
| A08 | Ввод 100; выходы 70 и 40 | ✅ | `test_step17_accounting_service.py::test_a08_signed_imbalance` (−10, −10 %); `test_step27_overview_summary.py::test_a08_imbalance_has_signed_value_and_percent`; `test_step13_accounting_contract.py::test_negative_imbalance_keeps_sign_a08`; `test_step40_topology_balance.py::test_t4_unmetered_branch_visible_and_named` | — |
| A09 | Ввод или обязательный выход неизвестен | ✅ | `test_step17_accounting_service.py::test_a09_missing_output_makes_balance_null` (value=null, availability не complete); `test_step40_topology_balance.py::test_t6_unmetered_single_input_makes_object_total_null`, `::test_t7_partially_metered_inputs_give_known_value_but_no_total` (known_value=100 отдельно, итог и небаланс null, причина `unmetered_input`) | — |
| A10 | Нет данных → `null`; исправная точка с нулём → `0` | 🟡 | `test_step13_accounting_contract.py::test_zero_consumption_is_not_missing`, `::test_all_missing_is_null_not_zero`; `test_step27_overview_summary.py::test_a10_no_data_vs_zero_distinguished` (null/missing и 0.0 в Обзоре и `metrics/query`); `test_step40_topology_balance.py::test_t8_zero_data_vs_missing_data_distinguished_in_network_balance` | Различие `null`/`0` в таблицах экрана и в CSV не проверено ни одним тестом (проверено только в ответах API) → 15 (браузерный проход) |
| A11 | Нулевой ввод или нет базы сравнения → процент `null` с причиной | ✅ | `test_step13_accounting_contract.py::test_percentage_a11`; `test_step27_overview_summary.py::test_a11_percentage_null_when_object_total_missing`; `test_step40_topology_balance.py::test_t9_zero_input_and_output_gives_zero_imbalance_and_null_percent`; `test_step28_reports_query.py::test_delta_percentage_no_division_by_zero_or_missing_base` | — |
| A12 | 9 из 10 часовых агрегатов | ✅ | `test_step17_accounting_service.py::test_a12_partial_hours_measured` (value=None, known_value=90 при 9 из 10 часов); `test_step13_accounting_contract.py::test_partial_sum_example_a12` | — |
| A13 | Накопитель `100 → 0 → 150` | ✅ | `test_step56_aggregates_a13_a14.py::test_a13_aggregate_detects_internal_reset` (агрегат: `reset`, известная часть 150, а не 50), `::test_a13_measured_point_known_part_and_no_fifty` (расход часа неизвестен, известная часть в `known_value` = 160 при соседнем часе 10, 50 нигде), `::test_a13_sum_does_not_leak_known_part`, `::test_a13_hourly_row_never_calls_known_part_consumption`, `::test_a13_legacy_period_calculation`; `test_step13_legacy_scenarios.py::test_internal_reset_detected_a13` (был характеризационным, переписан на правильное ожидание); `test_step17_accounting_service.py::test_a13_reset_not_silently_zero` | Исправлено в партии 12 (решение Кира от 10.10.2026). Уже посчитанные агрегаты не пересчитываются сами — `aggregates recompute`, см. CHANGELOG 0.22.0 |
| A14 | Нет промежуточных записей между граничными накопителями | ✅ | `test_step49_acceptance_gaps.py::test_a14_no_intermediate_samples_total_known_hourly_shares_not_invented` — 100.0 в 00:00 и 160.0 в 04:30: часы 0..4 — `null`/`sparse` (раньше 0, 0, 0, 0, 60), сумма за период 60 с `edge_approx` и пояснением «известен только целиком», период, режущий интервал, неизвестен; `test_step56_aggregates_a13_a14.py::test_a14_sparse_classification_boundaries`, `::test_a14_period_inside_cutting_and_mixed` (10 + 60 + 6 = 76), `::test_a14_realtime_worker_marks_earlier_hours_sparse` (почасовой воркер помечает ранние часы задним числом), `::test_a14_replaced_meter_does_not_merge_runs`, `::test_a14_legacy_hybrid_falls_back_to_rpc_on_sparse` | Решение Кира, партия 12. Порог «редких» показаний — разрыв ≥ 2 часа с ростом накопителя |
| A15 | Энергия не меняется, но жив Uptime; затем потеря связи | ✅ | `test_step49_acceptance_gaps.py::test_a15_unchanged_energy_with_live_uptime_is_ok_then_silence_is_not_green` — энергия не менялась 2 ч при свежих сообщениях: OK; тишина 300 с: WARNING; 700 с (> `no_connection_timeout_s`=600): NO_CONNECTION, зелёного нет | — |
| A16 | Запуск с устаревшими retained-сообщениями | ✅ | `test_step57_retained_a16_daemon.py` (настоящий mosquitto): retained до старта клиента принимается и показывается (энергия 110, U 230), `last_any_ts` не двигается, статус `unconfirmed` «не подтверждено», а не OK; живое сообщение Uptime снимает его (OK); без живого дольше `no_connection_timeout_s` — `no_connection`; повторная доставка retained при переподключении не оживляет прибор | Исправлено в партии 12 (решение Кира). A15 и A38 не затронуты (`test_step49`) |
| A17 | Один WB-MAP3E: total и L1 независимо | ✅ | `test_step15_binding_service.py::test_a17_total_and_phase_rejected_but_three_phases_allowed`; `::test_profiles_overlap_matrix`; `test_step29_replace_meter_provisioning.py` (регрессия старого пути) | — |
| A18 | Замена в 12:00, прежний MQTT ID: 12 + 8 = 20 | ✅ | `test_step15_binding_service.py::test_a18_replace_meter_segments_point_keeps_id`, `::test_a18_clean_hour_boundary_aggregate_not_straddling`; `test_step17_accounting_service.py::test_a18_a19_replace_meter_segmentation_end_to_end` (value = 20.0) | — |
| A19 | Замена в 12:20: цельный агрегат не делится по времени | ✅ | `test_step15_binding_service.py::test_a19_straddling_aggregate_not_split_by_time_share`; `test_step17_accounting_service.py::test_a18_a19_replace_meter_segmentation_end_to_end` | — |
| A20 | Два одновременных запроса: основная установка / питание | 🟡 | `test_step15_binding_service.py::test_a20_concurrent_open_binding_one_wins` — два потока на один физический scope: ровно один успех, второй `BindingConflict` | Гонка по смене питания (двойной родитель узла) отдельно не проверяется: защита есть (уникальный индекс и проверка при публикации, `test_step16_topology_service.py::test_publish_simultaneous_second_input_rejected`), но не под одновременными запросами → 15 |
| A21 | Перенос точки из арендатора А в Б с 15 числа | ✅ | `test_step55_topology_as_was.py::test_a21_a22_group_move_split_by_date` — P1 (1/ч) в А до 120-го часа, в Б после; P2 (2/ч) в А: `structure_mode=as_was` А=100, Б=20 (расход делится по дате), прошлый период не меняется, сравнение периодов 40 vs 60; `test_step28_reports_query.py::test_a21_composition_mode_as_was_keeps_past_report_stable`, `::test_composition_changed_flag_revealed_across_tenant_move` | Закрыто партией 12 (раздел 12.2, `structure_mode`). По умолчанию режим остаётся `current` |
| A22 | Отчёт в режиме `current` | ✅ | `test_step28_reports_query.py::test_a22_current_mode_uses_historically_correct_meter_data` — состав «на сейчас», число за старый период по историческому показанию (40); режим возвращается в ответе (`composition_mode`) | — |
| A23 | Старая БД не знает прежней группы | ✅ | `test_step55_topology_as_was.py::test_a23_period_before_journal_is_assumed_legacy` — период до начала журнала (или пересекающий его): `assumed_legacy=true`, `structure_quality=assumed_legacy`, структура берётся на момент начала журнала; период после начала — `false`/`verified`; режим `current` о «предположении» не знает | Закрыто партией 12 (раздел 12.2). Уточнение: до метки начала журнала берётся структура на момент метки, а не «текущая» |
| A24 | Цикл, самоссылка, второй вход, цикл места/группы | ✅ | `test_step16_topology_service.py::test_self_loop_detected`, `::test_duplicate_parent_detected`, `::test_cycle_detected_with_chain`, `::test_publish_cycle_rejected_and_structure_unchanged`, `::test_publish_simultaneous_second_input_rejected`; `test_step18_api_v2.py::test_topology_publish_cycle_409_and_structure_unchanged`, `::test_locations_crud_and_cycle_409`; `test_step25_api_v2_groups.py::test_group_set_parent_cycle_409` | — |
| A25 | Узел/щит без прибора | ✅ | `test_step49_acceptance_gaps.py::test_a25_panel_without_meter_exists_in_network_and_on_plan_without_invented_values` — щит есть в `GET /topology/nodes` и как элемент однолинейной схемы; баланс узла: `value=null`, `known_value=null`, `availability=missing`, `unavailable_reason=input_unmetered`, вход без точки; выходы X, Y видны; `test_step40_topology_balance.py::test_t5_unmetered_intermediate_line_does_not_block_deeper_boundaries` | — |
| A26 | Точка на двух планах и дважды на одном; холст без картинки | ❌ | Теста нет | Тест и, возможно, правки → 14 |
| A27 | Удаление значка, трассы или плана | 🟡 | `test_step21_plan_v2.py::test_plan_item_removal_preserves_entity` (значок удалён, точка осталась; проверка по `GET /points/<id>`); `::test_plan_delete_cascade_and_404s` (план удалён, элементы уходят вместе с ним) | Для трассы (edge view) и для удаления плана не проверено, что точки, связи и история остаются → 14 |
| A28 | Legacy-линия между двумя группами | ✅ | `test_step31_migration_wizard.py::test_get_legacy_links_lists_pending_with_zone_group_names` (линия видна, статус `pending`, подписи групп); `::test_confirm_creates_draft_edge_with_new_and_existing_node` (создаётся **черновик**, сам не публикуется и питающей связью не становится); `::test_old_model_untouched_after_migration` (старая связь сохранена) | — |
| A29 | Четыре угловые и одна несимметричная метки, zoom/resize/save/reload | ❌ | Теста нет (нужна проверка в браузере) | → 14 |
| A30 | Новый фон другого разрешения; обрезанный/повёрнутый фон | ❌ | Теста нет | → 14 |
| A31 | Токи 10/80/20 А, номинал 100 А → 80 %; пропала фаза → процент неизвестен | ❌ | Теста нет. В v2 расчёта загрузки линии нет: в `api_v2.py` есть только поле `rated_current_a`. Расчёт есть лишь в старом `api.py` (`/api/plans/<id>/live`, строки 1754–1776): берёт максимум по фазам, без правила пропавшей фазы | Реализация в v2 (с правилом пропавшей фазы) и тест → 14 |
| A32 | Нет тока/номинала, у соседней группы есть мощность | ❌ | Теста нет. В старом расчёте при отсутствии тока или номинала `load_pct=None`, состояние `neutral` (процент по мощности не подставляется), но причины в ответе нет; в v2 расчёта нет | Поле «причина» и тест → 14 |
| A33 | Поздний старый ответ не меняет выбранный период | ✅ | `test_b20_states.py` (партия 13, 13.4) — задержанный ответ по «Освещению» не перетирает новый по «Розеткам» в «Отчётах»; задержанный ответ по ЩР-1 не перетирает карточку ЩР-2 в «Структуре»; ответ на другой период прежним отчётом не подменяется; `core.js::feedStart/feedLive` — общий механизм | — |
| A34 | 20 переключений План → Обзор → План | ✅ | `test_b15_polling.py` раздел 6 (партия 13) — 20 переключений «План v3 → Обзор → План v3» настоящим браузером: один `.leaflet-container` у «Плана v3», число слоёв карты и живых таймеров прежнее, открытая карточка и зум сохранились; скрытая вкладка 12 с — ни одного запроса `/api/`; один общий таймер 5 с | — |
| A35 | Два браузера правят один план | ✅ | `test_step21_plan_v2.py::test_layout_revision_conflict_a35` — устаревшая `expected_revision` → 409, чужая правка не затёрта (геометрия осталась 111/55, ревизия 2); `test_step26_revision_protocol.py` — 409 для групп, мест, узлов, связей, публикации и границ баланса; `test_b21_matrix_gaps.py` (партия 13) — конфликт 409 при сохранении «Плана v2»: черновик остаётся на экране, «Загрузить чужую версию, мой черновик оставить» подгружает чужую ревизию, повторное сохранение проходит, в плане обе правки; «Обновить план» черновик отбрасывает; правка к убранному чужой версией элементу отбрасывается с пояснением | — |
| A36 | 360/768/1280/1920 px, обе темы, клавиатура | ✅ | `test_b14_responsive.py` (партия 13) — ширины 360/768/1280/1920 × две темы × все экраны набора v2: `scrollWidth ≤ innerWidth`, поиск и карточка достижимы; `test_b16_keyboard.py` — имена у всех кнопок-значков, видимый фокус, клавиатурный выбор в списках, Escape закрывает панель с возвратом фокуса на трёх ширинах, поля X/Y двигают узел (видно в API); `test_contrast_tokens.py` — 100 пар токенов обеих тем без нарушений; `test_b01_tabs.py` — обход вкладок в обеих темах | — |
| A37 | Без интернета; нет Alpine; нет MQTT | 🟡 | Вендорные файлы лежат локально (`wb_energy_meter/static/vendor/`, внешних URL в `index.html` нет), заглушка при отсутствии Alpine и `/api/selfcheck` покрыты `test_step12_selfcheck.py::test_selfcheck_ok_on_real_static`, `::test_index_html_has_boot_stub_without_alpine_directives`; состав пакета — `test_step45_package_data.py::test_every_static_file_is_covered_by_package_data` | Нет прохода в браузере с заблокированным Alpine и отсутствующим MQTT (проверка статическая) → 15 |
| A38 | Зарегистрирована, но MQTT не видел / MQTT видит незарегистрированную | ✅ | `test_step23_snapshot.py::test_snapshot_never_seen_device` (`device_status=never_seen`, мощность null); `test_step49_acceptance_gaps.py::test_a38_unregistered_mqtt_device_is_not_part_of_accounting` — устройство `ghost` есть в реестре MQTT, но не в `snapshot` и не в списке точек, учёт зарегистрированной точки (7.5) не меняется | — |
| A39 | Отключение/архивирование точки и остаточный MQTT-state | ✅ | `test_step49_acceptance_gaps.py::test_a39_archived_point_leaves_current_composition_history_kept` (группы: P архивирована в 5 ч — as_was[0,1 ч) = 13, as_was[10 ч,11 ч) = 4, current[0,1 ч) = 3, история P = 10); сеть — `test_step55_topology_as_was.py::test_a39_net_input_point_deactivated_line_becomes_unmetered` и `::test_a39_net_output_point_deactivated_unmetered_branch_grows_imbalance`: архивная точка и точка с `enabled=0` на линии не входят в итог объекта и баланс щита с даты выбытия, линия «без счётчика», история до неё сохраняется (небаланс 0 → 40) | Закрыто партией 12. В режиме `current` период делится по дате выбытия |
| A40 | 100 точек / 10 планов / 3 клиента / 30 минут | 🟡 | `docs/load-test-2026-09.md` и `scripts/loadtest/`: 100 точек, 10 планов, 5 клиентов, бюджеты p95 и RSS сняты инструментально; повторный замер партии 11 — «Находка 3» в том же документе | Прогон был 9–10 мин, не 30; на контроллере не запускался → 15 |
| A41 | Миграция 0.11.1: ошибка посередине, повторный запуск | 🟡 | `test_step19_legacy_migration.py::test_migrate_recovers_from_partial_failure` (точка создана, остального нет: повтор довооружает ту же точку, дубликата нет); `::test_migrate_idempotent_rerun`; `::test_migrate_preserves_aggregate_continuity`; `::test_migrate_leaves_db_integrity_clean` | Изображения планов в этих тестах не участвуют; сбой в других точках процесса (после привязки, после `migration_map`) не имитируется → 15 |
| A42 | Откат до и после первой записи v2 | 🟡 | `test_step47_generation_guard.py::test_incompatible_generation_exit_one_with_message` (после первой записи v2 старый код на новой БД не стартует); `test_step22_domain_generation.py::test_mark_v2_domain_write_sets_all_three_keys_atomically` | Откат **до** первой записи v2 (восстановление согласованной тройки код/БД/конфиг через `self-update.sh`) автоматическим тестом не покрыт → 15 |
| A43 | Один контекст/период на обзоре, плане, инспекторе и CSV; конфигурация меняется во время RPC | ✅ | `test_step26_revision_protocol.py::test_a43_metrics_query_pins_snapshot_against_concurrent_write` (запись проходит посреди расчёта, ответ остаётся на ревизии снимка, следующая ревизия — со следующим запросом); `::test_write_does_not_wait_for_reader_and_reader_snapshot_is_unchanged`; `test_step50_db_read_concurrency.py::test_read_block_sees_one_snapshot_a43` (механизм: один `read()` = одна WAL-транзакция); `test_step40_topology_balance.py::test_t12_node_balance_pins_revision_and_rejects_unknown`; `test_step28_reports_query.py::test_reports_query_pins_revision_and_rejects_unknown` | — |
| A44 | Сутки в зоне с переводом часов | 🟡 | `test_step49_acceptance_gaps.py::test_a44_dst_days_have_23_and_25_hours_and_boundary_not_counted_twice` — Europe/Berlin 2026: сутки 29.03 = 23 часа (23.0), соседние = 123.0 (граничный час 100.0 учтён один раз), сумма 146.0 = объединённый период; сутки 25.10 = 25 часов (25.0) | Параметр `timezone` в API только возвращается как подпись; границы суток строит клиент (`from`/`to` в unix-секундах), подписи с правильным offset не формируются → 15 |
| A45 | HTML, кавычки, формульный префикс в названиях | ✅ | `test_step28_reports_query.py::test_a45_csv_cell_neutralizes_formula_prefixes` — `csvCell` нейтрализует `=`, `+`, `-`, `@`, обычный текст и `null` не искажены; `test_b21_matrix_gaps.py` (партия 13) — точка, узел, линия, нагрузка и группа с именем `<img src=x onerror=alert(1)>"'=1+1` (и `<svg onload=…>`): в «Обзоре», «Структуре» (поиск и карточка), подсказках и подписях «Плана v3» и «Отчётах» видны как текст; перехвачены `dialog` и ошибки консоли: ни одного окна, ни ошибки, ни «Failed to load resource»; в DOM нет `<img src=x>` | — |

## Найденные ошибки

Ошибки, найденные при составлении матрицы. Исправлена одна (A39); остальные
я не правил молча: каждая меняет расчёт или связь с MQTT и требует решения Кира.

1. **A39 — исправлено в партии 11.** `MeteringPointRepo.archive()` ставил
   `archived_at` и закрывал версию состояния, но не закрывал членства в
   группах. Архивная точка оставалась в составе групп навсегда, и остаточные
   агрегаты за время после архивации входили в итог группы (в тесте — 11 вместо 4).
   Теперь `archive()` в той же транзакции закрывает открытые членства
   (`valid_to` = момент архивации; членство, начатое «в будущем», закрывается
   пустым интервалом). Строки и история до момента архивации сохраняются.
2. **A13 — исправлено в партии 12.** Накопитель `100 → 0 → 150` внутри часа
   `compute_hourly_aggregate` теперь распознаёт: падение между соседними
   показаниями — `reset`, в агрегат пишется сумма возрастающих участков (150),
   расход часа неизвестен, число 50 не выдаётся. Характеризационный тест
   переписан на правильное ожидание (`test_internal_reset_detected_a13`).
3. **A16 — исправлено в партии 12.** `_handle_message` читает `msg.retain`:
   retained-значения принимаются и показываются, но не двигают `last_any_ts`;
   пока живого сообщения не было — статус `unconfirmed` («не подтверждено»),
   по таймауту связи — `no_connection`, как раньше.
4. **A31/A32 — не реализовано.** Расчёта загрузки линии в v2 нет (только
   старый `/api/plans/<id>/live`, без правила пропавшей фазы и без причины
   отсутствия процента).
5. **A44 — частично.** Границы суток проходят верно (23/25 часов), но сервер
   не знает часового пояса: `timezone` — подпись в ответе.
6. **A21 — исправлено в партии 12.** Отчёт за период, пересекающий дату
   переноса, в режиме `structure_mode=as_was` делит расход по дате; режим по
   умолчанию остаётся `current`.

## Куда уходят пробелы

| Партия | Закрывает |
|---|---|
| 12 | ✅ закрыто: A07, A13, A14, A16, A21, A23, A39 (сеть) |
| 13 | ✅ закрыто: A02 (поиск по группе и узлу), A33, A34, A35 (черновик), A36, A45 (подсказки и SVG) |
| 14 | A26, A27 (трасса, план), A29, A30, A31, A32 |
| 15 | A10 (UI/CSV), A20 (гонка по питанию), A37, A40 (30 минут на контроллере), A41, A42, A44 (подписи offset) |

Партии указаны по `docs/TZ-finish-plan.md`; решение по A13, A14 и A16
принимает Кир.
