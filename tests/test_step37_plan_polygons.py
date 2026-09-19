"""Шаг 37 (партия 6, задача 2 + баг 4 из задачи 6): места как полигоны
и починка враньё "размещено на плане" (docs/TZ-batch6-simple-mode.md
§3, §5).

§3: `plan_geo_v2.validate_plan_item_geometry` раньше ВСЕГДА требовала
одиночную точку {x,y}, хотя схема `plan_items.geometry` всегда
допускала контур `[[x,y],...]` — теперь для `kind='location'` полигон
разрешён (минимум 3 точки), для всех остальных kind поведение не
изменилось ни на йоту (проверяется явно, парой к каждому "разрешили").

§5 (баг "состояния размещения врут"): `placed_on_plan` считала только
`kind='point'` элементы плана, хотя точка может быть физически нанесена
на план через СВОЙ узел (`kind='node'`, служебный код `sm-pt-<id>` —
см. simple_mode_service.py), заведённый простым режимом при включении
«ввод»/«питается от». Тест воспроизводит именно эту ситуацию через
HTTP /api/v2 и проверяет, что после починки `placed_on_plan` = true.

Каждая проверка "отказ" сопровождается парной "легитимный запрос
работает" (AGENTS.md: тест только-отказ ничего не доказывает).

Самостоятельный скрипт (не pytest):
    python tests/test_step37_plan_polygons.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry


def make_client():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    groups_repo = GroupRepo(db)
    meters_repo = MeterRepo(db, groups_repo)
    registry = MeterRegistry()
    plans_dir = tempfile.mkdtemp()
    state = _AppState(
        registry=registry, meters_repo=meters_repo, groups_repo=groups_repo,
        is_mqtt_connected=lambda: False, mqtt_message_count=lambda: 0,
        mqtt_error_count=lambda: 0, wb_db_client=None,
        consumption_service=None, started_at=time.time(), db=db,
        plans_dir=plans_dir,
    )
    app = create_app(state)
    return app.test_client(), db, path, plans_dir


def current_rev(client):
    return client.get("/api/v2/revision").get_json()["configuration_revision"]


def _make_plan(client, name="Однолинейная", width=2000, height=1200):
    r = client.post("/api/v2/plans", data={
        "name": name, "plan_kind": "single_line",
        "canvas_width": str(width), "canvas_height": str(height),
    }, content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _make_location(client, name, kind="room"):
    r = client.post("/api/v2/locations", json={"name": name, "kind": kind})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _make_point(client, code, name):
    rev = current_rev(client)
    r = client.post("/api/v2/points", json={
        "code": code, "name": name, "expected_revision": rev})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


SQUARE = [[10, 10], [200, 10], [200, 150], [10, 150]]


def test_location_polygon_accepted_point_kind_polygon_rejected():
    """Разрешили контур для kind='location' — но НЕ для kind='point'
    (полигон для точки физически бессмыслен и раньше тоже отвергался;
    важно, что починка §3 не расширила это по ошибке)."""
    client, db, path, plans_dir = make_client()
    try:
        plan = _make_plan(client)
        loc = _make_location(client, "Щитовая 1")

        # легитимный запрос: полигон для места принимается
        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "location", "location_id": loc["id"],
            "geometry": SQUARE, "coord_space": "canvas_xy_v2",
        })
        assert r.status_code == 201, r.get_json()
        item = r.get_json()
        assert item["geometry"] == SQUARE, item

        # отказ: тот же контур для kind='point' — по-прежнему {x,y}
        point = _make_point(client, "p1", "Точка 1")
        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "point", "point_id": point["id"],
            "geometry": SQUARE, "coord_space": "canvas_xy_v2",
        })
        assert r.status_code == 400, r.get_json()
        assert "объект" in r.get_json()["message"], r.get_json()

        # парная легитимная проверка: точка одиночным {x,y} — работает
        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "point", "point_id": point["id"],
            "geometry": {"x": 50, "y": 60}, "coord_space": "canvas_xy_v2",
        })
        assert r.status_code == 201, r.get_json()
        print("[OK] полигон разрешён для места, но не для точки (точка по-прежнему {x,y})")
    finally:
        db.close()
        os.unlink(path)


def test_location_polygon_minimum_points():
    """Контур места должен содержать минимум 3 точки — отказ на 2,
    парная легитимная проверка на ровно 3."""
    client, db, path, plans_dir = make_client()
    try:
        plan = _make_plan(client)
        loc = _make_location(client, "Щитовая 2")

        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "location", "location_id": loc["id"],
            "geometry": [[1, 1], [2, 2]], "coord_space": "canvas_xy_v2",
        })
        assert r.status_code == 400, r.get_json()
        assert "3" in r.get_json()["message"], r.get_json()

        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "location", "location_id": loc["id"],
            "geometry": [[1, 1], [2, 2], [3, 1]], "coord_space": "canvas_xy_v2",
        })
        assert r.status_code == 201, r.get_json()
        print("[OK] контур места < 3 точек отклонён; ровно 3 точки — легитимно")
    finally:
        db.close()
        os.unlink(path)


def test_update_geometry_route_respects_kind():
    """PATCH /items/<id> — обновление геометрии тоже учитывает kind
    существующего элемента (не только создание)."""
    client, db, path, plans_dir = make_client()
    try:
        plan = _make_plan(client)
        loc = _make_location(client, "Щитовая 3")
        point = _make_point(client, "p2", "Точка 2")

        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "location", "location_id": loc["id"],
            "geometry": SQUARE, "coord_space": "canvas_xy_v2",
        })
        loc_item = r.get_json()

        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "point", "point_id": point["id"],
            "geometry": {"x": 5, "y": 5}, "coord_space": "canvas_xy_v2",
        })
        point_item = r.get_json()

        # отказ: точке нельзя подменить геометрию на контур
        r = client.patch(f"/api/v2/plans/{plan['id']}/items/{point_item['id']}", json={
            "geometry": SQUARE,
        })
        assert r.status_code == 400, r.get_json()

        # парная легитимная проверка: месту — можно, новым контуром
        new_square = [[20, 20], [220, 20], [220, 170], [20, 170]]
        r = client.patch(f"/api/v2/plans/{plan['id']}/items/{loc_item['id']}", json={
            "geometry": new_square,
        })
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["geometry"] == new_square
        print("[OK] PATCH геометрии тоже запрещает контур точке и разрешает месту")
    finally:
        db.close()
        os.unlink(path)


def test_save_plan_layout_batch_upsert_with_polygon():
    """A35 §13 batch-сохранение (/plans/<id>/layout) — тоже проверяет
    kind при вставке нового элемента с контуром, а не только одиночный
    POST /items."""
    client, db, path, plans_dir = make_client()
    try:
        plan = _make_plan(client)
        loc = _make_location(client, "Щитовая 4")
        point = _make_point(client, "p3", "Точка 3")

        rev = plan["canvas_revision"]
        r = client.post(f"/api/v2/plans/{plan['id']}/layout", json={
            "expected_revision": rev,
            "item_ops": [
                {"op": "upsert", "id": None, "kind": "location",
                 "location_id": loc["id"], "geometry": SQUARE,
                 "coord_space": "canvas_xy_v2"},
                {"op": "upsert", "id": None, "kind": "point",
                 "point_id": point["id"], "geometry": {"x": 30, "y": 40},
                 "coord_space": "canvas_xy_v2"},
            ],
        })
        assert r.status_code == 200, r.get_json()
        updated = r.get_json()
        items = client.get(f"/api/v2/plans/{plan['id']}/items").get_json()
        by_kind = {i["kind"]: i for i in items}
        assert by_kind["location"]["geometry"] == SQUARE
        assert by_kind["point"]["geometry"] == {"x": 30, "y": 40}

        # отказ: тот же batch, но точке подсунут контур
        rev2 = updated["canvas_revision"]
        r = client.post(f"/api/v2/plans/{plan['id']}/layout", json={
            "expected_revision": rev2,
            "item_ops": [
                {"op": "upsert", "id": None, "kind": "point",
                 "point_id": point["id"], "geometry": SQUARE,
                 "coord_space": "canvas_xy_v2"},
            ],
        })
        assert r.status_code == 400, r.get_json()
        # и ничего не сохранилось (ревизия не сдвинулась)
        r = client.get(f"/api/v2/plans/{plan['id']}")
        assert r.get_json()["canvas_revision"] == rev2
        print("[OK] batch-сохранение layout тоже валидирует контур по kind; ничего не ломает при отказе")
    finally:
        db.close()
        os.unlink(path)


def test_placed_on_plan_bugfix_via_node_kind_item():
    """Баг из браузера (§5): точка, чей СОБСТВЕННЫЙ узел (заведён
    простым режимом при включении «ввод») нанесён на план как
    kind='node' (а не kind='point'), должна теперь считаться
    физически размещённой — placed_on_plan: true."""
    client, db, path, plans_dir = make_client()
    try:
        plan = _make_plan(client)
        point = _make_point(client, "p4", "Ввод")
        other_point = _make_point(client, "p5", "Без размещения")

        # включаем «ввод» через простой режим — это заводит служебный
        # узел точки (sm-pt-<id>) скрыто, без какой-либо ручки
        # /topology на глазах пользователя
        rev = current_rev(client)
        r = client.patch(f"/api/v2/points/{point['id']}", json={
            "is_input": True, "expected_revision": rev,
        })
        assert r.status_code == 200, r.get_json()

        # до размещения на плане — placed_on_plan должна быть false
        r = client.get("/api/v2/structure/points")
        by_id = {i["point_id"]: i for i in r.get_json()["points"]}
        assert by_id[point["id"]]["placed_on_plan"] is False
        assert by_id[other_point["id"]]["placed_on_plan"] is False

        # находим служебный узел точки по коду sm-pt-<id>
        nodes = client.get("/api/v2/topology/nodes").get_json()
        own_node = next(n for n in nodes if n["code"] == f"sm-pt-{point['id']}")

        # наносим на план как kind='node' (подробный режим это
        # позволяет — узел точки визуально не отличить от любого
        # другого узла)
        r = client.post(f"/api/v2/plans/{plan['id']}/items", json={
            "kind": "node", "node_id": own_node["id"],
            "geometry": {"x": 15, "y": 25}, "coord_space": "canvas_xy_v2",
        })
        assert r.status_code == 201, r.get_json()

        r = client.get("/api/v2/structure/points")
        by_id = {i["point_id"]: i for i in r.get_json()["points"]}
        assert by_id[point["id"]]["placed_on_plan"] is True, by_id[point["id"]]
        # контрольная точка без какого-либо plan_item — по-прежнему false
        # (доказывает, что проверка не стала "всегда true")
        assert by_id[other_point["id"]]["placed_on_plan"] is False
        print("[OK] точка, нанесённая на план через свой узел (kind='node'), "
              "теперь корректно считается размещённой; ненанесённая — по-прежнему нет")
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    test_location_polygon_accepted_point_kind_polygon_rejected()
    test_location_polygon_minimum_points()
    test_update_geometry_route_respects_kind()
    test_save_plan_layout_batch_upsert_with_polygon()
    test_placed_on_plan_bugfix_via_node_kind_item()
    print("\nВсе тесты полигонов мест и починки placed_on_plan (Шаг 37) пройдены.")
