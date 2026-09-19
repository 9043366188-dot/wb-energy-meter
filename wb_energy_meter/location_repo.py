"""Репозиторий мест учёта — объектов, зданий, этажей, помещений и т.д.
Управляет иерархией мест, историей смены родителей через location_parent_bindings
и защитой от циклических связей."""

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from . import domain_generation

log = logging.getLogger(__name__)

VALID_KINDS = ("object", "building", "floor", "room", "zone", "installation_point")
MAX_DEPTH = 32
NAME_MAX_LEN = 200


def validate_kind(s):
    """Проверяет, что kind — один из допустимых типов мест."""
    if s not in VALID_KINDS:
        raise ValueError(f"Неизвестный тип места: {s!r}. Допустимые: {VALID_KINDS}")


def validate_name(s):
    """Валидирует имя места: не пусто, не слишком длинное."""
    s = (s or "").strip()
    if not s:
        raise ValueError("имя не может быть пустым")
    if len(s) > NAME_MAX_LEN:
        raise ValueError(f"имя длиннее {NAME_MAX_LEN}")
    return s


@dataclass
class Location:
    """Место в иерархии объектов."""
    id: int
    parent_id: Optional[int]
    kind: str
    name: str
    code: Optional[str]
    sort_order: int
    archived_at: Optional[int]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row):
        """Создаёт Location из sqlite3.Row."""
        return cls(
            id=row["id"],
            parent_id=row["parent_id"],
            kind=row["kind"],
            name=row["name"],
            code=row["code"],
            sort_order=row["sort_order"],
            archived_at=row["archived_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class LocationRepo:
    """Репозиторий для работы с местами учёта и их иерархией."""

    def __init__(self, db):
        self._db = db

    def get_by_id(self, loc_id):
        """Получить место по ID."""
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM locations WHERE id = ?", (loc_id,)
            ).fetchone()
            return Location.from_row(row) if row else None

    def get_by_code(self, code):
        """Получить место по коду."""
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM locations WHERE code = ?", (code,)
            ).fetchone()
            return Location.from_row(row) if row else None

    def get_by_name(self, name):
        """Найти место по точному совпадению имени без учёта регистра
        (казефолд-нормализация, см. грабли AGENTS.md про COLLATE NOCASE
        и кириллицу) — партия 6, «План v3», §2: свободный текст «где
        стоит узла» под капотом ищет/заводит место по имени, без
        отдельного поля на electrical_nodes. Архивные места не
        возвращает — вводить текст, совпадающий с архивным местом,
        должно заводить новое, а не молча оживлять старое."""
        name = (name or "").strip()
        if not name:
            return None
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM locations WHERE name_norm = py_casefold(?) "
                "AND archived_at IS NULL LIMIT 1", (name,)
            ).fetchone()
            return Location.from_row(row) if row else None

    def list_all(self, include_archived=False):
        """Список всех мест, отсортировано по sort_order и имени.

        Если include_archived=False, исключает архивированные места."""
        with self._db.read() as c:
            query = "SELECT * FROM locations"
            params = []
            if not include_archived:
                query += " WHERE archived_at IS NULL"
            query += " ORDER BY sort_order, name COLLATE NOCASE"
            rows = c.execute(query, params).fetchall()
            return [Location.from_row(row) for row in rows]

    def list_children(self, parent_id):
        """Список прямых дочерних мест для данного parent_id.

        Если parent_id is None — возвращает корневые места."""
        with self._db.read() as c:
            if parent_id is None:
                rows = c.execute(
                    "SELECT * FROM locations WHERE parent_id IS NULL "
                    "ORDER BY sort_order, name COLLATE NOCASE"
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM locations WHERE parent_id = ? "
                    "ORDER BY sort_order, name COLLATE NOCASE",
                    (parent_id,),
                ).fetchall()
            return [Location.from_row(row) for row in rows]

    def add(self, name, kind, parent_id=None, code=None, sort_order=0):
        """Добавить новое место.

        Валидирует имя и тип, проверяет существование родителя (если задан),
        создаёт первую запись в location_parent_bindings в одной транзакции.
        При конфликте кода бросает ValueError."""
        name = validate_name(name)
        validate_kind(kind)

        if parent_id is not None:
            if self.get_by_id(parent_id) is None:
                raise ValueError(f"Родительское место {parent_id} не найдено")

        now = int(time.time())

        try:
            with self._db.transaction() as c:
                # INSERT в locations; name_norm через py_casefold в SQL
                cur = c.execute(
                    "INSERT INTO locations "
                    "(parent_id, kind, name, name_norm, code, sort_order, created_at, updated_at) "
                    "VALUES (?, ?, ?, py_casefold(?), ?, ?, ?, ?)",
                    (parent_id, kind, name, name, code, sort_order, now, now),
                )
                loc_id = cur.lastrowid

                # INSERT в location_parent_bindings
                c.execute(
                    "INSERT INTO location_parent_bindings "
                    "(location_id, parent_id, valid_from, valid_to, revision_id, created_at) "
                    "VALUES (?, ?, ?, NULL, NULL, ?)",
                    (loc_id, parent_id, now, now),
                )

                # docs/migration-plan-v2.md §7 п.2: первая предметная запись
                # через доменную модель v2 отмечается В ТОЙ ЖЕ транзакции —
                # см. domain_generation.py. Идемпотентно (не переставляет
                # маркер на повторных вызовах).
                domain_generation.mark_v2_domain_write(c)
        except sqlite3.IntegrityError as e:
            if "code" in str(e).lower():
                raise ValueError(f"Место с кодом {code!r} уже существует") from None
            raise

        return self.get_by_id(loc_id)

    def _would_create_cycle(self, c, loc_id, new_parent_id):
        """Проверяет, создаст ли назначение new_parent_id циклические связи.

        Вспомогательный метод, использует уже открытый курсор c
        (вызывается из существующей транзакции). Возвращает True если есть цикл."""
        if new_parent_id == loc_id:
            return True

        if new_parent_id is None:
            return False

        # Поднимаемся вверх от new_parent_id по кэшу, проверяя не встретим ли loc_id
        current_id = new_parent_id
        depth = 0

        while current_id is not None:
            if depth >= MAX_DEPTH:
                raise ValueError(
                    "Превышена максимальная глубина дерева мест (32)"
                )

            if current_id == loc_id:
                return True

            # Получаем родителя из кэша locations.parent_id
            row = c.execute(
                "SELECT parent_id FROM locations WHERE id = ?", (current_id,)
            ).fetchone()
            if row is None:
                break
            current_id = row["parent_id"]
            depth += 1

        return False

    def set_parent(self, loc_id, new_parent_id, at=None):
        """Изменить родителя места в одной транзакции.

        Проверяет существование мест, циклические связи, обновляет историю
        в location_parent_bindings и кэш в locations.parent_id."""
        loc = self.get_by_id(loc_id)
        if loc is None:
            raise ValueError(f"Место {loc_id} не найдено")

        if new_parent_id is not None:
            if self.get_by_id(new_parent_id) is None:
                raise ValueError(f"Родительское место {new_parent_id} не найдено")

        now = at or int(time.time())

        with self._db.transaction() as c:
            # Проверка цикла внутри транзакции, используя переданный курсор
            if self._would_create_cycle(c, loc_id, new_parent_id):
                raise ValueError("Перенос создал бы цикл в дереве мест")

            # Закрыть текущую открытую запись в location_parent_bindings
            c.execute(
                "UPDATE location_parent_bindings "
                "SET valid_to = ? "
                "WHERE location_id = ? AND valid_to IS NULL",
                (now, loc_id),
            )

            # Добавить новую открытую запись
            c.execute(
                "INSERT INTO location_parent_bindings "
                "(location_id, parent_id, valid_from, valid_to, revision_id, created_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?)",
                (loc_id, new_parent_id, now, now),
            )

            # Обновить кэш в locations
            c.execute(
                "UPDATE locations SET parent_id = ?, updated_at = ? WHERE id = ?",
                (new_parent_id, now, loc_id),
            )

        return self.get_by_id(loc_id)

    def update_fields(self, loc_id, *, name=None, code=None):
        """Переименовать место / сменить код (партия 6, задача 3, §4:
        "инлайн-редактирование без ухода с экрана") — раньше у мест не
        было ручки для этого вообще (только parent_id/archived через
        PATCH), name/code задавались один раз при создании (add()).
        name=None/code=None здесь значит "не менять" (в отличие от
        installation_location_id у точек, у места нет отдельного "поля
        со значащим None" — обнулять имя или код в простом режиме
        незачем)."""
        if name is None and code is None:
            return self.get_by_id(loc_id)

        if self.get_by_id(loc_id) is None:
            raise ValueError(f"Место {loc_id} не найдено")

        if name is not None:
            name = validate_name(name)

        now = int(time.time())
        set_parts = ["updated_at = ?"]
        params = [now]
        if name is not None:
            set_parts += ["name = ?", "name_norm = py_casefold(?)"]
            params += [name, name]
        if code is not None:
            set_parts.append("code = ?")
            params.append(code)
        params.append(loc_id)

        with self._db.transaction() as c:
            try:
                c.execute(
                    f"UPDATE locations SET {', '.join(set_parts)} WHERE id = ?",
                    params)
            except sqlite3.IntegrityError as e:
                raise ValueError(f"Не удалось обновить место: {e}") from None

        return self.get_by_id(loc_id)

    def archive(self, loc_id, at=None):
        """Архивировать место, проставляя archived_at.

        Проверяет ДО транзакции, что нет активных (неархивированных) дочерних мест."""
        loc = self.get_by_id(loc_id)
        if loc is None:
            raise ValueError(f"Место {loc_id} не найдено")

        # Проверяем наличие активных детей ДО транзакции
        children = self.list_children(loc_id)
        active_children = [
            child for child in children if child.archived_at is None
        ]
        if active_children:
            raise ValueError(
                "Нельзя архивировать место с активными дочерними местами"
            )

        now = at or int(time.time())

        with self._db.transaction() as c:
            c.execute(
                "UPDATE locations SET archived_at = ?, updated_at = ? WHERE id = ?",
                (now, now, loc_id),
            )

        return self.get_by_id(loc_id)

    def path_to_root(self, loc_id):
        """Получить путь от корня к месту включительно.

        Например: [Объект, Корпус А, Этаж 2, Комната 201].
        Использует кэш locations.parent_id для подъёма по иерархии."""
        with self._db.read() as c:
            path = []
            current_id = loc_id
            depth = 0

            # Собираем путь с начала (от места) до корня
            while current_id is not None:
                if depth >= MAX_DEPTH:
                    raise ValueError(
                        "Превышена максимальная глубина дерева мест (32) "
                        "при обходе path_to_root"
                    )

                row = c.execute(
                    "SELECT * FROM locations WHERE id = ?", (current_id,)
                ).fetchone()
                if row is None:
                    break

                loc = Location.from_row(row)
                path.append(loc)
                current_id = row["parent_id"]
                depth += 1

            # Развернуть путь, чтобы был порядок от корня к месту
            path.reverse()
            return path
