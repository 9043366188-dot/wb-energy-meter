"""План v2 — site_plans (plan_kind/canvas), plan_items, plan_edge_views,
plan_image_versions (ТЗ docs/TZ-metering-architecture-dashboard.md §7).

ОТДЕЛЬНО от wb_energy_meter/plan_repo.py (v1: plan_zones/plan_links
поверх meter_groups) — v1 НЕ трогается и продолжает работать как
раньше. Это параллельный слой поверх ТЕХ ЖЕ `site_plans` — миграция
005 расширила таблицу `plan_kind`/`canvas_*`/`canvas_revision`, старым
планам проставив `plan_kind='floor'`, так что v1-код (который эти
колонки не знает) остаётся рабочим без изменений.

Переиспользует файловый слой plan_repo.py (save_plan_image/
read_plan_image/delete_plan_image/plans_dir/plan_filename/
MAX_UPLOAD_BYTES) — путь/имя файла генерирует сервер, расширение — по
сигнатуре байт, а не по клиентскому имени (§6/§7.2 ТЗ одинаковы в этой
части и для v1, и для v2).

Специфичное для v2 (§7.2 ТЗ), чего не было в v1:
- предел декодированных размеров: длинная сторона <=8192px, площадь
  <=24 Мп;
- `plan_kind=floor|single_line`; у single_line `image_file` nullable,
  логические размеры — `canvas_width/canvas_height`, не пересчитываются
  при расширении холста;
- `plan_image_versions` — версии фона: замена НЕ удаляет предыдущую
  (§7.2: "сохранить старый фон/геометрию для отката");
- `canvas_revision` — оптимистичная блокировка сохранения layout, см.
  save_plan_layout()/RevisionConflict (A35 ТЗ §13: "конфликт ревизии
  409, чужие изменения не перезаписаны").

Координаты (`geometry`/`waypoints`) валидируются и хранятся через
plan_geo_v2 (dispatch по coord_space) — не через старый plan_geo.py.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import domain_generation, plan_geo_v2
from .image_meta import parse_image_size, ImageFormatError
from .plan_repo import (
    MAX_UPLOAD_BYTES, PlanError,
    save_plan_image, delete_plan_image,
)

log = logging.getLogger(__name__)

VALID_PLAN_KINDS = ("floor", "single_line")
VALID_PLAN_ITEM_KINDS = ("point", "location", "group", "node", "port", "annotation")
VALID_VIEW_KINDS = ("structural", "cable_route")

# §7.2 ТЗ: "дополнительно установить предел декодированных размеров:
# длинная сторона <=8192 px и площадь <=24 Мп" — проектные ограничения
# первой поставки v2, в v1 их не было и здесь они не влияют на v1.
MAX_DECODED_LONG_SIDE = 8192
MAX_DECODED_MEGAPIXELS = 24_000_000


class RevisionConflict(ValueError):
    """A35 ТЗ §13: два редактора сохраняют один план — второй получает
    409, а не тихую перезапись первого."""

    def __init__(self, plan_id: int, expected: int, actual: int):
        self.plan_id = plan_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"План id={plan_id}: ожидалась ревизия {expected}, сейчас "
            f"{actual} — его уже сохранил кто-то ещё"
        )


def _validate_decoded_size(width: int, height: int) -> None:
    if max(width, height) > MAX_DECODED_LONG_SIDE:
        raise PlanError(
            f"Слишком большая сторона изображения: {max(width, height)}px "
            f"(допустимо до {MAX_DECODED_LONG_SIDE}px)")
    if width * height > MAX_DECODED_MEGAPIXELS:
        raise PlanError(
            f"Слишком большая площадь изображения: {width * height} px² "
            f"(допустимо до {MAX_DECODED_MEGAPIXELS} px²)")


def _validate_plan_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise PlanError("Имя плана не может быть пустым")
    name = name.strip()
    if len(name) > 200:
        raise PlanError("Имя плана слишком длинное (макс. 200 символов)")
    return name


def _decode_and_check_image(image_bytes: bytes):
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise PlanError(f"Файл слишком большой (макс. {MAX_UPLOAD_BYTES} байт)")
    try:
        img_fmt, img_w, img_h = parse_image_size(image_bytes)
    except ImageFormatError as e:
        raise PlanError(str(e)) from None
    _validate_decoded_size(img_w, img_h)
    return img_fmt, img_w, img_h


# ---------------------------------------------------------------------
# site_plans (v2)
# ---------------------------------------------------------------------

@dataclass
class PlanV2:
    id: int
    name: str
    plan_kind: str
    image_file: Optional[str]
    image_width: Optional[int]
    image_height: Optional[int]
    canvas_width: Optional[int]
    canvas_height: Optional[int]
    canvas_revision: int
    is_default: bool
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row) -> "PlanV2":
        return cls(
            id=row["id"], name=row["name"], plan_kind=row["plan_kind"],
            image_file=row["image_file"] or None,
            image_width=row["image_width"], image_height=row["image_height"],
            canvas_width=row["canvas_width"], canvas_height=row["canvas_height"],
            canvas_revision=row["canvas_revision"],
            is_default=bool(row["is_default"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @property
    def effective_height(self) -> Optional[float]:
        """Высота для plan_geo_v2: у floor — image_height, у
        single_line без картинки — canvas_height."""
        return self.image_height if self.image_file else self.canvas_height

    @property
    def effective_width(self) -> Optional[float]:
        return self.image_width if self.image_file else self.canvas_width

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "plan_kind": self.plan_kind,
            "image_width": self.image_width, "image_height": self.image_height,
            "canvas_width": self.canvas_width, "canvas_height": self.canvas_height,
            "canvas_revision": self.canvas_revision,
            "is_default": self.is_default,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


class SitePlanRepoV2:
    def __init__(self, db, plans_directory: str):
        self._db = db
        self._dir = plans_directory

    def get_by_id(self, plan_id: int) -> Optional[PlanV2]:
        with self._db.read() as c:
            row = c.execute("SELECT * FROM site_plans WHERE id = ?",
                             (plan_id,)).fetchone()
            return PlanV2.from_row(row) if row else None

    def list_all(self, plan_kind: Optional[str] = None) -> List[PlanV2]:
        with self._db.read() as c:
            if plan_kind:
                rows = c.execute(
                    "SELECT * FROM site_plans WHERE plan_kind = ? "
                    "ORDER BY name COLLATE NOCASE", (plan_kind,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM site_plans ORDER BY name COLLATE NOCASE"
                ).fetchall()
            return [PlanV2.from_row(r) for r in rows]

    def create(self, name: str, plan_kind: str,
               image_bytes: Optional[bytes] = None,
               canvas_width: Optional[int] = None,
               canvas_height: Optional[int] = None) -> PlanV2:
        name = _validate_plan_name(name)
        if plan_kind not in VALID_PLAN_KINDS:
            raise PlanError(f"неизвестный plan_kind: {plan_kind!r}")

        img_fmt = img_w = img_h = None
        if image_bytes is not None:
            img_fmt, img_w, img_h = _decode_and_check_image(image_bytes)
        elif plan_kind == "floor":
            raise PlanError("План помещений требует изображения (§7.1 ТЗ)")

        if plan_kind == "single_line":
            if image_bytes is not None:
                canvas_width = canvas_width or img_w
                canvas_height = canvas_height or img_h
            elif not canvas_width or not canvas_height:
                raise PlanError(
                    "Для пустой однолинейной схемы нужны "
                    "canvas_width/canvas_height (например 2000x1200, §7.2 ТЗ)")
        else:
            canvas_width = canvas_height = None

        now = int(time.time())
        first = len(self.list_all()) == 0
        with self._db.transaction() as c:
            cur = c.execute(
                "INSERT INTO site_plans "
                "(name, plan_kind, image_file, image_width, image_height, "
                "canvas_width, canvas_height, canvas_revision, is_default, "
                "created_at, updated_at) "
                "VALUES (?, ?, '', ?, ?, ?, ?, 1, ?, ?, ?)",
                (name, plan_kind, img_w, img_h, canvas_width, canvas_height,
                 1 if first else 0, now, now))
            plan_id = cur.lastrowid

        if image_bytes is not None:
            try:
                image_file = save_plan_image(self._dir, plan_id, img_fmt, image_bytes)
            except OSError as e:
                with self._db.transaction() as c:
                    c.execute("DELETE FROM site_plans WHERE id = ?", (plan_id,))
                raise PlanError(f"Не удалось сохранить файл плана: {e}") from None
            with self._db.transaction() as c:
                c.execute("UPDATE site_plans SET image_file = ? WHERE id = ?",
                          (image_file, plan_id))
                c.execute(
                    "INSERT INTO plan_image_versions "
                    "(plan_id, image_file, image_width, image_height, file_hash, created_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?)",
                    (plan_id, image_file, img_w, img_h, now))

        log.info("Создан план v2: %r (id=%d, kind=%s)", name, plan_id, plan_kind)
        return self.get_by_id(plan_id)

    def replace_image(self, plan_id: int, image_bytes: bytes) -> PlanV2:
        """Новый фон версионируется, старый файл НЕ удаляется (§7.2:
        откат должен быть возможен). Геометрия существующих plan_items
        не пересчитывается — по ТЗ при обрезке/повороте фона нужна
        ручная перепривязка, автоматика здесь не обещается."""
        plan = self.get_by_id(plan_id)
        if plan is None:
            raise PlanError(f"План id={plan_id} не найден")
        img_fmt, img_w, img_h = _decode_and_check_image(image_bytes)

        now = int(time.time())
        ext = "png" if img_fmt == "png" else "jpg"
        versioned_name = f"plan_{int(plan_id)}_v{now}.{ext}"
        os.makedirs(self._dir, exist_ok=True)
        path = os.path.join(self._dir, versioned_name)
        tmp_path = f"{path}.tmp.{os.getpid()}"
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

        with self._db.transaction() as c:
            c.execute(
                "UPDATE site_plans SET image_file = ?, image_width = ?, "
                "image_height = ?, updated_at = ? WHERE id = ?",
                (versioned_name, img_w, img_h, now, plan_id))
            c.execute(
                "INSERT INTO plan_image_versions "
                "(plan_id, image_file, image_width, image_height, file_hash, created_at) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (plan_id, versioned_name, img_w, img_h, now))
        log.info("Заменён фон плана id=%d -> %s", plan_id, versioned_name)
        return self.get_by_id(plan_id)

    def delete(self, plan_id: int) -> bool:
        plan = self.get_by_id(plan_id)
        if plan is None:
            return False
        with self._db.transaction() as c:
            cur = c.execute("DELETE FROM site_plans WHERE id = ?", (plan_id,))
            removed = cur.rowcount > 0
        if removed and plan.image_file:
            delete_plan_image(self._dir, plan.image_file)
        return removed


# ---------------------------------------------------------------------
# plan_items
# ---------------------------------------------------------------------

@dataclass
class PlanItem:
    id: int
    plan_id: int
    kind: str
    point_id: Optional[int]
    location_id: Optional[int]
    group_id: Optional[int]
    node_id: Optional[int]
    target_plan_id: Optional[int]
    geometry: Any
    coord_space: str
    image_version_id: Optional[int]
    label: Optional[str]
    sort_order: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row) -> "PlanItem":
        return cls(
            id=row["id"], plan_id=row["plan_id"], kind=row["kind"],
            point_id=row["point_id"], location_id=row["location_id"],
            group_id=row["group_id"], node_id=row["node_id"],
            target_plan_id=row["target_plan_id"],
            geometry=json.loads(row["geometry"]),
            coord_space=row["coord_space"],
            image_version_id=row["image_version_id"],
            label=row["label"], sort_order=row["sort_order"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "plan_id": self.plan_id, "kind": self.kind,
            "point_id": self.point_id, "location_id": self.location_id,
            "group_id": self.group_id, "node_id": self.node_id,
            "target_plan_id": self.target_plan_id,
            "geometry": self.geometry, "coord_space": self.coord_space,
            "label": self.label, "sort_order": self.sort_order,
        }


def _plan_dims_for_geometry(c, plan_id):
    row = c.execute(
        "SELECT image_width, image_height, canvas_width, canvas_height, "
        "image_file FROM site_plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise PlanError(f"План id={plan_id} не найден")
    if row["image_file"]:
        return row["image_width"], row["image_height"]
    return row["canvas_width"], row["canvas_height"]


class PlanItemRepo:
    def __init__(self, db):
        self._db = db

    def get_by_id(self, item_id: int) -> Optional[PlanItem]:
        with self._db.read() as c:
            row = c.execute("SELECT * FROM plan_items WHERE id = ?",
                             (item_id,)).fetchone()
            return PlanItem.from_row(row) if row else None

    def list_for_plan(self, plan_id: int) -> List[PlanItem]:
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM plan_items WHERE plan_id = ? "
                "ORDER BY sort_order, id", (plan_id,)).fetchall()
            return [PlanItem.from_row(r) for r in rows]

    def add(self, plan_id: int, kind: str, geometry: Any, coord_space: str, *,
            point_id=None, location_id=None, group_id=None, node_id=None,
            target_plan_id=None, label=None, sort_order=0) -> PlanItem:
        if kind not in VALID_PLAN_ITEM_KINDS:
            raise PlanError(f"неизвестный kind: {kind!r}")
        refs = {"point_id": point_id, "location_id": location_id,
                "group_id": group_id, "node_id": node_id}
        given = [k for k, v in refs.items() if v is not None]
        if len(given) > 1:
            raise PlanError(f"plan_item может ссылаться максимум на одно поле: {given}")
        if kind == "port" and node_id is None:
            raise PlanError("kind='port' требует node_id (§7.1 ТЗ)")
        if kind in ("point", "location", "group", "node") and not given:
            raise PlanError(f"kind={kind!r} требует соответствующую ссылку")

        now = int(time.time())
        with self._db.transaction() as c:
            width, height = _plan_dims_for_geometry(c, plan_id)
            plan_geo_v2.validate_plan_item_geometry(geometry, coord_space, width, height)
            try:
                cur = c.execute(
                    "INSERT INTO plan_items (plan_id, kind, point_id, location_id, "
                    "group_id, node_id, target_plan_id, geometry, coord_space, "
                    "image_version_id, label, sort_order, revision_id, archived_at, "
                    "created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,NULL,NULL,?,?)",
                    (plan_id, kind, point_id, location_id, group_id, node_id,
                     target_plan_id, json.dumps(geometry), coord_space,
                     label, sort_order, now, now))
            except sqlite3.IntegrityError as e:
                raise PlanError(f"Не удалось создать элемент плана: {e}") from None
            item_id = cur.lastrowid

            # docs/migration-plan-v2.md §7 п.2: первая предметная запись
            # через доменную модель v2 отмечается В ТОЙ ЖЕ транзакции —
            # см. domain_generation.py. Идемпотентно (не переставляет
            # маркер на повторных вызовах).
            domain_generation.mark_v2_domain_write(c)
        return self.get_by_id(item_id)

    def update_geometry(self, item_id: int, geometry: Any,
                         coord_space: Optional[str] = None) -> PlanItem:
        existing = self.get_by_id(item_id)
        if existing is None:
            raise PlanError(f"Элемент плана id={item_id} не найден")
        cs = coord_space or existing.coord_space
        now = int(time.time())
        with self._db.transaction() as c:
            width, height = _plan_dims_for_geometry(c, existing.plan_id)
            plan_geo_v2.validate_plan_item_geometry(geometry, cs, width, height)
            c.execute(
                "UPDATE plan_items SET geometry = ?, coord_space = ?, "
                "updated_at = ? WHERE id = ?",
                (json.dumps(geometry), cs, now, item_id))
        return self.get_by_id(item_id)

    def remove_from_plan(self, item_id: int) -> bool:
        """§7.3 «Убрать с плана» — удаляет только представление, не
        предметную сущность. Зависимые plan_edge_views на этом же
        плане теряют ссылку (ON DELETE SET NULL в схеме), а не
        удаляются целиком — вызывающий код (API) обязан явно показать
        их список перед удалением узла-представления."""
        with self._db.transaction() as c:
            cur = c.execute("DELETE FROM plan_items WHERE id = ?", (item_id,))
            return cur.rowcount > 0


# ---------------------------------------------------------------------
# plan_edge_views
# ---------------------------------------------------------------------

@dataclass
class PlanEdgeView:
    id: int
    plan_id: int
    edge_id: int
    from_item_id: Optional[int]
    to_item_id: Optional[int]
    waypoints: Any
    view_kind: str
    confirmed_at: Optional[int]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row) -> "PlanEdgeView":
        return cls(
            id=row["id"], plan_id=row["plan_id"], edge_id=row["edge_id"],
            from_item_id=row["from_item_id"], to_item_id=row["to_item_id"],
            waypoints=json.loads(row["waypoints"]) if row["waypoints"] else None,
            view_kind=row["view_kind"], confirmed_at=row["confirmed_at"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "plan_id": self.plan_id, "edge_id": self.edge_id,
            "from_item_id": self.from_item_id, "to_item_id": self.to_item_id,
            "waypoints": self.waypoints, "view_kind": self.view_kind,
            "confirmed_at": self.confirmed_at,
        }


def _check_edge_view_endpoints(c, plan_id, edge_id, from_item_id, to_item_id):
    """§7.1/§7.3 ТЗ: "Сохранение layout проверяет, что from_item_id/
    to_item_id принадлежат этому плану и представляют соответствующие
    концы edge". Конец может быть None ("не размещён" — переход/порт
    на другой план, §7.1) — тогда проверять нечего."""
    edge = c.execute(
        "SELECT from_node_id, to_node_id FROM electrical_edges WHERE id = ?",
        (edge_id,)).fetchone()
    if edge is None:
        raise PlanError(f"Электрическая связь id={edge_id} не найдена")
    for label, item_id, expected_node in (
        ("from_item_id", from_item_id, edge["from_node_id"]),
        ("to_item_id", to_item_id, edge["to_node_id"]),
    ):
        if item_id is None:
            continue
        item = c.execute("SELECT plan_id, node_id FROM plan_items WHERE id = ?",
                          (item_id,)).fetchone()
        if item is None:
            raise PlanError(f"{label}: представление {item_id} не найдено")
        if item["plan_id"] != plan_id:
            raise PlanError(f"{label}: представление принадлежит другому плану")
        if item["node_id"] != expected_node:
            raise PlanError(
                f"{label}: представление (узел {item['node_id']}) не "
                f"соответствует концу связи (ожидался узел {expected_node})")


class PlanEdgeViewRepo:
    def __init__(self, db):
        self._db = db

    def get_by_id(self, view_id: int) -> Optional[PlanEdgeView]:
        with self._db.read() as c:
            row = c.execute("SELECT * FROM plan_edge_views WHERE id = ?",
                             (view_id,)).fetchone()
            return PlanEdgeView.from_row(row) if row else None

    def list_for_plan(self, plan_id: int) -> List[PlanEdgeView]:
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM plan_edge_views WHERE plan_id = ? ORDER BY id",
                (plan_id,)).fetchall()
            return [PlanEdgeView.from_row(r) for r in rows]

    def add(self, plan_id: int, edge_id: int,
            from_item_id: Optional[int], to_item_id: Optional[int],
            waypoints: Optional[Any] = None,
            view_kind: str = "structural") -> PlanEdgeView:
        if view_kind not in VALID_VIEW_KINDS:
            raise PlanError(f"неизвестный view_kind: {view_kind!r}")
        now = int(time.time())
        confirmed_at = now if view_kind == "cable_route" else None
        with self._db.transaction() as c:
            width, height = _plan_dims_for_geometry(c, plan_id)
            coord_space = plan_geo_v2.COORD_SPACE_IMAGE_V2
            row = c.execute("SELECT coord_space FROM plan_items WHERE id = ?",
                             (from_item_id or to_item_id,)).fetchone() \
                if (from_item_id or to_item_id) else None
            if row is not None:
                coord_space = row["coord_space"]
            plan_geo_v2.validate_waypoints_v2(waypoints, coord_space, width, height)
            _check_edge_view_endpoints(c, plan_id, edge_id, from_item_id, to_item_id)
            cur = c.execute(
                "INSERT INTO plan_edge_views (plan_id, edge_id, from_item_id, "
                "to_item_id, waypoints, view_kind, confirmed_at, revision_id, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,NULL,?,?)",
                (plan_id, edge_id, from_item_id, to_item_id,
                 json.dumps(waypoints) if waypoints is not None else None,
                 view_kind, confirmed_at, now, now))
            view_id = cur.lastrowid

            # docs/migration-plan-v2.md §7 п.2: первая предметная запись
            # через доменную модель v2 отмечается В ТОЙ ЖЕ транзакции —
            # см. domain_generation.py. Идемпотентно (не переставляет
            # маркер на повторных вызовах).
            domain_generation.mark_v2_domain_write(c)
        return self.get_by_id(view_id)

    def remove(self, view_id: int) -> bool:
        with self._db.transaction() as c:
            cur = c.execute("DELETE FROM plan_edge_views WHERE id = ?", (view_id,))
            return cur.rowcount > 0


# ---------------------------------------------------------------------
# Атомарное сохранение layout с проверкой ревизии (§7.3 ТЗ, A35)
# ---------------------------------------------------------------------

def save_plan_layout(db, plan_id: int, expected_revision: int, *,
                      item_ops: Optional[List[Dict[str, Any]]] = None,
                      edge_view_ops: Optional[List[Dict[str, Any]]] = None
                      ) -> PlanV2:
    """Применяет пакет изменений (items + edge_views) одной транзакцией
    с оптимистичной блокировкой по `canvas_revision`.

    A35 ТЗ §13: "два браузера редактируют один план" -> конфликт
    ревизии 409, чужие изменения не перезаписаны — при несовпадении
    `expected_revision` бросается RevisionConflict и В ТРАНЗАКЦИИ НЕ
    МЕНЯЕТСЯ НИЧЕГО (проверка — первая операция, до любых записей).

    item_ops / edge_view_ops — список словарей:
      {"op": "upsert", "id": <int|None>, ...поля item/edge_view}
      {"op": "remove", "id": <int>}
    Ссылка edge_view на item, созданный этим же вызовом (ещё не имеющий
    id), задаётся как "$<индекс в item_ops>" вместо числа."""
    item_ops = item_ops or []
    edge_view_ops = edge_view_ops or []

    with db.transaction() as c:
        row = c.execute(
            "SELECT canvas_revision, image_width, image_height, canvas_width, "
            "canvas_height, image_file FROM site_plans WHERE id = ?",
            (plan_id,)).fetchone()
        if row is None:
            raise PlanError(f"План id={plan_id} не найден")
        actual = row["canvas_revision"]
        if actual != expected_revision:
            raise RevisionConflict(plan_id, expected_revision, actual)

        width = row["image_width"] if row["image_file"] else row["canvas_width"]
        height = row["image_height"] if row["image_file"] else row["canvas_height"]

        now = int(time.time())
        item_id_map: Dict[int, int] = {}

        for idx, op in enumerate(item_ops):
            if op.get("op") == "remove":
                c.execute("DELETE FROM plan_items WHERE id = ? AND plan_id = ?",
                          (op["id"], plan_id))
                continue
            geometry = op["geometry"]
            coord_space = op.get("coord_space", plan_geo_v2.COORD_SPACE_IMAGE_V2)
            plan_geo_v2.validate_plan_item_geometry(geometry, coord_space, width, height)
            if op.get("id"):
                c.execute(
                    "UPDATE plan_items SET geometry = ?, coord_space = ?, "
                    "label = ?, sort_order = ?, updated_at = ? "
                    "WHERE id = ? AND plan_id = ?",
                    (json.dumps(geometry), coord_space, op.get("label"),
                     op.get("sort_order", 0), now, op["id"], plan_id))
                item_id_map[idx] = op["id"]
            else:
                kind = op["kind"]
                if kind not in VALID_PLAN_ITEM_KINDS:
                    raise PlanError(f"неизвестный kind: {kind!r}")
                cur = c.execute(
                    "INSERT INTO plan_items (plan_id, kind, point_id, location_id, "
                    "group_id, node_id, target_plan_id, geometry, coord_space, "
                    "image_version_id, label, sort_order, revision_id, archived_at, "
                    "created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,NULL,NULL,?,?)",
                    (plan_id, kind, op.get("point_id"), op.get("location_id"),
                     op.get("group_id"), op.get("node_id"), op.get("target_plan_id"),
                     json.dumps(geometry), coord_space, op.get("label"),
                     op.get("sort_order", 0), now, now))
                item_id_map[idx] = cur.lastrowid

        def _resolve(ref):
            if isinstance(ref, str) and ref.startswith("$"):
                return item_id_map[int(ref[1:])]
            return ref

        for op in edge_view_ops:
            if op.get("op") == "remove":
                c.execute("DELETE FROM plan_edge_views WHERE id = ? AND plan_id = ?",
                          (op["id"], plan_id))
                continue
            from_item_id = _resolve(op.get("from_item_id"))
            to_item_id = _resolve(op.get("to_item_id"))
            edge_id = op["edge_id"]
            view_kind = op.get("view_kind", "structural")
            if view_kind not in VALID_VIEW_KINDS:
                raise PlanError(f"неизвестный view_kind: {view_kind!r}")
            _check_edge_view_endpoints(c, plan_id, edge_id, from_item_id, to_item_id)
            waypoints = op.get("waypoints")
            wp_coord_space = plan_geo_v2.COORD_SPACE_IMAGE_V2
            wp_row = c.execute("SELECT coord_space FROM plan_items WHERE id = ?",
                                (from_item_id or to_item_id,)).fetchone() \
                if (from_item_id or to_item_id) else None
            if wp_row is not None:
                wp_coord_space = wp_row["coord_space"]
            plan_geo_v2.validate_waypoints_v2(waypoints, wp_coord_space, width, height)
            if op.get("id"):
                c.execute(
                    "UPDATE plan_edge_views SET from_item_id = ?, to_item_id = ?, "
                    "waypoints = ?, view_kind = ?, updated_at = ? "
                    "WHERE id = ? AND plan_id = ?",
                    (from_item_id, to_item_id,
                     json.dumps(waypoints) if waypoints is not None else None,
                     view_kind, now, op["id"], plan_id))
            else:
                confirmed_at = now if view_kind == "cable_route" else None
                c.execute(
                    "INSERT INTO plan_edge_views (plan_id, edge_id, from_item_id, "
                    "to_item_id, waypoints, view_kind, confirmed_at, revision_id, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,NULL,?,?)",
                    (plan_id, edge_id, from_item_id, to_item_id,
                     json.dumps(waypoints) if waypoints is not None else None,
                     view_kind, confirmed_at, now, now))

        new_revision = actual + 1
        c.execute("UPDATE site_plans SET canvas_revision = ?, updated_at = ? "
                  "WHERE id = ?", (new_revision, now, plan_id))

    with db.read() as c:
        row = c.execute("SELECT * FROM site_plans WHERE id = ?", (plan_id,)).fetchone()
    return PlanV2.from_row(row)
