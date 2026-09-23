"""Баланс по электрической схеме — партия 7, Этап 2 (docs/TZ-batch7-
review-fixes.md, §Э2). Пишется лично (не делегировано), как и topology_
service.py/accounting_service.py — здесь находится главная находка ревью
от 23.09.2026:

    "Небаланс на «Обзоре» считается неверно. Итог объекта берётся по
    электрической схеме, а вычитается сумма корневых УЧЁТНЫХ ГРУПП. Если
    схема собрана без групп, «Обзор» показывает небаланс 100%. Если точка
    входит в две группы, небаланс уходит в минус из-за двойного счёта."

До этой партии `/api/v2/overview/summary` брал итог объекта через
назначенный ввод (`accounting_service.sum_points` по source-рёбрам), а
вычитал сумму РУЧНЫХ учётных групп верхнего уровня — двух независимых,
никак не связанных источников числа. Модель group существует для
арендаторов/направлений затрат (§4.4 большого ТЗ) и НЕ обязана покрывать
всю сеть без пропусков и пересечений — поэтому её использование в
качестве вычитаемого небаланса было структурно гарантированной ошибкой,
а не редким craем.

Этот модуль вместо этого считает и небаланс объекта, и небаланс любого
отдельного узла (щита) ПО ОДНОЙ И ТОЙ ЖЕ электрической схеме
(`electrical_edges`/`electrical_nodes`, тот же активный граф, что
использует topology_service и accounting_service.check_sum_overlap) —
через общий алгоритм `first_measurements` (Э2.1, большое ТЗ §5.2: "поиск
останавливается на первом измерении в каждой ветви") поверх общего ядра
расчёта `accounting_service.balance_from_point_sets` (Э2.2).

Учётные группы (`branches` в ответе `/api/v2/overview/summary`) никуда не
делись — они остаются в ответе для совместимости и как разрез отчётности
("Распределение по учётным группам"), но с этой партии в вычислении
небаланса больше НЕ участвуют.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .accounting_contract import (
    MetricResult, ResultPeriod, resolve_percentage,
    MODE_SUM, AVAILABILITY_MISSING, AVAILABILITY_PARTIAL,
    STRUCTURE_VERIFIED, SOURCE_CALCULATED,
)
from .accounting_service import (
    AccountingConflict, balance_from_point_sets, measured_point, sum_points,
)

MAX_DEPTH = 32  # большое ТЗ §4.2 — защитный предел обхода дерева сети


class BalanceAlgorithmError(RuntimeError):
    """Э2.1: "Выходы по построению не пересекаются электрически, поэтому
    sum_points по ним не должен давать AccountingConflict. Если всё же
    дал — это ошибка алгоритма, а не данных". Такая ситуация означает
    баг в first_measurements/object_total_and_branches/balance_node, а не
    проблему пользовательских данных — поэтому отдельный тип исключения,
    не ValueError/AccountingConflict, которые API-слой транслирует в 4xx."""


@dataclass
class Boundary:
    """Одна измеряемая линия — "выход границы" (Э2.1)."""
    edge_id: int
    point_id: int
    to_node_id: int


@dataclass
class UnmeteredBranch:
    """Самая верхняя неизмеренная линия в ветви — дальше её поддерево не
    разворачивается (Э2.1)."""
    edge_id: int
    to_node_id: int


@dataclass
class NodeBalanceResult:
    """Ответ Э2.3/Э2.5 — баланс одного узла (щита)."""
    node_id: int
    input: Optional[dict]
    result: Optional[MetricResult]
    unavailable_reason: Optional[str]
    outputs: List[dict] = field(default_factory=list)
    unmetered_branches: List[dict] = field(default_factory=list)
    boundary_coverage: Optional[str] = None


@dataclass
class ObjectSummary:
    """Ответ Э2.4 — итог объекта и сетевые ветви уровня 1."""
    input_point_ids: List[int]
    object_total: Optional[MetricResult]
    object_total_unavailable_reason: Optional[str]
    unmetered_inputs: List[dict]
    network_branches: List[dict]
    unmetered_branches: List[dict]
    boundary_coverage: Optional[str]
    imbalance: Optional[MetricResult]
    imbalance_value: Optional[float]
    imbalance_percent: Optional[float]
    imbalance_percent_reason: Optional[str]


# ---------------------------------------------------------------------
# Э2.1 — first_measurements: выходы границы узла N.
# ---------------------------------------------------------------------

def first_measurements(edges: Sequence, node_id: int,
                        _visited: Optional[frozenset] = None,
                        _depth: int = 0):
    """Для каждой e ∈ outgoing(N): если e измеряемая — она выход, дальше
    по этой ветви не идти; иначе рекурсивно спускаемся в e.to_node. Если
    во всём поддереве под e (включая саму e) нет ни одной измеряемой
    линии, e целиком — неизмеренная ветвь (записывается самая верхняя
    такая линия, поддерево дальше не разворачивается). Защита от циклов
    — множество посещённых узлов; предел глубины 32 (большое ТЗ §4.2) —
    в валидном лесу (validate_forest уже гарантирует отсутствие циклов
    при публикации) не должен срабатывать, это чисто защитный код.

    Возвращает (boundaries: List[Boundary], unmetered: List[UnmeteredBranch])."""
    if _visited is None:
        _visited = frozenset()
    if node_id in _visited or _depth > MAX_DEPTH:
        return [], []
    _visited = _visited | {node_id}

    boundaries: List[Boundary] = []
    unmetered: List[UnmeteredBranch] = []

    for e in edges:
        if e.from_node_id != node_id:
            continue
        if e.primary_point_id is not None:
            boundaries.append(Boundary(edge_id=e.id, point_id=e.primary_point_id,
                                        to_node_id=e.to_node_id))
            continue
        sub_boundaries, sub_unmetered = first_measurements(
            edges, e.to_node_id, _visited, _depth + 1)
        if not sub_boundaries and not sub_unmetered:
            unmetered.append(UnmeteredBranch(edge_id=e.id, to_node_id=e.to_node_id))
        else:
            boundaries.extend(sub_boundaries)
            unmetered.extend(sub_unmetered)

    return boundaries, unmetered


# ---------------------------------------------------------------------
# Общие мелкие хелперы имени ветви (Э2.4: "name — имя линии, иначе имя
# узла-получателя") и построения индекса рёбер по id.
# ---------------------------------------------------------------------

def _edges_by_id(edges: Sequence) -> Dict[int, object]:
    return {e.id: e for e in edges}


def _node_name(node_repo, node_id: Optional[int]) -> Optional[str]:
    if node_id is None:
        return None
    node = node_repo.get_by_id(node_id)
    return node.name if node is not None else None


def _branch_name(edges_by_id: Dict[int, object], node_repo, edge_id: int,
                  to_node_id: int) -> Optional[str]:
    edge = edges_by_id.get(edge_id)
    if edge is not None and edge.name:
        return edge.name
    return _node_name(node_repo, to_node_id)


# ---------------------------------------------------------------------
# Э2.3 — баланс узла (щита).
# ---------------------------------------------------------------------

def balance_node(point_binding_repo, aggregates_repo, meter_source_repo,
                  edge_repo, node_repo, node_id, ts_from, ts_to,
                  timezone="UTC") -> NodeBalanceResult:
    """Таблица Э2.3:

    - incoming(N) нет (источник/висячий узел) -> no_incoming_line;
    - incoming(N) без счётчика -> input_unmetered; выходы и неизмеренные
      ветви всё равно в ответе;
    - нет выходов и нет неизмеренных ветвей (лист) -> no_outgoing_lines;
    - иначе -> ядро с input=[incoming.primary_point_id],
      output=first_measurements(N)."""
    edges = edge_repo.list_active_published()
    edges_by_id = _edges_by_id(edges)
    incoming = next((e for e in edges if e.to_node_id == node_id), None)

    if incoming is None:
        return NodeBalanceResult(
            node_id=node_id, input=None, result=None,
            unavailable_reason="no_incoming_line",
        )

    input_point_id = incoming.primary_point_id
    input_result = (
        measured_point(point_binding_repo, aggregates_repo, meter_source_repo,
                        input_point_id, ts_from, ts_to, timezone)
        if input_point_id is not None else None
    )
    input_entry = {
        "edge_id": incoming.id, "point_id": input_point_id,
        "name": _branch_name(edges_by_id, node_repo, incoming.id, node_id),
        "result": input_result,
    }

    boundaries, unmetered = first_measurements(edges, node_id)
    outputs = []
    for b in boundaries:
        r = measured_point(point_binding_repo, aggregates_repo, meter_source_repo,
                            b.point_id, ts_from, ts_to, timezone)
        outputs.append({
            "edge_id": b.edge_id, "point_id": b.point_id, "to_node_id": b.to_node_id,
            "name": _branch_name(edges_by_id, node_repo, b.edge_id, b.to_node_id),
            "result": r,
        })
    unmetered_branches = [
        {"edge_id": u.edge_id, "to_node_id": u.to_node_id,
         "name": _branch_name(edges_by_id, node_repo, u.edge_id, u.to_node_id)}
        for u in unmetered
    ]
    boundary_coverage = "verified" if not unmetered_branches else "has_unmetered_branches"

    if input_point_id is None:
        return NodeBalanceResult(
            node_id=node_id, input=input_entry, result=None,
            unavailable_reason="input_unmetered",
            outputs=outputs, unmetered_branches=unmetered_branches,
            boundary_coverage=boundary_coverage,
        )

    if not boundaries and not unmetered_branches:
        return NodeBalanceResult(
            node_id=node_id, input=input_entry, result=None,
            unavailable_reason="no_outgoing_lines",
        )

    try:
        result = balance_from_point_sets(
            point_binding_repo, aggregates_repo, meter_source_repo, edge_repo,
            [input_point_id], [b.point_id for b in boundaries],
            ts_from, ts_to, timezone,
        )
    except AccountingConflict as e:
        raise BalanceAlgorithmError(
            f"баланс узла {node_id}: first_measurements дал электрически "
            f"пересекающиеся точки — это ошибка алгоритма, а не данных: {e}"
        ) from e

    return NodeBalanceResult(
        node_id=node_id, input=input_entry, result=result,
        unavailable_reason=None, outputs=outputs,
        unmetered_branches=unmetered_branches, boundary_coverage=boundary_coverage,
    )


# ---------------------------------------------------------------------
# Э2.4 — итог и небаланс объекта.
# ---------------------------------------------------------------------

def _resolve_source_edges(node_repo, edge_repo):
    """Все действующие опубликованные связи, исходящие из узла
    kind='source' — ВСЕ, включая неизмеряемые (в отличие от старого
    api_v2._resolve_object_input_point_ids, который видел только
    измеряемые и молча пропускал остальные — это и была часть ошибки
    занижения итога, Э2.4)."""
    edges = edge_repo.list_active_published()
    out = []
    for e in edges:
        node = node_repo.get_by_id(e.from_node_id)
        if node is not None and node.kind == "source":
            out.append(e)
    return out


def object_summary(point_binding_repo, aggregates_repo, meter_source_repo,
                    edge_repo, node_repo, ts_from, ts_to,
                    timezone="UTC") -> ObjectSummary:
    """Э2.4: итог и небаланс объекта.

    - Нет ни одной линии от source: object_total=None,
      reason="no_input_assigned" (UI-кнопка "Настроить границу объекта").
    - Есть линии от source без счётчика: value=null, known_value = сумма
      измеряемых вводов, availability partial/missing, причина
      "unmetered_input", плюс unmetered_inputs — раньше такие линии
      молча пропускались и итог занижался.
    - Все вводы измеряемые: как раньше, через sum_points.
    - Выходы уровня 1 — объединение first_measurements(e_in.to_node) по
      всем ИЗМЕРЯЕМЫМ вводам, без повторов.
    - Небаланс объекта — ядро Э2.2 по вводам и выходам уровня 1; если
      итог объекта неполный — imbalance принудительно null, причина
      "object_total_incomplete" (а не generic "no_data" resolve_percentage,
      чтобы UI мог показать точную причину)."""
    source_edges = _resolve_source_edges(node_repo, edge_repo)

    if not source_edges:
        return ObjectSummary(
            input_point_ids=[], object_total=None,
            object_total_unavailable_reason="no_input_assigned",
            unmetered_inputs=[], network_branches=[], unmetered_branches=[],
            boundary_coverage=None, imbalance=None, imbalance_value=None,
            imbalance_percent=None, imbalance_percent_reason="object_total_incomplete",
        )

    measured_edges = [e for e in source_edges if e.primary_point_id is not None]
    unmetered_input_edges = [e for e in source_edges if e.primary_point_id is None]
    input_point_ids = list(dict.fromkeys(e.primary_point_id for e in measured_edges))

    unmetered_inputs = [
        {"edge_id": e.id,
         "from_node_name": _node_name(node_repo, e.from_node_id),
         "to_node_name": _node_name(node_repo, e.to_node_id)}
        for e in unmetered_input_edges
    ]

    try:
        measured_sum = (
            sum_points(point_binding_repo, aggregates_repo, meter_source_repo,
                       edge_repo, input_point_ids, ts_from, ts_to, timezone)
            if input_point_ids else None
        )
    except AccountingConflict as e:
        # Разные source-узлы — разные корни леса (validate_forest), у
        # каждого своя ветка, электрически пересечься они не могут —
        # если check_sum_overlap всё же нашёл пересечение, это ошибка
        # алгоритма/данных за пределами этой функции, не спрятать её.
        raise BalanceAlgorithmError(
            f"итог объекта: измеряемые вводы дали электрическое "
            f"пересечение — ожидалось, что разные source-узлы независимы: {e}"
        ) from e

    object_total_unavailable_reason = None
    if unmetered_input_edges:
        known_value = measured_sum.known_value if measured_sum is not None else None
        expected_count = (
            (measured_sum.expected_count if measured_sum is not None else 0)
            + len(unmetered_input_edges)
        )
        valid_count = measured_sum.valid_count if measured_sum is not None else 0
        object_total = MetricResult(
            metric="energy_import", unit="kWh", mode=MODE_SUM,
            value=None, known_value=known_value,
            availability=(AVAILABILITY_MISSING if valid_count == 0 else AVAILABILITY_PARTIAL),
            quality_flags=list(measured_sum.quality_flags) if measured_sum is not None else [],
            structure_quality=(
                measured_sum.structure_quality if measured_sum is not None else STRUCTURE_VERIFIED
            ),
            source=SOURCE_CALCULATED,
            expected_count=expected_count, valid_count=valid_count,
            missing_ids=list(measured_sum.missing_ids) if measured_sum is not None else [],
            period=ResultPeriod(ts_from=str(ts_from), ts_to=str(ts_to), timezone=timezone),
            explanation={"included_point_ids": input_point_ids},
        )
        object_total_unavailable_reason = "unmetered_input"
    else:
        object_total = measured_sum

    all_edges = edge_repo.list_active_published()
    edges_by_id = _edges_by_id(all_edges)

    seen_edge_ids = set()
    boundaries: List[Boundary] = []
    unmetered_branches_raw: List[UnmeteredBranch] = []
    for e in measured_edges:
        b_list, u_list = first_measurements(all_edges, e.to_node_id)
        for b in b_list:
            if b.edge_id not in seen_edge_ids:
                seen_edge_ids.add(b.edge_id)
                boundaries.append(b)
        for u in u_list:
            if u.edge_id not in seen_edge_ids:
                seen_edge_ids.add(u.edge_id)
                unmetered_branches_raw.append(u)

    network_branches = []
    for b in boundaries:
        r = measured_point(point_binding_repo, aggregates_repo, meter_source_repo,
                            b.point_id, ts_from, ts_to, timezone)
        pct, pct_reason = resolve_percentage(
            r.value, object_total.value if object_total is not None else None)
        network_branches.append({
            "edge_id": b.edge_id, "point_id": b.point_id,
            "name": _branch_name(edges_by_id, node_repo, b.edge_id, b.to_node_id),
            "result": r,
            "percentage_of_object": pct,
            "percentage_of_object_reason": pct_reason,
        })

    unmetered_branches = [
        {"edge_id": u.edge_id,
         "name": _branch_name(edges_by_id, node_repo, u.edge_id, u.to_node_id)}
        for u in unmetered_branches_raw
    ]

    boundary_coverage = None
    if not unmetered_input_edges:
        boundary_coverage = "verified" if not unmetered_branches else "has_unmetered_branches"
    elif measured_edges:
        # Часть вводов измерена, часть нет: покрытие относится к тому,
        # что удалось исследовать по измеряемым вводам — сам итог всё
        # равно неполон (unmetered_input), это отдельная причина.
        boundary_coverage = "verified" if not unmetered_branches else "has_unmetered_branches"

    if object_total is None or object_total.value is None:
        imbalance = None
        imbalance_value = None
        imbalance_percent = None
        imbalance_percent_reason = "object_total_incomplete"
    else:
        try:
            imbalance = balance_from_point_sets(
                point_binding_repo, aggregates_repo, meter_source_repo, edge_repo,
                input_point_ids, [b.point_id for b in boundaries],
                ts_from, ts_to, timezone,
            )
        except AccountingConflict as e:
            raise BalanceAlgorithmError(
                f"небаланс объекта: выходы уровня 1 дали электрическое "
                f"пересечение — ошибка алгоритма, а не данных: {e}"
            ) from e
        imbalance_value = imbalance.value
        imbalance_percent = imbalance.explanation.get("percentage")
        imbalance_percent_reason = imbalance.explanation.get("percentage_reason")

    return ObjectSummary(
        input_point_ids=input_point_ids,
        object_total=object_total,
        object_total_unavailable_reason=object_total_unavailable_reason,
        unmetered_inputs=unmetered_inputs,
        network_branches=network_branches,
        unmetered_branches=unmetered_branches,
        boundary_coverage=boundary_coverage,
        imbalance=imbalance,
        imbalance_value=imbalance_value,
        imbalance_percent=imbalance_percent,
        imbalance_percent_reason=imbalance_percent_reason,
    )


# ---------------------------------------------------------------------
# Э2.6 — сетевые ветви уровня 1 для dimension="branch" в /reports/query,
# те же числа, что в object_summary().network_branches (A43).
# ---------------------------------------------------------------------

def network_branches_for_reports(point_binding_repo, aggregates_repo, meter_source_repo,
                                  edge_repo, node_repo, ts_from, ts_to, timezone="UTC"):
    """Тонкая обёртка над object_summary() для /api/v2/reports/query
    dimension="branch" (Э2.6) — берёт те же network_branches, что и
    Обзор, чтобы числа на экране, в отчёте и в CSV совпадали по
    построению (A43), а не пересчитывались отдельной формулой."""
    summary = object_summary(point_binding_repo, aggregates_repo, meter_source_repo,
                              edge_repo, node_repo, ts_from, ts_to, timezone)
    return summary.network_branches
