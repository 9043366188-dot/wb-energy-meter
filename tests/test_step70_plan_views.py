"""Тесты партии 14 (A26, A27): план этажа и схема — только изображения топологии.

Самостоятельный скрипт (не pytest):
    python tests/test_step70_plan_views.py

A26 — одна точка (и один элемент схемы) на плане этажа и на однолинейной схеме, на схеме —
дважды; холст схемы без картинки; числа не зависят от того, сколько раз и где точка нарисована,
итог объекта не меняется; десять планов (7 этажей + 3 схемы) создаются и показываются.
A27 — удаление значка, линии (edge view) и целого плана удаляет только изображение: точки,
связи, привязки и история (часовые агрегаты) остаются теми же, их перечитывает API.
"""

from __future__ import annotations

import io
import os
import struct
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "browser"))

from harness import Harness  # noqa: E402

HOUR = 3600
FROM, TO = "2026-01-10", "2026-01-11"
# Запросы идут с timezone="UTC", а строки-даты сервер читает в поясе запроса (A44, timebounds.py):
# начало суток считаем в UTC, а не в поясе процесса — иначе тест зависел бы от пояса машины.
DAY_START = int(datetime.strptime(FROM, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def make_png(width, height):
    # минимальный PNG: сигнатура + IHDR (как tests/test_step21_plan_v2.py::make_png)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    return sig + ihdr


def seed_day(h, device_id, kwh):
    for k in range(24):
        h.seed_energy(device_id, kwh if k == 0 else 0.0,
                      period_start=DAY_START + k * HOUR, period_end=DAY_START + (k + 1) * HOUR)


def build_site(h):
    """Ввод → щит → две нагрузки; счётчики на вводе щита (10 кВт·ч) и на нагрузках (4 + 4)."""
    src = h.make_node("SRC", "Ввод", "source")
    panel = h.make_node("P1", "Щит 1", "panel")
    e_in = h.connect(src["id"], panel["id"])
    loads = [h.add_consumer(panel["id"], n) for n in ("Станок", "Свет")]
    spec = [(e_in["id"], "dev-in", 10.0), (loads[0]["edge"]["id"], "dev-l0", 4.0),
            (loads[1]["edge"]["id"], "dev-l1", 4.0)]
    points = []
    for i, (edge_id, dev, kwh) in enumerate(spec):
        pt = h.make_point(f"PT{i}", f"Точка {i}")
        h.bind_meter(pt["id"], dev, f"Счётчик {i}")
        h.set_meter_on_edge(edge_id, pt["id"])
        seed_day(h, dev, kwh)
        points.append(pt)
    return {"src": src, "panel": panel, "edge_in": e_in, "loads": loads, "points": points}


def post(h, path, body):
    r = h.client.post(path, json=body)
    assert r.status_code in (200, 201), (path, r.status_code, r.get_json())
    return r.get_json()


def get(h, path, ok=200):
    r = h.client.get(path)
    assert r.status_code == ok, (path, r.status_code, r.get_json())
    return r.get_json()


def create_floor(h, name):
    r = h.client.post("/api/v2/plans", data={
        "name": name, "plan_kind": "floor",
        "file": (io.BytesIO(make_png(800, 600)), "bg.png")}, content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def create_scheme(h, name, w=2000, hgt=1200):
    r = h.client.post("/api/v2/plans", data={
        "name": name, "plan_kind": "single_line",
        "canvas_width": str(w), "canvas_height": str(hgt)})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def energy_of_points(h, point_ids):
    body = post(h, "/api/v2/reports/query", {
        "dimension": "point", "scope_ids": point_ids, "from": FROM, "to": TO, "timezone": "UTC"})
    return {row["id"]: row["result"]["value"] for row in body["rows"]}


def site_snapshot(h, site):
    """Всё, что не должно зависеть от планов: связи, точки, привязки, итог объекта, энергия точек."""
    pids = [p["id"] for p in site["points"]]
    summary = post(h, "/api/v2/overview/summary", {"from": FROM, "to": TO, "timezone": "UTC"})
    edges = sorted((e["id"], e["from_node_id"], e["to_node_id"], e["primary_point_id"])
                   for e in get(h, "/api/v2/topology/edges?state=published"))
    nodes = sorted((n["id"], n["kind"]) for n in get(h, "/api/v2/topology/nodes"))
    bindings = {pid: [(b["id"], b["valid_from"], b["valid_to"]) for b in
                      get(h, f"/api/v2/points/{pid}/bindings")] for pid in pids}
    return {
        "object_total": (summary.get("object_total") or {}).get("value"),
        "edges": edges, "nodes": nodes, "bindings": bindings,
        "energy": energy_of_points(h, pids),
        "points": sorted(p["id"] for p in get(h, "/api/v2/points")),
    }


def test_a26_point_on_floor_plan_and_scheme_twice():
    with Harness() as h:
        site = build_site(h)
        pid = site["points"][0]["id"]
        before = site_snapshot(h, site)
        assert before["object_total"] == 10.0, before["object_total"]
        assert before["energy"][pid] == 10.0, before["energy"]

        # десять планов: 7 этажей + 3 схемы — «общий лимит 10 планов» (схемы считаются вместе)
        floors = [create_floor(h, f"Этаж {i}") for i in range(1, 8)]
        schemes = [create_scheme(h, f"Схема {i}") for i in range(1, 4)]
        plans = get(h, "/api/v2/plans")
        assert len(plans) == 10, len(plans)
        kinds = sorted(p["plan_kind"] for p in plans)
        assert kinds == ["floor"] * 7 + ["single_line"] * 3, kinds
        print("[OK] 10 планов (7 этажей + 3 схемы) созданы и перечислены")

        # холст схемы без картинки: размеры — холст, не изображение
        s1 = schemes[0]
        assert not s1.get("image_width") and s1["canvas_width"] == 2000 and s1["canvas_height"] == 1200

        # одна и та же точка: на плане этажа (пиксели картинки) и ДВАЖДЫ на схеме (холст)
        i_floor = post(h, f"/api/v2/plans/{floors[0]['id']}/items", {
            "kind": "point", "point_id": pid, "geometry": {"x": 120, "y": 80},
            "coord_space": "image_px_xy_v2"})
        i_sl_1 = post(h, f"/api/v2/plans/{s1['id']}/items", {
            "kind": "point", "point_id": pid, "geometry": {"x": 300, "y": 200},
            "coord_space": "canvas_xy_v2"})
        i_sl_2 = post(h, f"/api/v2/plans/{s1['id']}/items", {
            "kind": "point", "point_id": pid, "geometry": {"x": 1500, "y": 900},
            "coord_space": "canvas_xy_v2"})
        assert len({i_floor["id"], i_sl_1["id"], i_sl_2["id"]}) == 3
        on_floor = [i for i in get(h, f"/api/v2/plans/{floors[0]['id']}")["items"] if i["kind"] == "point"]
        on_scheme = [i for i in get(h, f"/api/v2/plans/{s1['id']}")["items"] if i["kind"] == "point"]
        assert [i["point_id"] for i in on_floor] == [pid]
        assert [i["point_id"] for i in on_scheme] == [pid, pid], on_scheme
        assert sorted((i["geometry"]["x"], i["geometry"]["y"]) for i in on_scheme) == [(300, 200), (1500, 900)]
        print("[OK] точка стоит на плане этажа и дважды на схеме (три разных значка, одна точка)")

        # элемент схемы (узел) — тоже на обоих видах планов, на схеме дважды
        for plan, space in ((floors[0], "image_px_xy_v2"), (s1, "canvas_xy_v2"), (s1, "canvas_xy_v2")):
            post(h, f"/api/v2/plans/{plan['id']}/items", {
                "kind": "node", "node_id": site["panel"]["id"],
                "geometry": {"x": 50 + 10 * len(plan.get("name", "")), "y": 400}, "coord_space": space})
        node_items = [i for i in get(h, f"/api/v2/plans/{s1['id']}")["items"] if i["kind"] == "node"]
        assert len(node_items) == 2 and {i["node_id"] for i in node_items} == {site["panel"]["id"]}
        print("[OK] узел щита: на плане этажа один значок, на схеме два")

        # значения те же, итог объекта не изменился (числа считаются по точкам учёта, не по значкам)
        after = site_snapshot(h, site)
        assert after == before, (before, after)
        print("[OK] A26: значения точек, связи, привязки и итог объекта до/после расстановки одинаковы "
              f"(итог {after['object_total']} кВт·ч, точка {after['energy'][pid]})")

        # холст без картинки: значок за границей холста не принимается
        r = h.client.post(f"/api/v2/plans/{s1['id']}/items", json={
            "kind": "point", "point_id": pid, "geometry": {"x": 99999, "y": 99999},
            "coord_space": "canvas_xy_v2"})
        assert r.status_code == 400, (r.status_code, r.get_json())
        print("[OK] холст схемы без картинки: значок за границей холста (99999, 99999) — 400")


def test_a27_removing_views_keeps_points_edges_and_history():
    with Harness() as h:
        site = build_site(h)
        pid = site["points"][0]["id"]
        before = site_snapshot(h, site)

        scheme = create_scheme(h, "Схема A27")
        other = create_scheme(h, "Схема-соседка")
        floor = create_floor(h, "Этаж A27")

        def lay_out(plan):
            """Узлы и все линии схемы на плане одним /layout."""
            nodes = [site["src"], site["panel"]] + [c["node"] for c in site["loads"]]
            ops = [{"op": "upsert", "kind": "node", "node_id": n["id"],
                    "geometry": {"x": 100 + 150 * i, "y": 100 + 120 * (i % 2)},
                    "coord_space": "canvas_xy_v2", "label": n["name"]} for i, n in enumerate(nodes)]
            idx = {n["id"]: i for i, n in enumerate(nodes)}
            edges = [(site["edge_in"]["id"], site["src"]["id"], site["panel"]["id"])]
            edges += [(c["edge"]["id"], c["edge"]["from_node_id"], c["edge"]["to_node_id"])
                      for c in site["loads"]]
            vops = [{"op": "upsert", "edge_id": eid, "from_item_id": "$%d" % idx[a],
                     "to_item_id": "$%d" % idx[b], "view_kind": "structural", "waypoints": None}
                    for eid, a, b in edges]
            rev = get(h, f"/api/v2/plans/{plan['id']}")["canvas_revision"]
            post(h, f"/api/v2/plans/{plan['id']}/layout", {
                "expected_revision": rev, "item_ops": ops, "edge_view_ops": vops})
            return get(h, f"/api/v2/plans/{plan['id']}")

        sch = lay_out(scheme)
        oth = lay_out(other)
        assert len(sch["items"]) == 4 and len(sch["edges"]) == 3, (len(sch["items"]), len(sch["edges"]))
        # точка ещё и на плане этажа
        post(h, f"/api/v2/plans/{floor['id']}/items", {
            "kind": "point", "point_id": pid, "geometry": {"x": 10, "y": 10}, "coord_space": "image_px_xy_v2"})
        assert site_snapshot(h, site) == before
        print("[OK] подготовка: две схемы с 4 значками и 3 линиями + точка на плане этажа")

        # --- удаление трассы (edge view) ---
        view = sch["edges"][0]
        r = h.client.delete(f"/api/v2/plans/{scheme['id']}/edges/{view['id']}")
        assert r.status_code == 200, r.get_json()
        now = get(h, f"/api/v2/plans/{scheme['id']}")
        assert len(now["edges"]) == 2 and len(now["items"]) == 4
        assert site_snapshot(h, site) == before, "удаление трассы не должно менять точки/связи/историю"
        assert len(get(h, f"/api/v2/plans/{other['id']}")["edges"]) == 3, "чужая схема не тронута"
        print("[OK] A27: удалена трасса — связь, точки, привязки, история и чужая схема на месте")

        # --- ручная трасса: сброс waypoints тоже только представление ---
        v2 = now["edges"][0]
        rev = now["canvas_revision"]
        post(h, f"/api/v2/plans/{scheme['id']}/layout", {"expected_revision": rev, "item_ops": [], "edge_view_ops": [
            {"op": "upsert", "id": v2["id"], "edge_id": v2["edge_id"], "from_item_id": v2["from_item_id"],
             "to_item_id": v2["to_item_id"], "view_kind": "structural",
             "waypoints": [[100, 100], [100, 200], [250, 200]]}]})
        again = get(h, f"/api/v2/plans/{scheme['id']}")
        assert next(v for v in again["edges"] if v["id"] == v2["id"])["waypoints"] == \
            [[100, 100], [100, 200], [250, 200]]
        post(h, f"/api/v2/plans/{scheme['id']}/layout", {
            "expected_revision": again["canvas_revision"], "item_ops": [], "edge_view_ops": [
                {"op": "upsert", "id": v2["id"], "edge_id": v2["edge_id"],
                 "from_item_id": v2["from_item_id"], "to_item_id": v2["to_item_id"],
                 "view_kind": "structural", "waypoints": None}]})
        assert next(v for v in get(h, f"/api/v2/plans/{scheme['id']}")["edges"]
                    if v["id"] == v2["id"])["waypoints"] in (None, [])
        assert site_snapshot(h, site) == before
        print("[OK] A27: ручная трасса записана и сброшена — топология и история те же")

        # --- удаление значка вместе с его линиями ---
        panel_item = next(i for i in now["items"] if i["node_id"] == site["panel"]["id"])
        r = h.client.delete(f"/api/v2/plans/{scheme['id']}/items/{panel_item['id']}?edge_views=remove")
        assert r.status_code == 200, r.get_json()
        now = get(h, f"/api/v2/plans/{scheme['id']}")
        assert len(now["items"]) == 3 and all(
            panel_item["id"] not in (v["from_item_id"], v["to_item_id"]) for v in now["edges"])
        assert site_snapshot(h, site) == before, "удаление значка щита не должно менять топологию"
        node_still = [n for n in get(h, "/api/v2/topology/nodes") if n["id"] == site["panel"]["id"]]
        assert node_still and not node_still[0].get("archived_at")
        print("[OK] A27: удалён значок щита с линиями — сам щит, линии, точки и история на месте")

        # --- удаление всего плана ---
        r = h.client.delete(f"/api/v2/plans/{scheme['id']}")
        assert r.status_code == 200, r.get_json()
        assert h.client.get(f"/api/v2/plans/{scheme['id']}").status_code == 404
        assert site_snapshot(h, site) == before, "удаление плана не должно менять топологию и историю"
        survivors = get(h, f"/api/v2/plans/{other['id']}")
        assert len(survivors["items"]) == 4 and len(survivors["edges"]) == 3, "чужая схема цела"
        assert len(get(h, f"/api/v2/plans/{floor['id']}")["items"]) == 1, "план этажа цел"
        print("[OK] A27: удалён план — точки, связи, привязки, история, другие планы целы "
              f"(итог объекта {before['object_total']} кВт·ч до и после)")

        # --- и последний план ---
        for plan in (other, floor):
            assert h.client.delete(f"/api/v2/plans/{plan['id']}").status_code == 200
        assert get(h, "/api/v2/plans") == []
        assert site_snapshot(h, site) == before
        print("[OK] A27: все планы удалены — топология, точки и история без изменений")


if __name__ == "__main__":
    test_a26_point_on_floor_plan_and_scheme_twice()
    test_a27_removing_views_keeps_points_edges_and_history()
    print("\n[ALL OK] test_step70_plan_views")
