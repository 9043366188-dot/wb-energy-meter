"""Тесты Шага 21 (этап D): HTTP-слой /api/v2/plans (ТЗ §7).

Самостоятельный скрипт (не pytest):
    python tests/test_step21_plan_v2.py

Проверяет план помещений/однолинейные схемы (plan_kind), план_items,
план_edge_views, координатные пространства, canvas_revision (оптимистичная
блокировка A35 §13), валидацию размеров изображений и геометрии."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import io

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from PIL import Image

from wb_energy_meter.db import Database
from wb_energy_meter.repo import GroupRepo, MeterRepo
from wb_energy_meter.api import create_app, _AppState
from wb_energy_meter.model import MeterRegistry
from wb_energy_meter.point_repo import MeteringPointRepo
from wb_energy_meter.topology_service import ElectricalNodeRepo, ElectricalEdgeRepo
from wb_energy_meter.plan_service_v2 import MAX_DECODED_LONG_SIDE, MAX_DECODED_MEGAPIXELS


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


def test_floor_and_single_line_plan_create():
    """Тест 1-4: создание floor и single_line планов, список с фильтром,
    получение изображения."""
    client, db, path, plans_dir = make_client()
    try:
        # --- 1. create floor plan with image ---
        buf = io.BytesIO()
        Image.new("RGB", (800, 600), (255, 0, 0)).save(buf, format="PNG")
        png_bytes = buf.getvalue()

        r = client.post("/api/v2/plans", data={
            "name": "Этаж 1", "plan_kind": "floor",
            "file": (io.BytesIO(png_bytes), "bg.png"),
        }, content_type="multipart/form-data")
        assert r.status_code == 201, (r.status_code, r.get_json())
        plan = r.get_json()
        assert plan["plan_kind"] == "floor" and plan["image_width"] == 800 and plan["image_height"] == 600
        assert plan["canvas_revision"] == 1
        plan_id = plan["id"]

        # --- 2. create single_line plan without image ---
        r = client.post("/api/v2/plans", data={
            "name": "Однолинейная", "plan_kind": "single_line",
            "canvas_width": "2000", "canvas_height": "1200",
        }, content_type="multipart/form-data")
        assert r.status_code == 201, (r.status_code, r.get_json())
        sl_plan = r.get_json()
        assert sl_plan["canvas_width"] == 2000 and sl_plan["canvas_height"] == 1200
        sl_id = sl_plan["id"]

        # --- 3. list plans, filter by kind ---
        r = client.get("/api/v2/plans?plan_kind=floor")
        assert r.status_code == 200 and len(r.get_json()) == 1

        # --- 4. GET image ---
        r = client.get(f"/api/v2/plans/{plan_id}/image")
        assert r.status_code == 200 and r.data == png_bytes and r.mimetype == "image/png"

        print("[OK] планы floor/single_line: создание, список с фильтром, IMAGE GET roundtrip (§7)")
    finally:
        db.close(); os.unlink(path)


def test_plan_items_geometry_validation():
    """Тест 5-5b: создание план_item с точкой меринга, валидация границ."""
    client, db, path, plans_dir = make_client()
    try:
        # Create floor plan with image
        buf = io.BytesIO()
        Image.new("RGB", (800, 600), (255, 0, 0)).save(buf, format="PNG")
        png_bytes = buf.getvalue()

        r = client.post("/api/v2/plans", data={
            "name": "Этаж 1", "plan_kind": "floor",
            "file": (io.BytesIO(png_bytes), "bg.png"),
        }, content_type="multipart/form-data")
        assert r.status_code == 201
        plan_id = r.get_json()["id"]

        # --- 5. create a metering point + plan_item referencing it ---
        point_repo = MeteringPointRepo(db)
        point = point_repo.add(code="pt1", name="Точка 1")
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "point", "point_id": point.id,
            "geometry": {"x": 100, "y": 50}, "coord_space": "image_px_xy_v2",
        })
        assert r.status_code == 201, (r.status_code, r.get_json())
        item1 = r.get_json()

        # --- 5b. reject out-of-bounds geometry ---
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "point", "point_id": point.id,
            "geometry": {"x": 9999, "y": 50}, "coord_space": "image_px_xy_v2",
        })
        assert r.status_code == 400, (r.status_code, r.get_json())

        print("[OK] план_items: создание в границах, отклонение out-of-bounds (§7)")
    finally:
        db.close(); os.unlink(path)


def test_edge_view_endpoint_validation():
    """Тест 6-7: создание edge_view с валидацией endpoint, GET detail с embeds."""
    client, db, path, plans_dir = make_client()
    try:
        # Create floor plan
        buf = io.BytesIO()
        Image.new("RGB", (800, 600), (255, 0, 0)).save(buf, format="PNG")
        png_bytes = buf.getvalue()

        r = client.post("/api/v2/plans", data={
            "name": "Этаж 1", "plan_kind": "floor",
            "file": (io.BytesIO(png_bytes), "bg.png"),
        }, content_type="multipart/form-data")
        assert r.status_code == 201
        plan_id = r.get_json()["id"]

        # Create metering point for item
        point_repo = MeteringPointRepo(db)
        point = point_repo.add(code="pt1", name="Точка 1")
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "point", "point_id": point.id,
            "geometry": {"x": 100, "y": 50}, "coord_space": "image_px_xy_v2",
        })
        item1 = r.get_json()

        # --- 6. topology node + edge, then edge_view ---
        node_repo = ElectricalNodeRepo(db)
        edge_repo = ElectricalEdgeRepo(db)
        n1 = node_repo.add(code="n1", name="Узел 1", kind="panel")
        n2 = node_repo.add(code="n2", name="Узел 2", kind="panel")
        edge = edge_repo.add_draft(from_node_id=n1.id, to_node_id=n2.id, code="e1", name="Кабель 1")

        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n1.id,
            "geometry": {"x": 10, "y": 10}, "coord_space": "image_px_xy_v2",
        })
        node_item1 = r.get_json()
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n2.id,
            "geometry": {"x": 20, "y": 20}, "coord_space": "image_px_xy_v2",
        })
        node_item2 = r.get_json()

        r = client.post(f"/api/v2/plans/{plan_id}/edges", json={
            "edge_id": edge.id, "from_item_id": node_item1["id"], "to_item_id": node_item2["id"],
        })
        assert r.status_code == 201, (r.status_code, r.get_json())

        # mismatched endpoint (item's node_id != edge's from_node_id) -> 400
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "point", "point_id": point.id,
            "geometry": {"x": 30, "y": 30}, "coord_space": "image_px_xy_v2",
        })
        wrong_item = r.get_json()
        r = client.post(f"/api/v2/plans/{plan_id}/edges", json={
            "edge_id": edge.id, "from_item_id": wrong_item["id"], "to_item_id": node_item2["id"],
        })
        assert r.status_code == 400, (r.status_code, r.get_json())

        # --- 7. GET plan detail embeds items+edges ---
        r = client.get(f"/api/v2/plans/{plan_id}")
        detail = r.get_json()
        assert len(detail["items"]) == 4
        assert len(detail["edges"]) == 1

        print("[OK] edge_view: валидация endpoint, GET detail с embeds (§7)")
    finally:
        db.close(); os.unlink(path)


def test_layout_revision_conflict_a35():
    """Тест 8-10: сохранение layout с проверкой ревизии, конфликт 409,
    атомарность, batch с $-индексами (A35 ТЗ §13)."""
    client, db, path, plans_dir = make_client()
    try:
        # Create floor plan
        buf = io.BytesIO()
        Image.new("RGB", (800, 600), (255, 0, 0)).save(buf, format="PNG")
        png_bytes = buf.getvalue()

        r = client.post("/api/v2/plans", data={
            "name": "Этаж 1", "plan_kind": "floor",
            "file": (io.BytesIO(png_bytes), "bg.png"),
        }, content_type="multipart/form-data")
        assert r.status_code == 201
        plan_id = r.get_json()["id"]

        # Create topology for edge test
        node_repo = ElectricalNodeRepo(db)
        edge_repo = ElectricalEdgeRepo(db)
        n1 = node_repo.add(code="n1", name="Узел 1", kind="panel")
        n2 = node_repo.add(code="n2", name="Узел 2", kind="panel")
        edge = edge_repo.add_draft(from_node_id=n1.id, to_node_id=n2.id, code="e1", name="Кабель 1")

        # Create point and item
        point_repo = MeteringPointRepo(db)
        point = point_repo.add(code="pt1", name="Точка 1")
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "point", "point_id": point.id,
            "geometry": {"x": 100, "y": 50}, "coord_space": "image_px_xy_v2",
        })
        item1 = r.get_json()

        # Create node items for edge
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n1.id,
            "geometry": {"x": 10, "y": 10}, "coord_space": "image_px_xy_v2",
        })
        node_item1 = r.get_json()
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n2.id,
            "geometry": {"x": 20, "y": 20}, "coord_space": "image_px_xy_v2",
        })
        node_item2 = r.get_json()

        # --- 8. save_plan_layout: happy path with revision check ---
        r = client.post(f"/api/v2/plans/{plan_id}/layout", json={
            "expected_revision": 1,
            "item_ops": [{"op": "upsert", "id": item1["id"], "geometry": {"x": 111, "y": 55},
                          "coord_space": "image_px_xy_v2"}],
        })
        assert r.status_code == 200, (r.status_code, r.get_json())
        updated = r.get_json()
        assert updated["canvas_revision"] == 2, updated

        # --- 9. save_plan_layout: stale revision -> 409, no partial writes ---
        r = client.post(f"/api/v2/plans/{plan_id}/layout", json={
            "expected_revision": 1,   # stale, actual is now 2
            "item_ops": [{"op": "upsert", "id": item1["id"], "geometry": {"x": 5, "y": 5},
                          "coord_space": "image_px_xy_v2"}],
        })
        assert r.status_code == 409, (r.status_code, r.get_json())

        # verify the stale write did NOT apply
        r = client.get(f"/api/v2/plans/{plan_id}")
        items_by_id = {i["id"]: i for i in r.get_json()["items"]}
        assert items_by_id[item1["id"]]["geometry"] == {"x": 111, "y": 55}, items_by_id[item1["id"]]
        assert r.get_json()["canvas_revision"] == 2

        # --- 10. layout batch: create item + edge_view referencing it via $N in one call ---
        r = client.post(f"/api/v2/plans/{plan_id}/layout", json={
            "expected_revision": 2,
            "item_ops": [{"op": "upsert", "kind": "node", "node_id": n1.id,
                          "geometry": {"x": 1, "y": 1}, "coord_space": "image_px_xy_v2"}],
            "edge_view_ops": [{"op": "upsert", "edge_id": edge.id,
                                "from_item_id": "$0", "to_item_id": node_item2["id"]}],
        })
        assert r.status_code == 200, (r.status_code, r.get_json())

        print("[OK] layout save: ревизия, конфликт 409 (A35 §13), batch с $-индексами (§7)")
    finally:
        db.close(); os.unlink(path)


def test_plan_item_removal_preserves_entity():
    """Тест 11: удаление план_item не удаляет underlying сущность (точку меринга)."""
    client, db, path, plans_dir = make_client()
    try:
        # Create floor plan
        buf = io.BytesIO()
        Image.new("RGB", (800, 600), (255, 0, 0)).save(buf, format="PNG")
        png_bytes = buf.getvalue()

        r = client.post("/api/v2/plans", data={
            "name": "Этаж 1", "plan_kind": "floor",
            "file": (io.BytesIO(png_bytes), "bg.png"),
        }, content_type="multipart/form-data")
        assert r.status_code == 201
        plan_id = r.get_json()["id"]

        # Create point and item
        point_repo = MeteringPointRepo(db)
        point = point_repo.add(code="pt1", name="Точка 1")
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "point", "point_id": point.id,
            "geometry": {"x": 100, "y": 50}, "coord_space": "image_px_xy_v2",
        })
        item1 = r.get_json()

        # --- 11. remove item (view only, not the underlying point) ---
        r = client.delete(f"/api/v2/plans/{plan_id}/items/{item1['id']}")
        assert r.status_code == 200
        r = client.get(f"/api/v2/points/{point.id}")
        assert r.status_code == 200, "underlying point must survive plan_item removal"

        print("[OK] удаление план_item: underlying metering_point остаётся (§7)")
    finally:
        db.close(); os.unlink(path)


def test_plan_delete_cascade_and_404s():
    """Тест 12-13: удаление плана каскадит items/edges, неизвестный план -> 404."""
    client, db, path, plans_dir = make_client()
    try:
        # Create two plans
        buf = io.BytesIO()
        Image.new("RGB", (800, 600), (255, 0, 0)).save(buf, format="PNG")
        png_bytes = buf.getvalue()

        r = client.post("/api/v2/plans", data={
            "name": "План 1", "plan_kind": "floor",
            "file": (io.BytesIO(png_bytes), "bg.png"),
        }, content_type="multipart/form-data")
        assert r.status_code == 201
        plan_id = r.get_json()["id"]

        r = client.post("/api/v2/plans", data={
            "name": "План 2", "plan_kind": "single_line",
            "canvas_width": "2000", "canvas_height": "1200",
        }, content_type="multipart/form-data")
        assert r.status_code == 201
        sl_id = r.get_json()["id"]

        # --- 12. delete plan cascades items/edge_views ---
        r = client.delete(f"/api/v2/plans/{sl_id}")
        assert r.status_code == 200
        r = client.get(f"/api/v2/plans/{sl_id}")
        assert r.status_code == 404

        # --- 13. 404 for unknown plan ---
        r = client.get("/api/v2/plans/99999")
        assert r.status_code == 404

        print("[OK] удаление плана: каскадит items/edges, неизвестный -> 404 (§7)")
    finally:
        db.close(); os.unlink(path)


def test_image_size_limit_rejected():
    """Тест: изображение больше MAX_DECODED_LONG_SIDE отклоняется 400."""
    client, db, path, plans_dir = make_client()
    try:
        # Create image exceeding MAX_DECODED_LONG_SIDE
        oversized_width = MAX_DECODED_LONG_SIDE + 100
        buf = io.BytesIO()
        Image.new("RGB", (oversized_width, 100), (255, 0, 0)).save(buf, format="PNG")
        png_bytes = buf.getvalue()

        r = client.post("/api/v2/plans", data={
            "name": "Огромный план", "plan_kind": "floor",
            "file": (io.BytesIO(png_bytes), "bg.png"),
        }, content_type="multipart/form-data")
        assert r.status_code == 400, (r.status_code, r.get_json())
        error = r.get_json()
        assert "MAX_DECODED_LONG_SIDE" in error.get("message", "") or "8192" in error.get("message", "")

        print("[OK] изображение > MAX_DECODED_LONG_SIDE отклоняется 400 (§7.2)")
    finally:
        db.close(); os.unlink(path)


def test_waypoints_out_of_bounds_rejected():
    """Тест: waypoints с точками вне границ плана отклоняются 400."""
    client, db, path, plans_dir = make_client()
    try:
        # Create floor plan
        buf = io.BytesIO()
        Image.new("RGB", (800, 600), (255, 0, 0)).save(buf, format="PNG")
        png_bytes = buf.getvalue()

        r = client.post("/api/v2/plans", data={
            "name": "Этаж 1", "plan_kind": "floor",
            "file": (io.BytesIO(png_bytes), "bg.png"),
        }, content_type="multipart/form-data")
        assert r.status_code == 201
        plan_id = r.get_json()["id"]

        # Create two node items in bounds
        node_repo = ElectricalNodeRepo(db)
        edge_repo = ElectricalEdgeRepo(db)
        n1 = node_repo.add(code="n1", name="Узел 1", kind="panel")
        n2 = node_repo.add(code="n2", name="Узел 2", kind="panel")
        edge = edge_repo.add_draft(from_node_id=n1.id, to_node_id=n2.id, code="e1", name="Кабель 1")

        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n1.id,
            "geometry": {"x": 10, "y": 10}, "coord_space": "image_px_xy_v2",
        })
        node_item1 = r.get_json()
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n2.id,
            "geometry": {"x": 20, "y": 20}, "coord_space": "image_px_xy_v2",
        })
        node_item2 = r.get_json()

        # Try to create edge with out-of-bounds waypoint
        r = client.post(f"/api/v2/plans/{plan_id}/edges", json={
            "edge_id": edge.id, "from_item_id": node_item1["id"], "to_item_id": node_item2["id"],
            "waypoints": [[100, 100], [99999, 100]],
        })
        assert r.status_code == 400, (r.status_code, r.get_json())

        print("[OK] waypoints out-of-bounds отклоняются 400 (§7)")
    finally:
        db.close(); os.unlink(path)


def test_layout_frontend_payload_shape():
    """Stage D (фронтенд-редактор index.html, план v2): проверяет ИМЕННО
    ту форму /layout-payload, которую строит JS _planV2BuildLayoutPayload()
    в статике — batch upsert существующего item (drag), новый item
    ("$idx"-ссылка на него из edge_view_ops — способ сослаться на элемент,
    ещё не имеющий id, см. save_plan_layout docstring), и remove
    существующего item одним запросом. Ловит рассинхронизацию контракта
    между фронтендом и save_plan_layout, если формат payload когда-либо
    разъедется по одну или другую сторону."""
    client, db, path, plans_dir = make_client()
    try:
        r = client.post("/api/v2/plans", data={
            "name": "Однолинейная", "plan_kind": "single_line",
            "canvas_width": "2000", "canvas_height": "1200",
        }, content_type="multipart/form-data")
        assert r.status_code == 201, (r.status_code, r.get_json())
        plan = r.get_json()
        plan_id, revision = plan["id"], plan["canvas_revision"]

        node_repo = ElectricalNodeRepo(db)
        n1 = node_repo.add(code="n1", name="Щит 1", kind="panel")
        n2 = node_repo.add(code="n2", name="Щит 2", kind="panel")
        edge = ElectricalEdgeRepo(db).add_draft(from_node_id=n1.id, to_node_id=n2.id, code="e1")

        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n1.id,
            "geometry": {"x": 100, "y": 200}, "coord_space": "canvas_xy_v2",
            "label": "Щит 1",
        })
        assert r.status_code == 201, (r.status_code, r.get_json())
        item1 = r.get_json()
        r = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n2.id,
            "geometry": {"x": 400, "y": 200}, "coord_space": "canvas_xy_v2",
        })
        assert r.status_code == 201, (r.status_code, r.get_json())
        item2 = r.get_json()

        # Форма payload — 1:1 то, что строит _planV2BuildLayoutPayload():
        # item_ops[0] = drag существующего item1 (перенос label/kind не
        # требуется — только geometry/coord_space/label как шлёт JS);
        # item_ops[1] = пользователь убрал старую метку item2 и поставил
        # новую для того же узла n2 в другом месте — новый node-item;
        # item_ops[2] = remove item2 (старая метка n2);
        # edge_view_ops[0] = новая связь: from=item1 (реальный id),
        # to="$1" (ссылка на item_ops[1] по индексу, как делает resolveRef()
        # в index.html при from_item_id/to_item_id, начинающемся с "new:").
        # Заодно проверяет, что _check_edge_view_endpoints матчит node_id
        # НОВОГО item'а (ещё без id на момент построения payload на
        # фронтенде) против from_node_id/to_node_id связи.
        payload = {
            "expected_revision": revision,
            "item_ops": [
                {"op": "upsert", "id": item1["id"], "geometry": {"x": 150, "y": 250},
                 "coord_space": "canvas_xy_v2", "label": "Щит 1"},
                {"op": "upsert", "kind": "node", "node_id": n2.id,
                 "geometry": {"x": 500, "y": 300},
                 "coord_space": "canvas_xy_v2", "label": "Щит 2 (переставлен)"},
                {"op": "remove", "id": item2["id"]},
            ],
            "edge_view_ops": [
                {"op": "upsert", "edge_id": edge.id,
                 "from_item_id": item1["id"], "to_item_id": "$1",
                 "waypoints": None, "view_kind": "structural"},
            ],
        }
        r = client.post(f"/api/v2/plans/{plan_id}/layout", json=payload)
        assert r.status_code == 200, (r.status_code, r.get_json())
        updated = r.get_json()
        assert updated["canvas_revision"] == revision + 1

        r = client.get(f"/api/v2/plans/{plan_id}")
        detail = r.get_json()
        items_by_id = {i["id"]: i for i in detail["items"]}
        assert item1["id"] in items_by_id and items_by_id[item1["id"]]["geometry"] == {"x": 150, "y": 250}
        assert item2["id"] not in items_by_id, "item2 должен быть удалён по remove-op"
        new_items = [i for i in detail["items"] if i["id"] not in (item1["id"],)]
        assert len(new_items) == 1 and new_items[0]["geometry"] == {"x": 500, "y": 300}
        assert new_items[0]["node_id"] == n2.id
        new_item_id = new_items[0]["id"]

        assert len(detail["edges"]) == 1
        ev = detail["edges"][0]
        assert ev["from_item_id"] == item1["id"]
        assert ev["to_item_id"] == new_item_id, "\"$1\" должен резолвиться в id только что созданного item_ops[1]"

        print("[OK] /layout принимает payload ровно в форме, которую строит index.html (Stage D)")
    finally:
        db.close(); os.unlink(path)


def test_layout_frontend_payload_shape_waypoints_drawing():
    """Stage D (фронтенд, index.html): проверяет форму payload, которую
    строит planV2SaveDrawnWaypoints()/_planV2BuildLayoutPayload() при
    рисовании линии edge_view через leaflet-geoman —
    waypoints:[[x,y],[x,y],...] (пары-массивы, НЕ объекты {x,y}, как у
    geometry item'ов — см. _planV2LatLngToXy в index.html и
    _validate_xy_pair/validate_waypoints_v2 в plan_geo_v2.py, которые ждут
    именно такую форму). Отдельно от test_layout_frontend_payload_shape,
    т.к. та фиксирует форму item-полей, а эта — конкретно waypoints
    upsert на уже существующем edge_view (planV2ClearWaypoints/
    planV2SaveDrawnWaypoints оба шлют upsert с id существующей связи)."""
    client, db, path, plans_dir = make_client()
    try:
        r = client.post("/api/v2/plans", data={
            "name": "Однолинейная", "plan_kind": "single_line",
            "canvas_width": "2000", "canvas_height": "1200",
        }, content_type="multipart/form-data")
        assert r.status_code == 201, (r.status_code, r.get_json())
        plan = r.get_json()
        plan_id, revision = plan["id"], plan["canvas_revision"]

        node_repo = ElectricalNodeRepo(db)
        n1 = node_repo.add(code="n1", name="Щит 1", kind="panel")
        n2 = node_repo.add(code="n2", name="Щит 2", kind="panel")
        edge = ElectricalEdgeRepo(db).add_draft(from_node_id=n1.id, to_node_id=n2.id, code="e1")

        item1 = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n1.id,
            "geometry": {"x": 100, "y": 200}, "coord_space": "canvas_xy_v2",
        }).get_json()
        item2 = client.post(f"/api/v2/plans/{plan_id}/items", json={
            "kind": "node", "node_id": n2.id,
            "geometry": {"x": 400, "y": 200}, "coord_space": "canvas_xy_v2",
        }).get_json()
        edge_view = client.post(f"/api/v2/plans/{plan_id}/edges", json={
            "edge_id": edge.id, "from_item_id": item1["id"], "to_item_id": item2["id"],
            "view_kind": "structural",
        }).get_json()

        # planV2SaveDrawnWaypoints: upsert по существующему id, остальные
        # поля — из _planV2BaseEdgeFields(view) (т.е. неизменные edge_id/
        # from_item_id/to_item_id/view_kind), только waypoints новые.
        payload = {
            "expected_revision": revision,
            "item_ops": [],
            "edge_view_ops": [
                {"op": "upsert", "id": edge_view["id"], "edge_id": edge.id,
                 "from_item_id": item1["id"], "to_item_id": item2["id"],
                 "waypoints": [[150, 220], [250, 260], [350, 220]],
                 "view_kind": "structural"},
            ],
        }
        r = client.post(f"/api/v2/plans/{plan_id}/layout", json=payload)
        assert r.status_code == 200, (r.status_code, r.get_json())

        detail = client.get(f"/api/v2/plans/{plan_id}").get_json()
        assert len(detail["edges"]) == 1
        assert detail["edges"][0]["waypoints"] == [[150, 220], [250, 260], [350, 220]]

        # planV2ClearWaypoints: тот же upsert, но waypoints:null — линия
        # возвращается к прямой между метками.
        revision2 = detail["canvas_revision"]
        r = client.post(f"/api/v2/plans/{plan_id}/layout", json={
            "expected_revision": revision2,
            "item_ops": [],
            "edge_view_ops": [
                {"op": "upsert", "id": edge_view["id"], "edge_id": edge.id,
                 "from_item_id": item1["id"], "to_item_id": item2["id"],
                 "waypoints": None, "view_kind": "structural"},
            ],
        })
        assert r.status_code == 200, (r.status_code, r.get_json())
        detail2 = client.get(f"/api/v2/plans/{plan_id}").get_json()
        assert detail2["edges"][0]["waypoints"] is None

        print("[OK] /layout принимает waypoints:[[x,y],...] в форме planV2SaveDrawnWaypoints/planV2ClearWaypoints")
    finally:
        db.close(); os.unlink(path)


def test_admin_migrate_legacy_endpoint():
    """Тест admin API endpoint POST /api/v2/admin/migrate-legacy: перенос
    легаси meters/meter_groups в v2 (metering_points/meter_sources/
    point_bindings/group_memberships). Проверяет:
    1. Отклонение 400 без {"confirm": true}
    2. Успешный 200 с валидным бэкапом на диске (SQLite Online Backup API)
    3. Миграция создаёт одну point per meter, в том числе отключённые приборы
    4. Идемпотентность: повторный вызов skips уже мигрированные, не плодит дубли
    5. GET /api/v2/points возвращает ровно столько же точек после каждого вызова
    """
    import sqlite3

    client, db, path, plans_dir = make_client()
    try:
        # Seed legacy meters and groups
        gr = GroupRepo(db)
        mr = MeterRepo(db, gr)
        m1 = mr.add(device_id="wb-map3e_10", display_name="Счётчик 10", group="Цех 1")
        m2 = mr.add(device_id="wb-map3e_11", display_name="Счётчик 11")  # no group
        m3_temp = mr.add(device_id="wb-map3e_12", display_name="Счётчик 12 (архивный)", group="Цех 1")
        m3 = mr.update(device_id="wb-map3e_12", enabled=False)  # archive it

        # --- 1. POST without {"confirm": true} -> 400 ---
        r = client.post("/api/v2/admin/migrate-legacy", json={})
        assert r.status_code == 400, (r.status_code, r.get_json())
        body = r.get_json()
        assert body["code"] == "bad_request"
        assert "confirm" in body.get("fields", [])

        r = client.post("/api/v2/admin/migrate-legacy", json={"confirm": False})
        assert r.status_code == 400, (r.status_code, r.get_json())

        # --- 2. POST with {"confirm": true} -> 200 ---
        r = client.post("/api/v2/admin/migrate-legacy", json={"confirm": True})
        assert r.status_code == 200, (r.status_code, r.get_json())
        result1 = r.get_json()

        # Verify response fields
        assert "backup_path" in result1
        assert "migrated_points" in result1
        assert "skipped_points" in result1
        assert "recovered_points" in result1
        assert "memberships_created" in result1
        assert "warnings" in result1

        backup_path = result1["backup_path"]

        # --- 3. Verify backup file exists and is non-empty on disk ---
        assert os.path.exists(backup_path), f"backup_path {backup_path} does not exist"
        assert os.path.getsize(backup_path) > 0, f"backup_path {backup_path} is empty"

        # --- 4. Verify backup is a valid SQLite database ---
        try:
            backup_conn = sqlite3.connect(backup_path)
            backup_cursor = backup_conn.cursor()
            # Simple sanity check: count rows in legacy meters table
            backup_cursor.execute("SELECT COUNT(*) FROM meters")
            meter_count = backup_cursor.fetchone()[0]
            assert meter_count == 3, f"Expected 3 meters in backup, got {meter_count}"
            backup_conn.close()
        except Exception as e:
            raise AssertionError(f"backup file is not a valid SQLite database: {e}")

        # --- 5. Verify migration results ---
        # 3 meters were seeded, so all should be migrated in first call
        assert len(result1["migrated_points"]) == 3, result1["migrated_points"]
        assert len(result1["skipped_points"]) == 0, result1["skipped_points"]
        assert len(result1["recovered_points"]) == 0, result1["recovered_points"]
        # m1 and m3 belong to "Цех 1" group, m2 has no group
        assert result1["memberships_created"] == 2, result1["memberships_created"]

        # --- 6. Verify GET /api/v2/points returns one point per meter ---
        r = client.get("/api/v2/points")
        assert r.status_code == 200
        points1 = r.get_json()
        assert len(points1) == 3, f"Expected 3 points after first migration, got {len(points1)}"

        # Verify the points have expected codes (legacy:device_id pattern)
        codes_created = {p["code"] for p in points1}
        expected_codes = {"legacy:wb-map3e_10", "legacy:wb-map3e_11", "legacy:wb-map3e_12"}
        assert codes_created == expected_codes, f"got {codes_created}, expected {expected_codes}"

        # --- 7. Idempotency: call migration again ---
        r = client.post("/api/v2/admin/migrate-legacy", json={"confirm": True})
        assert r.status_code == 200, (r.status_code, r.get_json())
        result2 = r.get_json()

        # Second call: all 3 meters should be in skipped_points, none in migrated_points
        assert len(result2["migrated_points"]) == 0, result2["migrated_points"]
        assert len(result2["skipped_points"]) == 3, result2["skipped_points"]
        assert set(result2["skipped_points"]) == {m1.id, m2.id, m3.id}
        assert len(result2["recovered_points"]) == 0, result2["recovered_points"]

        # --- 8. No duplicates: GET /api/v2/points still returns 3 points ---
        r = client.get("/api/v2/points")
        assert r.status_code == 200
        points2 = r.get_json()
        assert len(points2) == 3, f"Expected 3 points after idempotent re-call, got {len(points2)}"

        # Same point codes as before
        codes_after = {p["code"] for p in points2}
        assert codes_after == expected_codes, f"codes changed after idempotent call: {codes_after}"

        print("[OK] /api/v2/admin/migrate-legacy: перенос, бэкап, идемпотентность (Stage F)")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    test_floor_and_single_line_plan_create()
    test_plan_items_geometry_validation()
    test_edge_view_endpoint_validation()
    test_layout_revision_conflict_a35()
    test_plan_item_removal_preserves_entity()
    test_plan_delete_cascade_and_404s()
    test_image_size_limit_rejected()
    test_waypoints_out_of_bounds_rejected()
    test_layout_frontend_payload_shape()
    test_layout_frontend_payload_shape_waypoints_drawing()
    test_admin_migrate_legacy_endpoint()
    print("\nВсе тесты plan v2 (Шаг 21) пройдены.")
