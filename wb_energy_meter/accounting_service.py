"""Расчётный сервис — measured/sum/balance/comparison (ТЗ §5.1/§5.2).

"Сердце ТЗ" (формулировка пользователя) — пишется и проверяется лично,
не делегировано. Собирает воедино:

- accounting_contract.MetricResult/known_sum/resolve_percentage — форма
  результата и инварианты null-vs-0 (этап A);
- binding_service.resolve_primary_segments/classify_aggregate_against_segment
  — сегментация периода по истории привязок точки, запрет пропорционального
  деления агрегата на границе замены прибора (этап B, A18/A19);
- topology_service — достижимость по активному графу сети для отклонения
  электрически пересекающихся sum() (A03/A04).

Профиль привязки выбирает источник итогов (партия находок 0.22.3, П9,
docs/design-phase-aggregates.md): `total_3p` — `period_aggregates` (итог
прибора, `Total AP energy`), `phase_l1`…`phase_l3` — `phase_aggregates`
(часы фазы, `AP energy Lx`). Раньше профиль не смотрели вовсе, и точка на
фазе получала расход всего прибора. Сегментация по времени и замене прибора
(A18/A19, агрегат на границе не делится) от профиля не зависит.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

from .accounting_contract import (
    MetricResult, ResultPeriod, known_sum, resolve_percentage,
    MODE_MEASURED, MODE_SUM, MODE_BALANCE, MODE_COMPARISON,
    AVAILABILITY_COMPLETE, AVAILABILITY_PARTIAL, AVAILABILITY_MISSING,
    STRUCTURE_VERIFIED, STRUCTURE_UNVERIFIED,
    SOURCE_MEASURED, SOURCE_CALCULATED,
    QF_GAP, QF_EDGE_APPROX, QF_RESET, QF_SPARSE,
)
from .binding_service import resolve_primary_segments
from .topology_service import build_children_map, is_descendant

log = logging.getLogger(__name__)

HOUR = 3600


def _repo_for_profile(aggregates_repo, channel_profile):
    """Репозиторий часовых итогов для профиля привязки сегмента: `total_3p`
    — сам `aggregates_repo` (итоги прибора), `phase_lN` — итоги фазы N
    (`AggregateRepo.for_profile`). Репозиторий без `for_profile` пофазные
    итоги дать не может — это ошибка конфигурации, а не «нет данных»: молча
    отдать итог прибора фазе значило бы вернуть неверное число."""
    if channel_profile in (None, "total_3p"):
        return aggregates_repo
    for_profile = getattr(aggregates_repo, "for_profile", None)
    if for_profile is None:
        raise ValueError(
            "репозиторий агрегатов не умеет пофазные итоги (профиль %r)"
            % (channel_profile,))
    return for_profile(channel_profile)


class AccountingConflict(ValueError):
    """A04: явная сумма с электрическим перекрытием отклонена — API-слой
    должен вернуть 409 с описанием пути перекрытия (не молча исключать
    точку из состава)."""


def _align_hour(ts):
    return ts - (ts % HOUR)


def _hour_periods(ts_from, ts_to):
    """Часовые окна [h_start, h_end), выровненные на час, покрывающие
    [ts_from, ts_to) — та же гранулярность, что и period_aggregates."""
    if ts_to <= ts_from:
        return []
    h = _align_hour(ts_from)
    periods = []
    while h < ts_to:
        periods.append((h, h + HOUR))
        h += HOUR
    return periods


def _clock(ts, timezone_name):
    """ЧЧ:ММ момента `ts` в часовом поясе отчёта (UTC, если пояс неизвестен)."""
    from datetime import datetime, timezone as _utc
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001 — нет базы поясов / неверное имя
        tz = _utc.utc
    return datetime.fromtimestamp(int(ts), tz).strftime("%H:%M")


class _SparseRuns:
    """A14 (партия 12): ряды подряд идущих часов `sparse` одного прибора.

    Расход внутри интервала редких показаний известен только целиком (часы
    хранятся как `ap_energy_delta=NULL`, но с `start`/`end` накопителя).
    Ряд даёт известную сумму `sum(end - start)` по часам ряда, если он
    ЦЕЛИКОМ лежит в запрошенном периоде и внутри одного сегмента привязки;
    иначе часы ряда внутри периода неизвестны. Такая же осторожность, если ряд
    не замкнут (следующий час — сброс) или сумма отрицательна."""

    def __init__(self, aggregates_repo, meter_id, requested_hours):
        self._repo = aggregates_repo
        self._meter_id = meter_id
        self._requested = requested_hours       # set(period_start) запрошенных часов
        self._ext = {}                          # докачанные соседние строки
        self.runs = {}                          # period_start -> run dict

    def _row(self, loaded, h):
        if h in loaded:
            return loaded[h]
        if h not in self._ext:
            self._ext[h] = self._repo.get(self._meter_id, h)
        return self._ext[h]

    def resolve(self, h, loaded, seg):
        if h in self.runs:
            return self.runs[h]
        first = h
        while True:
            prev = self._row(loaded, first - HOUR)
            if prev is not None and prev.quality_flag == QF_SPARSE:
                first -= HOUR
            else:
                break
        last = h
        while True:
            nxt = self._row(loaded, last + HOUR)
            if nxt is not None and nxt.quality_flag == QF_SPARSE:
                last += HOUR
            else:
                break
        hours = list(range(first, last + HOUR, HOUR))
        rows = [self._row(loaded, x) for x in hours]
        inside = all(
            x in self._requested and x >= seg.ts_from and x + HOUR <= seg.ts_to
            for x in hours)
        total = 0.0
        known = True
        for r in rows:
            if r.ap_energy_start is None or r.ap_energy_end is None:
                known = False
                break
            total += r.ap_energy_end - r.ap_energy_start
        after = self._row(loaded, last + HOUR)
        closed = not (after is not None and after.quality_flag == QF_RESET)
        run = {
            "first": first, "last_end": last + HOUR, "hours": hours,
            "valid": bool(inside and known and closed and total >= 0),
            "total": round(total, 6) if known else None,
        }
        for x in hours:
            self.runs[x] = run
        return run


# ---------------------------------------------------------------------
# measured — §5.1: расход/показатель одной точки.
# ---------------------------------------------------------------------

def measured_point(point_binding_repo, aggregates_repo, meter_source_repo,
                    point_id, ts_from, ts_to, timezone="UTC", _cache=None):
    """§5.1 measured. Сегментирует период по истории привязок точки
    (resolve_primary_segments) и суммирует часовые агрегаты только внутри
    одного сегмента за раз. Час, пересекающий границу замены прибора,
    НЕ делится по доле времени (A19) — считается неизвестным для этого
    часа, quality_flags получает edge_approx как пояснение "частично
    покрыт, но не сведён".

    `_cache` (партия 10, этап E, docs/load-test-2026-09.md) — необязательный
    dict {(point_id, ts_from, ts_to, timezone): MetricResult}, общий на ОДИН
    HTTP-запрос. Без него ничего не меняется (совместимость со всеми
    существующими вызовами). С ним — устраняет конкретно найденную нагрузочным
    прогоном избыточность: overview/summary и reports/query считают
    measured_point для ОДНИХ И ТЕХ ЖЕ точек по нескольку раз за один ответ
    (один раз — для показа по отдельности, второй — внутри
    balance_from_point_sets/sum_points при расчёте небаланса; у reports/query
    ещё и третий — при формировании строк отчёта). Ключ кэша — сама точка И
    период, поэтому подстановка результата детерминирована и не зависит от
    порядка вызовов."""
    cache_key = (point_id, ts_from, ts_to, timezone)
    if _cache is not None and cache_key in _cache:
        return _cache[cache_key]

    bindings = point_binding_repo.list_for_point(point_id)
    segments = resolve_primary_segments(bindings, ts_from, ts_to)

    hours = _hour_periods(ts_from, ts_to)
    expected_ids = [str(h) for h, _ in hours]
    parts: List[Optional[float]] = []
    valid_ids: List[str] = []
    flags = set()

    # Партия 10, этап E (нагрузочная проверка, docs/load-test-2026-09.md):
    # раньше здесь было ДО ДВУХ SQL-запросов НА КАЖДЫЙ ЧАС периода —
    # meter_source_repo.get_by_id() и aggregates_repo.get() по одному часу.
    # На месячном отчёте (~720 часов) на КАЖДУЮ точку/ветвь это давало
    # тысячи запросов на один HTTP-ответ (нагрузочный прогон на ~100
    # точках поймал 9-28 тысяч запросов на один overview/summary или
    # reports/query — на порядки больше, чем реально нужно). Сегментов у
    # точки на практике 1-2 (замена прибора внутри запрошенного периода —
    # редкость), поэтому источник и агрегаты сегмента загружаются ОДНИМ
    # запросом каждый — на сегмент, а не на час; результат раскладывается
    # по часам в памяти. Логика по часам (гэпы/edge_approx/reset) не
    # изменилась ни на строчку — изменился только способ достать данные.
    # П9: итоги берутся из репозитория ПРОФИЛЯ сегмента (прибор или фаза).
    seg_data = {}  # id(segment) -> (meter_id | None, {period_start: HourlyAggregate}, repo)
    for seg in segments:
        src = meter_source_repo.get_by_id(seg.meter_source_id)
        if src is None:
            seg_data[id(seg)] = (None, {}, None)
            continue
        seg_repo = _repo_for_profile(aggregates_repo, seg.channel_profile)
        rows = seg_repo.list_range(src.meter_id, seg.ts_from, seg.ts_to)
        seg_data[id(seg)] = (src.meter_id, {a.period_start: a for a in rows}, seg_repo)

    requested_hours = set(h for h, _ in hours)
    sparse_by_seg = {}          # id(segment) -> _SparseRuns
    reset_known = 0.0           # A13: известные части часов со сбросом
    reset_known_seen = False
    whole_only = []             # A14: интервалы, известные только целиком
    sparse_unknown = []         # A14: ряды `sparse`, не вошедшие в период целиком
    seen_runs = set()

    for h_start, h_end in hours:
        hour_id = str(h_start)
        covering = [s for s in segments if h_start >= s.ts_from and h_end <= s.ts_to]

        if len(covering) != 1:
            parts.append(None)
            touches_boundary = any(
                s.ts_from < h_end and h_start < s.ts_to for s in segments
            )
            flags.add(QF_EDGE_APPROX if touches_boundary else QF_GAP)
            continue

        seg = covering[0]
        meter_id, agg_by_hour, seg_repo = seg_data[id(seg)]
        if meter_id is None:
            parts.append(None)
            flags.add(QF_GAP)
            continue

        agg = agg_by_hour.get(h_start)

        # A14: час внутри интервала редких показаний
        if agg is not None and agg.quality_flag == QF_SPARSE:
            runs = sparse_by_seg.get(id(seg))
            if runs is None:
                runs = _SparseRuns(seg_repo, meter_id, requested_hours)
                sparse_by_seg[id(seg)] = runs
            run = runs.resolve(h_start, agg_by_hour, seg)
            if run["valid"]:
                flags.add(QF_EDGE_APPROX)
                if h_start == run["first"]:
                    parts.append(run["total"])
                    whole_only.append(run)
                else:
                    parts.append(0.0)
                valid_ids.append(hour_id)
            else:
                flags.add(QF_SPARSE)
                parts.append(None)
                if run["first"] not in seen_runs:
                    seen_runs.add(run["first"])
                    sparse_unknown.append(run)
            continue

        # A13: сброс внутри часа — расход часа неизвестен, известная часть
        # (сумма возрастающих участков) учитывается отдельно
        if agg is not None and agg.quality_flag == QF_RESET:
            parts.append(None)
            flags.add(QF_RESET)
            if agg.ap_energy_delta is not None:
                reset_known += agg.ap_energy_delta
                reset_known_seen = True
            continue

        if agg is None or agg.ap_energy_delta is None:
            parts.append(None)
            flags.add(QF_GAP)
            continue

        if agg.quality_flag and agg.quality_flag not in ("ok",):
            flags.add(agg.quality_flag)

        parts.append(agg.ap_energy_delta)
        valid_ids.append(hour_id)

    value, known_value, expected_count, valid_count, missing_ids = known_sum(
        parts, expected_ids, valid_ids
    )

    explanation = {"point_id": point_id}

    # A13: известные части часов со сбросом входят в known_value, пока есть хоть
    # один полностью известный час (инвариант §5.3: без валидных частей
    # known_value обязан быть null) — иначе они остаются только в пояснении.
    if reset_known_seen:
        explanation["reset_known_kwh"] = round(reset_known, 6)
        if value is None and valid_count > 0:
            known_value = round((known_value or 0.0) + reset_known, 6)

    # A14: что известно только целиком — для подписи «расход за ЧЧ:ММ–ЧЧ:ММ
    # известен только целиком»
    if whole_only or sparse_unknown:
        notes = []
        if whole_only:
            explanation["whole_only_intervals"] = [
                {"from": r["first"], "to": r["last_end"], "value": r["total"]}
                for r in whole_only]
            for r in whole_only:
                notes.append(
                    f"расход за {_clock(r['first'], timezone)}–"
                    f"{_clock(r['last_end'], timezone)} известен только целиком")
        if sparse_unknown:
            explanation["sparse_unknown_intervals"] = [
                {"from": r["first"], "to": r["last_end"]} for r in sparse_unknown]
            for r in sparse_unknown:
                notes.append(
                    f"расход за {_clock(r['first'], timezone)}–"
                    f"{_clock(r['last_end'], timezone)} известен только целиком, "
                    f"а период охватывает его частично — по часам он неизвестен")
        explanation["notes"] = notes

    if expected_count == 0 or valid_count == 0:
        availability = AVAILABILITY_MISSING
    elif valid_count == expected_count:
        availability = AVAILABILITY_COMPLETE
    else:
        availability = AVAILABILITY_PARTIAL

    result = MetricResult(
        metric="energy_import", unit="kWh", mode=MODE_MEASURED,
        value=value, known_value=known_value, availability=availability,
        quality_flags=sorted(flags), structure_quality=STRUCTURE_VERIFIED,
        source=SOURCE_MEASURED if availability == AVAILABILITY_COMPLETE else SOURCE_CALCULATED,
        expected_count=expected_count, valid_count=valid_count,
        missing_ids=missing_ids,
        period=ResultPeriod(ts_from=str(ts_from), ts_to=str(ts_to), timezone=timezone),
        explanation=explanation,
    )
    if _cache is not None:
        _cache[cache_key] = result
    return result


# ---------------------------------------------------------------------
# sum — §5.1/A03/A04/A05: сумма независимых измеряемых сечений.
# ---------------------------------------------------------------------

def _resolve_measured_roots(edge_repo):
    """point_id -> to_node_id для каждой точки, у которой сейчас есть
    действующая (published, открытая) основная электрическая связь."""
    active_edges = edge_repo.list_active_published()
    children_map = build_children_map(active_edges)
    root_by_point = {}
    for e in active_edges:
        if e.primary_point_id is not None:
            root_by_point[e.primary_point_id] = e.to_node_id
    return root_by_point, children_map


def check_sum_overlap(edge_repo, point_ids):
    """A04: попарно проверяет, не является ли одна из точек электрическим
    потомком другой в опубликованной сети. Бросает AccountingConflict с
    описанием конкретного пути перекрытия при первом найденном конфликте
    (§5.1: "sum отклоняется с объяснением пути A -> ... -> B"). Точки без
    известной электрической привязки просто пропускаются здесь —
    structure_quality=unverified решает вызывающий код (sum_points)."""
    root_by_point, children_map = _resolve_measured_roots(edge_repo)

    for i in range(len(point_ids)):
        for j in range(i + 1, len(point_ids)):
            pi, pj = point_ids[i], point_ids[j]
            ri, rj = root_by_point.get(pi), root_by_point.get(pj)
            if ri is None or rj is None:
                continue
            if ri == rj:
                raise AccountingConflict(
                    f"точки {pi} и {pj} измеряют один и тот же узел сети — "
                    f"суммирование даст двойной счёт"
                )
            if is_descendant(children_map, ri, rj):
                raise AccountingConflict(
                    f"точка {pj} находится ниже точки {pi} в электрической сети "
                    f"(путь от узла {ri} до узла {rj}) — суммирование даст двойной "
                    f"счёт; варианты: оставить только {pi}, сравнить точки по "
                    f"отдельности, либо посчитать небаланс между ними"
                )
            if is_descendant(children_map, rj, ri):
                raise AccountingConflict(
                    f"точка {pi} находится ниже точки {pj} в электрической сети "
                    f"(путь от узла {rj} до узла {ri}) — суммирование даст двойной "
                    f"счёт; варианты: оставить только {pj}, сравнить точки по "
                    f"отдельности, либо посчитать небаланс между ними"
                )

    return root_by_point


def sum_points(point_binding_repo, aggregates_repo, meter_source_repo, edge_repo,
               point_ids, ts_from, ts_to, timezone="UTC", _cache=None):
    """§5.1 sum: сумма независимых точек. A05: повторяющиеся ID точки
    учитываются один раз (дедупликация с сохранением порядка). A04:
    электрическое перекрытие отклоняется целиком, а не молча исключает
    точку из состава.

    `_cache` — см. measured_point(); пробрасывается без изменения смысла."""
    point_ids = list(dict.fromkeys(point_ids))  # дедуп, порядок сохранён

    root_by_point = check_sum_overlap(edge_repo, point_ids)

    structure_quality = STRUCTURE_VERIFIED
    if any(root_by_point.get(p) is None for p in point_ids):
        structure_quality = STRUCTURE_UNVERIFIED

    results = [
        measured_point(point_binding_repo, aggregates_repo, meter_source_repo,
                        p, ts_from, ts_to, timezone, _cache=_cache)
        for p in point_ids
    ]

    # "Атомарная часть" суммы для known_sum() — ПОЛНОЕ значение точки
    # (result.value), а не её частично известный known_value. Иначе
    # известная часть одной неполной точки могла бы протечь в общий
    # value/known_value так, будто эта точка сама полностью известна —
    # противоречит инварианту §5.3 "неполнота одной части не выдаётся за
    # полноту целого".
    parts = [r.value for r in results]
    expected_ids = [str(p) for p in point_ids]
    valid_ids = [str(p) for p, r in zip(point_ids, results) if r.value is not None]

    value, known_value, expected_count, valid_count, missing_ids = known_sum(
        parts, expected_ids, valid_ids
    )

    if expected_count == 0 or valid_count == 0:
        availability = AVAILABILITY_MISSING
    elif valid_count == expected_count:
        availability = AVAILABILITY_COMPLETE
    else:
        availability = AVAILABILITY_PARTIAL

    flags = set()
    for r in results:
        flags.update(r.quality_flags)

    return MetricResult(
        metric="energy_import", unit="kWh", mode=MODE_SUM,
        value=value, known_value=known_value, availability=availability,
        quality_flags=sorted(flags), structure_quality=structure_quality,
        source=SOURCE_CALCULATED,
        expected_count=expected_count, valid_count=valid_count,
        missing_ids=missing_ids,
        period=ResultPeriod(ts_from=str(ts_from), ts_to=str(ts_to), timezone=timezone),
        explanation={"included_point_ids": point_ids},
    )


# ---------------------------------------------------------------------
# balance — §5.2: R = ΣE_in − ΣE_out, подписанный небаланс.
# ---------------------------------------------------------------------

def balance_from_point_sets(point_binding_repo, aggregates_repo, meter_source_repo,
                             edge_repo, input_ids, output_ids, ts_from, ts_to,
                             timezone="UTC", _cache=None):
    """§5.2 A08/A09, ядро (партия 7, Этап 2, Э2.2): R = ΣE_in − ΣE_out
    сохраняет знак; неполнота обязательного входа/выхода делает строгий
    небаланс null (value=None), но известные части остаются отдельно
    подписанными в known_value. Семантика ТА ЖЕ, что была в balance() до
    партии 7 — просто вынесена из-под чтения balance_scopes, чтобы её же
    использовал баланс по электрической схеме (overview_service.py):
    входы/выходы там — не члены ручной "границы баланса", а точки,
    вычисленные из топологии (назначенный ввод узла/объекта и
    first_measurements). balance() ниже — тонкая обёртка над этим ядром
    для balance_scopes; test_step17/test_step28 не должны увидеть разницы
    в поведении.

    `_cache` (партия 10, этап E) — см. measured_point(): здесь особенно
    важен, потому что input_ids/output_ids overview_service.object_summary()
    и balance_node() уже успели измерить ПО ОТДЕЛЬНОСТИ (для показа) до
    вызова этой функции — без общего кэша те же точки за тот же период
    считались бы ЕЩЁ РАЗ."""
    in_result = (
        sum_points(point_binding_repo, aggregates_repo, meter_source_repo, edge_repo,
                   input_ids, ts_from, ts_to, timezone, _cache=_cache)
        if input_ids else None
    )
    out_result = (
        sum_points(point_binding_repo, aggregates_repo, meter_source_repo, edge_repo,
                   output_ids, ts_from, ts_to, timezone, _cache=_cache)
        if output_ids else None
    )

    in_value = in_result.value if in_result else None
    out_value = out_result.value if out_result else None

    pct, pct_reason = None, "no_data"
    if in_value is not None and out_value is not None:
        r = round(in_value - out_value, 6)
        value = r
        known_value = r
        pct, pct_reason = resolve_percentage(r, in_value)
    else:
        value = None
        in_known = in_result.known_value if in_result else None
        out_known = out_result.known_value if out_result else None
        known_value = (
            round(in_known - out_known, 6)
            if in_known is not None and out_known is not None else None
        )

    valid_count = (in_result.valid_count if in_result else 0) + \
                  (out_result.valid_count if out_result else 0)
    expected_count = len(input_ids) + len(output_ids)

    if expected_count == 0 or (in_result is None) or (out_result is None):
        availability = AVAILABILITY_MISSING
    elif value is not None:
        availability = AVAILABILITY_COMPLETE
    elif known_value is not None:
        availability = AVAILABILITY_PARTIAL
    else:
        availability = AVAILABILITY_MISSING

    flags = set()
    structure_quality = STRUCTURE_VERIFIED
    missing_ids = []
    for res in (in_result, out_result):
        if res:
            flags.update(res.quality_flags)
            missing_ids.extend(res.missing_ids)
            if res.structure_quality != STRUCTURE_VERIFIED:
                structure_quality = STRUCTURE_UNVERIFIED

    return MetricResult(
        metric="imbalance", unit="kWh", mode=MODE_BALANCE,
        value=value, known_value=known_value, availability=availability,
        quality_flags=sorted(flags), structure_quality=structure_quality,
        source=SOURCE_CALCULATED,
        expected_count=expected_count, valid_count=valid_count,
        missing_ids=missing_ids,
        period=ResultPeriod(ts_from=str(ts_from), ts_to=str(ts_to), timezone=timezone),
        explanation={
            "input_point_ids": input_ids,
            "output_point_ids": output_ids,
            "percentage": pct,
            "percentage_reason": pct_reason,
            # партия 12: суммы входов/выходов нужны, чтобы пересчитать процент
            # небаланса при объединении интервалов (topology_history)
            "input_value": in_value,
            "output_value": out_value,
        },
    )


def balance(db, point_binding_repo, aggregates_repo, meter_source_repo, edge_repo,
            balance_scope_id, ts_from, ts_to, timezone="UTC"):
    """§5.2 — граница баланса (ручной состав input/output): резолвит
    input_ids/output_ids из balance_scope_members и делегирует расчёт
    ядру balance_from_point_sets() (Э2.2, партия 7). Публичное поведение
    не изменилось — только реализация."""
    with db.read() as c:
        scope = c.execute(
            "SELECT * FROM balance_scopes WHERE id = ?", (balance_scope_id,)
        ).fetchone()
        if scope is None:
            raise ValueError(f"Граница баланса {balance_scope_id} не найдена")
        members = c.execute(
            "SELECT * FROM balance_members WHERE scope_id = ? AND valid_to IS NULL",
            (balance_scope_id,)
        ).fetchall()

    input_ids = [m["point_id"] for m in members if m["side"] == "input"]
    output_ids = [m["point_id"] for m in members if m["side"] == "output"]

    return balance_from_point_sets(
        point_binding_repo, aggregates_repo, meter_source_repo, edge_repo,
        input_ids, output_ids, ts_from, ts_to, timezone,
    )


# ---------------------------------------------------------------------
# comparison — §5.1: ряд отдельных показателей, общий итог отсутствует.
# ---------------------------------------------------------------------

def comparison(point_binding_repo, aggregates_repo, meter_source_repo,
               point_ids, ts_from, ts_to, timezone="UTC") -> Dict[int, MetricResult]:
    """§5.1 comparison: никакого объединённого итога — только точки рядом,
    каждая посчитана независимо через measured_point. Дублирующиеся точки
    показываются один раз (тот же принцип дедупликации, что и в sum)."""
    point_ids = list(dict.fromkeys(point_ids))
    return {
        p: measured_point(point_binding_repo, aggregates_repo, meter_source_repo,
                           p, ts_from, ts_to, timezone)
        for p in point_ids
    }
