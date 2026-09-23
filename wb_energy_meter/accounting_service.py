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

Известное ограничение первой реализации (см. §6.2 ТЗ, "Ключ нового
исходного агрегата"): `period_aggregates` всё ещё хранит один час на
физический прибор (meter_id), без разделения по channel_profile/эпохе —
полная многоканальная схема агрегатов не входит в эту задачу и остаётся
отдельным, ещё не выполненным пунктом плана. measured_point() поэтому
корректно обрабатывает сегментацию по времени и замене прибора (A18/A19,
не делит агрегат на границе), но предполагает один канал на прибор, как
сейчас в проде — раздельные total_3p/phase_* агрегаты для одного и того же
физического WB-MAP3E пока не различаются на уровне сырых данных.
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
    QF_GAP, QF_EDGE_APPROX, QF_RESET,
)
from .binding_service import resolve_primary_segments
from .topology_service import build_children_map, is_descendant

log = logging.getLogger(__name__)

HOUR = 3600


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


# ---------------------------------------------------------------------
# measured — §5.1: расход/показатель одной точки.
# ---------------------------------------------------------------------

def measured_point(point_binding_repo, aggregates_repo, meter_source_repo,
                    point_id, ts_from, ts_to, timezone="UTC"):
    """§5.1 measured. Сегментирует период по истории привязок точки
    (resolve_primary_segments) и суммирует часовые агрегаты только внутри
    одного сегмента за раз. Час, пересекающий границу замены прибора,
    НЕ делится по доле времени (A19) — считается неизвестным для этого
    часа, quality_flags получает edge_approx как пояснение "частично
    покрыт, но не сведён"."""
    bindings = point_binding_repo.list_for_point(point_id)
    segments = resolve_primary_segments(bindings, ts_from, ts_to)

    hours = _hour_periods(ts_from, ts_to)
    expected_ids = [str(h) for h, _ in hours]
    parts: List[Optional[float]] = []
    valid_ids: List[str] = []
    flags = set()

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
        src = meter_source_repo.get_by_id(seg.meter_source_id)
        if src is None:
            parts.append(None)
            flags.add(QF_GAP)
            continue

        agg = aggregates_repo.get(src.meter_id, h_start)
        if agg is None or agg.ap_energy_delta is None:
            parts.append(None)
            if agg is not None and agg.quality_flag == "reset":
                flags.add(QF_RESET)
            else:
                flags.add(QF_GAP)
            continue

        if agg.quality_flag and agg.quality_flag not in ("ok",):
            flags.add(agg.quality_flag)

        parts.append(agg.ap_energy_delta)
        valid_ids.append(hour_id)

    value, known_value, expected_count, valid_count, missing_ids = known_sum(
        parts, expected_ids, valid_ids
    )

    if expected_count == 0 or valid_count == 0:
        availability = AVAILABILITY_MISSING
    elif valid_count == expected_count:
        availability = AVAILABILITY_COMPLETE
    else:
        availability = AVAILABILITY_PARTIAL

    return MetricResult(
        metric="energy_import", unit="kWh", mode=MODE_MEASURED,
        value=value, known_value=known_value, availability=availability,
        quality_flags=sorted(flags), structure_quality=STRUCTURE_VERIFIED,
        source=SOURCE_MEASURED if availability == AVAILABILITY_COMPLETE else SOURCE_CALCULATED,
        expected_count=expected_count, valid_count=valid_count,
        missing_ids=missing_ids,
        period=ResultPeriod(ts_from=str(ts_from), ts_to=str(ts_to), timezone=timezone),
        explanation={"point_id": point_id},
    )


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
               point_ids, ts_from, ts_to, timezone="UTC"):
    """§5.1 sum: сумма независимых точек. A05: повторяющиеся ID точки
    учитываются один раз (дедупликация с сохранением порядка). A04:
    электрическое перекрытие отклоняется целиком, а не молча исключает
    точку из состава."""
    point_ids = list(dict.fromkeys(point_ids))  # дедуп, порядок сохранён

    root_by_point = check_sum_overlap(edge_repo, point_ids)

    structure_quality = STRUCTURE_VERIFIED
    if any(root_by_point.get(p) is None for p in point_ids):
        structure_quality = STRUCTURE_UNVERIFIED

    results = [
        measured_point(point_binding_repo, aggregates_repo, meter_source_repo,
                        p, ts_from, ts_to, timezone)
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
                             timezone="UTC"):
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
    в поведении."""
    in_result = (
        sum_points(point_binding_repo, aggregates_repo, meter_source_repo, edge_repo,
                   input_ids, ts_from, ts_to, timezone)
        if input_ids else None
    )
    out_result = (
        sum_points(point_binding_repo, aggregates_repo, meter_source_repo, edge_repo,
                   output_ids, ts_from, ts_to, timezone)
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
