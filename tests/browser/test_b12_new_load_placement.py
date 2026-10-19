"""Партия находок 0.22.3, П8: новые нагрузки щита ложатся на план в разные
места, а не в одну точку (docs/pilot-findings.md, П8).

Раньше «+ Счётчик» → «+ новая нагрузка…» и «Добавить отходящую линию»
ставили значок всегда в (x щита + 60, y щита + 60): три нагрузки — три значка
и три подписи друг на друге. Теперь одна функция (`_planV3SpotNear`) ищет
свободное место в ряду под щитом с учётом уже стоящих значков и границ плана.

Сценарий (настоящий Chromium, настоящее Flask-приложение):
  1. «План v3»: «Ввод», «Щит 1» — кликом по карте.
  2. Щит → «+ Счётчик» → отходящая линия → «+ новая нагрузка…» — три раза
     («Станок», «Освещение», «Вентиляция»).
  3. Щит → «Добавить отходящую линию» — ещё два раза («Розетки», «Насос»).
  4. У пяти новых значков разные координаты, все ниже щита и внутри плана;
     рамки их подписей (`getBoundingClientRect` постоянных подсказок) не
     пересекаются ни друг с другом, ни с подписями ввода и щита, и все лежат в
     видимой области карты.
  5. Чистая функция: при полностью занятой строке уходит в следующий ряд; на
     забитом плане возвращает `null` (запасной путь — прежнее смещение).

Запуск: python tests/browser/test_b12_new_load_placement.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []
NEW = ["Станок", "Освещение", "Вентиляция", "Розетки", "Насос"]


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def api(page, path):
    return page.evaluate(f"fetch({path!r}).then(r=>r.json())")


def card(page):
    return page.locator("[data-testid=node-card]:visible")


def place_on_map(page, kind_value, name, fx, fy):
    page.click("button:has-text('+ Элемент схемы')")
    page.wait_for_timeout(200)
    page.select_option("[data-testid=planv3-kind-select]", kind_value)
    page.fill("input[placeholder='Название']", name)
    box = page.locator(".leaflet-container:visible").first.bounding_box()
    page.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
    page.wait_for_timeout(1200)


def rects_intersect(a, b, pad=0.0):
    return (a["left"] < b["right"] + pad and b["left"] < a["right"] + pad
            and a["top"] < b["bottom"] + pad and b["top"] < a["bottom"] + pad)


def main() -> int:
    with Harness() as h:
        for i in range(3):
            h.see_in_mqtt(f"wb-load-{i}", f"Прибор {i}")
        plan = h.create_plan("Смоук")

        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1000)
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(300)
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(1000)

        place_on_map(page, "source", "Ввод", 0.2, 0.3)
        place_on_map(page, "panel", "Щит 1", 0.3, 0.45)
        shield = next(n for n in api(page, "/api/v2/topology/nodes") if n["name"] == "Щит 1")
        check(card(page).count() == 1, "после размещения открыта карточка щита")

        # ---- 2. три нагрузки через «+ Счётчик» ---------------------------------
        for i, name in enumerate(NEW[:3]):
            card(page).locator("button", has_text="+ Счётчик").click()
            page.wait_for_timeout(300)
            page.select_option("[data-testid=node-meter-device]:visible", f"wb-load-{i}")
            page.locator("[data-testid=node-meter-form]:visible label",
                         has_text="Отходящую линию").locator("input").check()
            page.wait_for_timeout(200)
            page.select_option("[data-testid=node-meter-out-target]:visible", "new")
            page.fill("[data-testid=node-meter-new-load]:visible", name)
            page.click("[data-testid=node-meter-submit]:visible")
            page.wait_for_timeout(1800)

        # ---- 3. две нагрузки через «Добавить отходящую линию» -------------------
        for name in NEW[3:]:
            page.fill("input[placeholder='что питает линия']:visible", name)
            page.locator("[data-testid=node-card]:visible button", has_text="Добавить").click()
            page.wait_for_timeout(1800)

        nodes = {n["name"]: n for n in api(page, "/api/v2/topology/nodes")}
        check(all(n in nodes for n in NEW), f"созданы все пять нагрузок: {sorted(nodes)}")
        plan_now = api(page, f"/api/v2/plans/{plan['id']}")
        geom = {it["node_id"]: it["geometry"] for it in plan_now["items"] if it["kind"] == "node"}
        shield_xy = geom[shield["id"]]
        pts = {name: geom.get(nodes[name]["id"]) for name in NEW}
        check(all(pts.values()), f"все пять нагрузок стоят на плане: {pts}")

        # ---- 4. координаты и подписи ---------------------------------------------
        xy = [(round(p["x"]), round(p["y"])) for p in pts.values()]
        check(len(set(xy)) == 5, f"у пяти значков разные координаты: {xy}")
        W, H = plan_now.get("canvas_width") or 2000, plan_now.get("canvas_height") or 1200
        check(all(p["y"] > shield_xy["y"] and 0 <= p["x"] <= W and 0 <= p["y"] <= H
                  for p in pts.values()),
              f"все ниже щита ({shield_xy}) и внутри плана {W}×{H}")

        page.wait_for_timeout(500)
        tips = page.evaluate("""() => [...document.querySelectorAll('.planv3-node-tip')].map(t => {
            const r = t.getBoundingClientRect();
            return {text: t.textContent.trim(), left: r.left, right: r.right, top: r.top, bottom: r.bottom};
        })""")
        view = page.locator(".leaflet-container:visible").first.bounding_box()
        names = ["Ввод", "Щит 1"] + NEW
        got = {t["text"].split(" · ")[0]: t for t in tips}
        check(all(n in got for n in names), f"подписи всех семи значков на карте: {sorted(got)}")
        clash = [(a, b) for i, a in enumerate(names) for b in names[i + 1:]
                 if a in got and b in got and rects_intersect(got[a], got[b])]
        check(not clash, f"подписи не пересекаются (пересечения: {clash})")
        outside = [n for n in names if n in got and not (
            got[n]["left"] >= view["x"] - 1 and got[n]["right"] <= view["x"] + view["width"] + 1
            and got[n]["top"] >= view["y"] - 1 and got[n]["bottom"] <= view["y"] + view["height"] + 1)]
        check(not outside, f"подписи лежат в видимой области карты (вне её: {outside})")
        bp.screenshot("b12_new_loads")

        # ---- 5. чистая функция ----------------------------------------------------
        res = page.evaluate("""() => {
            const base = {W: 2000, H: 1200, unit: 1, parent: {x: 300, y: 300}, label: 'Нагрузка'};
            const first = _planV3FreeSpot(Object.assign({}, base, {occupied: []}));
            const again = _planV3FreeSpot(Object.assign({}, base, {occupied: []}));
            const second = _planV3FreeSpot(Object.assign({}, base, {occupied: [{x: first.x, y: first.y, text: 'Нагрузка'}]}));
            // вся полоса первого ряда занята → следующий ряд
            const rowBlock = [];
            for (let x = 0; x <= 2000; x += 60) rowBlock.push({x, y: 340, text: 'Нагрузка'});
            const nextRow = _planV3FreeSpot(Object.assign({}, base, {occupied: rowBlock}));
            // план забит целиком → null
            const full = [];
            for (let y = 0; y <= 1200; y += 20) for (let x = 0; x <= 2000; x += 60) full.push({x, y, text: 'Нагрузка'});
            const none = _planV3FreeSpot(Object.assign({}, base, {occupied: full}));
            return {first, again, second, nextRow, none};
        }""")
        check(res["first"] == {"x": 300, "y": 340} and res["first"] == res["again"],
              f"первое место — прямо под родителем и детерминировано: {res['first']}")
        check(res["second"]["y"] == 340 and res["second"]["x"] > 300 + 100,
              f"второй значок в том же ряду правее первой подписи: {res['second']}")
        check(res["nextRow"]["y"] > 340, f"ряд занят целиком → следующий ряд: {res['nextRow']}")
        check(res["none"] is None, "забитый план → null")

        check(not bp.errors, f"консоль без ошибок ({len(bp.errors)}): " + "; ".join(bp.errors[:5]))
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
