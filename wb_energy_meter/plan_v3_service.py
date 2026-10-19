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
    сохраняется, буквально, а не только «не публикуется»."""


class LineAlreadyMetered(PlanV3Conflict):
    """Линия уже измеряется другой действующей точкой (партия 13, П5):
    осознанная замена счётчика — отдельное действие, «+ Счётчик» её молча
    не делает. API-слой отдаёт 409 `already_metered`."""

    def __init__(self, message: str, edge_id: int, point_id: int):
        super().__init__(message)
        self.edge_id = edge_id
        self.point_id = point_id


# Что может измерять счётчик в узле каждого типа (партия 13, П5, R10).
# «input» — входящую линию узла, «outgoing» — одну из отходящих. Счётчик
# ставится на ЛИНИЮ (точка измеряет edge, не node — модель расчёта не
# меняется), узел только определяет, какие линии у него бывают.
METER_MEASURES_BY_KIND = {
    "source": ("outgoing",),
    "panel": ("input", "outgoing"),
    "junction": ("input", "outgoing"),
    "load": ("input",),
}

def _node_label(node_repo, node_id: int, cache: Dict[int, str]) -> str:
    if node_id in cache:
        return cache[node_id]
    node = node_repo.get_by_id(node_id)
    label = node.name if node is not None else f"узел {node_id}"
    cache[node_id] = label
    return label


def translate_violations(violations, node_repo) -> str:
    """Конфликты леса в терминах узлов/линий напрямую (План v3 не прячет
    узлы за точками — они и есть то, что пользователь только что
    нарисовал на карте)."""
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
    выбирает источник из выпадающего списка — молча заменить
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
                       cable_note: Optional[str] = None,
                       phase_count: Optional[int] = None):
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
        code=edge_code, rated_current_a=rated_current_a, cable_note=cable_note,
        phase_count=phase_count)
    return new_node, edge


def resolve_meter_line(node_repo, edge_repo, node_id: int, measures: str, *,
                       line_id: Optional[int] = None,
                       to_node_id: Optional[int] = None,
                       new_load_name: Optional[str] = None,
                       fed_from_node_id: Optional[int] = None,
                       phase_count: Optional[int] = None):
    """Какую линию измеряет новый счётчик узла `node_id` (партия 13, П5,
    «+ Счётчик»). Возвращает (edge, created_edge, new_node): линию,
    признак «линия создана этим же действием» и новый узел-нагрузку (или
    None). Недостающую линию (и нагрузку) создаёт ЗДЕСЬ тем же кодом, что
    «соединить линией» (`connect_nodes`) и «добавить отходящую линию»
    (`add_consumer_line`); сам счётчик (точка, привязка, «измеряет линию»)
    ставит вызывающий код api_v2.py — ВСЁ внутри одного
    with_revision_check, поэтому при любом отказе не остаётся ничего, в
    том числе созданной линии и нагрузки.

    measures:
      "input"    — входящая линия узла. Если её нет — нужен
                   `fed_from_node_id` («Откуда щит питается?»), линия
                   создаётся от этого узла;
      "outgoing" — одна из отходящих: уже существующая (`line_id`), либо
                   к существующему узлу (`to_node_id`; линия создаётся,
                   если её не было), либо к НОВОЙ нагрузке
                   (`new_load_name`).
    `phase_count` (П9, «по фазам») — число фаз для СОЗДАВАЕМЫХ этим вызовом
    линий (у линии одной фазы — 1); существующую линию проставляет вызывающий.
    ValueError — неверные входные данные (400); PlanV3Conflict — правила
    леса (409)."""
    node = node_repo.get_by_id(node_id)
    if node is None:
        raise ValueError(f"Узел {node_id} не найден")
    if node.archived_at is not None:
        raise ValueError(f"«{node.name}» в архиве — счётчик в него не добавить")
    allowed = METER_MEASURES_BY_KIND.get(node.kind, ())
    if measures not in ("input", "outgoing"):
        raise ValueError(
            "укажите, что измеряет счётчик: measures=\"input\" (входящую "
            "линию) или \"outgoing\" (отходящую)")
    if measures not in allowed:
        if node.kind == "source":
            raise ValueError(
                f"«{node.name}» — ввод: в него не входит линия, счётчик "
                f"ставится на отходящую")
        if node.kind == "load":
            raise ValueError(
                f"«{node.name}» — нагрузка: от неё не отходят линии, "
                f"счётчик ставится на входящую")
        raise ValueError(f"в узле «{node.name}» нельзя поставить такой счётчик")

    published = edge_repo.list_active_published()

    if measures == "input":
        incoming = next((e for e in published if e.to_node_id == node_id), None)
        if line_id is not None and (incoming is None or incoming.id != line_id):
            raise ValueError(f"линия {line_id} не входит в «{node.name}»")
        if incoming is not None:
            return incoming, False, None
        if fed_from_node_id is None:
            raise ValueError(
                f"«{node.name}» пока ни от чего не питается — укажите, "
                f"откуда (fed_from_node_id): линия создастся вместе со счётчиком")
        if fed_from_node_id == node_id:
            raise ValueError(f"«{node.name}» нельзя питать от самого себя")
        feeder = node_repo.get_by_id(fed_from_node_id)
        if feeder is None or feeder.archived_at is not None:
            raise ValueError(f"Узел {fed_from_node_id} не найден")
        edge = connect_nodes(node_repo, edge_repo, fed_from_node_id, node_id,
                             phase_count=phase_count)
        return edge, True, None

    # outgoing
    outs = [e for e in published if e.from_node_id == node_id]
    if line_id is not None:
        edge = next((e for e in outs if e.id == line_id), None)
        if edge is None:
            raise ValueError(f"линия {line_id} не отходит от «{node.name}»")
        return edge, False, None
    if to_node_id is not None:
        target = node_repo.get_by_id(to_node_id)
        if target is None or target.archived_at is not None:
            raise ValueError(f"Узел {to_node_id} не найден")
        if to_node_id == node_id:
            raise ValueError(f"«{node.name}» нельзя соединить линией с самим собой")
        existing = next((e for e in outs if e.to_node_id == to_node_id), None)
        if existing is not None:
            return existing, False, None
        edge = connect_nodes(node_repo, edge_repo, node_id, to_node_id,
                             phase_count=phase_count)
        return edge, True, None
    if new_load_name is not None and str(new_load_name).strip():
        new_node, edge = add_consumer_line(
            node_repo, edge_repo, node_id, str(new_load_name),
            phase_count=phase_count)
        return edge, True, new_node
    raise ValueError(
        "укажите, какую отходящую линию измеряет счётчик: существующую "
        "(line_id), к другому узлу (to_node_id) или к новой нагрузке "
        "(new_load_name)")


def describe_node_lines(node_repo, edge_repo, point_repo, node_id: int,
                        binding_repo=None, source_repo=None) -> List[dict]:
    """Список линий узла для карточки (§1 задания: «в карточке узла
    показывать список линий этого узла») — одна входящая (максимум,
    инвариант леса) и сколько угодно отходящих, каждая — со своей
    измеряющей точкой (или без неё, линия без счётчика допустима).

    С `binding_repo` и `source_repo` (П9) у линии с точкой добавлены
    `meter_device_id`, `channel_profile` и `phase` ("L1"…"L3" или null) —
    карточка узла пишет «wb-map3e_16 · L1 — линия к «Освещение»»."""
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
        if point is not None and point.archived_at is not None:
            # Партия 13, П1: архивная точка линию больше не измеряет
            # (устойчивость к базам, где ссылка на неё осталась).
            point = None
        out.append({
            "edge_id": e.id, "direction": direction,
            "other_node_id": other_id, "other_node_name": other_node_name(other_id),
            "code": e.code, "name": e.name,
            "primary_point_id": point.id if point else None,
            "primary_point_code": point.code if point else None,
            "primary_point_name": point.name if point else None,
            "phase_count": e.phase_count, "rated_current_a": e.rated_current_a,
            "cable_note": e.cable_note,
        })
        if binding_repo is not None and source_repo is not None:
            device_id = profile = phase = None
            if point is not None:
                binding = binding_repo.get_open_primary(point.id)
                if binding is not None:
                    src = source_repo.get_by_id(binding.meter_source_id)
                    device_id = src.device_id if src is not None else None
                    profile = binding.channel_profile
                    phase = {"phase_l1": "L1", "phase_l2": "L2",
                             "phase_l3": "L3"}.get(profile)
            out[-1].update({"meter_device_id": device_id,
                            "channel_profile": profile, "phase": phase})
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


def _edge_display_label(edge, from_name: str, to_name: str) -> str:
    """Человеческая подпись линии: своё имя, иначе «Ввод → ЩР-1» по
    концам. Код связи — только если у концов не нашлось имён.

    Порядок именно такой, а не «имя или код»: код здесь всегда есть
    (`connect_nodes`/`add_consumer` генерируют `pv3-e-<from>-<to>-<нс>`),
    поэтому вариант с кодом на первом месте означал бы, что у каждой
    неименованной линии в интерфейсе стоит `pv3-e-3-4-1790434002725159`,
    а ветка «A → B» недостижима. Это ровно тот баг, который уже чинили в
    партии 6, §5 («подпись связи edge_id: 1 — показывать имя или
    „Ввод → ЩР-1“ по концам; число только как запасной вариант»)."""
    if edge.name:
        return edge.name
    if from_name and to_name:
        return f"{from_name} → {to_name}"
    return edge.code or f"#{edge.id}"


def describe_point_supply(node_repo, edge_repo, point_id) -> dict:
    """Питание точки учёта, выведенное из топологии «Плана v3» (партия 8,
    этап A: замена удалённого simple_mode_service.describe_power_supply).
    Точка не имеет своего узла — она измеряет линию
    (electrical_edges.primary_point_id). Возвращает:
    - measured_edge_id/measured_edge_label — какую опубликованную линию
      измеряет точка (label — имя связи, иначе "A → B" по узлам; код —
      только если у концов нет имён, см. _edge_display_label), либо
      None, если точка не назначена измерением ни одной линии;
    - is_input — эта линия выходит из узла-ввода (kind='source');
    - fed_from_point_id — точка, измеряющая ближайшую вверх по дереву
      измеряемую линию (немереные промежуточные сегменты пропускаются),
      либо None, если такой нет (в т. ч. когда is_input=True)."""
    edges = edge_repo.list_active_published()
    measured = next((e for e in edges if e.primary_point_id == point_id), None)
    if measured is None:
        return {"is_input": False, "fed_from_point_id": None,
                "measured_edge_id": None, "measured_edge_label": None}

    cache: Dict[int, str] = {}
    label = _edge_display_label(
        measured,
        _node_label(node_repo, measured.from_node_id, cache),
        _node_label(node_repo, measured.to_node_id, cache))

    from_node = node_repo.get_by_id(measured.from_node_id)
    is_input = bool(from_node is not None and from_node.kind == "source")

    fed_from_point_id = None
    if not is_input:
        edges_by_to_node = {e.to_node_id: e for e in edges}
        seen_nodes = set()
        cur_node_id = measured.from_node_id
        while cur_node_id is not None and cur_node_id not in seen_nodes:
            seen_nodes.add(cur_node_id)
            parent_edge = edges_by_to_node.get(cur_node_id)
            if parent_edge is None:
                break
            if parent_edge.primary_point_id is not None:
                fed_from_point_id = parent_edge.primary_point_id
                break
            cur_node_id = parent_edge.from_node_id

    return {
        "is_input": is_input,
        "fed_from_point_id": fed_from_point_id,
        "measured_edge_id": measured.id,
        "measured_edge_label": label,
    }


def bulk_describe_point_supply(node_repo, edge_repo, point_ids) -> Dict[int, dict]:
    """Как describe_point_supply(), но одним проходом по всем узлам и
    опубликованным связям — для списков (/api/v2/structure/points), где
    отдельный запрос на точку означал бы O(n) чтений БД."""
    edges = edge_repo.list_active_published()
    measured_by_point = {
        e.primary_point_id: e for e in edges if e.primary_point_id is not None}
    edges_by_to_node = {e.to_node_id: e for e in edges}
    nodes_by_id = {n.id: n for n in node_repo.list_all(include_archived=True)}
    label_cache: Dict[int, str] = {}

    def node_name(node_id):
        if node_id in label_cache:
            return label_cache[node_id]
        n = nodes_by_id.get(node_id)
        name = n.name if n is not None else f"узел {node_id}"
        label_cache[node_id] = name
        return name

    out = {}
    for pid in point_ids:
        measured = measured_by_point.get(pid)
        if measured is None:
            out[pid] = {"is_input": False, "fed_from_point_id": None,
                        "measured_edge_id": None, "measured_edge_label": None}
            continue

        from_node = nodes_by_id.get(measured.from_node_id)
        is_input = bool(from_node is not None and from_node.kind == "source")

        fed_from_point_id = None
        if not is_input:
            seen_nodes = set()
            cur_node_id = measured.from_node_id
            while cur_node_id is not None and cur_node_id not in seen_nodes:
                seen_nodes.add(cur_node_id)
                parent_edge = edges_by_to_node.get(cur_node_id)
                if parent_edge is None:
                    break
                if parent_edge.primary_point_id is not None:
                    fed_from_point_id = parent_edge.primary_point_id
                    break
                cur_node_id = parent_edge.from_node_id

        out[pid] = {
            "is_input": is_input,
            "fed_from_point_id": fed_from_point_id,
            "measured_edge_id": measured.id,
            "measured_edge_label": _edge_display_label(
                measured, node_name(measured.from_node_id), node_name(measured.to_node_id)),
        }
    return out
