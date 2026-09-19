"""Сервис вкладки «План v3» (docs/TZ-batch6-plan-v3.md) — тонкий слой
поверх topology_service/location_repo для действий, которые видит
пользователь как «на карте», а не как «узел/связь/публикация».

Ничего нового в модели не заводит: узлы и связи — те же
electrical_nodes/electrical_edges, что и у «Структуры»/«Плана v2»;
единственная электрическая привязка точки учёта остаётся к edge
(большое ТЗ §4.3, "точка не является node") — этот модуль не создаёт
никакого параллельного parent_meter_id и вообще не пишет в
metering_points.

Ключевое упрощение задания (§1): «точки, установленные в узле» — это
точки, измеряющие линии, СМЕЖНЫЕ с этим узлом (одна входящая, сколько
угодно отходящих) — вычисляется на лету по electrical_edges, отдельного
поля/таблицы для этого не существует и не нужно.

Конфликты леса (цикл, второй источник питания одного узла) — это ровно
те же TopologyViolation, что жила в topology_service.validate_forest
до этой партии; здесь только перевод в текст на языке узлов и линий, по
их именам, а не по сырым id (§2 задания: "конфликт объясняется в
терминах узлов и линий, а не сырым ответом валидатора")."""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from .topology_service import TopologyConflict, VALID_NODE_KINDS


class PlanV3Conflict(ValueError):
    """Отклонено правилами леса (цикл, второй источник питания и т. п.)
    — API-слой транслирует в HTTP 409. Вызывающий код (api_v2.py) всегда
    оборачивает connect_nodes/add_consumer_line в with_revision_check —
    вся цепочка (в т. ч. add_draft, а у add_consumer_line ещё и
    node_repo.add нового узла-потребителя) выполняется ВНУТРИ одной
    реентрантной db.transaction() (db.py), поэтому при любом исключении
    откатывается целиком: ни черновик связи, ни узел-потребитель, из-за
    которого распознан конфликт, в БД не остаются — ничего не
    сохраняется, буквально, а не только «не публикуется» (тот же
    механизм, на котором держится simple_mode_service.apply_power_supply)."""


def _node_label(node_repo, node_id: int, cache: Dict[int, str]) -> str:
    if node_id in cache:
        return cache[node_id]
    node = node_repo.get_by_id(node_id)
    label = node.name if node is not None else f"узел {node_id}"
    cache[node_id] = label
    return label


def translate_violations(violations, node_repo) -> str:
    """Как simple_mode_service._translate_violations, но в терминах
    узлов/линий напрямую (План v3 не прячет узлы за точками — они и
    есть то, что пользователь только что нарисовал на карте)."""
    cache: Dict[int, str] = {}

    def lbl(node_id):
        return _node_label(node_repo, node_id, cache)

    messages = []
    for v in violations:
        if v.kind == "cycle":
            chain = " → ".join(lbl(n) for n in v.node_ids)
            messages.append(
                f"циклическое питание: {chain} — узел не может питаться "
                f"сам от себя через цепочку линий; выберите другую линию")
        elif v.kind == "duplicate_parent":
            target = lbl(v.node_ids[0]) if v.node_ids else "узел"
            sources = [lbl(n) for n in v.node_ids[1:]]
            src_txt = " и ".join(sources) if sources else "нескольких узлов"
            messages.append(
                f"«{target}» уже получает питание от {src_txt} — у узла "
                f"может быть только один источник; сначала уберите "
                f"действующую входящую линию")
        elif v.kind == "source_has_incoming":
            messages.append(
                f"«{lbl(v.node_ids[0])}» — ввод, к нему нельзя провести "
                f"линию питания от другого узла")
        elif v.kind == "self_loop":
            messages.append(f"«{lbl(v.node_ids[0])}» нельзя соединить линией с самим собой")
        elif v.kind == "load_has_outgoing":
            messages.append(
                f"«{lbl(v.node_ids[0])}» — потребитель, от него нельзя "
                f"провести отходящую линию")
        else:
            messages.append(v.message)
    return "; ".join(messages) if messages else "конфликт топологии"


def _auto_edge_code(from_node_id: int, to_node_id: int) -> str:
    return f"pv3-e-{from_node_id}-{to_node_id}-{int(time.time() * 1_000_000)}"


def _auto_node_code(kind: str) -> str:
    return f"pv3-{kind}-{int(time.time() * 1_000_000)}"


def _find_active_incoming_edge(edge_repo, to_node_id):
    for e in edge_repo.list_active_published():
        if e.to_node_id == to_node_id:
            return e
    return None


def connect_nodes(node_repo, edge_repo, from_node_id: int, to_node_id: int, *,
                   code: Optional[str] = None, name: Optional[str] = None,
                   primary_point_id: Optional[int] = None,
                   phase_count: Optional[int] = None,
                   rated_current_a: Optional[float] = None,
                   cable_note: Optional[str] = None):
    """«Соединить линией» (§2 задания) — создаёт черновик связи и сразу
    публикует его. При конфликте леса (проверяется ДО попытки
    опубликовать, тем же _compute_candidate/validate_forest, что и штатный
    /api/v2/topology/publish) — PlanV3Conflict с текстом по именам узлов,
    активный (опубликованный) граф не меняется.

    Отдельная явная проверка «второй источник питания у узла» ДО
    add_draft (задание §8: "второй источник питания у одного узла
    отклоняется... ничего не сохраняет"): publish_edges сам по себе
    считает второй ввод не конфликтом, а СМЕНОЙ родителя — старая
    входящая связь узла автоматически "вытесняется" новой в одной
    транзакции (см. ElectricalEdgeRepo.publish_edges/_compute_candidate
    — так и должно быть для случая "переключить источник"). Но
    инструмент «соединить линией» на карте рисует НОВУЮ линию, а не
    выбирает источник из выпадающего списка (как это делает простой
    режим точки, docs/TZ-batch6-simple-mode.md) — молча заменить
    существующий ввод узла было бы неожиданно и опасно, поэтому здесь
    это явная ошибка, а не тихая замена."""
    if node_repo.get_by_id(from_node_id) is None:
        raise ValueError(f"Узел {from_node_id} не найден")
    if node_repo.get_by_id(to_node_id) is None:
        raise ValueError(f"Узел {to_node_id} не найден")

    existing_incoming = _find_active_incoming_edge(edge_repo, to_node_id)
    if existing_incoming is not None and existing_incoming.from_node_id != from_node_id:
        to_name = _node_label(node_repo, to_node_id, {})
        old_src_name = _node_label(node_repo, existing_incoming.from_node_id, {})
        new_src_name = _node_label(node_repo, from_node_id, {})
        raise PlanV3Conflict(
            f"«{to_name}» уже получает питание от «{old_src_name}» — у узла "
            f"может быть только один источник; чтобы подключить «{new_src_name}», "
            f"сначала уберите действующую линию от «{old_src_name}»")

    draft = edge_repo.add_draft(
        from_node_id=from_node_id, to_node_id=to_node_id,
        code=code or _auto_edge_code(from_node_id, to_node_id),
        name=name, primary_point_id=primary_point_id,
        phase_count=phase_count, rated_current_a=rated_current_a,
        cable_note=cable_note,
    )

    violations = edge_repo.validate_edges([draft.id])
    if violations:
        raise PlanV3Conflict(translate_violations(violations, node_repo))

    try:
        published = edge_repo.publish_edges([draft.id])
    except TopologyConflict as e:
        raise PlanV3Conflict(str(e)) from e
    return published[0]


def add_consumer_line(node_repo, edge_repo, from_node_id: int, load_name: str, *,
                       load_code: Optional[str] = None,
                       edge_code: Optional[str] = None,
                       rated_current_a: Optional[float] = None,
                       cable_note: Optional[str] = None):
    """«Добавить отходящую линию» (§1 задания) — заводит узел-потребителя
    (kind='load') с именем load_name и линию к нему от from_node_id,
    одним действием пользователя. Проверка "from_node_id не load" —
    заранее, чтобы не заводить узел-сироту, если линию всё равно нельзя
    будет провести (load не бывает источником, §4.3 большого ТЗ)."""
    from_node = node_repo.get_by_id(from_node_id)
    if from_node is None:
        raise ValueError(f"Узел {from_node_id} не найден")
    if from_node.kind == "load":
        raise PlanV3Conflict(
            f"«{from_node.name}» — потребитель, от него нельзя провести "
            f"отходящую линию")

    load_name = (load_name or "").strip()
    if not load_name:
        raise ValueError("укажите, что питает новая линия (имя узла-потребителя)")

    new_node = node_repo.add(
        code=load_code or _auto_node_code("load"), name=load_name, kind="load")

    edge = connect_nodes(
        node_repo, edge_repo, from_node_id, new_node.id,
        code=edge_code, rated_current_a=rated_current_a, cable_note=cable_note)
    return new_node, edge


def describe_node_lines(node_repo, edge_repo, point_repo, node_id: int) -> List[dict]:
    """Список линий узла для карточки (§1 задания: «в карточке узла
    показывать список линий этого узла») — одна входящая (максимум,
    инвариант леса) и сколько угодно отходящих, каждая — со своей
    измеряющей точкой (или без неё, линия без счётчика допустима)."""
    node = node_repo.get_by_id(node_id)
    if node is None:
        raise ValueError(f"Узел {node_id} не найден")

    published = edge_repo.list_active_published()
    nodes_cache: Dict[int, str] = {}

    def other_node_name(nid):
        return _node_label(node_repo, nid, nodes_cache)

    out = []
    for e in published:
        if e.to_node_id == node_id:
            direction, other_id = "in", e.from_node_id
        elif e.from_node_id == node_id:
            direction, other_id = "out", e.to_node_id
        else:
            continue
        point = point_repo.get_by_id(e.primary_point_id) if e.primary_point_id else None
        out.append({
            "edge_id": e.id, "direction": direction,
            "other_node_id": other_id, "other_node_name": other_node_name(other_id),
            "code": e.code, "name": e.name,
            "primary_point_id": e.primary_point_id,
            "primary_point_code": point.code if point else None,
            "primary_point_name": point.name if point else None,
            "phase_count": e.phase_count, "rated_current_a": e.rated_current_a,
            "cable_note": e.cable_note,
        })
    # входящая линия — первой, отходящие — по имени другого узла
    out.sort(key=lambda d: (d["direction"] != "in", d["other_node_name"] or ""))
    return out


def resolve_location_text(location_repo, text: Optional[str]):
    """Свободный текст «где стоит узла» (§2 задания) — под капотом
    ищет место по имени (казефолд) и заводит новое (kind='room'), если
    не нашлось; сущность locations не удаляем, просто не требуем и не
    рисуем отдельно (§2: "Сущность locations из модели не удалять... но
    не требовать и на карте не рисовать"). text="" / None — снять место
    (возвращает None)."""
    text = (text or "").strip()
    if not text:
        return None
    existing = location_repo.get_by_name(text)
    if existing is not None:
        return existing
    return location_repo.add(name=text, kind="room")
