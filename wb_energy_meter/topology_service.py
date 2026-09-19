"""Сервис электрической сети — узлы и связи, лес направленных радиальных
деревьев (ТЗ §4.3, сценарий приёмки A24).

Пишется лично (не делегировано) — топологическая корректность сети прямо
определяет, какие суммы электрически независимы (§5.1: "Электрическую
проверку выполнять по достижимости в дереве"), т.е. ошибка здесь напрямую
ведёт к двойному счёту или к скрытой потере ветви.

Инварианты леса (§4.3):
- узел `source` — корень, не имеет входящих связей ("source не бывает
  приёмником");
- узел `load` — лист, не имеет исходящих связей;
- у узла максимум ОДНА действующая питающая связь (edge.to_node_id
  уникален среди published+открытых — уже гарантировано частичным
  уникальным индексом idx_electrical_edges_one_parent в схеме, но сервис
  обязан обнаружить конфликт ДО попытки вставки и дать понятную цепочку,
  а не полагаться на голый sqlite3.IntegrityError);
- никаких циклов и самоссылок (from_node_id != to_node_id — тоже уже CHECK
  в схеме, но сервис проверяет заранее для единого отчёта об ошибке);
- несколько независимых `source` = несколько корней, это НЕ конфликт.

Публикация (publish_edges) атомарна и всё-или-ничего: если предлагаемый
набор связей делает граф невалидным, СТРУКТУРА НЕ МЕНЯЕТСЯ (A24: "текущая
структура не меняется") — ни одна старая связь не закрывается, ни одна
новая не создаётся, конфликт описывает точную цепочку/узлы."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import domain_generation

log = logging.getLogger(__name__)

VALID_NODE_KINDS = ("source", "panel", "junction", "load")
_UNSET_NODE = object()  # см. ElectricalNodeRepo.update_fields.location_id


class TopologyConflict(ValueError):
    """Публикация отклонена из-за нарушения инварианта леса (A24) —
    API-слой должен транслировать это в HTTP 409, структура не менялась."""


# ---------------------------------------------------------------------
# Чистая часть: валидация леса над произвольным набором "активных" рёбер.
# Не трогает БД — принимает простые кортежи (edge_id, from_node_id,
# to_node_id) и словарь node_id -> kind, поэтому полностью тестируема без
# фикстур и переиспользуема и для publish, и для превью в UI.
# ---------------------------------------------------------------------

@dataclass
class EdgeRef:
    edge_id: int
    from_node_id: int
    to_node_id: int


@dataclass
class TopologyViolation:
    kind: str  # self_loop | duplicate_parent | cycle | source_has_incoming | load_has_outgoing
    message: str
    node_ids: List[int]
    edge_ids: List[int]


def validate_forest(edges: List[EdgeRef], node_kind_by_id: Dict[int, str]) -> List[TopologyViolation]:
    """Проверяет набор рёбер как лес направленных радиальных деревьев.
    Возвращает список нарушений (пустой = валидно). Не бросает исключений
    сама — вызывающий код (publish_edges) решает, что с этим делать."""
    violations: List[TopologyViolation] = []

    # 1) самоссылки
    for e in edges:
        if e.from_node_id == e.to_node_id:
            violations.append(TopologyViolation(
                "self_loop",
                f"связь {e.edge_id} ссылается сама на себя (узел {e.from_node_id})",
                [e.from_node_id], [e.edge_id],
            ))

    # 2) максимум один родитель на узел (по to_node_id)
    parents_of: Dict[int, List[EdgeRef]] = {}
    for e in edges:
        if e.from_node_id == e.to_node_id:
            continue  # уже отмечено выше, не даём self-loop замусорить дальнейшие проверки
        parents_of.setdefault(e.to_node_id, []).append(e)

    for node_id, incoming in parents_of.items():
        if len(incoming) > 1:
            violations.append(TopologyViolation(
                "duplicate_parent",
                f"узел {node_id} получает более одной действующей питающей связи "
                f"от узлов {[e.from_node_id for e in incoming]}",
                [node_id] + [e.from_node_id for e in incoming],
                [e.edge_id for e in incoming],
            ))

    # 3) source не бывает приёмником; load не бывает источником
    for e in edges:
        to_kind = node_kind_by_id.get(e.to_node_id)
        from_kind = node_kind_by_id.get(e.from_node_id)
        if to_kind == "source":
            violations.append(TopologyViolation(
                "source_has_incoming",
                f"узел {e.to_node_id} имеет тип source, но получает связь {e.edge_id} "
                f"от узла {e.from_node_id} — source не может быть приёмником",
                [e.to_node_id], [e.edge_id],
            ))
        if from_kind == "load":
            violations.append(TopologyViolation(
                "load_has_outgoing",
                f"узел {e.from_node_id} имеет тип load, но питает связь {e.edge_id} "
                f"к узлу {e.to_node_id} — load не может иметь исходящих связей",
                [e.from_node_id], [e.edge_id],
            ))

    # 4) циклы — только по узлам с ровно одним родителем (дублирующиеся
    # родители уже отмечены выше и не должны маскировать/искажать поиск
    # цикла, поэтому строим parent_of только по чистым from duplicate записям)
    parent_of: Dict[int, int] = {}
    parent_edge_of: Dict[int, int] = {}
    for node_id, incoming in parents_of.items():
        if len(incoming) == 1:
            parent_of[node_id] = incoming[0].from_node_id
            parent_edge_of[node_id] = incoming[0].edge_id

    all_nodes = set(parent_of.keys()) | {e.from_node_id for e in edges} | {e.to_node_id for e in edges}
    color: Dict[int, int] = {}  # 0/отсутствует = не посещён, 1 = в процессе, 2 = завершён
    reported_cycle_nodes = set()

    for start in all_nodes:
        if color.get(start, 0) == 2:
            continue
        path = []
        node = start
        while True:
            c = color.get(node, 0)
            if c == 0:
                color[node] = 1
                path.append(node)
                parent = parent_of.get(node)
                if parent is None:
                    for n in path:
                        color[n] = 2
                    break
                node = parent
            elif c == 1:
                idx = path.index(node)
                cycle_nodes = path[idx:] + [node]
                if not (set(cycle_nodes) & reported_cycle_nodes):
                    cycle_edges = [parent_edge_of[n] for n in cycle_nodes[:-1] if n in parent_edge_of]
                    violations.append(TopologyViolation(
                        "cycle",
                        "цикл питания: " + " -> ".join(str(n) for n in cycle_nodes),
                        cycle_nodes, cycle_edges,
                    ))
                    reported_cycle_nodes.update(cycle_nodes)
                for n in path:
                    color[n] = 2
                break
            else:  # c == 2
                for n in path:
                    color[n] = 2
                break

    return violations


# ---------------------------------------------------------------------
# Репозитории — тонкие обёртки над electrical_nodes/electrical_edges,
# использующие validate_forest перед любой мутацией активного графа.
# ---------------------------------------------------------------------

def validate_code(s, label="код"):
    s = (s or "").strip()
    if not s:
        raise ValueError(f"{label} не может быть пустым")
    if len(s) > 64:
        raise ValueError(f"{label} длиннее 64 символов")
    return s


def validate_name(s):
    s = (s or "").strip()
    if not s:
        raise ValueError("имя не может быть пустым")
    if len(s) > 200:
        raise ValueError("имя длиннее 200 символов")
    return s


def validate_node_kind(kind):
    if kind not in VALID_NODE_KINDS:
        raise ValueError(f"неизвестный тип узла: {kind!r}. Допустимые: {VALID_NODE_KINDS}")


@dataclass
class ElectricalNode:
    id: int
    code: str
    name: str
    kind: str
    location_id: Optional[int]
    archived_at: Optional[int]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"], code=row["code"], name=row["name"], kind=row["kind"],
            location_id=row["location_id"], archived_at=row["archived_at"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


class ElectricalNodeRepo:
    def __init__(self, db):
        self._db = db

    def get_by_id(self, node_id):
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM electrical_nodes WHERE id = ?", (node_id,)
            ).fetchone()
            return ElectricalNode.from_row(row) if row else None

    def get_by_code(self, code):
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM electrical_nodes WHERE code = ?", (code,)
            ).fetchone()
            return ElectricalNode.from_row(row) if row else None

    def list_all(self, include_archived=False):
        with self._db.read() as c:
            q = "SELECT * FROM electrical_nodes"
            if not include_archived:
                q += " WHERE archived_at IS NULL"
            q += " ORDER BY code COLLATE NOCASE"
            rows = c.execute(q).fetchall()
            return [ElectricalNode.from_row(r) for r in rows]

    def add(self, code, name, kind, location_id=None):
        code = validate_code(code)
        name = validate_name(name)
        validate_node_kind(kind)
        now = int(time.time())
        with self._db.transaction() as c:
            if location_id is not None and c.execute(
                "SELECT 1 FROM locations WHERE id = ?", (location_id,)
            ).fetchone() is None:
                raise ValueError(f"Место {location_id} не найдено")
            try:
                cur = c.execute(
                    "INSERT INTO electrical_nodes "
                    "(code, name, kind, location_id, archived_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (code, name, kind, location_id, now, now),
                )
            except Exception as e:
                if "code" in str(e).lower() or "unique" in str(e).lower():
                    raise ValueError(f"Узел с кодом {code!r} уже существует") from None
                raise
            node_id = cur.lastrowid

            # docs/migration-plan-v2.md §7 п.2: первая предметная запись
            # через доменную модель v2 отмечается В ТОЙ ЖЕ транзакции —
            # см. domain_generation.py. Идемпотентно (не переставляет
            # маркер на повторных вызовах).
            domain_generation.mark_v2_domain_write(c)
        return self.get_by_id(node_id)

    def update_fields(self, node_id, *, name=None, location_id=_UNSET_NODE):
        """Переименовать узел / сменить место (партия 6, «План v3», §2:
        карточка узла редактируется на месте — «где стоит» приходит сюда
        уже разрешённым в location_id, поиск/заведение места по
        свободному тексту делает вызывающий код api_v2.py, не репозиторий).
        location_id=_UNSET_NODE (по умолчанию) значит «не менять»;
        location_id=None — явно снять место (в отличие от «не менять» —
        нужен отдельный маркер, как и у installation_location_id точек)."""
        if name is None and location_id is _UNSET_NODE:
            return self.get_by_id(node_id)
        if self.get_by_id(node_id) is None:
            raise ValueError(f"Узел {node_id} не найден")
        if name is not None:
            name = validate_name(name)
        now = int(time.time())
        set_parts = ["updated_at = ?"]
        params = [now]
        if name is not None:
            set_parts.append("name = ?")
            params.append(name)
        if location_id is not _UNSET_NODE:
            set_parts.append("location_id = ?")
            params.append(location_id)
        params.append(node_id)
        with self._db.transaction() as c:
            if location_id is not _UNSET_NODE and location_id is not None and c.execute(
                "SELECT 1 FROM locations WHERE id = ?", (location_id,)
            ).fetchone() is None:
                raise ValueError(f"Место {location_id} не найдено")
            c.execute(
                f"UPDATE electrical_nodes SET {', '.join(set_parts)} WHERE id = ?",
                params)
        return self.get_by_id(node_id)

    def archive(self, node_id, at=None):
        now = at or int(time.time())
        with self._db.transaction() as c:
            row = c.execute(
                "SELECT 1 FROM electrical_nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Узел {node_id} не найден")
            active_edge = c.execute(
                "SELECT 1 FROM electrical_edges WHERE (from_node_id = ? OR to_node_id = ?) "
                "AND state = 'published' AND valid_to IS NULL",
                (node_id, node_id)
            ).fetchone()
            if active_edge:
                raise ValueError(
                    "Нельзя архивировать узел с действующими опубликованными связями"
                )
            c.execute(
                "UPDATE electrical_nodes SET archived_at = ?, updated_at = ? WHERE id = ?",
                (now, now, node_id)
            )
        return self.get_by_id(node_id)


@dataclass
class ElectricalEdge:
    id: int
    code: Optional[str]
    name: Optional[str]
    from_node_id: int
    to_node_id: int
    primary_point_id: Optional[int]
    phase_count: Optional[int]
    rated_current_a: Optional[float]
    cable_note: Optional[str]
    state: str
    valid_from: Optional[int]
    valid_to: Optional[int]
    archived_at: Optional[int]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"], code=row["code"], name=row["name"],
            from_node_id=row["from_node_id"], to_node_id=row["to_node_id"],
            primary_point_id=row["primary_point_id"], phase_count=row["phase_count"],
            rated_current_a=row["rated_current_a"], cable_note=row["cable_note"],
            state=row["state"], valid_from=row["valid_from"], valid_to=row["valid_to"],
            archived_at=row["archived_at"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class ElectricalEdgeRepo:
    def __init__(self, db):
        self._db = db

    def get_by_id(self, edge_id):
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM electrical_edges WHERE id = ?", (edge_id,)
            ).fetchone()
            return ElectricalEdge.from_row(row) if row else None

    def list_active_published(self):
        """Все опубликованные и сейчас действующие связи (valid_to IS NULL) —
        это и есть текущий активный граф сети, используемый достижимостью
        и балансом (§5.1)."""
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM electrical_edges WHERE state = 'published' AND valid_to IS NULL"
            ).fetchall()
            return [ElectricalEdge.from_row(r) for r in rows]

    def list_drafts(self):
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM electrical_edges WHERE state = 'draft' AND archived_at IS NULL"
            ).fetchall()
            return [ElectricalEdge.from_row(r) for r in rows]

    def add_draft(self, from_node_id, to_node_id, code=None, name=None,
                  primary_point_id=None, phase_count=None, rated_current_a=None,
                  cable_note=None):
        """Черновик: НЕ входит в автоматический баланс/достижимость, пока
        не опубликован через publish_edges. Базовая проверка (существование
        узлов, самоссылка) — сразу; полная проверка леса — при публикации."""
        if from_node_id == to_node_id:
            raise ValueError("связь не может соединять узел сам с собой")
        if rated_current_a is not None and rated_current_a <= 0:
            raise ValueError("номинальный ток должен быть положительным")
        now = int(time.time())
        with self._db.transaction() as c:
            for node_id in (from_node_id, to_node_id):
                if c.execute(
                    "SELECT 1 FROM electrical_nodes WHERE id = ?", (node_id,)
                ).fetchone() is None:
                    raise ValueError(f"Узел {node_id} не найден")
            cur = c.execute(
                "INSERT INTO electrical_edges "
                "(code, name, from_node_id, to_node_id, primary_point_id, phase_count, "
                "rated_current_a, cable_note, state, valid_from, valid_to, revision_id, "
                "archived_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', NULL, NULL, NULL, NULL, ?, ?)",
                (code, name, from_node_id, to_node_id, primary_point_id, phase_count,
                 rated_current_a, cable_note, now, now),
            )
            edge_id = cur.lastrowid

            # docs/migration-plan-v2.md §7 п.2: первая предметная запись
            # через доменную модель v2 отмечается В ТОЙ ЖЕ транзакции —
            # см. domain_generation.py. Идемпотентно (не переставляет
            # маркер на повторных вызовах).
            domain_generation.mark_v2_domain_write(c)
        return self.get_by_id(edge_id)

    def _compute_candidate(self, c, edge_ids):
        """Общая часть validate_edges/publish_edges: загружает предлагаемые
        draft-рёбра, текущий активный граф, вычисляет итоговый кандидат
        (текущие связи минус вытесняемые тем же to_node_id, плюс новые) и
        прогоняет validate_forest. Возвращает (violations, new_rows,
        superseded, kept) — вызывающий код решает, применять изменения или
        только сообщить о результате."""
        new_rows = []
        for eid in edge_ids:
            row = c.execute(
                "SELECT * FROM electrical_edges WHERE id = ?", (eid,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Связь {eid} не найдена")
            if row["state"] != "draft":
                raise ValueError(f"Связь {eid} не является черновиком (state={row['state']!r})")
            new_rows.append(row)

        current_rows = c.execute(
            "SELECT * FROM electrical_edges WHERE state = 'published' AND valid_to IS NULL"
        ).fetchall()

        new_to_nodes = {row["to_node_id"] for row in new_rows}
        superseded = [row for row in current_rows if row["to_node_id"] in new_to_nodes]
        kept = [row for row in current_rows if row["to_node_id"] not in new_to_nodes]

        candidate_rows = kept + new_rows
        edge_refs = [
            EdgeRef(row["id"], row["from_node_id"], row["to_node_id"])
            for row in candidate_rows
        ]
        node_ids = {row["from_node_id"] for row in candidate_rows} | \
                   {row["to_node_id"] for row in candidate_rows}
        node_kind_by_id = {}
        for node_id in node_ids:
            nrow = c.execute(
                "SELECT kind FROM electrical_nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if nrow is not None:
                node_kind_by_id[node_id] = nrow["kind"]

        violations = validate_forest(edge_refs, node_kind_by_id)
        return violations, new_rows, superseded, kept

    def validate_edges(self, edge_ids: List[int]):
        """Сухая проверка (POST /api/v2/topology/validate, §9.2): те же
        правила, что и publish_edges, но НИЧЕГО не меняет — только читает.
        Возвращает список TopologyViolation (пустой = можно публиковать)."""
        with self._db.read() as c:
            violations, _new_rows, _superseded, _kept = self._compute_candidate(c, edge_ids)
        return violations

    def publish_edges(self, edge_ids: List[int], at: Optional[int] = None):
        """Публикует набор draft-рёбер атомарно (A24): вычисляет
        результирующий активный граф (текущие опубликованные связи минус
        те, что будут вытеснены тем же to_node_id, плюс предлагаемые),
        валидирует его целиком через validate_forest, и только если
        валиден — закрывает вытесняемые старые связи и активирует новые
        в одной транзакции. При конфликте НИЧЕГО не меняется и бросается
        TopologyConflict с точной цепочкой/узлами."""
        now = int(time.time())
        at = at if at is not None else now

        with self._db.transaction() as c:
            violations, new_rows, superseded, kept = self._compute_candidate(c, edge_ids)

            if violations:
                raise TopologyConflict(
                    "; ".join(v.message for v in violations)
                )

            # Валидно — применяем: закрываем вытесняемые, активируем новые.
            for row in superseded:
                c.execute(
                    "UPDATE electrical_edges SET valid_to = ?, updated_at = ? WHERE id = ?",
                    (at, now, row["id"])
                )
            for eid in edge_ids:
                c.execute(
                    "UPDATE electrical_edges SET state = 'published', valid_from = ?, "
                    "valid_to = NULL, updated_at = ? WHERE id = ?",
                    (at, now, eid)
                )

        log.info(
            "Опубликованы связи %s (вытеснено %d предыдущих)",
            edge_ids, len(superseded)
        )
        return [self.get_by_id(eid) for eid in edge_ids]

    def set_primary_point(self, edge_id, point_id, at=None):
        """Назначить/снять измеряющую точку у СУЩЕСТВУЮЩЕЙ связи (партия
        6, «План v3», §1: «действие поставить сюда счётчик спрашивает,
        на какую линию»). До этой партии primary_point_id задавался
        только один раз, при создании черновика (add_draft) — изменить
        его у уже существующей связи (в том числе опубликованной) было
        нечем. point_id=None снимает измерение с линии (линия без
        счётчика допустима, §1 большого ТЗ).

        Уникальность «одна точка — максимум одна ДЕЙСТВУЮЩАЯ связь»
        (idx_electrical_edges_one_point, только для state='published' И
        valid_to IS NULL) уже гарантирована схемой — здесь она только
        транслируется в понятный ValueError вместо голого
        sqlite3.IntegrityError."""
        now = at or int(time.time())
        with self._db.transaction() as c:
            row = c.execute(
                "SELECT * FROM electrical_edges WHERE id = ?", (edge_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Связь {edge_id} не найдена")
            try:
                c.execute(
                    "UPDATE electrical_edges SET primary_point_id = ?, "
                    "updated_at = ? WHERE id = ?",
                    (point_id, now, edge_id)
                )
            except Exception as e:
                if "unique" in str(e).lower():
                    raise ValueError(
                        "Эта точка уже измеряет другую действующую линию — "
                        "сначала снимите её оттуда"
                    ) from None
                raise
        return self.get_by_id(edge_id)

    def retire_edge(self, edge_id, at=None):
        """Закрыть опубликованную связь без замены (узел временно теряет
        питание/измерение, не входит в достижимость дальше)."""
        now = at if at is not None else int(time.time())
        with self._db.transaction() as c:
            row = c.execute(
                "SELECT * FROM electrical_edges WHERE id = ?", (edge_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Связь {edge_id} не найдена")
            if row["state"] != "published" or row["valid_to"] is not None:
                raise ValueError(f"Связь {edge_id} не является действующей опубликованной")
            c.execute(
                "UPDATE electrical_edges SET valid_to = ?, updated_at = ? WHERE id = ?",
                (now, now, edge_id)
            )
        return self.get_by_id(edge_id)


# ---------------------------------------------------------------------
# Достижимость — используется расчётным контрактом (§5.1) для определения
# "потомок ли B по отношению к вводу A" при проверке пересечения sum().
# ---------------------------------------------------------------------

def build_children_map(edges: List[ElectricalEdge]) -> Dict[int, List[int]]:
    """from_node_id -> [to_node_id, ...] по активному (опубликованному,
    открытому) графу."""
    children: Dict[int, List[int]] = {}
    for e in edges:
        children.setdefault(e.from_node_id, []).append(e.to_node_id)
    return children


def is_descendant(children_map: Dict[int, List[int]], ancestor_node_id: int,
                   candidate_node_id: int, max_depth: int = 10000) -> bool:
    """True, если candidate_node_id достижим от ancestor_node_id по
    активному графу (т.е. ancestor "выше по дереву"). Используется для
    A04: отклонить sum(A, B), если B — потомок A."""
    if ancestor_node_id == candidate_node_id:
        return False
    stack = [ancestor_node_id]
    seen = set()
    depth = 0
    while stack and depth <= max_depth:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for child in children_map.get(node, []):
            if child == candidate_node_id:
                return True
            stack.append(child)
        depth += 1
    return False
