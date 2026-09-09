"""Планы объекта, зоны на плане и кабельные связи — БД и файлы (ТЗ v0.11.0).

Зона на плане ссылается на СУЩЕСТВУЮЩУЮ `meter_groups` — вторая
сущность "зона" сознательно не заводится (§5, §12 ТЗ). Картинка плана
хранится файлом на диске рядом с БД (`<каталог БД>/plans/`), путь и имя
файла на диске генерируются нами — то, что пришло в запросе (в том
числе исходное имя загруженного файла), никогда не используется ни в
пути, ни в shell-командах (§6, §12 ТЗ).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from . import plan_geo

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 МБ (§6 ТЗ)

# Расширение файла на диске зависит только от формата, определённого по
# сигнатуре (image_meta.detect_image_format) — никогда от клиентского
# имени файла.
_EXT_BY_FORMAT = {"png": "png", "jpeg": "jpg"}


class PlanError(ValueError):
    """Ошибка валидации/логики плана — текст на русском, годится для
    прямой отдачи в API-ответе."""


# ---------------------------------------------------------------------
# Файлы на диске
# ---------------------------------------------------------------------

def plans_dir(db_path: str) -> str:
    """Каталог с картинками планов — рядом с БД, переживает обновление
    сервиса (в проде это `/mnt/data/.../plans/`)."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "plans")


def plan_filename(plan_id: int, image_format: str) -> str:
    ext = _EXT_BY_FORMAT.get(image_format)
    if not ext:
        raise PlanError(f"Неизвестный формат изображения: {image_format!r}")
    # plan_id — целое число из БД (никогда не из запроса), поэтому
    # безопасно участвует в имени файла напрямую.
    return f"plan_{int(plan_id)}.{ext}"


def _atomic_write(path: str, payload: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = os.path.join(directory, f".{os.path.basename(path)}.tmp.{os.getpid()}")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_plan_image(plans_directory: str, plan_id: int, image_format: str,
                    data: bytes) -> str:
    """Сохраняет картинку плана под сгенерированным нами именем.
    Возвращает имя файла (не путь) — оно же кладётся в БД."""
    filename = plan_filename(plan_id, image_format)
    path = os.path.join(plans_directory, filename)
    _atomic_write(path, data)
    return filename


def read_plan_image(plans_directory: str, image_file: str) -> Optional[bytes]:
    """Читает картинку плана. `image_file` берётся ТОЛЬКО из уже
    прочитанной строки БД (сгенерированной нами), никогда из запроса —
    поэтому здесь достаточно проверить, что итоговый путь не убежал за
    пределы каталога, без разбора произвольного пользовательского ввода."""
    path = os.path.join(plans_directory, image_file)
    real_dir = os.path.realpath(plans_directory)
    real_path = os.path.realpath(path)
    if os.path.commonpath([real_dir, real_path]) != real_dir:
        log.error("Подозрительный путь картинки плана: %r", image_file)
        return None
    try:
        with open(real_path, "rb") as f:
            return f.read()
    except OSError as e:
        log.warning("Не удалось прочитать картинку плана %s: %s", path, e)
        return None


def delete_plan_image(plans_directory: str, image_file: str) -> None:
    if not image_file:
        return
    path = os.path.join(plans_directory, image_file)
    real_dir = os.path.realpath(plans_directory)
    real_path = os.path.realpath(path)
    if os.path.commonpath([real_dir, real_path]) != real_dir:
        return
    try:
        os.unlink(real_path)
    except OSError:
        pass


# ---------------------------------------------------------------------
# site_plans
# ---------------------------------------------------------------------

@dataclass
class SitePlan:
    id: int
    name: str
    image_file: str
    image_width: int
    image_height: int
    is_default: bool
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"], name=row["name"], image_file=row["image_file"],
            image_width=row["image_width"], image_height=row["image_height"],
            is_default=bool(row["is_default"]),
            created_at=row["created_at"], updated_at=row["updated_at"])

    def to_dict(self):
        return {
            "id": self.id, "name": self.name,
            "image_width": self.image_width, "image_height": self.image_height,
            "is_default": self.is_default,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


def _validate_plan_name(name) -> str:
    name = (name or "").strip()
    if not name:
        raise PlanError("Имя плана не может быть пустым")
    if len(name) > 200:
        raise PlanError("Имя плана длиннее 200 символов")
    return name


class SitePlanRepo:
    def __init__(self, db):
        self._db = db

    def get_by_id(self, plan_id) -> Optional[SitePlan]:
        with self._db.read() as c:
            row = c.execute("SELECT * FROM site_plans WHERE id = ?",
                            (plan_id,)).fetchone()
            return SitePlan.from_row(row) if row else None

    def list_all(self) -> List[SitePlan]:
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM site_plans ORDER BY is_default DESC, "
                "name COLLATE NOCASE").fetchall()
            return [SitePlan.from_row(r) for r in rows]

    def create(self, name: str, image_format: str, width: int, height: int,
              image_bytes: bytes, plans_directory: str) -> SitePlan:
        """Создаёт запись и файл картинки атомарно относительно наблюдателя:
        сначала вставляется строка (чтобы получить id для имени файла),
        затем пишется файл; если запись файла не удалась — строка
        откатывается, в БД ничего не остаётся."""
        name = _validate_plan_name(name)
        now = int(time.time())
        first = len(self.list_all()) == 0
        with self._db.transaction() as c:
            cur = c.execute(
                "INSERT INTO site_plans (name, image_file, image_width, "
                "image_height, is_default, created_at, updated_at) "
                "VALUES (?, '', ?, ?, ?, ?, ?)",
                (name, width, height, 1 if first else 0, now, now))
            plan_id = cur.lastrowid
        try:
            filename = save_plan_image(plans_directory, plan_id,
                                       image_format, image_bytes)
        except OSError as e:
            with self._db.transaction() as c:
                c.execute("DELETE FROM site_plans WHERE id = ?", (plan_id,))
            raise PlanError(f"Не удалось сохранить файл плана: {e}")
        with self._db.transaction() as c:
            c.execute("UPDATE site_plans SET image_file = ? WHERE id = ?",
                     (filename, plan_id))
        log.info("Создан план: %r (id=%d, %dx%d, файл=%s)",
                 name, plan_id, width, height, filename)
        return self.get_by_id(plan_id)

    def update(self, plan_id, *, name=None, is_default=None) -> SitePlan:
        existing = self.get_by_id(plan_id)
        if existing is None:
            raise PlanError(f"План id={plan_id} не найден")
        sets, params = [], []
        if name is not None:
            sets.append("name = ?"); params.append(_validate_plan_name(name))
        if is_default:
            with self._db.transaction() as c:
                c.execute("UPDATE site_plans SET is_default = 0")
        if is_default is not None:
            sets.append("is_default = ?"); params.append(1 if is_default else 0)
        if not sets:
            return existing
        sets.append("updated_at = ?"); params.append(int(time.time()))
        params.append(plan_id)
        with self._db.transaction() as c:
            c.execute(f"UPDATE site_plans SET {', '.join(sets)} WHERE id = ?",
                     tuple(params))
        return self.get_by_id(plan_id)

    def delete(self, plan_id, plans_directory: str) -> bool:
        """Удаляет план, зоны и связи (каскадом через FK — PRAGMA
        foreign_keys=ON включена глобально в db.py) и файл картинки."""
        plan = self.get_by_id(plan_id)
        if plan is None:
            return False
        with self._db.transaction() as c:
            cur = c.execute("DELETE FROM site_plans WHERE id = ?", (plan_id,))
            removed = cur.rowcount > 0
        if removed and plan.image_file:
            delete_plan_image(plans_directory, plan.image_file)
        if removed:
            log.info("Удалён план: %r (id=%d)", plan.name, plan_id)
        return removed


# ---------------------------------------------------------------------
# plan_zones
# ---------------------------------------------------------------------

@dataclass
class PlanZone:
    id: int
    plan_id: int
    group_id: int
    shape_type: str
    geometry: Any
    anchor: Optional[Any]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"], plan_id=row["plan_id"], group_id=row["group_id"],
            shape_type=row["shape_type"],
            geometry=json.loads(row["geometry"]),
            anchor=json.loads(row["anchor"]) if row["anchor"] else None,
            created_at=row["created_at"], updated_at=row["updated_at"])

    def to_dict(self):
        return {
            "id": self.id, "plan_id": self.plan_id, "group_id": self.group_id,
            "shape_type": self.shape_type, "geometry": self.geometry,
            "anchor": self.anchor,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


class PlanZoneRepo:
    def __init__(self, db):
        self._db = db

    def get(self, plan_id, group_id) -> Optional[PlanZone]:
        with self._db.read() as c:
            row = c.execute(
                "SELECT * FROM plan_zones WHERE plan_id = ? AND group_id = ?",
                (plan_id, group_id)).fetchone()
            return PlanZone.from_row(row) if row else None

    def get_by_id(self, zone_id) -> Optional[PlanZone]:
        with self._db.read() as c:
            row = c.execute("SELECT * FROM plan_zones WHERE id = ?",
                            (zone_id,)).fetchone()
            return PlanZone.from_row(row) if row else None

    def list_by_plan(self, plan_id) -> List[PlanZone]:
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM plan_zones WHERE plan_id = ? ORDER BY id",
                (plan_id,)).fetchall()
            return [PlanZone.from_row(r) for r in rows]

    def upsert(self, plan, group_id, shape_type, geometry, anchor) -> PlanZone:
        """Создать либо обновить контур зоны на плане. `plan` —
        объект SitePlan (для image_width/height — валидация границ)."""
        shape_type = (shape_type or "polygon").strip().lower()
        plan_geo.validate_geometry(geometry, shape_type,
                                   plan.image_width, plan.image_height)
        plan_geo.validate_anchor(anchor, plan.image_width, plan.image_height)
        now = int(time.time())
        geometry_json = json.dumps(geometry)
        anchor_json = json.dumps(anchor) if anchor is not None else None
        with self._db.transaction() as c:
            existing = c.execute(
                "SELECT id FROM plan_zones WHERE plan_id = ? AND group_id = ?",
                (plan.id, group_id)).fetchone()
            if existing:
                c.execute(
                    "UPDATE plan_zones SET shape_type = ?, geometry = ?, "
                    "anchor = ?, updated_at = ? WHERE id = ?",
                    (shape_type, geometry_json, anchor_json, now,
                     existing["id"]))
                zone_id = existing["id"]
            else:
                cur = c.execute(
                    "INSERT INTO plan_zones (plan_id, group_id, shape_type, "
                    "geometry, anchor, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (plan.id, group_id, shape_type, geometry_json,
                     anchor_json, now, now))
                zone_id = cur.lastrowid
        return self.get_by_id(zone_id)

    def delete(self, plan_id, group_id) -> bool:
        with self._db.transaction() as c:
            cur = c.execute(
                "DELETE FROM plan_zones WHERE plan_id = ? AND group_id = ?",
                (plan_id, group_id))
            return cur.rowcount > 0


# ---------------------------------------------------------------------
# plan_links
# ---------------------------------------------------------------------

@dataclass
class PlanLink:
    id: int
    plan_id: int
    from_zone_id: int
    to_zone_id: int
    source_meter_id: Optional[int]
    rated_current_a: Optional[float]
    waypoints: Optional[Any]
    label: Optional[str]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"], plan_id=row["plan_id"],
            from_zone_id=row["from_zone_id"], to_zone_id=row["to_zone_id"],
            source_meter_id=row["source_meter_id"],
            rated_current_a=row["rated_current_a"],
            waypoints=json.loads(row["waypoints"]) if row["waypoints"] else None,
            label=row["label"],
            created_at=row["created_at"], updated_at=row["updated_at"])

    def to_dict(self):
        return {
            "id": self.id, "plan_id": self.plan_id,
            "from_zone_id": self.from_zone_id, "to_zone_id": self.to_zone_id,
            "source_meter_id": self.source_meter_id,
            "rated_current_a": self.rated_current_a,
            "waypoints": self.waypoints, "label": self.label,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


def _validate_label(label) -> Optional[str]:
    if label is None:
        return None
    label = str(label).strip()
    if not label:
        return None
    if len(label) > 200:
        raise PlanError("Подпись связи длиннее 200 символов")
    return label


class PlanLinkRepo:
    def __init__(self, db):
        self._db = db

    def get_by_id(self, link_id) -> Optional[PlanLink]:
        with self._db.read() as c:
            row = c.execute("SELECT * FROM plan_links WHERE id = ?",
                            (link_id,)).fetchone()
            return PlanLink.from_row(row) if row else None

    def list_by_plan(self, plan_id) -> List[PlanLink]:
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM plan_links WHERE plan_id = ? ORDER BY id",
                (plan_id,)).fetchall()
            return [PlanLink.from_row(r) for r in rows]

    def _validate_zone_pair(self, plan_id, from_zone_id, to_zone_id,
                            zone_repo: PlanZoneRepo) -> None:
        if from_zone_id == to_zone_id:
            raise PlanError("Связь не может соединять зону саму с собой")
        for zid in (from_zone_id, to_zone_id):
            z = zone_repo.get_by_id(zid)
            if z is None or z.plan_id != plan_id:
                raise PlanError(
                    f"Зона id={zid} не найдена на этом плане")

    def create(self, plan_id, from_zone_id, to_zone_id, zone_repo: PlanZoneRepo,
              source_meter_id=None, rated_current_a=None, waypoints=None,
              label=None, plan=None) -> PlanLink:
        self._validate_zone_pair(plan_id, from_zone_id, to_zone_id, zone_repo)
        rated_current_a = plan_geo.validate_rated_current(rated_current_a)
        if plan is not None:
            plan_geo.validate_waypoints(waypoints, plan.image_width,
                                        plan.image_height)
        label = _validate_label(label)
        now = int(time.time())
        waypoints_json = json.dumps(waypoints) if waypoints is not None else None
        with self._db.transaction() as c:
            cur = c.execute(
                "INSERT INTO plan_links (plan_id, from_zone_id, to_zone_id, "
                "source_meter_id, rated_current_a, waypoints, label, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (plan_id, from_zone_id, to_zone_id, source_meter_id,
                 rated_current_a, waypoints_json, label, now, now))
            link_id = cur.lastrowid
        return self.get_by_id(link_id)

    def update(self, link_id, zone_repo: PlanZoneRepo, plan=None, *,
              from_zone_id=None, to_zone_id=None, source_meter_id=None,
              rated_current_a=None, waypoints=None, label=None,
              _fields=None) -> PlanLink:
        """`_fields` — множество реально переданных ключей (различаем
        "не передан" от "передан как null/0"), как в MeterRepo.update()."""
        existing = self.get_by_id(link_id)
        if existing is None:
            raise PlanError(f"Связь id={link_id} не найдена")
        fields = _fields or set()
        new_from = from_zone_id if "from_zone_id" in fields else existing.from_zone_id
        new_to = to_zone_id if "to_zone_id" in fields else existing.to_zone_id
        self._validate_zone_pair(existing.plan_id, new_from, new_to, zone_repo)

        sets, params = ["from_zone_id = ?", "to_zone_id = ?"], [new_from, new_to]
        if "source_meter_id" in fields:
            sets.append("source_meter_id = ?"); params.append(source_meter_id)
        if "rated_current_a" in fields:
            rated_current_a = plan_geo.validate_rated_current(rated_current_a)
            sets.append("rated_current_a = ?"); params.append(rated_current_a)
        if "waypoints" in fields:
            if plan is not None:
                plan_geo.validate_waypoints(waypoints, plan.image_width,
                                            plan.image_height)
            sets.append("waypoints = ?")
            params.append(json.dumps(waypoints) if waypoints is not None else None)
        if "label" in fields:
            sets.append("label = ?"); params.append(_validate_label(label))
        sets.append("updated_at = ?"); params.append(int(time.time()))
        params.append(link_id)
        with self._db.transaction() as c:
            c.execute(f"UPDATE plan_links SET {', '.join(sets)} WHERE id = ?",
                     tuple(params))
        return self.get_by_id(link_id)

    def delete(self, link_id) -> bool:
        with self._db.transaction() as c:
            cur = c.execute("DELETE FROM plan_links WHERE id = ?", (link_id,))
            return cur.rowcount > 0
