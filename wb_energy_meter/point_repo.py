"""Репозиторий для работы с точками учёта и источниками данных приборов.

Управляет таблицами metering_points и meter_sources, обеспечивая консистентность
денормализованного кэша enabled в metering_points через point_state_versions."""

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from . import domain_generation

log = logging.getLogger(__name__)

CODE_MAX_LEN = 64
CODE_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f]")

NAME_MAX_LEN = 200
NAME_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f]")


def validate_code(s):
    """Валидирует код точки учёта."""
    s = (s or "").strip()
    if not s:
        raise ValueError("код не может быть пустым")
    if len(s) > CODE_MAX_LEN:
        raise ValueError(f"код длиннее {CODE_MAX_LEN}")
    if CODE_FORBIDDEN_RE.search(s):
        raise ValueError("код содержит управляющие символы")
    return s


def validate_name(s):
    """Валидирует имя объекта."""
    s = (s or "").strip()
    if not s:
        raise ValueError("имя не может быть пустым")
    if len(s) > NAME_MAX_LEN:
        raise ValueError(f"имя длиннее {NAME_MAX_LEN}")
    if NAME_FORBIDDEN_RE.search(s):
        raise ValueError("имя содержит управляющие символы")
    return s


@dataclass
class MeterSource:
    id: int
    meter_id: int
    controller_key: str
    device_id: str
    valid_from: int
    valid_to: Optional[int]
    created_at: int

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            meter_id=row["meter_id"],
            controller_key=row["controller_key"],
            device_id=row["device_id"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            created_at=row["created_at"]
        )


class MeterSourceRepo:
    def __init__(self, db):
        self._db = db

    def get_by_id(self, source_id):
        """Получить источник по id."""
        with self._db.read() as c:
            row = c.execute("SELECT * FROM meter_sources WHERE id = ?",
                            (source_id,)).fetchone()
            return MeterSource.from_row(row) if row else None

    def get_current(self, meter_id):
        """Получить открытый (текущий) источник для прибора."""
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM meter_sources WHERE meter_id = ? AND valid_to IS NULL",
                (meter_id,)
            ).fetchone()
            return MeterSource.from_row(row) if row else None

    def list_for_meter(self, meter_id):
        """Получить все источники прибора, отсортированно по valid_from.

        id как вторичный ключ сортировки — записи могут иметь одинаковый
        valid_from (секундная точность time.time()) при быстром
        переприсваивании, id гарантирует порядок создания."""
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM meter_sources WHERE meter_id = ? ORDER BY valid_from, id",
                (meter_id,)
            ).fetchall()
            return [MeterSource.from_row(r) for r in rows]

    def open_source(self, meter_id, controller_key, device_id):
        """Открыть новый источник для прибора."""
        now = int(time.time())
        with self._db.transaction() as c:
            try:
                cur = c.execute(
                    "INSERT INTO meter_sources "
                    "(meter_id, controller_key, device_id, valid_from, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (meter_id, controller_key, device_id, now, now)
                )
            except sqlite3.IntegrityError as e:
                # UNIQUE(controller_key, device_id) WHERE valid_to IS NULL —
                # самая вероятная причина, но meter_id — это FK на meters(id),
                # так что несуществующий прибор даст ту же IntegrityError.
                if "unique" in str(e).lower():
                    raise ValueError(
                        f"Адрес {controller_key}/{device_id} уже занят другим прибором"
                    ) from None
                if "foreign key" in str(e).lower():
                    raise ValueError(f"Прибор meter_id={meter_id} не найден") from None
                raise
            source_id = cur.lastrowid
        log.info(
            "Открыт источник %s/%s для прибора %d (id=%d)",
            controller_key, device_id, meter_id, source_id
        )
        return self.get_by_id(source_id)

    def close_source(self, source_id, at=None):
        """Закрыть источник, проставив valid_to."""
        now = at if at is not None else int(time.time())
        with self._db.transaction() as c:
            row = c.execute(
                "SELECT * FROM meter_sources WHERE id = ?",
                (source_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Источник {source_id} не найден")
            if row["valid_to"] is not None:
                raise ValueError(f"Источник {source_id} уже закрыт")
            c.execute(
                "UPDATE meter_sources SET valid_to = ? WHERE id = ?",
                (now, source_id)
            )
        log.info("Закрыт источник %d", source_id)

    def reassign_source(self, meter_id, controller_key, device_id, at=None):
        """Атомарно переприсвоить источник прибору."""
        now = at if at is not None else int(time.time())
        with self._db.transaction() as c:
            # Закрыть текущий открытый источник, если есть
            current = c.execute(
                "SELECT id FROM meter_sources WHERE meter_id = ? AND valid_to IS NULL",
                (meter_id,)
            ).fetchone()
            if current:
                c.execute(
                    "UPDATE meter_sources SET valid_to = ? WHERE id = ?",
                    (now, current["id"])
                )
                log.info("Закрыт текущий источник %d при переприсваивании", current["id"])

            # Открыть новый источник
            try:
                cur = c.execute(
                    "INSERT INTO meter_sources "
                    "(meter_id, controller_key, device_id, valid_from, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (meter_id, controller_key, device_id, now, now)
                )
            except sqlite3.IntegrityError as e:
                if "unique" in str(e).lower():
                    raise ValueError(
                        f"Адрес {controller_key}/{device_id} уже занят другим прибором"
                    ) from None
                if "foreign key" in str(e).lower():
                    raise ValueError(f"Прибор meter_id={meter_id} не найден") from None
                raise
            source_id = cur.lastrowid

            # Получить созданный источник (всё ещё в транзакции)
            row = c.execute("SELECT * FROM meter_sources WHERE id = ?",
                           (source_id,)).fetchone()
            new_source = MeterSource.from_row(row)

        log.info(
            "Переприсвоен источник %s/%s для прибора %d (id=%d)",
            controller_key, device_id, meter_id, source_id
        )
        return new_source


@dataclass
class MeteringPoint:
    id: int
    code: str
    name: str
    description: Optional[str]
    installation_location_id: Optional[int]
    installation_note: Optional[str]
    enabled: int  # 0/1
    archived_at: Optional[int]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row):
        keys = row.keys()
        return cls(
            id=row["id"],
            code=row["code"],
            name=row["name"],
            description=row["description"] if "description" in keys else None,
            installation_location_id=row["installation_location_id"]
                if "installation_location_id" in keys else None,
            installation_note=row["installation_note"] if "installation_note" in keys else None,
            enabled=row["enabled"],
            archived_at=row["archived_at"] if "archived_at" in keys else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"]
        )


_UNSET = object()  # см. update_fields.installation_location_id


class MeteringPointRepo:
    def __init__(self, db):
        self._db = db

    def get_by_id(self, point_id):
        """Получить точку по id."""
        with self._db.read() as c:
            row = c.execute("SELECT * FROM metering_points WHERE id = ?",
                            (point_id,)).fetchone()
            return MeteringPoint.from_row(row) if row else None

    def get_by_code(self, code):
        """Получить точку по коду."""
        with self._db.read() as c:
            row = c.execute("SELECT * FROM metering_points WHERE code = ?",
                            (code,)).fetchone()
            return MeteringPoint.from_row(row) if row else None

    def list_all(self, include_archived=False):
        """Получить все точки, отсортированно по коду."""
        with self._db.read() as c:
            if include_archived:
                rows = c.execute(
                    "SELECT * FROM metering_points ORDER BY code COLLATE NOCASE"
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM metering_points WHERE archived_at IS NULL "
                    "ORDER BY code COLLATE NOCASE"
                ).fetchall()
            return [MeteringPoint.from_row(r) for r in rows]

    def add(self, code, name, description=None, installation_location_id=None,
            installation_note=None):
        """Создать новую точку и первую версию состояния."""
        code = validate_code(code)
        name = validate_name(name)
        now = int(time.time())

        with self._db.transaction() as c:
            try:
                cur = c.execute(
                    "INSERT INTO metering_points "
                    "(code, name, description, installation_location_id, "
                    "installation_note, enabled, archived_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, NULL, ?, ?)",
                    (code, name, description, installation_location_id,
                     installation_note, now, now)
                )
            except sqlite3.IntegrityError as e:
                # UNIQUE(code) — самая вероятная причина, но это может быть и
                # нарушение FK на installation_location_id (несуществующее
                # место) — не путать одно с другим в сообщении об ошибке.
                if "code" in str(e).lower():
                    raise ValueError(f"Точка с кодом {code!r} уже существует") from None
                if "foreign key" in str(e).lower():
                    raise ValueError(
                        f"Место installation_location_id={installation_location_id} "
                        f"не найдено") from None
                raise

            point_id = cur.lastrowid

            # Создать первую версию состояния
            c.execute(
                "INSERT INTO point_state_versions "
                "(point_id, enabled, valid_from, valid_to, revision_id, created_at) "
                "VALUES (?, 1, ?, NULL, NULL, ?)",
                (point_id, now, now)
            )

            # docs/migration-plan-v2.md §7 п.2: первая предметная запись
            # через доменную модель v2 отмечается В ТОЙ ЖЕ транзакции —
            # см. domain_generation.py. Идемпотентно (не переставляет
            # маркер на повторных вызовах).
            domain_generation.mark_v2_domain_write(c)

        log.info("Создана точка учёта: %s (id=%d)", code, point_id)
        return self.get_by_id(point_id)

    def set_enabled(self, point_id, enabled, at=None):
        """Включить/отключить точку с управлением версиями состояния."""
        now = at if at is not None else int(time.time())
        enabled_int = 1 if enabled else 0

        with self._db.read() as c:
            point = c.execute("SELECT enabled FROM metering_points WHERE id = ?",
                             (point_id,)).fetchone()
            if not point:
                raise ValueError(f"Точка {point_id} не найдена")

            current_enabled = point["enabled"]

        # Идемпотентно: если уже в нужном состоянии, ничего не делаем
        if current_enabled == enabled_int:
            return self.get_by_id(point_id)

        with self._db.transaction() as c:
            # Закрыть текущую открытую версию состояния
            c.execute(
                "UPDATE point_state_versions SET valid_to = ? "
                "WHERE point_id = ? AND valid_to IS NULL",
                (now, point_id)
            )

            # Вставить новую открытую версию
            c.execute(
                "INSERT INTO point_state_versions "
                "(point_id, enabled, valid_from, valid_to, revision_id, created_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?)",
                (point_id, enabled_int, now, now)
            )

            # Обновить кэш в metering_points
            c.execute(
                "UPDATE metering_points SET enabled = ?, updated_at = ? WHERE id = ?",
                (enabled_int, now, point_id)
            )

        log.info("Изменено состояние точки %d: enabled=%s", point_id, bool(enabled_int))
        return self.get_by_id(point_id)

    def archive(self, point_id, at=None):
        """Архивировать точку (выключить и проставить archived_at)."""
        now = at if at is not None else int(time.time())

        with self._db.read() as c:
            point = c.execute("SELECT * FROM metering_points WHERE id = ?",
                             (point_id,)).fetchone()
            if not point:
                raise ValueError(f"Точка {point_id} не найдена")

        with self._db.transaction() as c:
            # Закрыть текущую открытую версию состояния
            c.execute(
                "UPDATE point_state_versions SET valid_to = ? "
                "WHERE point_id = ? AND valid_to IS NULL",
                (now, point_id)
            )

            # Вставить новую открытую версию с enabled=0
            c.execute(
                "INSERT INTO point_state_versions "
                "(point_id, enabled, valid_from, valid_to, revision_id, created_at) "
                "VALUES (?, 0, ?, NULL, NULL, ?)",
                (point_id, now, now)
            )

            # Обновить кэш в metering_points
            c.execute(
                "UPDATE metering_points SET enabled = 0, archived_at = ?, "
                "updated_at = ? WHERE id = ?",
                (now, now, point_id)
            )

        log.info("Архивирована точка %d", point_id)
        return self.get_by_id(point_id)

    def update_fields(self, point_id, *, name=None, description=None,
                      installation_note=None, installation_location_id=_UNSET):
        """Обновить простые поля точки. installation_location_id — партия
        5, задача 2 (§8.3: "редактировать принадлежность" в карточке
        точки) — раньше место установки можно было задать только при
        создании точки (repo.add), сменить его позже было нечем. Отдельный
        сентинел _UNSET, а не None по умолчанию: None здесь — ЗНАЧАЩЕЕ
        значение ("снять место установки"), а не "не менять"."""
        if (name is None and description is None and installation_note is None
                and installation_location_id is _UNSET):
            return self.get_by_id(point_id)

        # Валидируем переданные значения
        if name is not None:
            name = validate_name(name)

        now = int(time.time())

        with self._db.transaction() as c:
            point = c.execute("SELECT * FROM metering_points WHERE id = ?",
                             (point_id,)).fetchone()
            if not point:
                raise ValueError(f"Точка {point_id} не найдена")

            # Подготовим UPDATE с динамическим набором полей
            updates = {"updated_at": now}
            if name is not None:
                updates["name"] = name
            if description is not None:
                updates["description"] = description
            if installation_note is not None:
                updates["installation_note"] = installation_note
            if installation_location_id is not _UNSET:
                updates["installation_location_id"] = installation_location_id

            # Формируем SQL
            set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
            params = list(updates.values()) + [point_id]

            try:
                c.execute(
                    f"UPDATE metering_points SET {set_clause} WHERE id = ?",
                    params
                )
            except sqlite3.IntegrityError as e:
                if "foreign key" in str(e).lower():
                    raise ValueError(
                        f"Место installation_location_id={installation_location_id} "
                        f"не найдено") from None
                raise

        log.info("Обновлена точка %d", point_id)
        return self.get_by_id(point_id)
