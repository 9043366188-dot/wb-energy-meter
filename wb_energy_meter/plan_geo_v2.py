"""Координаты плана v2 (ТЗ docs/TZ-metering-architecture-dashboard.md §7.2).

ОТДЕЛЬНО от wb_energy_meter/plan_geo.py — не путать, не "улучшать"
старый модуль на месте. Причина: §7.2 явно предупреждает "Legacy не
переворачивать по комментариям" — старый код (`pixel_to_leaflet`)
кладёт `lat=y` БЕЗ переворота оси (см. его собственный docstring), а
новый канон v2 требует ПЕРЕВОРОТ: `lat=H-y`. Поэтому здесь заведён
явный `coord_space` dispatch (`to_leaflet`/`from_leaflet`), а не тихая
замена: `legacy_leaflet_yx_v1` продолжает использовать НЕТРОНУТЫЙ
`plan_geo.py`, новые `image_px_xy_v2`/`canvas_xy_v2` — формулу ниже.

Эмпирическая проверка "по факту, как рисует текущий JS" (угловые
метки на асимметичном тестовом изображении, требуемая ТЗ до переноса
пользовательских данных) ПРОВЕДЕНА: реальный `wb_energy_meter/static/
vendor/leaflet.js` (тот же файл, что грузит index.html) прогнан в
headless Node (jsdom) с `L.CRS.Simple`, `L.imageOverlay(bounds=
[[0,0],[H,W]])`, `map.fitBounds` и четырьмя маркерами по формуле ниже
на углах тестового изображения 800x500 — `map.latLngToContainerPoint`
(та же функция, которой Leaflet реально позиционирует DOM-элементы
слоёв) подтвердила, что (0,0)/(W,0)/(0,H)/(W,H) ложатся ровно в
top-left/top-right/bottom-left/bottom-right контейнера, без переворота
и без зеркалирования. Это настоящая библиотека, не переизложение
формулы на словах — но пиксельного скриншота в реальном браузере НЕ
получено: рабочее окружение агента не имеет ни root (нет системных
библиотек для headless-хромиума), ни сетевого моста между
device_bash-шеллом (изолированная Linux VM) и Chrome/встроенным
браузером Cowork, так что локальный http.server на устройстве не был
доступен для браузерных инструментов. leaflet-geoman.min.js в этой
проверке НЕ грузился (падает в jsdom на `ReferenceError: Element`
из-за особенностей его бандла) — но geoman не участвует в проекции
координат (только рисование/редактирование), так что для вопроса
"не перевёрнута ли ось" это не имеет значения. Итог: формула ниже
верна для реального Leaflet; когда появится настоящий
редактор в браузере, разумно один раз визуально подтвердить то же
самое глазами (особенно для anchors/waypoints/полигонов — их эта
конкретная проверка не покрывала, только одиночные точки).

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


MIN_LOCATION_POLYGON_POINTS = 3


def _validate_polygon(points: Any, width: Optional[float], height: Optional[float],
                       what: str) -> None:
    """Контур места — список точек [[x,y],...], минимум
    MIN_LOCATION_POLYGON_POINTS (партия 6, docs/TZ-batch6-simple-mode.md
    §3: "Разрешить полигон для элементов kind='location'" — раньше
    _validate_xy_obj отвергала список безусловно, хотя схема
    (plan_items.geometry) полигон всегда допускала)."""
    if not isinstance(points, (list, tuple)):
        raise ValueError(f'{what}: контур места должен быть массивом точек [[x,y],...]')
    if len(points) < MIN_LOCATION_POLYGON_POINTS:
        raise ValueError(
            f"{what}: контур места должен содержать не менее "
            f"{MIN_LOCATION_POLYGON_POINTS} точек")
    for idx, pt in enumerate(points):
        _validate_xy_pair(pt, width, height, f"{what}[{idx}]")


_POLYGON_ALLOWED_KINDS = ("location", "group")


def validate_plan_item_geometry(geometry: Any, coord_space: str,
                                 width: Optional[float], height: Optional[float],
                                 kind: Optional[str] = None) -> None:
    """Валидация geometry для plan_item. По умолчанию (и для всех kind
    кроме 'location'/'group') — одиночная точка {x,y}. Для kind='location'
    допускается ЛИБО точка (место как маркер), ЛИБО контур-полигон
    [[x,y],...] (место как область, партия 6 §3) — разрешаем список
    ТОЛЬКО когда вызывающий код явно передал подходящий kind, чтобы не
    ослабить валидацию для point/node/port/annotation, для которых
    полигон физически не имеет смысла и раньше гарантированно
    отвергался.

    kind='group' (зона учёта областью, «План v3» §2/§5 задания
    docs/TZ-batch6-plan-v3.md: "Полигон схемой допущен, но
    validate_plan_item_geometry принимает только точку — это
    недоделка, исправить") добавлен партией «План v3» — схема
    (plan_items.geometry TEXT) полигон для group допускала всегда,
    просто здесь раньше был захардкожен ровно один kind."""
    if coord_space not in VALID_COORD_SPACES:
        raise ValueError(f"неизвестный coord_space: {coord_space!r}")
    if kind in _POLYGON_ALLOWED_KINDS and isinstance(geometry, (list, tuple)):
        _validate_polygon(geometry, width, height, "geometry")
        return
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
