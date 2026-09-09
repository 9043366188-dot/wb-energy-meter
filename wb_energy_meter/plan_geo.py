"""Координаты плана: пиксели <-> Leaflet CRS.Simple, валидация геометрии.

Договорённость проекта (ТЗ v0.11.0, §5): координаты хранятся в БД в
пикселях исходного изображения, ось Y — вниз от левого верхнего угла
(как в самой картинке). Leaflet в режиме `CRS.Simple` с границами
`[[0,0],[image_height,image_width]]` (см. официальный пример
crs-simple.html) кладёт `lat` на строку изображения, `lng` — на столбец,
БЕЗ переворота оси — то есть точка `[lat, lng]` в Leaflet и есть `[y, x]`
в пикселях исходной картинки. Отсюда — специально заведённые функции
`pixel_to_leaflet`/`leaflet_to_pixel`, чтобы место, где выбрана
конвенция, было ровно одно и было покрыто тестом (в ТЗ явно предупреждают
"на этом легко ошибиться").
"""

from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Tuple

Point = Tuple[float, float]


def pixel_to_leaflet(x: float, y: float) -> List[float]:
    """(x, y) в пикселях исходного изображения -> [lat, lng] Leaflet
    (CRS.Simple с bounds [[0,0],[height,width]]) — это [y, x]."""
    return [float(y), float(x)]


def leaflet_to_pixel(point: Sequence[float]) -> Point:
    """[lat, lng] Leaflet -> (x, y) в пикселях исходного изображения."""
    if len(point) != 2:
        raise ValueError("Точка должна быть парой [lat, lng]")
    lat, lng = point
    return float(lng), float(lat)


def _is_finite_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    return math.isfinite(v)


def _validate_point(pt: Any, width: int, height: int, what: str) -> None:
    if (not isinstance(pt, (list, tuple))) or len(pt) != 2:
        raise ValueError(f"{what}: точка должна быть массивом из двух чисел [y, x]")
    y, x = pt
    if not _is_finite_number(y) or not _is_finite_number(x):
        raise ValueError(f"{what}: координаты должны быть конечными числами")
    if not (0 <= y <= height):
        raise ValueError(
            f"{what}: y={y} вне границ изображения [0, {height}]")
    if not (0 <= x <= width):
        raise ValueError(
            f"{what}: x={x} вне границ изображения [0, {width}]")


def validate_geometry(geometry: Any, shape_type: str,
                      width: int, height: int) -> None:
    """Бросает ValueError с русским текстом при любой проблеме.
    Ничего не возвращает — вызывающий код при ошибке ничего не пишет в БД."""
    shape_type = (shape_type or "polygon").strip().lower()
    if shape_type == "marker":
        _validate_point(geometry, width, height, "geometry")
        return
    if shape_type != "polygon":
        raise ValueError(f"Неизвестный shape_type: {shape_type!r}")
    if not isinstance(geometry, (list, tuple)):
        raise ValueError("geometry должна быть массивом точек")
    if len(geometry) < 3:
        raise ValueError(
            "Контур зоны должен содержать не меньше 3 точек")
    for idx, pt in enumerate(geometry):
        _validate_point(pt, width, height, f"geometry[{idx}]")


def validate_anchor(anchor: Optional[Any], width: int, height: int) -> None:
    if anchor is None:
        return
    _validate_point(anchor, width, height, "anchor")


def validate_waypoints(waypoints: Optional[Any], width: int, height: int) -> None:
    if waypoints is None:
        return
    if not isinstance(waypoints, (list, tuple)):
        raise ValueError("waypoints должны быть массивом точек")
    for idx, pt in enumerate(waypoints):
        _validate_point(pt, width, height, f"waypoints[{idx}]")


def validate_rated_current(value: Optional[Any]) -> Optional[float]:
    """None -> None (не задан). Иначе — конечное положительное число."""
    if value is None:
        return None
    if not _is_finite_number(value):
        raise ValueError("rated_current_a должен быть конечным числом")
    v = float(value)
    if v <= 0:
        raise ValueError("rated_current_a должен быть положительным числом")
    return v
