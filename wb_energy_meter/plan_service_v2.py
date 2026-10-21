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
- предел декодированных размеров: длинная сторона <=8192px (предел
  площади 24 Мп снят решением R16, 20.10.2026);
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

from . import change_journal, domain_generation, plan_geo_v2
from .image_meta import parse_image_size, ImageFormatError
from .plan_repo import (
    MAX_UPLOAD_BYTES, PlanError,
    save_plan_image, delete_plan_image,
)

log = logging.getLogger(__name__)

VALID_PLAN_KINDS = ("floor", "single_line")
VALID_PLAN_ITEM_KINDS = ("point", "location", "group", "node", "port", "annotation")
VALID_VIEW_KINDS = ("structural", "cable_route")

# §7.2 ТЗ задавало предел декодированных размеров: длинная сторона
# <=8192 px и площадь <=24 Мп. Решение Кира R16 (20.10.2026): предел
# площади снят — план 7200×4800 (35 Мп) отклонялся без причины: сервер
# картинку не декодирует (только читает размеры из заголовка), а браузер
# такой план показывает. Длинная сторона 8192 px остаётся: больше —
# предел текстуры видеокарты на многих устройствах, такая подложка просто
# не отрисуется. Картинки больше браузер уменьшает сам перед загрузкой
# («План v3», plan-v3.js::_planV3FitImage), так что для человека
# ограничения нет. Площадь теперь ограничена только стороной.
MAX_DECODED_LONG_SIDE = 8192
MAX_DECODED_MEGAPIXELS = MAX_DECODED_LONG_SIDE * MAX_DECODED_LONG_SIDE


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


# Соотношение сторон считается «тем же», если отличается не больше чем на 1 %
# (картинку масштабируют с округлением до целых пикселей).
ASPECT_TOLERANCE = 0.01
IMAGE_CHANGE_MODES = ("keep", "scale")


class ReanchorRequired(PlanError):
    """Соотношение сторон нового фона другое: пропорциональный пересчёт значков
    невозможен, нужна ручная перепривязка (A30)."""


def _check_mode(mode: str) -> None:
    if mode not in IMAGE_CHANGE_MODES:
        raise PlanError("mode должен быть keep или scale")


def classify_image_change(old_w, old_h, new_w, new_h) -> dict:
    """`same` — размеры те же; `scalable` — другое разрешение, то же
    соотношение сторон (значки можно пересчитать пропорционально);
    `reanchor` — обрезан/повёрнут/другой формат: нужна перепривязка."""
    sx, sy = new_w / old_w, new_h / old_h
    if new_w == old_w and new_h == old_h:
        compat = "same"
    elif abs(sx / sy - 1.0) <= ASPECT_TOLERANCE:
        compat = "scalable"
    else:
        compat = "reanchor"
    return {"compat": compat, "scale_x": round(sx, 6), "scale_y": round(sy, 6)}


def _geometry_points(geom):
    """Точки геометрии элемента: {x,y} или полигон [[x,y],...]."""
    if isinstance(geom, dict):
        return [(geom.get("x"), geom.get("y"))]
    if isinstance(geom, (list, tuple)):
        return [(p[0], p[1]) for p in geom if isinstance(p, (list, tuple)) and len(p) == 2]
    return []


def _geometry_inside(geom, width, height) -> bool:
    for x, y in _geometry_points(geom):
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return False
        if not (0 <= x <= width and 0 <= y <= height):
            return False
    return True


def _scale_value(v, k, limit):
    return min(float(limit), max(0.0, round(float(v) * k, 2)))


def _scale_geometry(geom, sx, sy, width, height):
    if isinstance(geom, dict):
        return {**geom, "x": _scale_value(geom["x"], sx, width),
                "y": _scale_value(geom["y"], sy, height)}
    return [[_scale_value(p[0], sx, width), _scale_value(p[1], sy, height)] for p in geom]


def _scale_plan_content(c, plan_id, sx, sy, width, height):
    """Пересчёт положений значков и изломов трасс плана (в транзакции замены
    фона). Возвращает (сколько значков, сколько трасс изменено)."""
    items = 0
    for r in c.execute("SELECT id, geometry FROM plan_items WHERE plan_id = ?",
                       (plan_id,)).fetchall():
        g = _scale_geometry(json.loads(r["geometry"]), sx, sy, width, height)
        c.execute("UPDATE plan_items SET geometry = ? WHERE id = ?",
                  (json.dumps(g), r["id"]))
        items += 1
    views = 0
    for r in c.execute("SELECT id, waypoints FROM plan_edge_views "
                       "WHERE plan_id = ? AND waypoints IS NOT NULL",
                       (plan_id,)).fetchall():
        pts = json.loads(r["waypoints"])
        if not pts:
            continue
        c.execute("UPDATE plan_edge_views SET waypoints = ? WHERE id = ?",
                  (json.dumps([[_scale_value(p[0], sx, width),
                                _scale_value(p[1], sy, height)] for p in pts]), r["id"]))
        views += 1
    return items, views


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

    def update(self, plan_id: int, *, name: Optional[str] = None,
               is_default: Optional[bool] = None) -> PlanV2:
        """Партия 11, этап 11.5: переименование плана и смена «плана по
        умолчанию». `None` у аргумента = «не менять» (имя `null` в JSON
        обработчик отсекает до вызова — оно не равно «не менять»).

        `plan_kind` и размеры/изображение здесь НЕ меняются: смена вида
        (floor ↔ single_line) сделала бы недействительными `plan_items`/
        `plan_edge_views` с `coord_space` прежнего вида, а фон версионируется
        отдельным `replace_image`. Поэтому метод и не принимает эти поля.

        `is_default=True` делает план единственным по умолчанию: у остальных
        флаг снимается в той же транзакции (нет окна «два плана по умолчанию»
        и нет окна «ни одного» между двумя запросами, как было бы при
        последовательных вызовах); `is_default=False` снимает флаг только с
        этого плана (как v1 `SitePlanRepo.update` — «по умолчанию» нет совсем,
        интерфейс тогда показывает первый план списка)."""
        validated_name = _validate_plan_name(name) if name is not None else None
        if is_default is not None and not isinstance(is_default, bool):
            raise PlanError("is_default должен быть true или false")
        now = int(time.time())
        with self._db.transaction() as c:
            row = c.execute("SELECT id FROM site_plans WHERE id = ?",
                            (plan_id,)).fetchone()
            if row is None:
                raise PlanError(f"План id={plan_id} не найден")
            if is_default:
                c.execute("UPDATE site_plans SET is_default = 0 "
                          "WHERE is_default = 1 AND id != ?", (plan_id,))
            sets, params = [], []
            if validated_name is not None:
                sets.append("name = ?"); params.append(validated_name)
            if is_default is not None:
                sets.append("is_default = ?"); params.append(1 if is_default else 0)
            if sets:
                sets.append("updated_at = ?"); params.append(now)
                params.append(plan_id)
                c.execute(f"UPDATE site_plans SET {', '.join(sets)} WHERE id = ?",
                          tuple(params))
        log.info("Обновлён план v2 id=%d: name=%r is_default=%r",
                 plan_id, validated_name, is_default)
        return self.get_by_id(plan_id)

    # --- Фон плана: предпросмотр, замена, версии, возврат (A30) -------------

    def _plan_dims(self, plan: PlanV2):
        """Размеры, в которых стоят значки: картинка, а без неё — холст."""
        if plan.image_file:
            return plan.image_width, plan.image_height
        return plan.canvas_width, plan.canvas_height

    def preview_image(self, plan_id: int, image_bytes: bytes) -> dict:
        """Разбор новой картинки БЕЗ записи: во что превратится план.
        Ответ: `compat` (`same` | `scalable` | `reanchor`), коэффициенты,
        сколько значков окажется за краем картинки, если оставить их на
        месте (`mode=keep`), и сколько останется внутри при масштабе."""
        plan = self.get_by_id(plan_id)
        if plan is None:
            raise PlanError(f"План id={plan_id} не найден")
        _fmt, new_w, new_h = _decode_and_check_image(image_bytes)
        return self._describe_change(plan, new_w, new_h)

    def _describe_change(self, plan: PlanV2, new_w: int, new_h: int) -> dict:
        old_w, old_h = self._plan_dims(plan)
        info = classify_image_change(old_w, old_h, new_w, new_h)
        geoms = []
        with self._db.read() as c:
            for r in c.execute("SELECT geometry FROM plan_items WHERE plan_id = ?",
                               (plan.id,)).fetchall():
                geoms.append(json.loads(r["geometry"]))
        outside = sum(1 for g in geoms if not _geometry_inside(g, new_w, new_h))
        info.update({
            "old": {"width": old_w, "height": old_h},
            "new": {"width": new_w, "height": new_h},
            "items_total": len(geoms),
            # если значки оставить на прежних пикселях (mode=keep)
            "items_outside_if_kept": outside,
        })
        return info

    def replace_image(self, plan_id: int, image_bytes: bytes,
                      mode: str = "keep") -> PlanV2:
        """Новый фон версионируется, старый файл НЕ удаляется (§7.2:
        откат возможен). `mode`:
        - `keep` (по умолчанию) — значки остаются на прежних пикселях; так
          делается, когда фон обрезан/повёрнут и значки нужно перепривязать
          вручную (A30), автоматика здесь не обещается;
        - `scale` — только при том же соотношении сторон (`compat` =
          `scalable`): положения значков и изломов трасс пересчитываются
          пропорционально, значки остаются на тех же местах рисунка."""
        plan = self.get_by_id(plan_id)
        if plan is None:
            raise PlanError(f"План id={plan_id} не найден")
        img_fmt, img_w, img_h = _decode_and_check_image(image_bytes)
        _check_mode(mode)
        if mode == "scale":
            info = self._describe_change(plan, img_w, img_h)
            if info["compat"] == "reanchor":
                raise ReanchorRequired(
                    "соотношение сторон новой картинки другое: пропорционально "
                    "пересчитать значки нельзя — оставьте их на месте и "
                    "перепривязайте вручную (mode=keep)")

        now = int(time.time())
        ext = "png" if img_fmt == "png" else "jpg"
        os.makedirs(self._dir, exist_ok=True)
        # Имя уникально и внутри одной секунды: две замены подряд не должны
        # затирать файл предыдущей версии (иначе её нельзя вернуть).
        n = 0
        while True:
            versioned_name = f"plan_{int(plan_id)}_v{now}{'' if n == 0 else '_' + str(n)}.{ext}"
            path = os.path.join(self._dir, versioned_name)
            if not os.path.exists(path):
                break
            n += 1
        tmp_path = f"{path}.tmp.{os.getpid()}"
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

        with self._db.transaction() as c:
            old_w, old_h = self._plan_dims(plan)
            scaled = 0
            if mode == "scale":
                scaled = _scale_plan_content(c, plan_id, img_w / old_w, img_h / old_h,
                                             img_w, img_h)
            c.execute(
                "UPDATE site_plans SET image_file = ?, image_width = ?, "
                "image_height = ?, canvas_revision = canvas_revision + 1, "
                "updated_at = ? WHERE id = ?",
                (versioned_name, img_w, img_h, now, plan_id))
            c.execute(
                "INSERT INTO plan_image_versions "
                "(plan_id, image_file, image_width, image_height, file_hash, created_at) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (plan_id, versioned_name, img_w, img_h, now))
            if scaled and (scaled[0] or scaled[1]):
                change_journal.record(
                    c, "plan_layout", plan_id, "update",
                    new={"items": {"added": 0, "updated": scaled[0], "removed": 0},
                         "edge_views": {"added": 0, "updated": scaled[1], "removed": 0},
                         "image_scale": [round(img_w / old_w, 6), round(img_h / old_h, 6)]})
        log.info("Заменён фон плана id=%d -> %s (mode=%s)", plan_id, versioned_name, mode)
        return self.get_by_id(plan_id)

    def list_image_versions(self, plan_id: int) -> List[dict]:
        """Версии фона, от новых к старым. `is_current` — какая сейчас на плане;
        у остальных — как изменится план при возврате (`compat`, коэффициенты,
        `items_outside_if_kept`), чтобы показать это до подтверждения."""
        plan = self.get_by_id(plan_id)
        if plan is None:
            raise PlanError(f"План id={plan_id} не найден")
        with self._db.read() as c:
            rows = c.execute(
                "SELECT id, image_file, image_width, image_height, created_at "
                "FROM plan_image_versions WHERE plan_id = ? "
                "ORDER BY id DESC", (plan_id,)).fetchall()
        out = []
        for r in rows:
            cur = r["image_file"] == plan.image_file
            item = {"id": r["id"], "image_width": r["image_width"],
                    "image_height": r["image_height"],
                    "created_at": r["created_at"], "is_current": cur}
            if not cur:
                item.update(self._describe_change(plan, r["image_width"], r["image_height"]))
            out.append(item)
        return out

    def image_version_file(self, plan_id: int, version_id: int) -> Optional[str]:
        with self._db.read() as c:
            row = c.execute(
                "SELECT image_file FROM plan_image_versions WHERE id = ? AND plan_id = ?",
                (version_id, plan_id)).fetchone()
        return row["image_file"] if row else None

    def restore_image(self, plan_id: int, version_id: int,
                      mode: str = "keep") -> PlanV2:
        """Вернуть прежний фон. Новая версия не создаётся — текущей становится
        выбранная. Значки обрабатываются по тем же правилам, что при замене
        (`keep` — не двигать, `scale` — пропорционально при том же соотношении
        сторон): если фон возвращают после обрезки, значки, перепривязанные
        под обрезанный вариант, придётся перепривязать снова."""
        plan = self.get_by_id(plan_id)
        if plan is None:
            raise PlanError(f"План id={plan_id} не найден")
        _check_mode(mode)
        with self._db.read() as c:
            row = c.execute(
                "SELECT image_file, image_width, image_height FROM plan_image_versions "
                "WHERE id = ? AND plan_id = ?", (version_id, plan_id)).fetchone()
        if row is None:
            raise PlanError(f"Версия фона {version_id} не найдена у плана {plan_id}")
        new_w, new_h = row["image_width"], row["image_height"]
        if mode == "scale":
            info = self._describe_change(plan, new_w, new_h)
            if info["compat"] == "reanchor":
                raise ReanchorRequired(
                    "соотношение сторон прежнего фона другое: пропорционально "
                    "пересчитать значки нельзя — верните фон с mode=keep и "
                    "перепривязайте значки вручную")
        now = int(time.time())
        with self._db.transaction() as c:
            old_w, old_h = self._plan_dims(plan)
            scaled = 0
            if mode == "scale":
                scaled = _scale_plan_content(c, plan_id, new_w / old_w, new_h / old_h,
                                             new_w, new_h)
            c.execute(
                "UPDATE site_plans SET image_file = ?, image_width = ?, "
                "image_height = ?, canvas_revision = canvas_revision + 1, "
                "updated_at = ? WHERE id = ?",
                (row["image_file"], new_w, new_h, now, plan_id))
            if scaled and (scaled[0] or scaled[1]):
                change_journal.record(
                    c, "plan_layout", plan_id, "update",
                    new={"items": {"added": 0, "updated": scaled[0], "removed": 0},
                         "edge_views": {"added": 0, "updated": scaled[1], "removed": 0},
                         "image_scale": [round(new_w / old_w, 6), round(new_h / old_h, 6)]})
        log.info("Возвращён фон плана id=%d: версия %d (mode=%s)", plan_id, version_id, mode)
        return self.get_by_id(plan_id)

    def delete(self, plan_id: int) -> bool:
        plan = self.get_by_id(plan_id)
        if plan is None:
            return False
        with self._db.read() as c:
            files = {r["image_file"] for r in c.execute(
                "SELECT image_file FROM plan_image_versions WHERE plan_id = ?",
                (plan_id,)).fetchall()}
        if plan.image_file:
            files.add(plan.image_file)
        with self._db.transaction() as c:
            cur = c.execute("DELETE FROM site_plans WHERE id = ?", (plan_id,))
            removed = cur.rowcount > 0
        if removed:
            for name in files:
                delete_plan_image(self._dir, name)
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


def _journal_plan_change(c, plan_id: int, kind: str, op: str) -> None:
    """Журнал изменений (партия 12): одиночная правка элемента или связи на
    плане — запись `plan_layout` (покоординатно раскладка не журналируется).
    `kind` — "items" | "edge_views", `op` — "added" | "updated" | "removed"."""
    counts = {"items": {"added": 0, "updated": 0, "removed": 0},
              "edge_views": {"added": 0, "updated": 0, "removed": 0}}
    counts[kind][op] = 1
    change_journal.record(c, "plan_layout", plan_id, "update", new=counts)


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
            plan_geo_v2.validate_plan_item_geometry(
                geometry, coord_space, width, height, kind=kind)
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
            _journal_plan_change(c, plan_id, "items", "added")

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
            plan_geo_v2.validate_plan_item_geometry(
                geometry, cs, width, height, kind=existing.kind)
            c.execute(
                "UPDATE plan_items SET geometry = ?, coord_space = ?, "
                "updated_at = ? WHERE id = ?",
                (json.dumps(geometry), cs, now, item_id))
            _journal_plan_change(c, existing.plan_id, "items", "updated")
        return self.get_by_id(item_id)

    def list_for_node(self, node_id: int) -> List[PlanItem]:
        """Значки узла на всех планах (партия 13, П1: архивация узла убирает
        их со всех планов — это представление, не предметная сущность)."""
        with self._db.read() as c:
            rows = c.execute(
                "SELECT * FROM plan_items WHERE node_id = ? ORDER BY id",
                (node_id,)).fetchall()
            return [PlanItem.from_row(r) for r in rows]

    def remove_from_plan(self, item_id: int, with_edge_views: bool = False) -> bool:
        """§7.3 «Убрать с плана» — удаляет только представление, не
        предметную сущность. Зависимые plan_edge_views на этом же
        плане теряют ссылку (ON DELETE SET NULL в схеме), а не
        удаляются целиком — вызывающий код (API) обязан явно показать
        их список перед удалением узла-представления.

        with_edge_views=True (партия 13, П1) — вместе со значком убрать с
        этого плана и нарисованные к нему линии (только их изображение на
        плане; сами линии в схеме остаются). Иначе линия с оборванным
        концом никогда не нарисуется снова, даже если узел разместят
        заново. Одной транзакцией."""
        with self._db.transaction() as c:
            row = c.execute("SELECT plan_id FROM plan_items WHERE id = ?",
                            (item_id,)).fetchone()
            if row is not None and with_edge_views:
                vcur = c.execute(
                    "DELETE FROM plan_edge_views WHERE plan_id = ? AND "
                    "(from_item_id = ? OR to_item_id = ?)",
                    (row["plan_id"], item_id, item_id))
                if vcur.rowcount > 0:
                    _journal_plan_change(c, row["plan_id"], "edge_views", "removed")
            cur = c.execute("DELETE FROM plan_items WHERE id = ?", (item_id,))
            if row is not None and cur.rowcount > 0:
                _journal_plan_change(c, row["plan_id"], "items", "removed")
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
            _journal_plan_change(c, plan_id, "edge_views", "added")

            # docs/migration-plan-v2.md §7 п.2: первая предметная запись
            # через доменную модель v2 отмечается В ТОЙ ЖЕ транзакции —
            # см. domain_generation.py. Идемпотентно (не переставляет
            # маркер на повторных вызовах).
            domain_generation.mark_v2_domain_write(c)
        return self.get_by_id(view_id)

    def remove_for_edge(self, edge_id: int) -> int:
        """Убрать изображение линии со ВСЕХ планов (партия 13, П1: линию
        вывели из работы — на планах она больше не рисуется; сама связь и
        её история остаются). Возвращает число убранных изображений."""
        removed = 0
        with self._db.transaction() as c:
            rows = c.execute(
                "SELECT id, plan_id FROM plan_edge_views WHERE edge_id = ?",
                (edge_id,)).fetchall()
            for r in rows:
                c.execute("DELETE FROM plan_edge_views WHERE id = ?", (r["id"],))
                _journal_plan_change(c, r["plan_id"], "edge_views", "removed")
                removed += 1
        return removed

    def remove(self, view_id: int) -> bool:
        with self._db.transaction() as c:
            row = c.execute("SELECT plan_id FROM plan_edge_views WHERE id = ?",
                            (view_id,)).fetchone()
            cur = c.execute("DELETE FROM plan_edge_views WHERE id = ?", (view_id,))
            if row is not None and cur.rowcount > 0:
                _journal_plan_change(c, row["plan_id"], "edge_views", "removed")
            return cur.rowcount > 0


# ---------------------------------------------------------------------
# Атомарное сохранение layout с проверкой ревизии (§7.3 ТЗ, A35)
# ---------------------------------------------------------------------

def _layout_counts(ops: List[Dict[str, Any]]) -> Dict[str, int]:
    """Счётчики пакета раскладки для журнала: добавлено / изменено / удалено."""
    out = {"added": 0, "updated": 0, "removed": 0}
    for op in ops:
        if op.get("op") == "remove":
            out["removed"] += 1
        elif op.get("id"):
            out["updated"] += 1
        else:
            out["added"] += 1
    return out


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
            if op.get("id"):
                existing_row = c.execute(
                    "SELECT kind FROM plan_items WHERE id = ? AND plan_id = ?",
                    (op["id"], plan_id)).fetchone()
                item_kind = existing_row["kind"] if existing_row else None
                plan_geo_v2.validate_plan_item_geometry(
                    geometry, coord_space, width, height, kind=item_kind)
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
                plan_geo_v2.validate_plan_item_geometry(
                    geometry, coord_space, width, height, kind=kind)
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
        # Журнал изменений (партия 12): раскладка покоординатно не пишется,
        # одна запись на сохранение — что и сколько изменено.
        change_journal.record(
            c, "plan_layout", plan_id, "update",
            old={"canvas_revision": actual},
            new={"canvas_revision": new_revision,
                 "items": _layout_counts(item_ops),
                 "edge_views": _layout_counts(edge_view_ops)})

    with db.read() as c:
        row = c.execute("SELECT * FROM site_plans WHERE id = ?", (plan_id,)).fetchone()
    return PlanV2.from_row(row)
