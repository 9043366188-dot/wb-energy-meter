"""Простой режим (партия 6, docs/TZ-batch6-simple-mode.md) — интерфейсный
слой поверх узлов/связей, БЕЗ изменения модели.

Не хранит ничего своего: нет новой таблицы и нет `parent_meter_id` —
дерево питания остаётся целиком в `electrical_edges` (большое ТЗ §4.3:
"Родитель в UI выводится из связей; не хранить отдельное конкурирующее
parent_meter_id"). Этот модуль только скрыто заводит по одному узлу на
точку (по служебному коду `sm-pt-<point_id>`, восстанавливаемому
обратно — см. `point_id_from_node_code`) и, для точек с галочкой
«ввод», отдельный узел-источник (`sm-src-<point_id>`), затем создаёт
draft-связь и публикует её через уже существующий
`ElectricalEdgeRepo.publish_edges` (validate_forest никак не меняется).

Пользователь в простом режиме работает только с точками: «Это ввод»
(галочка) и «Питается от» (выбор другой точки). При конфликте
(цикл, два родителя, попытка сделать ввод сам себе источником)
поднимается `SimpleModeConflict` с сообщением уже в терминах точек
учёта — см. `_translate_violations`."""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from .topology_service import TopologyConflict

POINT_NODE_CODE_PREFIX = "sm-pt-"
SOURCE_NODE_CODE_PREFIX = "sm-src-"


class SimpleModeConflict(ValueError):
    """Отклонено простым режимом — сообщение уже человеческое, в
    терминах точек учёта (см. docs/TZ-batch6-simple-mode.md §2:
    "показать человеческий текст в терминах точек учёта")."""


def point_node_code(point_id: int) -> str:
    return f"{POINT_NODE_CODE_PREFIX}{point_id}"


def source_node_code(point_id: int) -> str:
    return f"{SOURCE_NODE_CODE_PREFIX}{point_id}"


def point_id_from_node_code(code: Optional[str]) -> Optional[int]:
    """Обратное преобразование служебного кода узла в id точки — либо
    None, если узел не заведён простым режимом (создан вручную в
    подробном режиме и не обязан подчиняться этому соглашению)."""
    if not code:
        return None
    for prefix in (POINT_NODE_CODE_PREFIX, SOURCE_NODE_CODE_PREFIX):
        if code.startswith(prefix):
            try:
                return int(code[len(prefix):])
            except ValueError:
                return None
    return None


def is_source_node_code(code: Optional[str]) -> bool:
    return bool(code) and code.startswith(SOURCE_NODE_CODE_PREFIX)


def _ensure_point_node(node_repo, point):
    """Узел точки — ровно один на точку, всегда kind='panel' (и для
    вводов тоже: "ввод" — это отдельный узел-источник ПЕРЕД узлом точки,
    см. модульный докстринг), заводится скрыто при первом обращении."""
    code = point_node_code(point.id)
    existing = node_repo.get_by_code(code)
    if existing is not None:
        return existing
    return node_repo.add(code=code, name=point.name, kind="panel",
                          location_id=point.installation_location_id)


def _ensure_source_node(node_repo, point):
    """Узел-источник внешнего питания для точки-ввода — по одному на
    каждый ввод (несколько вводов = несколько корней, большое ТЗ §4.3),
    не показывается пользователю простого режима."""
    code = source_node_code(point.id)
    existing = node_repo.get_by_code(code)
    if existing is not None:
        return existing
    return node_repo.add(code=code, name=f"Внешняя сеть ({point.name})", kind="source")


def _find_active_parent_edge(edge_repo, to_node_id):
    for e in edge_repo.list_active_published():
        if e.to_node_id == to_node_id:
            return e
    return None


def _maybe_archive_orphan_source(node_repo, node_id):
    """Best-effort уборка: если узел, потерявший единственную исходящую
    связь, — это НАШ служебный источник-ввод и на нём больше нет
    действующих связей, архивируем его, чтобы не копить в «Требует
    внимания» вечные «узел без связек». Не наш узел или узел, у которого
    связи ещё остались, — не трогаем (archive() сама откажет)."""
    node = node_repo.get_by_id(node_id)
    if node is None or node.kind != "source" or not is_source_node_code(node.code):
        return
    try:
        node_repo.archive(node_id)
    except ValueError:
        pass


def _node_label(node_repo, point_repo, node_id, cache: Dict[int, str]) -> str:
    if node_id in cache:
        return cache[node_id]
    node = node_repo.get_by_id(node_id)
    label = None
    if node is not None:
        if is_source_node_code(node.code):
            label = "внешняя сеть"
        else:
            pid = point_id_from_node_code(node.code)
            if pid is not None:
                p = point_repo.get_by_id(pid)
                if p is not None:
                    label = p.name
            if label is None:
                label = node.name
    cache[node_id] = label or f"узел {node_id}"
    return cache[node_id]


def _translate_violations(violations, node_repo, point_repo) -> str:
    cache: Dict[int, str] = {}

    def lbl(node_id):
        return _node_label(node_repo, point_repo, node_id, cache)

    messages = []
    for v in violations:
        if v.kind == "cycle":
            chain = " → ".join(lbl(n) for n in v.node_ids)
            messages.append(
                f"циклическое питание: {chain} — точка не может питаться сама "
                f"от себя через цепочку «питается от»; выберите другой источник")
        elif v.kind == "duplicate_parent":
            target = lbl(v.node_ids[0]) if v.node_ids else "точка"
            sources = [lbl(n) for n in v.node_ids[1:]]
            src_txt = " и ".join(sources) if sources else "нескольких источников"
            messages.append(
                f"«{target}» уже питается от {src_txt}; выберите один источник")
        elif v.kind == "source_has_incoming":
            messages.append(
                f"«{lbl(v.node_ids[0])}» — ввод, он не может «питаться от» другой точки")
        elif v.kind == "self_loop":
            messages.append(f"«{lbl(v.node_ids[0])}» не может питаться сама от себя")
        elif v.kind == "load_has_outgoing":
            messages.append(
                f"«{lbl(v.node_ids[0])}» не может питать другие точки "
                f"(доступно только в подробном режиме)")
        else:
            messages.append(v.message)
    return "; ".join(messages) if messages else "конфликт топологии"


def describe_power_supply(node_repo, edge_repo, point_id) -> dict:
    """Текущее состояние «питания» точки для карточки простого режима:
    is_input (галочка «ввод»), fed_from_point_id («питается от», либо
    None). Ничего не пишет, только читает уже опубликованный граф."""
    own_node = node_repo.get_by_code(point_node_code(point_id))
    if own_node is None:
        return {"is_input": False, "fed_from_point_id": None}
    edge = _find_active_parent_edge(edge_repo, own_node.id)
    if edge is None:
        return {"is_input": False, "fed_from_point_id": None}
    parent_node = node_repo.get_by_id(edge.from_node_id)
    if parent_node is not None and parent_node.kind == "source" and \
            is_source_node_code(parent_node.code):
        return {"is_input": True, "fed_from_point_id": None}
    parent_point_id = point_id_from_node_code(parent_node.code) if parent_node else None
    return {"is_input": False, "fed_from_point_id": parent_point_id}


def bulk_describe_power_supply(node_repo, edge_repo, point_ids) -> Dict[int, dict]:
    """Как describe_power_supply(), но одним проходом по всем узлам и
    опубликованным связям — для списков (structure/points), где отдельный
    запрос на точку означал бы O(n) чтений БД."""
    nodes_by_id = {n.id: n for n in node_repo.list_all(include_archived=True)}
    nodes_by_code = {n.code: n for n in nodes_by_id.values()}
    edges = edge_repo.list_active_published()
    parent_edge_by_to_node = {}
    for e in edges:
        parent_edge_by_to_node[e.to_node_id] = e

    out = {}
    for pid in point_ids:
        own_node = nodes_by_code.get(point_node_code(pid))
        if own_node is None:
            out[pid] = {"is_input": False, "fed_from_point_id": None}
            continue
        edge = parent_edge_by_to_node.get(own_node.id)
        if edge is None:
            out[pid] = {"is_input": False, "fed_from_point_id": None}
            continue
        parent_node = nodes_by_id.get(edge.from_node_id)
        if parent_node is not None and parent_node.kind == "source" and \
                is_source_node_code(parent_node.code):
            out[pid] = {"is_input": True, "fed_from_point_id": None}
            continue
        parent_point_id = point_id_from_node_code(parent_node.code) if parent_node else None
        out[pid] = {"is_input": False, "fed_from_point_id": parent_point_id}
    return out


def apply_power_supply(point_repo, node_repo, edge_repo, point_id,
                        is_input: bool, fed_from_point_id: Optional[int]):
    """Единая точка входа простого режима (docs/TZ-batch6-simple-mode.md
    §2). Вызывающий код (api_v2.py) оборачивает это в
    with_revision_check — одна ревизия на весь вызов, откат целиком при
    любой ошибке (ничего не остаётся полусохранённым).

    is_input=True и fed_from_point_id одновременно — противоречие
    (ввод не может питаться от другой точки). Оба "пусто" — снять
    питание (закрыть текущую связь, если была)."""
    is_input = bool(is_input)
    if is_input and fed_from_point_id is not None:
        raise SimpleModeConflict(
            "Ввод не может «питаться от» другой точки — снимите галочку "
            "«ввод», если нужно указать источник")
    if fed_from_point_id is not None and fed_from_point_id == point_id:
        raise SimpleModeConflict("Точка не может «питаться от» самой себя")

    point = point_repo.get_by_id(point_id)
    if point is None:
        raise ValueError(f"Точка {point_id} не найдена")

    own_node = _ensure_point_node(node_repo, point)
    current_edge = _find_active_parent_edge(edge_repo, own_node.id)

    if not is_input and fed_from_point_id is None:
        if current_edge is not None:
            edge_repo.retire_edge(current_edge.id)
            _maybe_archive_orphan_source(node_repo, current_edge.from_node_id)
        return point

    if is_input:
        from_node = _ensure_source_node(node_repo, point)
    else:
        parent_point = point_repo.get_by_id(fed_from_point_id)
        if parent_point is None:
            raise ValueError(f"Точка {fed_from_point_id} не найдена")
        from_node = _ensure_point_node(node_repo, parent_point)

    if current_edge is not None and current_edge.from_node_id == from_node.id:
        return point  # уже так и опубликовано — идемпотентно, писать нечего

    draft = edge_repo.add_draft(
        from_node_id=from_node.id, to_node_id=own_node.id,
        primary_point_id=point.id,
        code=f"sm-{point.id}-{int(time.time() * 1_000_000)}",
    )

    violations = edge_repo.validate_edges([draft.id])
    if violations:
        raise SimpleModeConflict(_translate_violations(violations, node_repo, point_repo))

    try:
        edge_repo.publish_edges([draft.id])
    except TopologyConflict as e:
        raise SimpleModeConflict(str(e)) from e

    if current_edge is not None and current_edge.from_node_id != from_node.id:
        _maybe_archive_orphan_source(node_repo, current_edge.from_node_id)

    return point
