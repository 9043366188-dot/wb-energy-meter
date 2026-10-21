"""Партия 14 (0.24.0), 14.5: однолинейная схема в настоящем Chromium.

(В ТЗ сценарий назван test_b09_single_line.py; b09 к этому моменту занят «Структурой»
— tests/browser/test_b09_structure_tree.py, поэтому номер b23.)

Сценарий (чистая БД, настоящее Flask-приложение):
  1. Топология через API: ввод, два щита, пять нагрузок; на вводе щита 1 и на его трёх
     нагрузках стоят счётчики с энергией (баланс щита 1 определён), щит 2 без счётчиков.
  2. «План v3» → «+ Схема» → «Создать схему» → «Разложить неразмещённые».
  3. Все восемь узлов размещены, рамки значков с подписями не пересекаются.
  4. Все сегменты линий строго горизонтальны или вертикальны; линии вводa к двум щитам делят
     одну «шину»; линии без счётчика — пунктир, со счётчиком — сплошные.
  5. Перезагрузка — позиции те же; значок баланса щита 1 равен ответу /balance (и его нет у щита 2).
  6. «Перераскладить всё» после сдвига значка → «Отменить автораскладку» возвращает прежние позиции.
  7. 409: чужое сохранение раскладки → окно конфликта, «Разложить заново» применяет раскладку.
  8. Ручная трасса: «Нарисовать трассу» → отрезки по осям, концы на значках; сдвиг щита делает её
     «не по осям»; «Сделать автоматической» снова считает трассу сама.
  9. Перетаскивание значка мышью сохраняется, автоматические линии остаются ортогональными.
 10. Печать: стиль @media print прячет шапку и панели; PDF строится без ошибок консоли.

Запуск: python tests/browser/test_b23_single_line.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []
LOADS_1 = ["Станок", "Освещение", "Вентиляция"]
LOADS_2 = ["Розетки", "Насос"]
# Окно баланса — целые сутки: каждый час окна должен быть закрыт агрегатом (иначе «gap»,
# см. комментарий в test_b03_planv3_flow.py). Сутки в прошлом; привязки открыты с valid_from=0.
# Даты в строках сервер разбирает по своему местному времени — границу суток считаем так же.
FROM, TO = "2026-01-10", "2026-01-11"
DAY_START = int(datetime.strptime(FROM, "%Y-%m-%d").timestamp())
HOUR = 3600


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def api(page, path):
    return page.evaluate(f"fetch({path!r}).then(r=>r.json())")


def js_layout_state(page):
    """Положения значков, точки линий и стиль штриха — прямо из слоёв Leaflet."""
    return page.evaluate("""() => {
      const items = {}, edges = {};
      _planV3ItemLayers.forEach((l, id) => { const p = l.getLatLng(); items[id] = [p.lat, p.lng]; });
      _planV3EdgeLayers.forEach((l, id) => {
        edges[id] = {pts: l.getLatLngs().map(p => [p.lat, p.lng]),
                     dash: l.options.dashArray || null,
                     bad: (l.options.className || '').includes('sl-line-bad')};
      });
      return {items, edges};
    }""")


def is_ortho(pts, tol=0.6):
    return all(abs(a[0] - b[0]) <= tol or abs(a[1] - b[1]) <= tol
               for a, b in zip(pts, pts[1:]))


def rects_intersect(a, b, pad=0.0):
    return (a["left"] < b["right"] + pad and b["left"] < a["right"] + pad
            and a["top"] < b["bottom"] + pad and b["top"] < a["bottom"] + pad)


def plan_nodes(page, plan_id):
    plan = api(page, f"/api/v2/plans/{plan_id}")
    return plan, {it["node_id"]: it for it in plan["items"] if it["kind"] == "node"}


def open_scheme_tab(page):
    page.wait_for_function("() => !!document.querySelector('[x-data]')._x_dataStack")
    if page.evaluate(APP + ".uiSet") != "v2":
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(300)
    page.locator("nav button:visible", has_text="План v3").click()
    page.wait_for_function("() => typeof _planV3Map !== 'undefined' && !!_planV3Map", timeout=15000)


def wait_state(page, expr, timeout=15000):
    page.wait_for_function(f"() => {{ const a = {APP}; return !!({expr}); }}", timeout=timeout)


def main() -> int:
    with Harness() as h:
        # ---- 1. топология и счётчики -------------------------------------------------
        src = h.make_node("SRC", "Ввод", "source")
        p1 = h.make_node("P1", "Щит 1", "panel")
        p2 = h.make_node("P2", "Щит 2", "panel")
        e_in1 = h.connect(src["id"], p1["id"])
        e_in2 = h.connect(src["id"], p2["id"])
        c1 = [h.add_consumer(p1["id"], n) for n in LOADS_1]
        c2 = [h.add_consumer(p2["id"], n) for n in LOADS_2]
        meters = [(e_in1["id"], "dev-in1", 10.0)]
        meters += [(c["edge"]["id"], f"dev-l{i}", kwh)
                   for i, (c, kwh) in enumerate(zip(c1, (3.0, 3.0, 2.0)))]
        for i, (edge_id, dev, kwh) in enumerate(meters):
            pt = h.make_point(f"PT{i}", f"Точка {i}")
            h.bind_meter(pt["id"], dev, f"Счётчик {i}")
            h.set_meter_on_edge(edge_id, pt["id"])
            for k in range(24):
                h.seed_energy(dev, kwh if k == 0 else 0.0,
                              period_start=DAY_START + k * HOUR, period_end=DAY_START + (k + 1) * HOUR)

        bp = open_browser(h.base_url, viewport={"width": 1400, "height": 1000}, timezone_id="UTC")
        page = bp.page
        page.wait_for_timeout(1000)
        open_scheme_tab(page)
        page.evaluate(f"{APP}.ovFrom = {FROM!r}; {APP}.ovTo = {TO!r}")

        # ---- 2. новая схема и автораскладка -----------------------------------------
        check(not page.locator("[data-testid=sl-new-scheme]").is_visible(),
              "пока планов нет, есть только форма первого плана (кнопка «+ Схема» появляется с первым планом)")
        page.fill("input[placeholder='Название плана']", "Схема 1")
        page.click("[data-testid=sl-empty-create]")
        wait_state(page, "a.planV3Current && a.planV3Current.plan_kind==='single_line' "
                         "&& a.structNodes.length===8")
        plan_id = page.evaluate(APP + ".planV3Current.id")
        dims = page.evaluate(f"{APP}._planV3EffectiveDims({APP}.planV3Current)")
        check(1200 <= dims[0] <= 2000 and 700 <= dims[1] <= 1200,
              f"холст схемы подобран по топологии (небольшая схема — не 2000×1200): {dims}")
        check(page.locator("[data-testid=planv3-kind]").inner_text() == "Схема",
              "в панели плана подписан вид «Схема»")
        groups = page.evaluate("[...document.querySelectorAll('[data-testid=planv3-select] optgroup')]"
                               ".map(g => g.label)")
        check(groups == ["Однолинейные схемы"], f"список планов сгруппирован по виду: {groups}")

        wait_state(page, "a.slUnplacedCount()===8")
        label = page.locator("[data-testid=sl-auto-unplaced]").inner_text()
        check("(8)" in label, f"кнопка раскладки показывает число неразмещённых: {label!r}")
        page.click("[data-testid=sl-auto-unplaced]")
        wait_state(page, "a.planV3Current.items.filter(i=>i.kind==='node').length===8 "
                         "&& a.planV3Current.edges.length===7 && !a.slAutoBusy")
        plan, items = plan_nodes(page, plan_id)
        nodes = {n["name"]: n for n in api(page, "/api/v2/topology/nodes")}
        check(len(items) == 8, f"все 8 узлов размещены на схеме: {len(items)}")
        check(len(plan["edges"]) == 7, f"нарисованы все 7 линий: {len(plan['edges'])}")
        xy = {name: (items[n["id"]]["geometry"]["x"], items[n["id"]]["geometry"]["y"])
              for name, n in nodes.items()}
        check(xy["Ввод"][1] < xy["Щит 1"][1] < xy["Станок"][1],
              f"вводы сверху, щиты ниже, нагрузки внизу: {xy}")
        check(xy["Щит 1"][1] == xy["Щит 2"][1] and len({xy[n][1] for n in LOADS_1 + LOADS_2}) == 1,
              "щиты в одном ряду, нагрузки в одном ряду")
        check(all(0 <= x <= dims[0] and 0 <= y <= dims[1] for x, y in xy.values()),
              "все значки внутри холста")
        check(page.evaluate(APP + ".slUnplacedCount()") == 0, "список неразмещённых пуст")
        page.wait_for_timeout(600)
        zoom = page.evaluate("_planV3Map.getZoom()")
        check(abs(zoom) < 0.01, f"после раскладки схема вписана по содержимому в натуральную величину (zoom {zoom})")

        # ---- 3. рамки значков и подписей не пересекаются ----------------------------
        boxes = page.evaluate("""() => {
          const out = {};
          _planV3ItemLayers.forEach((l, id) => {
            const a = l._icon.getBoundingClientRect();
            const t = l.getTooltip() && l.getTooltip()._container ? l.getTooltip()._container.getBoundingClientRect() : a;
            out[id] = {left: Math.min(a.left, t.left), right: Math.max(a.right, t.right),
                       top: Math.min(a.top, t.top), bottom: Math.max(a.bottom, t.bottom),
                       text: l.getTooltip() ? l.getTooltip()._container.textContent.trim() : ''};
          });
          return out;
        }""")
        ids = list(boxes)
        clash = [(boxes[a]["text"], boxes[b]["text"]) for i, a in enumerate(ids) for b in ids[i + 1:]
                 if rects_intersect(boxes[a], boxes[b])]
        check(len(ids) == 8 and not clash, f"рамки значков с подписями не пересекаются: {clash}")
        icon_kinds = page.evaluate("[...document.querySelectorAll('.sl-ico')].map(e => e.className)")
        check(sum("k-source" in c for c in icon_kinds) == 1 and sum("k-panel" in c for c in icon_kinds) == 2
              and sum("k-load" in c for c in icon_kinds) == 5,
              f"значки по типу узла (1 ввод, 2 щита, 5 нагрузок): {icon_kinds}")
        check(page.evaluate("[...document.querySelectorAll('.sl-ico svg')].length") == 8,
              "у каждого значка свой SVG")

        # ---- 4. ортогональность, шина, пунктир ---------------------------------------
        st = js_layout_state(page)
        bad = [k for k, e in st["edges"].items() if not is_ortho(e["pts"])]
        check(len(st["edges"]) == 7 and not bad, f"все сегменты 7 линий горизонтальны/вертикальны (не по осям: {bad})")
        centres = {nid: st["items"][str(items[n["id"]]["id"])] for nid, n in nodes.items()}
        item_by_node = {it["node_id"]: it["id"] for it in items.values()}
        view_of = {v["edge_id"]: v for v in plan["edges"]}
        ends_ok = True
        for v in plan["edges"]:
            pts = st["edges"][str(v["id"])]["pts"]
            a = st["items"][str(v["from_item_id"])]
            b = st["items"][str(v["to_item_id"])]
            ends_ok &= (abs(pts[0][0] - a[0]) < 0.6 and abs(pts[0][1] - a[1]) < 0.6
                        and abs(pts[-1][0] - b[0]) < 0.6 and abs(pts[-1][1] - b[1]) < 0.6)
        check(ends_ok, "каждая линия начинается и заканчивается в центрах своих значков")

        def bus_level(edge_id):
            pts = st["edges"][str(view_of[edge_id]["id"])]["pts"]
            horiz = [(a, b) for a, b in zip(pts, pts[1:]) if abs(a[0] - b[0]) < 0.6 and abs(a[1] - b[1]) > 1]
            return sorted({round(a[0], 1) for a, _ in horiz})

        bus1, bus2 = bus_level(e_in1["id"]), bus_level(e_in2["id"])
        check(bus1 and bus1 == bus2, f"линии ввода к двум щитам делят одну шину (lat {bus1} / {bus2})")
        metered = {e_in1["id"]} | {c["edge"]["id"] for c in c1}
        dash_ok = all((st["edges"][str(view_of[eid]["id"])]["dash"] is None) == (eid in metered)
                      for eid in view_of)
        check(dash_ok, "со счётчиком — сплошная линия, без счётчика — пунктир")
        labels = page.evaluate("[...document.querySelectorAll('.sl-ll')].map(e => e.textContent)")
        check(len(labels) == 7 and sum("без счётчика" in t for t in labels) == 3,
              f"подписи линий: имя + счётчик/«без счётчика» (всего {len(labels)})")
        check(sum("P" in t or "Вт" in t for t in labels if "без счётчика" not in t) == 4,
              "у линий со счётчиком в подписи P сейчас (или «P —», если данных нет)")

        # ---- 5. значок баланса щита 1 == ответу /balance -----------------------------
        bal = page.evaluate("""(args) => fetch('/api/v2/topology/nodes/' + args.id + '/balance', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({from: args.from, to: args.to, timezone: 'UTC'})}).then(r => r.json())""",
                            {"id": p1["id"], "from": FROM, "to": TO})
        check(bal.get("value") is not None, f"сервер определил баланс щита 1: {bal.get('value')}")
        page.wait_for_function("() => document.querySelectorAll('.sl-bal').length >= 1", timeout=15000)
        badges = page.evaluate("[...document.querySelectorAll('.sl-bal')].map(e => ({v: e.dataset.slBalance, t: e.textContent}))")
        check(len(badges) == 1, f"значок баланса только у щита 1 (у щита 2 баланс не определён): {badges}")
        check(badges and float(badges[0]["v"]) == float(bal["value"]),
              f"значок баланса равен ответу /balance ({badges[0]['v'] if badges else None} == {bal.get('value')})")
        check(badges and badges[0]["t"].startswith("Δ ") and "кВт·ч" in badges[0]["t"],
              f"значок со знаком и единицами: {badges[0]['t'] if badges else None!r}")
        bp.screenshot("b23_scheme")

        # ---- reload: позиции те же ---------------------------------------------------
        before = js_layout_state(page)
        page.reload()
        open_scheme_tab(page)
        wait_state(page, "a.planV3Current && a.planV3Current.id===" + str(plan_id) + " && a.planV3Current.items.length===8")
        page.wait_for_timeout(800)
        after = js_layout_state(page)
        same = before["items"] == after["items"] and {k: v["pts"] for k, v in before["edges"].items()} == \
            {k: v["pts"] for k, v in after["edges"].items()}
        check(same, "после перезагрузки значки и трассы на тех же местах")

        # ---- 6. «Перераскладить всё» и отмена ---------------------------------------
        page.evaluate(f"{APP}.ovFrom = {FROM!r}; {APP}.ovTo = {TO!r}")
        plan, items = plan_nodes(page, plan_id)
        victim = items[nodes["Насос"]["id"]]
        orig = {n: dict(it["geometry"]) for n, it in ((k, items[v["id"]]) for k, v in nodes.items())}
        r = page.evaluate("""(args) => fetch('/api/v2/plans/' + args.p + '/items/' + args.i, {
            method: 'PATCH', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({geometry: {x: 111, y: 222}, coord_space: args.cs})}).then(r => r.status)""",
                          {"p": plan_id, "i": victim["id"], "cs": victim["coord_space"]})
        check(r == 200, "значок «Насос» сдвинут вручную (имитация перетаскивания)")
        page.evaluate(APP + ".reloadPlanV3Items()")
        wait_state(page, "a.planV3Current.items.find(i=>i.id===" + str(victim["id"]) + ").geometry.x===111")
        moved_state = plan_nodes(page, plan_id)[1]
        pre = {k: dict(moved_state[v["id"]]["geometry"]) for k, v in nodes.items()}
        page.click("[data-testid=sl-auto-all]")
        page.wait_for_selector("[data-testid=ui-confirm-ok]", timeout=5000)
        page.click("[data-testid=ui-confirm-ok]")
        wait_state(page, "a.slUndo && !a.slAutoBusy && !a.uiConfirmOpen")
        wait_state(page, "a.planV3Current.items.find(i=>i.id===" + str(victim["id"]) + ").geometry.x!==111")
        post = {k: dict(plan_nodes(page, plan_id)[1][v["id"]]["geometry"]) for k, v in nodes.items()}
        check(post["Насос"] == orig["Насос"], f"«перераскладить всё» вернула «Насос» в сетку: {post['Насос']}")
        check(page.locator("[data-testid=sl-undo]").is_visible(), "кнопка «Отменить автораскладку» появилась")
        page.click("[data-testid=sl-undo]")
        wait_state(page, "!a.slUndo && !a.slAutoBusy")
        back = {k: dict(plan_nodes(page, plan_id)[1][v["id"]]["geometry"]) for k, v in nodes.items()}
        check(back == pre, "«Отменить автораскладку» вернула прежние позиции всех значков")
        # вернуть сетку для дальнейших шагов
        page.click("[data-testid=sl-auto-all]")
        page.click("[data-testid=ui-confirm-ok]")
        wait_state(page, "a.slUndo && !a.slAutoBusy && !a.uiConfirmOpen")
        grid = {k: dict(plan_nodes(page, plan_id)[1][v["id"]]["geometry"]) for k, v in nodes.items()}
        check(grid == orig, "повторная раскладка даёт те же позиции (детерминированность)")

        # ---- 7. 409: чужая правка -----------------------------------------------------
        plan, items = plan_nodes(page, plan_id)
        client_rev = page.evaluate(APP + ".planV3Current.canvas_revision")
        it = items[nodes["Станок"]["id"]]
        r = page.evaluate("""(args) => fetch('/api/v2/plans/' + args.p + '/layout', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({expected_revision: args.rev, edge_view_ops: [],
              item_ops: [{op: 'upsert', id: args.it.id, geometry: args.it.geometry,
                          coord_space: args.it.coord_space, label: args.it.label,
                          sort_order: args.it.sort_order || 0}]})}).then(r => r.status)""",
                          {"p": plan_id, "rev": client_rev, "it": it})
        check(r == 200, "«другое окно» сохранило раскладку (ревизия плана выросла)")
        page.click("[data-testid=sl-auto-all]")
        page.click("[data-testid=ui-confirm-ok]")
        page.wait_for_selector("[data-testid=sl-conflict]", timeout=8000)
        check(True, "при 409 открылось окно конфликта ревизий")
        page.click("[data-testid=sl-conflict-retry]")
        page.wait_for_selector("[data-testid=sl-conflict]", state="detached", timeout=8000)
        wait_state(page, "!a.slAutoBusy && a.slUndo")
        retried = {k: dict(plan_nodes(page, plan_id)[1][v["id"]]["geometry"]) for k, v in nodes.items()}
        check(retried == orig, "«Разложить заново» применила раскладку на свежей ревизии")

        # ---- 8. ручная трасса -------------------------------------------------------
        edge_id = e_in1["id"]
        page.evaluate(f"{APP}.openInspectorInPlace('edge', {edge_id})")
        page.wait_for_selector("[data-testid=sl-trace]", timeout=8000)
        check(page.locator("[data-testid=sl-trace-state]").inner_text() == "автоматическая",
              "карточка линии: трасса автоматическая")
        check(not page.locator("[data-testid=sl-trace-auto]").is_visible(),
              "«Сделать автоматической» не показана у автоматической трассы")
        page.click("[data-testid=sl-trace-draw]")
        wait_state(page, "a.slDrawingEdgeId===" + str(edge_id))
        geo = {k: v for k, v in grid.items()}
        sx, sy = geo["Ввод"]["x"], geo["Ввод"]["y"]
        px, py = geo["Щит 1"]["x"], geo["Щит 1"]["y"]
        box = page.locator("#site-plan-v3-map").bounding_box()

        def screen(x, y):
            pt = page.evaluate("""(a) => { const m = _planV3Map; const H = a.H;
                const p = m.latLngToContainerPoint([H - a.y, a.x]); return [p.x, p.y]; }""",
                               {"x": x, "y": y, "H": dims[1]})
            return box["x"] + pt[0], box["y"] + pt[1]

        pts = [screen(sx, sy + 45), screen(px - 70, sy + 45), screen(px - 70, py - 35)]
        for p in pts[:-1]:
            page.mouse.move(*p)
            page.mouse.click(*p)
        page.mouse.move(*pts[-1])
        page.mouse.dblclick(*pts[-1])
        wait_state(page, "a.slDrawingEdgeId===null && !a.slTraceBusy")
        page.wait_for_function(
            "(id) => { const a = " + APP + "; const v = a.planV3Current.edges.find(e => e.edge_id === id);"
            " return v && Array.isArray(v.waypoints) && v.waypoints.length >= 2; }", arg=edge_id, timeout=8000)
        plan = api(page, f"/api/v2/plans/{plan_id}")
        view = next(v for v in plan["edges"] if v["edge_id"] == edge_id)
        wp = view["waypoints"]
        xy_ortho = all(abs(a[0] - b[0]) <= 1 or abs(a[1] - b[1]) <= 1 for a, b in zip(wp, wp[1:]))
        check(xy_ortho and len(wp) >= 4, f"ручная трасса сохранена, все отрезки по осям: {wp}")
        check(abs(wp[0][0] - sx) <= 1 and abs(wp[0][1] - sy) <= 1 and abs(wp[-1][0] - px) <= 1
              and abs(wp[-1][1] - py) <= 1, "ручная трасса начинается и заканчивается в центрах значков")
        try:
            page.wait_for_selector("[data-testid=sl-trace-auto]", timeout=8000)
        except Exception:
            print("DEBUG", page.evaluate("""() => { const a = %s; return {insp: a.structInspector && [a.structInspector.kind, a.structInspector.id],
              manual: a.slEdgeIsManual(a.structInspector && a.structInspector.id),
              views: a.planV3Current.edges.map(v => [v.id, v.edge_id, v.waypoints && v.waypoints.length]),
              state: (document.querySelector('[data-testid=sl-trace-state]')||{}).textContent}; }""" % APP))
            raise
        check(page.locator("[data-testid=sl-trace-state]").inner_text() == "ручная",
              "карточка линии: трасса ручная, видна «Сделать автоматической»")
        st = js_layout_state(page)
        mpts = st["edges"][str(view["id"])]["pts"]
        check(len(mpts) == len(wp), "на карте нарисована сохранённая ручная трасса")

        # значок щита сдвинут — ручная трасса помечается, автоматические пересчитываются сами
        itm = {it["node_id"]: it for it in plan["items"] if it["kind"] == "node"}[nodes["Щит 1"]["id"]]
        page.evaluate("""(args) => fetch('/api/v2/plans/' + args.p + '/items/' + args.i, {
            method: 'PATCH', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({geometry: {x: args.x + 37, y: args.y + 23}, coord_space: args.cs})})""",
                      {"p": plan_id, "i": itm["id"], "x": px, "y": py, "cs": itm["coord_space"]})
        page.evaluate(APP + ".reloadPlanV3Items()")
        page.wait_for_function(
            "() => { const a = " + APP + "; return a.planV3Current.items.some(i => i.geometry && i.geometry.x === "
            + str(px + 37) + "); }", timeout=8000)
        page.wait_for_function("() => [..._planV3EdgeLayers.values()].some(l => (l.options.className || '').includes('sl-line-bad'))",
                               timeout=8000)
        check(True, "сдвиг щита: ручная трасса, ставшая неортогональной, помечена")
        page.wait_for_function(
            "() => { const t = document.querySelector('[data-testid=sl-trace-state]'); return t && t.textContent.includes('не по осям'); }",
            timeout=8000)
        st = js_layout_state(page)
        auto_bad = [k for k, e in st["edges"].items() if k != str(view["id"]) and not is_ortho(e["pts"])]
        check(not auto_bad, f"автоматические линии после сдвига щита по-прежнему ортогональны (не по осям: {auto_bad})")
        page.click("[data-testid=sl-trace-auto]")
        page.wait_for_function(
            "(id) => { const a = " + APP + "; const v = a.planV3Current.edges.find(e => e.edge_id === id);"
            " return v && v.waypoints == null; }", arg=edge_id, timeout=8000)
        st = js_layout_state(page)
        check(all(is_ortho(e["pts"]) and not e["bad"] for e in st["edges"].values()),
              "«Сделать автоматической» сбросила точки: все линии снова ортогональны и без пометки")

        # ---- 9. перетаскивание значка мышью -----------------------------------------
        page.evaluate(APP + ".closeInspector()")
        plan, items = plan_nodes(page, plan_id)
        target = items[nodes["Освещение"]["id"]]
        gx, gy = target["geometry"]["x"], target["geometry"]["y"]
        x0, y0 = screen(gx, gy)
        page.mouse.move(x0, y0)
        page.mouse.down()
        page.mouse.move(x0 + 25, y0 + 10, steps=4)
        page.mouse.move(x0 + 70, y0 + 20, steps=4)
        page.mouse.up()
        page.wait_for_function(
            "(args) => { const a = " + APP + "; const it = a.planV3Current.items.find(i => i.id === args.id);"
            " return it && (Math.abs(it.geometry.x - args.x) > 5); }",
            arg={"id": target["id"], "x": gx}, timeout=8000)
        plan, items2 = plan_nodes(page, plan_id)
        g2 = items2[nodes["Освещение"]["id"]]["geometry"]
        check(abs(g2["x"] - gx) > 5, f"перетаскивание значка сохранено: ({gx:.0f},{gy:.0f}) -> ({g2['x']:.0f},{g2['y']:.0f})")
        others_same = all(items2[v["id"]]["geometry"] == items[v["id"]]["geometry"]
                          for k, v in nodes.items() if k != "Освещение")
        check(others_same, "остальные значки не сдвинулись")
        st = js_layout_state(page)
        check(all(is_ortho(e["pts"]) for e in st["edges"].values()),
              "после перетаскивания все линии по-прежнему ортогональны")

        # ---- 10. печать -------------------------------------------------------------
        page.emulate_media(media="print")
        hidden = page.evaluate("""() => ({
            header: getComputedStyle(document.querySelector('header')).display,
            panel: getComputedStyle(document.querySelector('#planv3-panel')).display,
            toolbar: getComputedStyle(document.querySelector('[data-testid=sl-toolbar]')).display,
            bg: getComputedStyle(document.body).backgroundColor})""")
        check(hidden["header"] == "none" and hidden["panel"] == "none" and hidden["toolbar"] == "none",
              f"печать: шапка, панель и кнопки скрыты: {hidden}")
        check(hidden["bg"] in ("rgb(255, 255, 255)", "rgba(255, 255, 255, 1)"), "печать: белый фон")
        page.evaluate("window.dispatchEvent(new Event('beforeprint'))")
        page.wait_for_timeout(300)
        in_view = page.evaluate("""() => { const b = _planV3Map.getBounds(); const w = _slBounds;
            return b.getSouth() <= w[0][0] + 1 && b.getNorth() >= w[1][0] - 1 && b.getWest() <= w[0][1] + 1 && b.getEast() >= w[1][1] - 1; }""")
        check(in_view, "печать: вся схема помещается на лист")
        pdf = page.pdf(format="A4", landscape=True)
        check(pdf[:4] == b"%PDF" and len(pdf) > 1500, f"печатная версия строится в PDF ({len(pdf)} байт)")
        page.evaluate("window.dispatchEvent(new Event('afterprint'))")
        page.emulate_media(media="screen")
        page.wait_for_timeout(300)

        # 409 в консоли — ожидаемый ответ шага 7 (чужая правка), остальное — ошибки
        errors = [e for e in bp.errors if "409" not in e]
        check(not errors, f"консоль без ошибок, кроме ожидаемого 409 ({len(errors)}): {errors[:3]}")
        bp.close()

    print()
    if fails:
        print("ИТОГ: провалено проверок:", len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("ИТОГ: всё прошло")
    return 0


if __name__ == "__main__":
    sys.exit(main())
