"""Партия 14 (0.24.0), A29: метки на холсте схемы — четыре угловые и одна несимметричная
внутренняя; нет отражения и поворота, положение совпадает, погрешность переноса ≤ 1 исходного px
при zoom / resize / save / reload (docs/TZ-metering-architecture-dashboard.md, A29).

Положение каждого значка измеряется по DOM (`getBoundingClientRect` значка), а не пересчётом
Leaflet: из двух угловых значков (верх-лево, низ-право) собирается масштаб «экранные px на px холста»,
по нему положение каждого значка переводится обратно в координаты холста и сравнивается с
сохранёнными. Поворот и отражение дают расхождение осей: у верхне-правой метки Y совпадает с
верхне-левой, у нижне-левой — X.

Погрешность считается в исходных px холста. Леафлет кладёт значок в целый экранный px, поэтому при
масштабе < 1 один экранный px — это больше одного px холста; в таких кадрах допуск равен одному
экранному px (1/масштаб), а при масштабе ≥ 1 — 1 px холста, как требует A29.

Запуск: python tests/browser/test_b24_label_transfer.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from waits import wait_map_v3_ready  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
W, H = 2000, 1200
SPEC = {  # имя → (x, y) в координатах холста
    "Верх-лево": (0, 0), "Верх-право": (W, 0), "Низ-лево": (0, H), "Низ-право": (W, H),
    "Внутри": (613, 347),  # несимметричная: не на осях и не в центре
}
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def measure(page):
    """{node_id: [cx, cy]} — центры значков на экране + id узла → (x, y) из API."""
    raw = page.evaluate("""() => {
      const a = %s, out = {};
      (a.planV3Current.items || []).forEach(it => {
        const l = _planV3ItemLayers.get(String(it.id));
        if (!l || !l._icon) return;
        const r = l._icon.getBoundingClientRect();
        out[it.node_id] = {cx: r.left + r.width / 2, cy: r.top + r.height / 2,
                           x: it.geometry.x, y: it.geometry.y};
      });
      return out;
    }""" % APP)
    return {int(k): v for k, v in raw.items()}


def verify(page, ids, label):
    m = measure(page)
    if not all(i in m for i in ids.values()):
        check(False, f"{label}: не все пять значков на карте ({sorted(m)})")
        return
    tl, tr, bl, br, inner = (m[ids[n]] for n in ("Верх-лево", "Верх-право", "Низ-лево", "Низ-право", "Внутри"))
    sx = (br["cx"] - tl["cx"]) / W
    sy = (br["cy"] - tl["cy"]) / H
    tol = max(1.0, 1.0 / min(abs(sx), abs(sy)) if min(abs(sx), abs(sy)) < 1 else 1.0)
    # без отражения: вправо — вправо, вниз — вниз; без поворота: «верх» горизонтален, «лево» вертикально
    check(sx > 0 and sy > 0 and tr["cx"] > tl["cx"] and bl["cy"] > tl["cy"],
          f"{label}: нет отражения (масштаб {sx:.3f}×{sy:.3f})")
    check(abs(tr["cy"] - tl["cy"]) <= 1 and abs(bl["cx"] - tl["cx"]) <= 1 and abs(br["cx"] - tr["cx"]) <= 1
          and abs(br["cy"] - bl["cy"]) <= 1, f"{label}: нет поворота (стороны по осям)")
    check(abs(sx - sy) / max(sx, sy) < 0.002, f"{label}: масштаб по осям одинаков ({sx:.4f} / {sy:.4f})")
    worst = 0.0
    for name, nid in ids.items():
        p = m[nid]
        ex = (p["cx"] - tl["cx"]) / sx
        ey = (p["cy"] - tl["cy"]) / sy
        worst = max(worst, abs(ex - p["x"]), abs(ey - p["y"]))
    check(worst <= tol, f"{label}: максимум расхождения {worst:.2f} px холста (допуск {tol:.2f})")
    # несимметричная метка стоит там, где её сохранили
    ix = (inner["cx"] - tl["cx"]) / sx
    iy = (inner["cy"] - tl["cy"]) / sy
    check(abs(ix - SPEC["Внутри"][0]) <= tol and abs(iy - SPEC["Внутри"][1]) <= tol,
          f"{label}: «Внутри» на ({ix:.1f}, {iy:.1f}), сохранено {SPEC['Внутри']}")


def open_plan(page):
    page.wait_for_function("() => !!document.querySelector('[x-data]')._x_dataStack")
    if page.evaluate(APP + ".uiSet") != "v2":
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(300)
    page.locator("nav button:visible", has_text="План v3").click()
    page.wait_for_function(
        "() => { const a = %s; return a.planV3Current && a.planV3Current.items.length === 5; }" % APP,
        timeout=15000)
    wait_map_v3_ready(page)
    page.wait_for_timeout(300)


def main() -> int:
    with Harness() as h:
        plan = h.create_plan("Холст A29")
        ids = {}
        for name, (x, y) in SPEC.items():
            node = h.make_node(f"N-{len(ids)}", name, "load")
            ids[name] = node["id"]
            r = h.client.post(f"/api/v2/plans/{plan['id']}/items", json={
                "kind": "node", "node_id": node["id"], "geometry": {"x": x, "y": y},
                "coord_space": "canvas_xy_v2", "label": name})
            assert r.status_code == 201, r.get_json()

        bp = open_browser(h.base_url, viewport={"width": 1400, "height": 1000})
        page = bp.page
        page.wait_for_timeout(800)
        open_plan(page)
        check(page.evaluate(f"{APP}.planV3IsScheme()"), "план без картинки открыт как схема (холст 2000×1200)")

        # --- 1. вписанный вид ---
        verify(page, ids, "вписано в окно")

        # --- 2. zoom: масштаб 1:1 и 2:1 (центр карты — центр холста) ---
        for zoom, label in ((0, "zoom 1:1"), (1, "zoom 2:1"), (-1, "zoom 1:2")):
            page.evaluate(f"_planV3Map.setView([{H} / 2, {W} / 2], {zoom}, {{animate: false}})")
            page.wait_for_timeout(250)
            verify(page, ids, label)

        # --- 3. resize окна: 1400×1000 → 900×700 → 1600×900 ---
        for vw, vh in ((900, 700), (1600, 900)):
            page.set_viewport_size({"width": vw, "height": vh})
            page.wait_for_timeout(500)
            page.evaluate(f"_planV3Map.setView([{H} / 2, {W} / 2], 0, {{animate: false}})")
            page.wait_for_timeout(250)
            verify(page, ids, f"resize {vw}×{vh}, zoom 1:1")
        page.set_viewport_size({"width": 1400, "height": 1000})
        page.wait_for_timeout(500)

        # --- 4. save: перетаскиваем «Внутри» мышью, сохранённое положение = место, куда отпустили ---
        page.evaluate(f"_planV3Map.setView([{H} / 2, {W} / 2], 0, {{animate: false}})")
        page.wait_for_timeout(300)
        before = measure(page)[ids["Внутри"]]
        dx, dy = 87, 41  # экранных px при масштабе 1:1 — столько же px холста
        page.mouse.move(before["cx"], before["cy"])
        page.mouse.down()
        page.mouse.move(before["cx"] + dx / 2, before["cy"] + dy / 2, steps=3)
        page.mouse.move(before["cx"] + dx, before["cy"] + dy, steps=3)
        page.mouse.up()
        page.wait_for_function(
            "(a) => { const it = %s.planV3Current.items.find(i => i.node_id === a);"
            " return it && Math.abs(it.geometry.x - %s) > 20; }" % (APP, SPEC["Внутри"][0]),
            arg=ids["Внутри"], timeout=8000)
        item = next(i for i in h.client.get(f"/api/v2/plans/{plan['id']}").get_json()["items"]
                    if i["node_id"] == ids["Внутри"])
        gx, gy = item["geometry"]["x"], item["geometry"]["y"]
        check(abs(gx - (SPEC["Внутри"][0] + dx)) <= 1 and abs(gy - (SPEC["Внутри"][1] + dy)) <= 1,
              f"перетаскивание на ({dx}, {dy}) экранных px при 1:1 сохранено с ошибкой "
              f"({gx - SPEC['Внутри'][0] - dx:+.2f}, {gy - SPEC['Внутри'][1] - dy:+.2f}) px холста")
        SPEC["Внутри"] = (gx, gy)
        verify(page, ids, "после сохранения перетаскивания")

        # --- 5. reload ---
        page.reload()
        open_plan(page)
        verify(page, ids, "после перезагрузки (вписано)")
        page.evaluate(f"_planV3Map.setView([{H} / 2, {W} / 2], 0, {{animate: false}})")
        page.wait_for_timeout(250)
        verify(page, ids, "после перезагрузки, zoom 1:1")

        errors = [e for e in bp.errors]
        check(not errors, f"консоль без ошибок ({len(errors)}): {errors[:3]}")
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
