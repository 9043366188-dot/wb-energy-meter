"""История топологии: структура «как было на момент T» — партия 12, этап 12.2
(docs/design-history.md §2, docs/TZ-finish-plan.md §5).

Новых таблиц нет (решение R7). Структура на момент `T` собирается из того,
что уже хранится:

* **существование связи** — по её `state='published'` и интервалу
  `valid_from <= T < valid_to` (публикация вытесняет старую связь,
  `retire` закрывает; история интервалов есть и у БД, созданной до 0.22.0);
* **поля, меняющиеся «на месте»** — `primary_point_id` связи (измеритель
  линии) и `kind` узла — откатом журнала изменений: записи `update` с
  `recorded_at > T` проходим от новых к старым и возвращаем `old_value`;
* **выбывшие измерители** (A39, сеть) — архивная точка или точка с
  `enabled=0` на момент `T` перестаёт быть измерителем: линия считается
  «без счётчика». Это правило действует и в режиме `current`.

Граница истории — метка `kv.change_log_started_at` (`change_journal`).
Если `T` раньше метки, у нас нет данных о правках «на месте» до неё: берётся
структура на момент метки, а результат помечается `assumed_legacy` (A23) —
историю не выдумываем, а предположение называем.

Период отчёта может пересекать изменение структуры. Модуль режет период на
интервалы постоянной структуры: границы — моменты изменений, **округлённые
вверх до часа** (час принадлежит структуре, действовавшей в его начале, —
границу часа не делим, как при замене прибора, A19); каждый интервал
считается существующими функциями на структуре его начала, интервалы
суммируются (`merge_results`). Без HTTP.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import change_journal
from .accounting_contract import (
    MetricResult, ResultPeriod, resolve_percentage, MODE_BALANCE,
    AVAILABILITY_COMPLETE, AVAILABILITY_PARTIAL, AVAILABILITY_MISSING,
    STRUCTURE_VERIFIED, STRUCTURE_UNVERIFIED, STRUCTURE_ASSUMED_LEGACY,
    SOURCE_CALCULATED,
)
from .topology_service import ElectricalEdge, ElectricalNode

log = logging.getLogger(__name__)

HOUR = 3600
STRUCTURE_MODES = ("current", "as_was")
DEFAULT_STRUCTURE_MODE = "current"
# Защита от лавины: период, пересекающий больше изменений, считать по
# интервалам слишком дорого — пользователю предлагается сузить период.
MAX_INTERVALS = 200


class HistoryTooFine(ValueError):
    """В периоде слишком много изменений структуры для расчёта по интервалам."""


def ceil_hour(t: int) -> int:
    """Ближайшая граница часа не раньше `t`."""
    t = int(t)
    return -(-t // HOUR) * HOUR


def _loads(raw) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------
# Снимок структуры на момент T
# ---------------------------------------------------------------------

@dataclass
class StructureSnapshot:
    """Структура сети на момент `requested_at`.

    `edges is None` — снимок совпадает с живой структурой (режим `current`
    без выбывших измерителей): вызывающий код берёт обычные репозитории, и
    поведение полностью прежнее."""
    requested_at: int
    effective_at: int
    mode: str
    assumed_legacy: bool
    edges: Optional[List[ElectricalEdge]]
    node_kinds: Dict[int, str]          # узел -> вид на момент T (только отличающиеся)
    masked_point_ids: List[int]         # измерители, снятые как неактивные

    @property
    def is_live(self) -> bool:
        return self.edges is None


def _inactive_point_ids(c, at: int) -> set:
    """Точки, не являющиеся измерителями на момент `at`: архивные к этому
    моменту либо выключенные (`enabled=0`) по версиям состояния. Нет версии
    состояния — точка считается включённой (старые БД)."""
    out = set()
    for r in c.execute(
            "SELECT id FROM metering_points "
            "WHERE archived_at IS NOT NULL AND archived_at <= ?", (at,)):
        out.add(r["id"])
    for r in c.execute(
            "SELECT point_id FROM point_state_versions WHERE enabled = 0 "
            "AND valid_from <= ? AND (valid_to IS NULL OR ? < valid_to)",
            (at, at)):
        out.add(r["point_id"])
    return out


def _journal_reversal(c, at: int) -> Tuple[Dict[int, Optional[int]], Dict[int, str]]:
    """Откат правок «на месте» после `at`: измеритель связи и вид узла.
    Записи идут от новых к старым, поэтому итог — значение до самой старой
    откатываемой правки."""
    edge_point: Dict[int, Optional[int]] = {}
    node_kind: Dict[int, str] = {}
    rows = c.execute(
        "SELECT entity_type, entity_id, old_value FROM change_log "
        "WHERE entity_type IN ('electrical_edge', 'electrical_node') "
        "AND action = 'update' AND recorded_at > ? ORDER BY id DESC", (at,))
    for r in rows:
        old = _loads(r["old_value"])
        if r["entity_type"] == "electrical_edge":
            if "primary_point_id" in old:
                edge_point[r["entity_id"]] = old["primary_point_id"]
        elif "kind" in old:
            node_kind[r["entity_id"]] = old["kind"]
    return edge_point, node_kind


def _mask_inactive(edges: Iterable[ElectricalEdge], inactive: set
                   ) -> Tuple[List[ElectricalEdge], List[int]]:
    out, masked = [], []
    for e in edges:
        if e.primary_point_id is not None and e.primary_point_id in inactive:
            masked.append(e.primary_point_id)
            e = dataclasses.replace(e, primary_point_id=None)
        out.append(e)
    return out, sorted(set(masked))


def structure_at(db, at: int, mode: str = "as_was") -> StructureSnapshot:
    """Структура сети на момент `at`.

    `mode="as_was"` — полностью по истории (существование связей по
    интервалам, правки «на месте» откатом журнала, выбывшие измерители).
    `mode="current"` — живая структура (связи, действующие сейчас), но
    измеритель, неактивный на `at`, снимается с линии (A39, сеть)."""
    at = int(at)
    if mode not in STRUCTURE_MODES:
        raise ValueError(f"structure_mode — допустимые: {'|'.join(STRUCTURE_MODES)}")
    with db.read() as c:
        inactive = _inactive_point_ids(c, at)

        if mode == "current":
            if not inactive:
                return StructureSnapshot(at, at, mode, False, None, {}, [])
            rows = c.execute(
                "SELECT * FROM electrical_edges "
                "WHERE state = 'published' AND valid_to IS NULL").fetchall()
            edges, masked = _mask_inactive(
                [ElectricalEdge.from_row(r) for r in rows], inactive)
            if not masked:
                return StructureSnapshot(at, at, mode, False, None, {}, [])
            return StructureSnapshot(at, at, mode, False, edges, {}, masked)

        started = change_journal.started_at(c)
        legacy = started is None or at < started
        effective = at if started is None else max(at, started)
        edge_point: Dict[int, Optional[int]] = {}
        node_kind: Dict[int, str] = {}
        if started is not None:
            edge_point, node_kind = _journal_reversal(c, effective)

        rows = c.execute(
            "SELECT * FROM electrical_edges WHERE state = 'published'").fetchall()
        existing: List[ElectricalEdge] = []
        for r in rows:
            e = ElectricalEdge.from_row(r)
            if e.valid_from is None or e.valid_from > effective:
                continue
            if e.valid_to is not None and effective >= e.valid_to:
                continue
            if e.id in edge_point:
                e = dataclasses.replace(e, primary_point_id=edge_point[e.id])
            existing.append(e)
        edges, masked = _mask_inactive(existing, inactive)

        current_kinds = {r["id"]: r["kind"] for r in c.execute(
            "SELECT id, kind FROM electrical_nodes")}
        kinds = {nid: k for nid, k in node_kind.items()
                 if nid in current_kinds and current_kinds[nid] != k}
    return StructureSnapshot(at, effective, mode, legacy, edges, kinds, masked)


class SnapshotEdgeRepo:
    """Только чтение: тот же интерфейс, что у `ElectricalEdgeRepo`, который
    нужен расчётным сервисам, — поверх снимка структуры."""

    def __init__(self, snapshot: StructureSnapshot):
        if snapshot.edges is None:
            raise ValueError("снимок совпадает с живой структурой — репозиторий не нужен")
        self._edges = list(snapshot.edges)

    def list_active_published(self) -> List[ElectricalEdge]:
        return list(self._edges)

    def get_by_id(self, edge_id: int) -> Optional[ElectricalEdge]:
        for e in self._edges:
            if e.id == edge_id:
                return e
        return None


class SnapshotNodeRepo:
    """Живой репозиторий узлов, у которого вид (`kind`) возвращается как на
    момент снимка. Имена и прочие поля — текущие."""

    def __init__(self, live_repo, snapshot: StructureSnapshot):
        self._live = live_repo
        self._kinds = dict(snapshot.node_kinds)

    def get_by_id(self, node_id: int) -> Optional[ElectricalNode]:
        node = self._live.get_by_id(node_id)
        if node is not None and node_id in self._kinds:
            node = dataclasses.replace(node, kind=self._kinds[node_id])
        return node


# ---------------------------------------------------------------------
# Границы интервалов
# ---------------------------------------------------------------------

def structure_breakpoints(db, ts_from: int, ts_to: int, mode: str) -> List[int]:
    """Моменты внутри `(ts_from, ts_to)`, где меняется структура, округлённые
    вверх до часа. `as_was`: открытие/закрытие связей, правки измерителя
    связи и вида узла (по журналу), активность измерителей. `current`:
    только активность измерителей действующих линий (A39, сеть)."""
    raw: set = set()
    relevant: set = set()
    with db.read() as c:
        if mode == "as_was":
            for r in c.execute(
                    "SELECT valid_from, valid_to, primary_point_id "
                    "FROM electrical_edges WHERE state = 'published'"):
                raw.add(r["valid_from"])
                raw.add(r["valid_to"])
                if r["primary_point_id"] is not None:
                    relevant.add(r["primary_point_id"])
            for r in c.execute(
                    "SELECT entity_type, old_value, new_value, recorded_at "
                    "FROM change_log WHERE entity_type IN "
                    "('electrical_edge', 'electrical_node') AND action = 'update' "
                    "AND recorded_at > ?", (ts_from,)):
                old, new = _loads(r["old_value"]), _loads(r["new_value"])
                key = "primary_point_id" if r["entity_type"] == "electrical_edge" else "kind"
                if key not in old or old.get(key) == new.get(key):
                    continue
                if r["recorded_at"] < ts_to:
                    raw.add(r["recorded_at"])
                if key == "primary_point_id":
                    relevant.update(p for p in (old.get(key), new.get(key))
                                    if p is not None)
        else:
            for r in c.execute(
                    "SELECT primary_point_id FROM electrical_edges "
                    "WHERE state = 'published' AND valid_to IS NULL "
                    "AND primary_point_id IS NOT NULL"):
                relevant.add(r["primary_point_id"])

        if relevant:
            ids = sorted(relevant)
            marks = ",".join("?" * len(ids))
            for r in c.execute(
                    f"SELECT archived_at FROM metering_points "
                    f"WHERE id IN ({marks}) AND archived_at IS NOT NULL", ids):
                raw.add(r["archived_at"])
            for r in c.execute(
                    f"SELECT valid_from, valid_to FROM point_state_versions "
                    f"WHERE point_id IN ({marks}) AND enabled = 0", ids):
                raw.add(r["valid_from"])
                raw.add(r["valid_to"])
    return snap_breakpoints(raw, ts_from, ts_to)


def snap_breakpoints(raw: Iterable[Optional[int]], ts_from: int, ts_to: int
                     ) -> List[int]:
    """Привести моменты изменений к границам часов внутри `(ts_from, ts_to)`."""
    out = set()
    for t in raw:
        if t is None or not ts_from < t < ts_to:
            continue
        snapped = ceil_hour(t)
        if ts_from < snapped < ts_to:
            out.add(snapped)
    return sorted(out)


def group_composition_breakpoints(db, group_id: int, ts_from: int, ts_to: int
                                  ) -> List[int]:
    """Моменты изменения эффективного состава группы внутри периода (A21):
    членство точек и привязки подгрупп в самой группе и во всех её
    потомках за всё время. Лишние границы безвредны (час не делится)."""
    raw: set = set()
    with db.read() as c:
        ids = [int(group_id)]
        frontier = list(ids)
        for _ in range(32):                       # глубина дерева групп ограничена
            if not frontier:
                break
            marks = ",".join("?" * len(frontier))
            rows = c.execute(
                f"SELECT group_id, valid_from, valid_to FROM group_parent_bindings "
                f"WHERE parent_id IN ({marks})", frontier).fetchall()
            frontier = []
            for r in rows:
                raw.add(r["valid_from"])
                raw.add(r["valid_to"])
                if r["group_id"] not in ids:
                    ids.append(r["group_id"])
                    frontier.append(r["group_id"])
        marks = ",".join("?" * len(ids))
        for r in c.execute(
                f"SELECT valid_from, valid_to FROM group_memberships "
                f"WHERE group_id IN ({marks})", ids):
            raw.add(r["valid_from"])
            raw.add(r["valid_to"])
    return snap_breakpoints(raw, ts_from, ts_to)


@dataclass
class Interval:
    """Часть периода `[a, b)` с постоянной структурой."""
    a: int
    b: int
    snapshot: StructureSnapshot
    edge_repo: object
    node_repo: object

    @property
    def is_live(self) -> bool:
        return self.snapshot.is_live

    @property
    def assumed_legacy(self) -> bool:
        return self.snapshot.assumed_legacy

    def to_dict(self) -> dict:
        return {"from": self.a, "to": self.b,
                "assumed_legacy": self.assumed_legacy}


class HistoryContext:
    """Режим структуры одного HTTP-запроса: делит период на интервалы и
    отдаёт для каждого репозитории связей/узлов на структуре его начала."""

    def __init__(self, db, mode: str, live_edge_repo, live_node_repo):
        if mode not in STRUCTURE_MODES:
            raise ValueError(
                f"structure_mode — допустимые: {'|'.join(STRUCTURE_MODES)}")
        self.db = db
        self.mode = mode
        self._live_edges = live_edge_repo
        self._live_nodes = live_node_repo
        self._snapshots: Dict[int, StructureSnapshot] = {}
        self._intervals: Dict[tuple, List[Interval]] = {}
        self._started: Optional[int] = None
        self._started_loaded = False

    # -- граница истории ------------------------------------------------
    def journal_started_at(self) -> Optional[int]:
        if not self._started_loaded:
            with self.db.read() as c:
                self._started = change_journal.started_at(c)
            self._started_loaded = True
        return self._started

    def assumed_legacy_for(self, ts_from: int) -> bool:
        """Режим `as_was` и начало периода раньше начала истории (A23)."""
        if self.mode != "as_was":
            return False
        started = self.journal_started_at()
        return started is None or int(ts_from) < started

    # -- интервалы ------------------------------------------------------
    def _snapshot(self, at: int) -> StructureSnapshot:
        snap = self._snapshots.get(at)
        if snap is None:
            snap = structure_at(self.db, at, self.mode)
            self._snapshots[at] = snap
        return snap

    def intervals(self, ts_from: int, ts_to: int,
                  extra: Sequence[Optional[int]] = ()) -> List[Interval]:
        """Интервалы периода. `extra` — дополнительные моменты изменений
        (состав группы, члены границы баланса): будут округлены до часа."""
        ts_from, ts_to = int(ts_from), int(ts_to)
        key = (ts_from, ts_to, tuple(sorted(int(x) for x in extra if x is not None)))
        cached = self._intervals.get(key)
        if cached is not None:
            return cached
        marks = set(structure_breakpoints(self.db, ts_from, ts_to, self.mode))
        marks.update(snap_breakpoints(extra, ts_from, ts_to))
        if len(marks) + 1 > MAX_INTERVALS:
            raise HistoryTooFine(
                f"период пересекает слишком много изменений структуры "
                f"({len(marks)}); сузьте период")
        bounds = [ts_from] + sorted(marks) + [ts_to]
        out: List[Interval] = []
        for a, b in zip(bounds, bounds[1:]):
            snap = self._snapshot(a)
            if snap.is_live:
                out.append(Interval(a, b, snap, self._live_edges, self._live_nodes))
            else:
                out.append(Interval(
                    a, b, snap, SnapshotEdgeRepo(snap),
                    SnapshotNodeRepo(self._live_nodes, snap)))
        self._intervals[key] = out
        return out

    def trivial(self, intervals: Sequence[Interval]) -> bool:
        """Один интервал на живой структуре: расчёт идёт по-старому."""
        return len(intervals) == 1 and intervals[0].is_live

    @staticmethod
    def describe(intervals: Sequence[Interval]) -> List[dict]:
        return [iv.to_dict() for iv in intervals]


# ---------------------------------------------------------------------
# Объединение результатов интервалов
# ---------------------------------------------------------------------

def adjust_quality(interval: Interval, result: MetricResult) -> MetricResult:
    """Часть периода до начала истории — `assumed_legacy`, если иначе
    результат считался бы подтверждённым."""
    if interval.assumed_legacy and result.structure_quality == STRUCTURE_VERIFIED:
        return dataclasses.replace(result, structure_quality=STRUCTURE_ASSUMED_LEGACY)
    return result


def _union(seqs: Iterable[Iterable]) -> list:
    out: list = []
    for seq in seqs:
        for x in seq or []:
            if x not in out:
                out.append(x)
    return out


def _sum_or_none(values: Sequence[Optional[float]]) -> Optional[float]:
    if not values or any(v is None for v in values):
        return None
    return round(sum(values), 6)


def merge_results(parts: Sequence[Tuple[Interval, Optional[MetricResult]]],
                  ts_from: int, ts_to: int, timezone: str, *,
                  missing_parts: int = 0) -> Optional[MetricResult]:
    """Сумма результатов по интервалам.

    * значение — сумма, только если каждая часть определена (инвариант §5.3:
      `value` лишь при `valid_count == expected_count`); иначе `value=None`,
      а известные части суммируются в `known_value`;
    * `missing_parts` — интервалы, где результата нет совсем, хотя он
      должен быть (например, у объекта не было ввода): считаются неизвестными;
    * `structure_quality`: unverified > assumed_legacy > verified;
    * для единственного интервала результат возвращается как есть (с
      поправкой качества структуры)."""
    present = [(iv, adjust_quality(iv, r)) for iv, r in parts if r is not None]
    if not present:
        return None
    if len(parts) == 1:
        return present[0][1]

    results = [r for _, r in present]
    first = results[0]
    expected = sum(r.expected_count for r in results) + missing_parts
    valid = sum(r.valid_count for r in results)

    if missing_parts == 0 and all(r.value is not None for r in results):
        value = _sum_or_none([r.value for r in results])
        known = value
    else:
        value = None
        known_parts = [r.value if r.value is not None else r.known_value
                       for r in results]
        known_parts = [k for k in known_parts if k is not None]
        known = round(sum(known_parts), 6) if known_parts and valid > 0 else None
    if valid == 0:
        known = None
    if value is not None and valid < expected:
        value = None            # защита инварианта §5.3
    if value is not None:
        availability = AVAILABILITY_COMPLETE
    elif known is not None:
        availability = AVAILABILITY_PARTIAL
    else:
        availability = AVAILABILITY_MISSING

    qualities = {r.structure_quality for r in results}
    if STRUCTURE_UNVERIFIED in qualities:
        quality = STRUCTURE_UNVERIFIED
    elif STRUCTURE_ASSUMED_LEGACY in qualities:
        quality = STRUCTURE_ASSUMED_LEGACY
    else:
        quality = STRUCTURE_VERIFIED

    explanation = _merge_explanations(results, value, first.mode)
    explanation["structure_intervals"] = [iv.to_dict() for iv, _ in parts]

    return MetricResult(
        metric=first.metric, unit=first.unit, mode=first.mode,
        value=value, known_value=known, availability=availability,
        quality_flags=sorted({f for r in results for f in r.quality_flags}),
        structure_quality=quality, source=SOURCE_CALCULATED,
        expected_count=expected, valid_count=valid,
        missing_ids=_union(r.missing_ids for r in results),
        period=ResultPeriod(ts_from=str(ts_from), ts_to=str(ts_to), timezone=timezone),
        explanation=explanation,
    )


def _merge_explanations(results: Sequence[MetricResult], value: Optional[float],
                        mode: str) -> dict:
    expl = [dict(r.explanation or {}) for r in results]
    out = dict(expl[0]) if expl else {}
    for key in ("included_point_ids", "input_point_ids", "output_point_ids"):
        if any(key in e for e in expl):
            out[key] = _union(e.get(key) for e in expl)
    if any("point_id" in e for e in expl):
        ids = _union([e["point_id"]] for e in expl if "point_id" in e)
        out["point_id"] = ids[-1]
        if len(ids) > 1:
            out["point_ids"] = ids
    if mode == MODE_BALANCE:
        in_total = _sum_or_none([e.get("input_value") for e in expl])
        out_total = _sum_or_none([e.get("output_value") for e in expl])
        out["input_value"] = in_total
        out["output_value"] = out_total
        pct, reason = (resolve_percentage(value, in_total)
                       if value is not None and in_total is not None
                       else (None, "no_data"))
        out["percentage"] = pct
        out["percentage_reason"] = reason
    return out
