"""Координаты плана v2 (ТЗ docs/TZ-metering-architecture-dashboard.md §7.2).

ОТДЕЛЬНО от wb_energy_meter/plan_geo.py — не путать, не "улучшать"
старый модуль на месте. Причина: §7.2 явно предупреждает "Legacy не
переворачивать по комментариям" — старый код (`pixel_to_leaflet`)
кладёт `lat=y` БЕЗ переворота оси (см. его собственный docstring), а
новый канон v2 требует ПЕРЕВОРОТ: `lat=H-y`. Проверка "по факту, как
рисует текущий JS" (угловые метки на асимметичном тестовом
изображении, требуемая ТЗ до переноса реальных данных) в рамках этой
правки НЕ проводилась — это чисто бэкенд-слой (репозитории/API),
фронтенд-редактор ещё не подключён. Поэтому здесь заведён явный
`coord_space` dispatch (`to_leaflet`/`from_leaflet`), а не тихая
замена: `legacy_leaflet_yx_v1` продолжает использовать НЕТРОНУТЫЙ
`plan_geo.py`, новые `image_px_xy_v2`/`canvas_xy_v2` — формулу ниже.
Когда до реального Leaflet-редактора дойдёт очередь, эмпирическую
проверку по четырём угловым меткам нужно провести до того, как на
этих формулах будут построены пользовательские данные.

Канон v2 (ТЗ §7.2): `image_px_xy_v2` — `{x,y}` в пикселях исходного
изображения, начало слева сверху, x вправо, y вниз (как в самом
изображении). Для CRS.Simple с обычным ImageOverlay и
`bounds=[[0,0],[H,W]]`:

    lat = H - y      lng = x
    x = lng          y = H - lat

`canvas_xy_v2` (пустая однолинейная схема) использует ту же формулу,
подставляя логические `canvas_width/canvas_height` вместо W/H
изображения — координаты не меняются при расширении холста (§7.2:
"расширение холста не меняет сохранённые x/y").
"""

from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Tuple

Point = Tuple[float, float]

COORD_SPACE_IMAGE_V2 = "image_px_xy_v2"
COORD_SPACE_CANVAS_V2 = "canvas_xy_v2"
COORD_SPACE_LEGACY = "legacy_leaflet_yx_v1"
VALID_COORD_SPACES = (COORD_SPACE_IMAGE_V2, COORD_SPACE_CANVAS_V2, COORD_SPACE_LEGACY)

_V2_SPACES = (COORD_SPACE_IMAGE_V2, COORD_SPACE_CANVAS_V2)


def xy_v2_to_leaflet(x: float, y: float, height: float) -> List[float]:
    """{x,y} v2 -> [lat,lng] Leaflet. lat=H-y, lng=x."""
    return [float(height) - float(y), float(x)]


def leaflet_to_xy_v2(point: Sequence[float], height: float) -> Point:
    """[lat,lng] Leaflet -> (x,y) v2. x=lng, y=H-lat."""
    if len(point) != 2:
        raise ValueError("Точка должна быть парой [lat, lng]")
    lat, lng = point
    return float(lng), float(height) - float(lat)


def to_leaflet(coord_space: str, x: float, y: float, height: float) -> List[float]:
    """Единая точка входа с dispatch по coord_space (ТЗ §7.2: "единое
    место преобразования") — вызывающий код никогда не выбирает
    формулу переворота вручную."""
    if coord_space in _V2_SPACES:
        return xy_v2_to_leaflet(x, y, height)
    if coord_space == COORD_SPACE_LEGACY:
        from . import plan_geo
        return plan_geo.pixel_to_leaflet(x, y)
    raise ValueError(f"неизвестный coord_space: {coord_space!r}")


def from_leaflet(coord_space: str, point: Sequence[float], height: float) -> Point:
    if coord_space in _V2_SPACES:
        return leaflet_to_xy_v2(point, height)
    if coord_space == COORD_SPACE_LEGACY:
        from . import plan_geo
        return plan_geo.leaflet_to_pixel(point)
    raise ValueError(f"неизвестный coord_space: {coord_space!r}")


def _is_finite_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    return math.isfinite(v)


def _validate_xy_obj(pt: Any, width: Optional[float], height: Optional[float],
                      what: str) -> None:
    """{"x":..,"y":..} — форма geometry для одиночной точки (marker/port/
    annotation-точка) в v2 (schema: `geometry TEXT -- JSON: {x,y} | [[x,y],...]`)."""
    if not isinstance(pt, dict) or "x" not in pt or "y" not in pt:
        raise ValueError(f'{what}: точка должна быть объектом {{"x":.., "y":..}}')
    x, y = pt["x"], pt["y"]
    if not _is_finite_number(x) or not _is_finite_number(y):
        raise ValueError(f"{what}: координаты должны быть конечными числами")
    if width is not None and not (0 <= x <= width):
        raise ValueError(f"{what}: x={x} вне границ [0, {width}]")
    if height is not None and not (0 <= y <= height):
        raise ValueError(f"{what}: y={y} вне границ [0, {height}]")


def _validate_xy_pair(pt: Any, width: Optional[float], height: Optional[float],
                       what: str) -> None:
    """[x, y] — форма точки внутри путей (waypoints/полигон), список из
    двух чисел, а не объект (см. plan_edge_views.waypoints: JSON [[x,y],...])."""
    if (not isinstance(pt, (list, tuple))) or len(pt) != 2:
        raise ValueError(f"{what}: точка должна быть массивом [x, y]")
    x, y = pt
    if not _is_finite_number(x) or not _is_finite_number(y):
        raise ValueError(f"{what}: координаты должны быть конечными числами")
    if width is not None and not (0 <= x <= width):
        raise ValueError(f"{what}: x={x} вне границ [0, {width}]")
    if height is not None and not (0 <= y <= height):
        raise ValueError(f"{what}: y={y} вне границ [0, {height}]")


def validate_plan_item_geometry(geometry: Any, coord_space: str,
                                 width: Optional[float], height: Optional[float]) -> None:
    """Валидация geometry для plan_item: всегда одиночная точка {x,y}
    (полигоны мест — это location, не отдельная геометрия plan_item в
    v2; контуры мест/зон представлены тем же point-item с меткой —
    полноценные полигон-контуры мест не входят в этот срез, см. §7.1
    "Контур места" как отдельное будущее действие редактора)."""
    if coord_space not in VALID_COORD_SPACES:
        raise ValueError(f"неизвестный coord_space: {coord_space!r}")
    _validate_xy_obj(geometry, width, height, "geometry")


def validate_waypoints_v2(waypoints: Optional[Any], coord_space: str,
                           width: Optional[float], height: Optional[float]) -> None:
    if waypoints is None:
        return
    if coord_space not in VALID_COORD_SPACES:
        raise ValueError(f"неизвестный coord_space: {coord_space!r}")
    if not isinstance(waypoints, (list, tuple)):
        raise ValueError("waypoints должны быть массивом точек [x,y]")
    for idx, pt in enumerate(waypoints):
        _validate_xy_pair(pt, width, height, f"waypoints[{idx}]")
