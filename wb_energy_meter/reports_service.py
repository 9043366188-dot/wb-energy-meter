"""Расчёт срезов отчёта `POST /api/v2/reports/query` — партия 11, этап 11.4
(F1, docs/TZ-finish-plan.md): расчёт вынесен из обработчика `api_v2.py` по
образцу `overview_service.py`. В обработчике остались разбор тела запроса,
фиксация ревизии (A43: один `db.read()` = один снимок WAL) и перевод
исключений в HTTP; всё, что превращает идентификаторы срезов в строки
отчёта, живёт здесь и проверяется без HTTP (`tests/test_step51_reports_service.py`).

Срезы (`dimension`) считаются поверх ТОГО ЖЕ расчётного слоя v2
(`accounting_service`), что и Обзор и `metrics/query`: числа совпадают по
построению, отдельной формулы для отчётов нет (ТЗ §5: «не реализовывать
разные формулы в дашборде, отчётах и JavaScript»).

A21/A22 — режимы состава группы:
  composition_mode="as_was" — эффективный состав группы определяется НА
  МОМЕНТ начала запрошенного периода (`group_repo.resolve_effective_members(
  gid, at=ts_from)`); перевод точки в другую группу ПОСЛЕ периода не меняет
  уже посчитанный прошлый отчёт.
  composition_mode="current" — состав группы на сейчас, но приборы
  резолвятся исторически верно: `measured_point()` всегда сегментирует
  период по истории привязок точки независимо от режима.

Сквозной `cache` (партия 10, этап E) — один словарь на ОДИН HTTP-ответ,
общий для всех вызовов `measured_point`/`sum_points`: для
`dimension="branch"` точки этих же строк уже посчитаны внутри
`overview_service.network_branches_for_reports()`, без общего кэша это был
бы третий пересчёт одного и того же.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import overview_service
from .accounting_contract import resolve_percentage
from .accounting_service import (
    AccountingConflict, balance, measured_point, sum_points,
)

DIMENSIONS = ("point", "branch", "group", "balance_scope")
COMPOSITION_MODES = ("as_was", "current")


class ReportTargetNotFound(LookupError):
    """Запрошенный срез (точка, группа, граница баланса) не существует.
    Обработчик переводит в 404 с `ids`; сообщение уже готово для ответа."""

    def __init__(self, message: str, ids: Sequence[int]):
        super().__init__(message)
        self.ids = list(ids)


def resolve_group_points(group_repo, group_id: int, composition_mode: str,
                         ts_from: int) -> List[int]:
    """Состав группы для среза: `as_was` — на момент начала периода,
    `current` — на сейчас."""
    at = ts_from if composition_mode == "as_was" else None
    return [m["point_id"] for m in group_repo.resolve_effective_members(group_id, at=at)]


def row_result(dimension: str, point_ids: Optional[Sequence[int]], binding_repo,
               aggregates_repo, source_repo, edge_repo, ts_from: int, ts_to: int,
               timezone_name: str, cache: Optional[dict] = None):
    """Точка — `measured_point` по единственному id; ветвь/группа —
    `sum_points` по составу. A04: подтверждённое электрическое пересечение
    НЕ схлопывает всю выгрузку — только эта строка получает `conflict_reason`
    и `result=None`, остальные строки считаются как обычно (в отличие от
    Обзора, здесь без отката в сравнение: отчёт технический, конфликт должен
    быть виден и устранён в топологии, а не молча подменён поточной
    раскладкой).

    Возвращает `(MetricResult | None, conflict_reason | None)`."""
    if dimension == "point":
        if not point_ids:
            return None, "точка не найдена"
        result = measured_point(binding_repo, aggregates_repo, source_repo,
                                point_ids[0], ts_from, ts_to, timezone_name,
                                _cache=cache)
        return result, None
    if not point_ids:
        return None, None
    try:
        result = sum_points(binding_repo, aggregates_repo, source_repo, edge_repo,
                            point_ids, ts_from, ts_to, timezone_name, _cache=cache)
        return result, None
    except AccountingConflict as e:
        return None, str(e)


def resolve_targets(c, *, dimension: str, scope_ids: Optional[Sequence[int]],
                    ts_from: int, ts_to: int, timezone_name: str,
                    composition_mode: str, binding_repo, aggregates_repo,
                    source_repo, edge_repo, node_repo, group_repo, point_repo,
                    cache: dict) -> List[Tuple[int, str, Optional[List[int]]]]:
    """Список `(id, name, point_ids | None)` по запрошенным срезам. Для
    `balance_scope` состав точек считает сам `balance()`, поэтому `None`.
    Несуществующий срез — `ReportTargetNotFound` (до какого-либо расчёта)."""
    targets: List[Tuple[int, str, Optional[List[int]]]] = []
    if dimension == "point":
        for pid in scope_ids:
            p = point_repo.get_by_id(pid)
            if p is None:
                raise ReportTargetNotFound(f"Точка {pid} не найдена", [pid])
            targets.append((p.id, p.name, [p.id]))
    elif dimension == "branch":
        # Партия 7, Этап 2, Э2.6: «branch» = сетевые ветви уровня 1 из
        # overview_service (те же числа, что на Обзоре, A43), а не дубль
        # dimension="group". composition_mode/ts_from здесь не участвуют:
        # структура сети — только «текущая» (Э2.1, историческая топология
        # вне объёма), в отличие от исторического состава учётных групп.
        for nb in overview_service.network_branches_for_reports(
                binding_repo, aggregates_repo, source_repo, edge_repo,
                node_repo, ts_from, ts_to, timezone_name, _cache=cache):
            targets.append((nb["edge_id"], nb["name"], [nb["point_id"]]))
    elif dimension == "group":
        for gid in scope_ids:
            g = group_repo.get_by_id(gid)
            if g is None:
                raise ReportTargetNotFound(f"Группа {gid} не найдена", [gid])
            pts = resolve_group_points(group_repo, gid, composition_mode, ts_from)
            targets.append((g.id, g.name, pts))
    else:  # balance_scope
        for sid in scope_ids:
            row = c.execute(
                "SELECT * FROM balance_scopes WHERE id = ?", (sid,)).fetchone()
            if row is None:
                raise ReportTargetNotFound(f"Граница баланса {sid} не найдена", [sid])
            targets.append((row["id"], row["name"], None))
    return targets


def build_rows(c, *, db, dimension: str, scope_ids: Optional[Sequence[int]],
               ts_from: int, ts_to: int, timezone_name: str, composition_mode: str,
               compare_range: Optional[Tuple[int, int]], pinned_revision: int,
               binding_repo, aggregates_repo, source_repo, edge_repo, node_repo,
               group_repo, point_repo,
               tag_result: Callable[[object, int], object],
               cache: Optional[dict] = None) -> List[Dict]:
    """Строки отчёта. `c` — соединение открытого `db.read()` (один снимок на
    весь расчёт, A43); `tag_result(result, revision_id)` проставляет
    ревизию и `as_of` на готовый результат — это делает HTTP-слой, а не
    расчётный сервис. `compare_range` — `(from, to)` второго периода или
    `None`: тогда в строку добавляются поля сравнения (A21/A22: явно
    раскрываем отличие состава между периодами, а не молча публикуем разницу
    чисел, посчитанных по разному составу точек)."""
    if cache is None:
        cache = {}
    cmp_ts_from = cmp_ts_to = None
    if compare_range is not None:
        cmp_ts_from, cmp_ts_to = compare_range

    targets = resolve_targets(
        c, dimension=dimension, scope_ids=scope_ids, ts_from=ts_from, ts_to=ts_to,
        timezone_name=timezone_name, composition_mode=composition_mode,
        binding_repo=binding_repo, aggregates_repo=aggregates_repo,
        source_repo=source_repo, edge_repo=edge_repo, node_repo=node_repo,
        group_repo=group_repo, point_repo=point_repo, cache=cache)

    rows = []
    for scope_id, name, point_ids in targets:
        if dimension == "balance_scope":
            result = balance(db, binding_repo, aggregates_repo, source_repo, edge_repo,
                             scope_id, ts_from, ts_to, timezone_name)
            conflict_reason = None
            member_ids = (result.explanation.get("input_point_ids", [])
                          + result.explanation.get("output_point_ids", []))
        else:
            result, conflict_reason = row_result(
                dimension, point_ids, binding_repo, aggregates_repo, source_repo,
                edge_repo, ts_from, ts_to, timezone_name, cache=cache)
            member_ids = point_ids

        if result is not None:
            tag_result(result, pinned_revision)

        row = {
            "dimension": dimension,
            "id": scope_id,
            "name": name,
            "member_point_ids": member_ids,
            "result": result.to_dict() if result is not None else None,
            "conflict_reason": conflict_reason,
        }

        if compare_range is not None:
            if dimension == "group":
                cmp_point_ids = resolve_group_points(
                    group_repo, scope_id, composition_mode, cmp_ts_from)
            else:
                # «branch» (Э2.6, партия 7): структура сети — только текущая
                # (Э2.1), у сетевой ветви нет исторического состава, в
                # отличие от учётной группы — тот же point_id сравнивается
                # за оба периода.
                cmp_point_ids = point_ids

            if dimension == "balance_scope":
                cmp_result = balance(db, binding_repo, aggregates_repo, source_repo,
                                     edge_repo, scope_id, cmp_ts_from, cmp_ts_to,
                                     timezone_name)
                cmp_conflict = None
                cmp_member_ids = (cmp_result.explanation.get("input_point_ids", [])
                                  + cmp_result.explanation.get("output_point_ids", []))
            else:
                cmp_result, cmp_conflict = row_result(
                    dimension, cmp_point_ids, binding_repo, aggregates_repo,
                    source_repo, edge_repo, cmp_ts_from, cmp_ts_to, timezone_name,
                    cache=cache)
                cmp_member_ids = cmp_point_ids

            if cmp_result is not None:
                tag_result(cmp_result, pinned_revision)

            # В режиме composition_mode="current" состав в обоих периодах
            # резолвится «на сейчас» (at=None) — по определению одинаков,
            # composition_changed всегда False (это и есть смысл режима).
            composition_changed = (
                sorted(member_ids or []) != sorted(cmp_member_ids or [])
                if dimension == "group" else False
            )

            delta_value = None
            if (result is not None and cmp_result is not None
                    and result.value is not None and cmp_result.value is not None):
                delta_value = round(result.value - cmp_result.value, 6)
            delta_pct, delta_pct_reason = resolve_percentage(
                delta_value, cmp_result.value if cmp_result is not None else None)

            row["compare_member_point_ids"] = cmp_member_ids
            row["compare_result"] = cmp_result.to_dict() if cmp_result is not None else None
            row["compare_conflict_reason"] = cmp_conflict
            row["composition_changed"] = composition_changed
            row["delta_value"] = delta_value
            row["delta_percentage"] = delta_pct
            row["delta_percentage_reason"] = delta_pct_reason

        rows.append(row)
    return rows
