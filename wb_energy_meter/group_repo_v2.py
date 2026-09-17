"""Репозиторий учётных групп v2 (ТЗ §4.4) — версионируемая иерархия
через `group_parent_bindings` и версионируемый состав (точек) через
`group_memberships`, поверх уже существующей таблицы `meter_groups`
(решение ТЗ §2: не заводить вторую сущность, использовать meter_groups +
новую колонку `category`, а не отдельную v2-таблицу групп).

Пишется лично (не делегировано Haiku) — по аналогии с location_repo.py/
binding_service.py: версионируемая иерархия с защитой от циклов и
дедупликация состава группы напрямую влияют на то, что покажет
"Обзор"/отчёты, поэтому это "самая ответственная часть задачи".

Ключевые решения из ТЗ §4.4, реализованные здесь:

  - "Разрешить одну точку в нескольких группах" — в отличие от
    point_bindings (role=primary эксклюзивна для точки), здесь НЕТ
    эксклюзивности точки: одна и та же точка может быть открытым членом
    произвольного числа РАЗНЫХ групп одновременно. Запрещено только
    дублирующее открытое членство в ОДНОЙ и той же группе — это
    гарантирует уникальный индекс idx_group_memberships_open.
  - "Группы могут иметь одного родителя внутри категории" — у группы не
    больше одного родителя (как и у Location), и связь родитель-потомок
    допускается только когда категории совпадают либо хотя бы одна из
    них ещё не назначена (category IS NULL трактуется как "категория
    ещё не решена", а не как отдельная четвёртая категория, которую
    можно свободно мешать с другими).
  - "Циклы запрещены" — та же защита, что и в LocationRepo
    (_would_create_cycle), но по дереву meter_groups.parent_id.
  - "Состав родителя — множество непосредственно добавленных точек и
    точек его дочерних групп... одинаковую точку, встретившуюся
    несколькими путями, учитывать один раз и раскрывать происхождение
    включения" — resolve_effective_members() обходит поддерево группы,
    собирает прямые членства каждой группы поддерева, действовавшие на
    момент `at`, и дедуплицирует по point_id, сохраняя список групп-
    источников (via) для каждой точки. Это финансово значимая логика
    (защита от двойного счёта при раскрытии дерева групп на "Обзоре"/в
    отчётах) — реализована и протестирована здесь, а не в UI.

`meter_groups.parent_id` — денормализованный КЭШ (см. заголовок
migrations/005_v2_domain_schema.sql: "пишут только сервисы... ОДНОВРЕМЕННО
с соответствующей строкой версии... в одной транзакции"), в точности как
у LocationRepo/locations.parent_id. Этот репозиторий — единственный код,
которому разрешено его писать; читается он только этим репозиторием для
быстрого обхода дерева (path_to_root/_would_create_cycle), наружу отдаётся
через GroupV2.parent_id как read-only проекция.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import domain_generation
from .repo import GROUP_COLOR_PALETTE

log = logging.getLogger(__name__)

MAX_DEPTH = 32
NAME_MAX_LEN = 200


def validate_name(s):
    """Валидирует имя группы: не пусто, не слишком длинное."""
    s = (s or "").strip()
    if not s:
        raise ValueError("имя группы не может быть пустым")
    if len(s) > NAME_MAX_LEN:
        raise ValueError(f"имя длиннее {NAME_MAX_LEN}")
    return s


@dataclass
class GroupV2:
    """Учётная группа v2 — проекция над meter_groups + категория +
    актуальный parent_id (кэш, синхронизированный с group_parent_bindings)."""
    id: int
    name: str
    category: Optional[str]
    parent_id: Optional[int]
    color: Optional[str]
    created_at: int


@dataclass
class GroupMembership:
    id: int
    group_id: int
    point_id: int
    valid_from: int
    valid_to: Optional[int]
    created_at: int

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            group_id=row["group_id"],
            point_id=row["point_id"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            created_at=row["created_at"],
        )


class GroupRepoV2:
    """Репозиторий для работы с учётными группами v2 и их иерархией/составом."""

    def __init__(self, db):
        self._db = db

    # -- чтение группы ----------------------------------------------------

    @staticmethod
    def _group_from_row(row):
        return GroupV2(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            parent_id=row["parent_id"],
            color=row["color"],
            created_at=row["created_at"],
        )

    def get_by_id(self, group_id):
        """Получить группу по ID."""
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM meter_groups WHERE id = ?", (group_id,)
            ).fetchone()
            return self._group_from_row(row) if row else None

    def list_all(self):
        """Список всех групп, отсортировано по имени."""
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM meter_groups ORDER BY name COLLATE NOCASE"
            ).fetchall()
            return [self._group_from_row(r) for r in rows]

    def list_children(self, parent_id):
        """Прямые дочерние группы для parent_id (None — корневые).

        Построено поверх list_all()/GroupV2.parent_id, а не отдельным
        SQL-запросом к group_parent_bindings — так группы, никогда не
        проходившие через этот сервис (созданные легаси GroupRepo.create(),
        у которых нет ни одной строки в group_parent_bindings), корректно
        считаются корневыми наравне с группами, у которых есть открытая
        запись с parent_id IS NULL: и то, и другое читается как
        meter_groups.parent_id IS NULL."""
        return [g for g in self.list_all() if g.parent_id == parent_id]

    # -- создание -----------------------------------------------------------

    def _check_category_scope(self, child_category, parent):
        """ТЗ §4.4: "группы могут иметь одного родителя внутри категории".

        category IS NULL трактуется как "ещё не решено" и не блокирует
        связь ни в одну сторону — проверка срабатывает только когда ОБЕ
        категории заданы и различаются."""
        if parent is None:
            return
        if child_category is not None and parent.category is not None \
                and child_category != parent.category:
            raise ValueError(
                f"Группа-родитель id={parent.id} имеет категорию "
                f"{parent.category!r}, нельзя назначить её родителем для "
                f"группы категории {child_category!r} (ТЗ §4.4: родитель — "
                f"только внутри категории)"
            )

    def add(self, name, category=None, parent_id=None, color=None):
        """Добавить новую группу.

        Валидирует имя, проверяет существование и категорию родителя (если
        задан), создаёт первую запись в group_parent_bindings и кэш
        meter_groups.parent_id в одной транзакции. При конфликте имени
        бросает ValueError (как и легаси GroupRepo.create())."""
        name = validate_name(name)

        parent = None
        if parent_id is not None:
            parent = self.get_by_id(parent_id)
            if parent is None:
                raise ValueError(f"Родительская группа {parent_id} не найдена")
            self._check_category_scope(category, parent)

        now = int(time.time())

        try:
            with self._db.transaction() as c:
                color_value = color
                if not color_value:
                    row = c.execute(
                        "SELECT COUNT(*) AS n FROM meter_groups"
                    ).fetchone()
                    color_value = GROUP_COLOR_PALETTE[
                        int(row["n"]) % len(GROUP_COLOR_PALETTE)
                    ]

                cur = c.execute(
                    "INSERT INTO meter_groups "
                    "(name, name_norm, parent_id, category, color, created_at) "
                    "VALUES (?, py_casefold(?), ?, ?, ?, ?)",
                    (name, name, parent_id, category, color_value, now),
                )
                group_id = cur.lastrowid

                c.execute(
                    "INSERT INTO group_parent_bindings "
                    "(group_id, parent_id, valid_from, valid_to, revision_id, created_at) "
                    "VALUES (?, ?, ?, NULL, NULL, ?)",
                    (group_id, parent_id, now, now),
                )

                # docs/migration-plan-v2.md §7 п.2: первая предметная запись
                # через доменную модель v2 отмечается В ТОЙ ЖЕ транзакции.
                domain_generation.mark_v2_domain_write(c)
        except sqlite3.IntegrityError as e:
            if "name" in str(e).lower():
                raise ValueError(f"Группа с именем {name!r} уже существует") from None
            raise

        return self.get_by_id(group_id)

    # -- иерархия -----------------------------------------------------------

    def _would_create_cycle(self, c, group_id, new_parent_id):
        """Проверяет, создаст ли назначение new_parent_id цикл.

        Использует уже открытый курсор c (вызывается из существующей
        транзакции), поднимаясь по кэшу meter_groups.parent_id."""
        if new_parent_id == group_id:
            return True
        if new_parent_id is None:
            return False

        current_id = new_parent_id
        depth = 0
        while current_id is not None:
            if depth >= MAX_DEPTH:
                raise ValueError(
                    "Превышена максимальная глубина дерева групп (32)"
                )
            if current_id == group_id:
                return True
            row = c.execute(
                "SELECT parent_id FROM meter_groups WHERE id = ?", (current_id,)
            ).fetchone()
            if row is None:
                break
            current_id = row["parent_id"]
            depth += 1
        return False

    def set_parent(self, group_id, new_parent_id, at=None):
        """Изменить родителя группы в одной транзакции.

        Проверяет существование групп, совпадение категорий (ТЗ §4.4),
        циклические связи, обновляет историю в group_parent_bindings и
        кэш в meter_groups.parent_id."""
        group = self.get_by_id(group_id)
        if group is None:
            raise ValueError(f"Группа {group_id} не найдена")

        new_parent = None
        if new_parent_id is not None:
            new_parent = self.get_by_id(new_parent_id)
            if new_parent is None:
                raise ValueError(f"Родительская группа {new_parent_id} не найдена")
            self._check_category_scope(group.category, new_parent)

        now = at or int(time.time())

        with self._db.transaction() as c:
            if self._would_create_cycle(c, group_id, new_parent_id):
                raise ValueError("Перенос создал бы цикл в дереве групп")

            c.execute(
                "UPDATE group_parent_bindings "
                "SET valid_to = ? "
                "WHERE group_id = ? AND valid_to IS NULL",
                (now, group_id),
            )
            c.execute(
                "INSERT INTO group_parent_bindings "
                "(group_id, parent_id, valid_from, valid_to, revision_id, created_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?)",
                (group_id, new_parent_id, now, now),
            )
            c.execute(
                "UPDATE meter_groups SET parent_id = ? WHERE id = ?",
                (new_parent_id, group_id),
            )

        return self.get_by_id(group_id)

    def path_to_root(self, group_id):
        """Путь от корня к группе включительно (использует кэш parent_id)."""
        with self._db.read() as c:
            path = []
            current_id = group_id
            depth = 0
            while current_id is not None:
                if depth >= MAX_DEPTH:
                    raise ValueError(
                        "Превышена максимальная глубина дерева групп (32) "
                        "при обходе path_to_root"
                    )
                row = c.execute(
                    "SELECT * FROM meter_groups WHERE id = ?", (current_id,)
                ).fetchone()
                if row is None:
                    break
                path.append(self._group_from_row(row))
                current_id = row["parent_id"]
                depth += 1
            path.reverse()
            return path

    # -- состав (членство точек) --------------------------------------------

    def get_membership(self, membership_id):
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM group_memberships WHERE id = ?", (membership_id,)
            ).fetchone()
            return GroupMembership.from_row(row) if row else None

    def list_members(self, group_id, include_closed=False):
        """Прямые членства данной группы (без раскрытия дочерних групп —
        см. resolve_effective_members для рекурсивного состава)."""
        with self._db.read() as c:
            if include_closed:
                rows = c.execute(
                    "SELECT * FROM group_memberships WHERE group_id = ? "
                    "ORDER BY valid_from, id", (group_id,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM group_memberships WHERE group_id = ? "
                    "AND valid_to IS NULL ORDER BY valid_from, id", (group_id,)
                ).fetchall()
            return [GroupMembership.from_row(r) for r in rows]

    def list_groups_for_point(self, point_id, include_closed=False):
        """Все группы, в которых прямо состоит точка (ТЗ §4.4: точка может
        состоять в нескольких группах одновременно)."""
        with self._db.read() as c:
            if include_closed:
                rows = c.execute(
                    "SELECT * FROM group_memberships WHERE point_id = ? "
                    "ORDER BY valid_from, id", (point_id,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM group_memberships WHERE point_id = ? "
                    "AND valid_to IS NULL ORDER BY valid_from, id", (point_id,)
                ).fetchall()
            return [GroupMembership.from_row(r) for r in rows]

    def add_member(self, group_id, point_id, valid_from=None):
        """Добавить точку в группу (открыть версионированное членство).

        В отличие от point_bindings/role=primary здесь нет эксклюзивности
        точки — можно быть открытым членом произвольного числа разных
        групп одновременно (ТЗ §4.4). Запрещено только повторное открытое
        членство в ТОЙ ЖЕ группе (уникальный индекс на БД)."""
        now = int(time.time())
        vf = valid_from if valid_from is not None else now

        with self._db.transaction() as c:
            if c.execute(
                "SELECT 1 FROM meter_groups WHERE id = ?", (group_id,)
            ).fetchone() is None:
                raise ValueError(f"Группа {group_id} не найдена")
            if c.execute(
                "SELECT 1 FROM metering_points WHERE id = ?", (point_id,)
            ).fetchone() is None:
                raise ValueError(f"Точка {point_id} не найдена")

            try:
                cur = c.execute(
                    "INSERT INTO group_memberships "
                    "(group_id, point_id, valid_from, valid_to, created_at) "
                    "VALUES (?, ?, ?, NULL, ?)",
                    (group_id, point_id, vf, now),
                )
            except sqlite3.IntegrityError:
                raise ValueError(
                    f"Точка {point_id} уже состоит в группе {group_id} "
                    "(открытое членство)"
                ) from None
            membership_id = cur.lastrowid

            domain_generation.mark_v2_domain_write(c)

        log.info("Точка %d добавлена в группу %d (членство id=%d)",
                  point_id, group_id, membership_id)
        return self.get_membership(membership_id)

    def remove_member(self, group_id, point_id, at=None):
        """Исключить точку из группы (закрыть открытое членство)."""
        now = at if at is not None else int(time.time())
        with self._db.transaction() as c:
            row = c.execute(
                "SELECT * FROM group_memberships "
                "WHERE group_id = ? AND point_id = ? AND valid_to IS NULL",
                (group_id, point_id),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"Точка {point_id} не состоит в группе {group_id} "
                    "(нет открытого членства)"
                )
            if now <= row["valid_from"]:
                raise ValueError(
                    "Момент исключения должен быть позже начала членства"
                )
            c.execute(
                "UPDATE group_memberships SET valid_to = ? WHERE id = ?",
                (now, row["id"]),
            )
        log.info("Точка %d исключена из группы %d", point_id, group_id)

    # -- рекурсивный состав с дедупликацией ---------------------------------

    def _subtree_ids(self, c, group_id, at):
        """BFS по group_parent_bindings, действовавшим на момент at, вниз
        от group_id. Возвращает список ID группы и всех её потомков
        (group_id включён первым)."""
        subtree_ids = [group_id]
        frontier = [group_id]
        depth = 0
        while frontier:
            if depth >= MAX_DEPTH:
                raise ValueError(
                    "Превышена максимальная глубина дерева групп (32) "
                    "при обходе состава"
                )
            placeholders = ",".join("?" * len(frontier))
            rows = c.execute(
                f"SELECT DISTINCT group_id FROM group_parent_bindings "
                f"WHERE parent_id IN ({placeholders}) "
                f"AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
                (*frontier, at, at),
            ).fetchall()
            frontier = [r["group_id"] for r in rows if r["group_id"] not in subtree_ids]
            subtree_ids.extend(frontier)
            depth += 1
        return subtree_ids

    def resolve_effective_members(self, group_id, at=None):
        """Эффективный состав группы (ТЗ §4.4): точки, прямо добавленные в
        саму группу, плюс точки всех её дочерних групп рекурсивно, на
        момент `at` (по умолчанию сейчас). Одинаковая точка, попавшая
        несколькими путями (несколько дочерних групп содержат её, или она
        же прямо добавлена в саму группу И в её потомка), учитывается
        ОДИН раз — но список групп-источников (via) сохраняется, чтобы
        вызывающий код мог раскрыть происхождение включения, а не молча
        просуммировать точку дважды.

        Возвращает список {"point_id": int, "via": [group_id, ...]},
        отсортированный по point_id; via отсортирован по возрастанию id."""
        if self.get_by_id(group_id) is None:
            raise ValueError(f"Группа {group_id} не найдена")
        at = at if at is not None else int(time.time())

        with self._db.read() as c:
            subtree_ids = self._subtree_ids(c, group_id, at)
            placeholders = ",".join("?" * len(subtree_ids))
            rows = c.execute(
                f"SELECT group_id, point_id FROM group_memberships "
                f"WHERE group_id IN ({placeholders}) "
                f"AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
                (*subtree_ids, at, at),
            ).fetchall()

        by_point: Dict[int, List[int]] = {}
        for row in rows:
            by_point.setdefault(row["point_id"], []).append(row["group_id"])

        return [
            {"point_id": pid, "via": sorted(via)}
            for pid, via in sorted(by_point.items())
        ]
