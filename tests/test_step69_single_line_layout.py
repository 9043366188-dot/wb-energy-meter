"""Тесты партии 14 (14.2/14.3/14.5): раскладка и трассы однолинейной схемы.

Самостоятельный скрипт (не pytest):
    python tests/test_step69_single_line_layout.py

Чистые функции wb_energy_meter/static/js/scheme-layout.js (без DOM) гоняются
под системным `node` без новых зависимостей; сохранение раскладки и чтение
назад — через настоящий POST /api/v2/plans/<id>/layout. Если node нет в
песочнице, JS-часть пропускается явно ([SKIP]), как и у остальных тестов,
которым нужен node (test_step28_reports_query.py)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

JS_PATH = os.path.join(REPO_ROOT, "wb_energy_meter", "static", "js", "scheme-layout.js")


def node_available():
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def run_js(body):
    """body — JS-код, видит SL (модуль) и обязан вызвать out(значение)."""
    src = (
        "const SL = require(%s);\n"
        "const out = (v) => console.log(JSON.stringify(v));\n%s\n"
    ) % (json.dumps(JS_PATH), body)
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# Типовая сеть из сценария партии: 1 ввод, 2 щита, 5 потребителей.
STD_NODES = [
    {"id": 1, "kind": "source", "label": "Ввод 1"},
    {"id": 2, "kind": "panel", "label": "Щит 1"},
    {"id": 3, "kind": "panel", "label": "Щит 2"},
    {"id": 4, "kind": "load", "label": "Освещение"},
    {"id": 5, "kind": "load", "label": "Розетки"},
    {"id": 6, "kind": "load", "label": "Вентиляция"},
    {"id": 7, "kind": "load", "label": "Кондиционер"},
    {"id": 8, "kind": "load", "label": "Серверная"},
]
STD_EDGES = [
    {"id": 1, "from": 1, "to": 2}, {"id": 2, "from": 1, "to": 3},
    {"id": 3, "from": 2, "to": 4}, {"id": 4, "from": 2, "to": 5},
    {"id": 5, "from": 3, "to": 6}, {"id": 6, "from": 3, "to": 7},
    {"id": 7, "from": 3, "to": 8},
]
CANVAS = {"width": 2000, "height": 1200}


def big_network(panels=6, loads=12):
    """Ввод → panels щитов → у каждого loads нагрузок и один подщит с 3 нагрузками."""
    nodes = [{"id": 1, "kind": "source", "label": "Ввод 1"}]
    edges = []
    nid, eid = 2, 1
    for p in range(panels):
        pid = nid; nid += 1
        nodes.append({"id": pid, "kind": "panel", "label": "Щит %d" % (p + 1)})
        edges.append({"id": eid, "from": 1, "to": pid}); eid += 1
        sub = nid; nid += 1
        nodes.append({"id": sub, "kind": "panel", "label": "Подщит %d" % (p + 1)})
        edges.append({"id": eid, "from": pid, "to": sub}); eid += 1
        for k in range(3):
            n = nid; nid += 1
            nodes.append({"id": n, "kind": "load", "label": "Нагр. %d.%d" % (p + 1, k + 1)})
            edges.append({"id": eid, "from": sub, "to": n}); eid += 1
        for k in range(loads):
            n = nid; nid += 1
            nodes.append({"id": n, "kind": "load", "label": "Нагрузка %d-%d" % (p + 1, k + 1)})
            edges.append({"id": eid, "from": pid, "to": n}); eid += 1
    return nodes, edges


def layout_js(nodes, edges, canvas=CANVAS, mode="all", fixed=None):
    return ("SL.layoutTree({nodes: %s, edges: %s, canvas: %s, mode: %s, fixed: %s})" % (
        json.dumps(nodes), json.dumps(edges), json.dumps(canvas),
        json.dumps(mode), json.dumps(fixed or {})))


def test_layout_is_deterministic_and_order_independent():
    nodes, edges = STD_NODES, STD_EDGES
    res = run_js("""
      const a = %s;
      const b = %s;
      const rn = %s.slice().reverse(), re = %s.slice().reverse();
      const c = SL.layoutTree({nodes: rn, edges: re, canvas: %s, mode: 'all'});
      out({a: a.positions, b: b.positions, c: c.positions});
    """ % (layout_js(nodes, edges), layout_js(nodes, edges),
           json.dumps(nodes), json.dumps(edges), json.dumps(CANVAS)))
    assert res["a"] == res["b"], "дважды один и тот же вход — разные позиции"
    assert res["a"] == res["c"], "порядок узлов/связей на входе влияет на результат"
    assert set(res["a"]) == {str(n["id"]) for n in nodes}
    print("[OK] test_layout_is_deterministic_and_order_independent")


def test_rows_sources_top_panels_middle_loads_bottom():
    res = run_js("out(%s)" % layout_js(STD_NODES, STD_EDGES))
    pos = res["positions"]
    assert res["fits"] is True
    y = {k: v["y"] for k, v in pos.items()}
    assert y["1"] < y["2"] == y["3"] < y["4"] == y["5"] == y["6"] == y["7"] == y["8"], y
    # родитель — по центру над детьми (щит 2 над своими тремя нагрузками)
    xs = [pos[str(i)]["x"] for i in (6, 7, 8)]
    assert abs(pos["3"]["x"] - (min(xs) + max(xs)) / 2) <= 1
    # соседние ветви не пересекаются по горизонтали: нагрузки щита 1 левее нагрузок щита 2
    assert max(pos["4"]["x"], pos["5"]["x"]) < min(xs)
    print("[OK] test_rows_sources_top_panels_middle_loads_bottom")


def test_no_overlap_by_boxes_with_labels():
    nodes, edges = big_network()
    canvas = {"width": 6000, "height": 1200}
    res = run_js("""
      const nodes = %s, edges = %s;
      const L = SL.layoutTree({nodes, edges, canvas: %s, mode: 'all'});
      const boxes = {};
      nodes.forEach(n => { boxes[n.id] = SL.nodeBox(L.positions[n.id], n.label); });
      out({fits: L.fits, needed: L.needed, overlaps: SL.findOverlaps(boxes), n: nodes.length});
    """ % (json.dumps(nodes), json.dumps(edges), json.dumps(canvas)))
    assert res["n"] > 100
    assert res["overlaps"] == [], res["overlaps"][:5]
    print("[OK] test_no_overlap_by_boxes_with_labels (%d узлов)" % res["n"])


def test_auto_routes_are_strictly_orthogonal_and_share_bus():
    nodes, edges = STD_NODES, STD_EDGES
    res = run_js("""
      const L = %s;
      const items = {};
      Object.keys(L.positions).forEach(k => { items[k] = L.positions[k]; });
      const R = SL.routeEdges({items, edges: %s.map(e => ({id: e.id, from: String(e.from), to: String(e.to), waypoints: null}))});
      out({routes: R.routes, pos: L.positions});
    """ % (layout_js(nodes, edges), json.dumps(edges)))
    routes = res["routes"]
    assert len(routes) == len(STD_EDGES)
    for eid, r in routes.items():
        pts = r["points"]
        assert r["manual"] is False and r["orthogonal"] is True
        for a, b in zip(pts, pts[1:]):
            assert a[0] == b[0] or a[1] == b[1], ("не ортогонально", eid, pts)
    # линии одного щита (id 3 → 6,7,8 это связи 5,6,7) делят один горизонтальный участок
    def horiz_y(eid):
        pts = routes[str(eid)]["points"]
        ys = {a[1] for a, b in zip(pts, pts[1:]) if a[1] == b[1] and a[0] != b[0]}
        return ys
    assert horiz_y(5) == horiz_y(6) == horiz_y(7) and len(horiz_y(5)) == 1
    assert horiz_y(1) == horiz_y(2) and len(horiz_y(1)) == 1
    # трасса начинается в центре значка-источника и кончается в центре получателя
    pos = res["pos"]
    p = routes["3"]["points"]
    assert p[0] == [pos["2"]["x"], pos["2"]["y"]] and p[-1] == [pos["4"]["x"], pos["4"]["y"]]
    print("[OK] test_auto_routes_are_strictly_orthogonal_and_share_bus")


def test_route_edge_cases_sideways_upward_and_aligned():
    res = run_js("""
      const items = {a: {x: 100, y: 100}, b: {x: 100, y: 300}, c: {x: 400, y: 100},
                     d: {x: 250, y: 20}, e: {x: 100, y: 130}};
      const R = SL.routeEdges({items, edges: [
        {id: 1, from: 'a', to: 'b', waypoints: null},   // ровно вниз: прямая
        {id: 2, from: 'a', to: 'c', waypoints: null},   // в том же ряду: горизонталь
        {id: 3, from: 'a', to: 'd', waypoints: null},   // вверх-вбок
        {id: 4, from: 'a', to: 'e', waypoints: null},   // слишком близко вниз
      ]});
      out(R.routes);
    """)
    assert res["1"]["points"] == [[100, 100], [100, 300]]
    assert res["2"]["points"] == [[100, 100], [400, 100]]
    for k in ("3", "4"):
        pts = res[k]["points"]
        assert pts[0] == [100, 100]
        for a, b in zip(pts, pts[1:]):
            assert a[0] == b[0] or a[1] == b[1], (k, pts)
    print("[OK] test_route_edge_cases_sideways_upward_and_aligned")


def test_manual_route_follows_nodes_and_flags_non_orthogonal():
    res = run_js("""
      const wp = [[100, 100], [100, 200], [300, 200], [300, 300]];
      const edge = {id: 1, from: 'a', to: 'b', waypoints: wp};
      const ok = SL.routeEdges({items: {a: {x: 100, y: 100}, b: {x: 300, y: 300}}, edges: [edge]});
      // узел-получатель сдвинули — конец тянется за ним, последний отрезок косой
      const moved = SL.routeEdges({items: {a: {x: 100, y: 100}, b: {x: 340, y: 320}}, edges: [edge]});
      out({ok: ok.routes[1], moved: moved.routes[1]});
    """)
    assert res["ok"]["manual"] is True and res["ok"]["orthogonal"] is True
    assert res["ok"]["points"] == [[100, 100], [100, 200], [300, 200], [300, 300]]
    assert res["moved"]["manual"] is True and res["moved"]["orthogonal"] is False
    assert res["moved"]["points"][-1] == [340, 320]
    print("[OK] test_manual_route_follows_nodes_and_flags_non_orthogonal")


def test_snap_orthogonal_makes_every_segment_axis_aligned():
    res = run_js("""
      const from = {x: 100, y: 100}, to = {x: 500, y: 420};
      const drawn = [[130, 112], [310, 140], [305, 330], [480, 345]];   // «от руки», косо
      const path = SL.snapOrthogonal(drawn, from, to);
      out({path, ortho: SL.isOrthogonal(path, 0)});
    """)
    assert res["ortho"] is True, res["path"]
    assert res["path"][0] == [100, 100] and res["path"][-1] == [500, 420]
    print("[OK] test_snap_orthogonal_makes_every_segment_axis_aligned")


def test_unplaced_mode_keeps_placed_nodes_and_avoids_them():
    fixed = {"1": {"x": 600, "y": 96}, "2": {"x": 400, "y": 246}}
    res = run_js("out(%s)" % layout_js(STD_NODES, STD_EDGES, mode="unplaced", fixed=fixed))
    pos = res["positions"]
    assert "1" not in pos and "2" not in pos, "стоящие значки не должны двигаться"
    assert set(pos) == {"3", "4", "5", "6", "7", "8"}
    boxes = {}
    labels = {str(n["id"]): n["label"] for n in STD_NODES}
    allp = dict(fixed); allp.update(pos)
    out = run_js("""
      const labels = %s, allp = %s;
      const boxes = {};
      Object.keys(allp).forEach(k => { boxes[k] = SL.nodeBox(allp[k], labels[k]); });
      out(SL.findOverlaps(boxes));
    """ % (json.dumps(labels), json.dumps(allp)))
    assert out == [], out
    # без стоящих значков 'unplaced' даёт ту же раскладку, что и 'all'
    a = run_js("out(%s)" % layout_js(STD_NODES, STD_EDGES, mode="unplaced"))
    b = run_js("out(%s)" % layout_js(STD_NODES, STD_EDGES, mode="all"))
    assert a["positions"] == b["positions"]
    print("[OK] test_unplaced_mode_keeps_placed_nodes_and_avoids_them")


def test_cycles_multi_parent_and_isolated_do_not_break():
    nodes = [
        {"id": 1, "kind": "source", "label": "Ввод"},
        {"id": 2, "kind": "panel", "label": "А"},
        {"id": 3, "kind": "panel", "label": "Б"},
        {"id": 4, "kind": "load", "label": "Н"},
        {"id": 5, "kind": "panel", "label": "Одинокий щит"},
        {"id": 6, "kind": "load", "label": "Одинокая нагрузка"},
        {"id": 7, "kind": "panel", "label": "Цикл-1"},
        {"id": 8, "kind": "panel", "label": "Цикл-2"},
    ]
    edges = [
        {"id": 1, "from": 1, "to": 2}, {"id": 2, "from": 1, "to": 3},
        {"id": 3, "from": 2, "to": 4}, {"id": 4, "from": 3, "to": 4},   # два родителя
        {"id": 5, "from": 7, "to": 8}, {"id": 6, "from": 8, "to": 7},   # цикл
        {"id": 7, "from": 2, "to": 2},                                  # петля
        {"id": 8, "from": 1, "to": 999},                                # висячая связь
    ]
    res = run_js("""
      const nodes = %s, edges = %s;
      const L = SL.layoutTree({nodes, edges, canvas: %s, mode: 'all'});
      const boxes = {};
      nodes.forEach(n => { boxes[n.id] = SL.nodeBox(L.positions[n.id], n.label); });
      out({n: Object.keys(L.positions).length, overlaps: SL.findOverlaps(boxes), fits: L.fits});
    """ % (json.dumps(nodes), json.dumps(edges), json.dumps(CANVAS)))
    assert res["n"] == len(nodes) and res["overlaps"] == [] and res["fits"] is True
    print("[OK] test_cycles_multi_parent_and_isolated_do_not_break")


def test_too_wide_tree_reports_needed_canvas():
    nodes, edges = big_network()
    res = run_js("out(%s)" % layout_js(nodes, edges, canvas={"width": 2000, "height": 1200}))
    assert res["fits"] is False
    assert res["needed"]["width"] > 2000
    res2 = run_js("out(%s)" % layout_js(
        nodes, edges, canvas={"width": res["needed"]["width"], "height": 1200}))
    assert res2["fits"] is True
    print("[OK] test_too_wide_tree_reports_needed_canvas (нужно %d px)" % res["needed"]["width"])


def test_natural_compare_orders_numbers():
    res = run_js("""out(['Щит 10', 'Щит 2', 'щит 1', 'Ввод'].sort(SL.naturalCompare));""")
    assert res == ["Ввод", "щит 1", "Щит 2", "Щит 10"], res
    print("[OK] test_natural_compare_orders_numbers")


# --------- сохранение через настоящий /layout и чтение назад ---------

def make_plan_client():
    from wb_energy_meter.api import create_app, _AppState
    from wb_energy_meter.db import Database
    from wb_energy_meter.model import MeterRegistry
    from wb_energy_meter.repo import GroupRepo, MeterRepo
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)
    db = Database(path=path)
    db.open()
    groups_repo = GroupRepo(db)
    state = _AppState(
        registry=MeterRegistry(), meters_repo=MeterRepo(db, groups_repo),
        groups_repo=groups_repo, is_mqtt_connected=lambda: False,
        mqtt_message_count=lambda: 0, mqtt_error_count=lambda: 0,
        wb_db_client=None, consumption_service=None, started_at=time.time(),
        db=db, plans_dir=tempfile.mkdtemp())
    return create_app(state).test_client(), db, path


def test_layout_roundtrip_through_api_returns_same_coordinates():
    client, db, path = make_plan_client()
    try:
        def rev():
            return client.get("/api/v2/revision").get_json()["configuration_revision"]
        ids = {}
        for n in STD_NODES:
            r = client.post("/api/v2/topology/nodes", json={
                "code": "n%d" % n["id"], "name": n["label"], "kind": n["kind"],
                "expected_revision": rev()})
            assert r.status_code == 201, r.get_json()
            ids[n["id"]] = r.get_json()["id"]
        edge_ids = {}
        for e in STD_EDGES:
            r = client.post("/api/v2/topology/edges/connect", json={
                "from_node_id": ids[e["from"]], "to_node_id": ids[e["to"]],
                "expected_revision": rev()})
            assert r.status_code == 201, r.get_json()
            edge_ids[e["id"]] = r.get_json()["id"]

        r = client.post("/api/v2/plans", data={
            "name": "Схема", "plan_kind": "single_line",
            "canvas_width": "2000", "canvas_height": "1200"})
        assert r.status_code == 201, r.get_json()
        plan = r.get_json()
        assert plan["plan_kind"] == "single_line" and plan["canvas_revision"] == 1

        nodes = [{"id": ids[n["id"]], "kind": n["kind"], "label": n["label"]} for n in STD_NODES]
        edges = [{"id": edge_ids[e["id"]], "from": ids[e["from"]], "to": ids[e["to"]]}
                 for e in STD_EDGES]
        lay = run_js("out(%s)" % layout_js(nodes, edges))
        assert lay["fits"] is True
        pos = lay["positions"]

        item_ops = [{"op": "upsert", "kind": "node", "node_id": n["id"],
                     "geometry": pos[str(n["id"])], "coord_space": "canvas_xy_v2",
                     "label": n["label"]} for n in nodes]
        idx = {n["id"]: i for i, n in enumerate(nodes)}
        view_ops = [{"op": "upsert", "edge_id": e["id"], "from_item_id": "$%d" % idx[e["from"]],
                     "to_item_id": "$%d" % idx[e["to"]], "view_kind": "structural",
                     "waypoints": None} for e in edges]
        r = client.post("/api/v2/plans/%d/layout" % plan["id"], json={
            "expected_revision": plan["canvas_revision"],
            "item_ops": item_ops, "edge_view_ops": view_ops})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["canvas_revision"] == 2

        got = client.get("/api/v2/plans/%d" % plan["id"]).get_json()
        assert len(got["items"]) == len(nodes) and len(got["edges"]) == len(edges)
        for it in got["items"]:
            want = pos[str(it["node_id"])]
            assert it["geometry"] == {"x": want["x"], "y": want["y"]}, (it, want)
            assert it["coord_space"] == "canvas_xy_v2"
        # устаревшая ревизия — 409, ничего не изменилось
        r = client.post("/api/v2/plans/%d/layout" % plan["id"], json={
            "expected_revision": 1, "item_ops": [], "edge_view_ops": []})
        assert r.status_code == 409 and r.get_json()["code"] == "revision_conflict"
        print("[OK] test_layout_roundtrip_through_api_returns_same_coordinates")
    finally:
        db.close(); os.unlink(path)


if __name__ == "__main__":
    if not node_available():
        print("[SKIP] раскладка схемы: node недоступен в этой песочнице")
    else:
        test_layout_is_deterministic_and_order_independent()
        test_rows_sources_top_panels_middle_loads_bottom()
        test_no_overlap_by_boxes_with_labels()
        test_auto_routes_are_strictly_orthogonal_and_share_bus()
        test_route_edge_cases_sideways_upward_and_aligned()
        test_manual_route_follows_nodes_and_flags_non_orthogonal()
        test_snap_orthogonal_makes_every_segment_axis_aligned()
        test_unplaced_mode_keeps_placed_nodes_and_avoids_them()
        test_cycles_multi_parent_and_isolated_do_not_break()
        test_too_wide_tree_reports_needed_canvas()
        test_natural_compare_orders_numbers()
        test_layout_roundtrip_through_api_returns_same_coordinates()
    print("[ALL OK] test_step69_single_line_layout")
