"""Партия 13 (находка пилота П6): «Структура» — это схема, а не «места» — настоящий
Chromium, настоящее Flask-приложение.

Схема стенда: Ввод → ЩР-1 (линия e1, счётчик «Счётчик ввода» = dev-1, 2500 Вт) →
{Станок (e2, счётчик dev-2), Свет (e4, без счётчика)}; Ввод → ЩР-2 (e3, без счётчика).
Вне схемы: точка «Забытая точка» без линии (dev-off) и прибор dev-free без точки.
Группа «Цех» из точки dev-1. Те же элементы размещены на плане «Смоук».

  1. Дерево «Структуры» (элемент, глубина) = схема «Плана v3»: тот же состав элементов и та
     же вложенность, что у линий на плане и у действующих линий API.
  2. Счётчик в дереве стоит один раз — под элементом, который линия питает: «Счётчик ввода
     … — ввод щита», «Станок … — вход нагрузки»; рядом живая мощность «2.50 кВт» (= снимок API).
  3. «Не на схеме» — сверху, над деревом: «Забытая точка» и прибор dev-free с «+ Счётчик → куда?»;
     выбор элемента открывает его карточку с формой «+ Счётчик» и прибором уже выбранным.
  4. «Мест» нигде нет: ни в дереве, ни в «Не на схеме», ни в карточках, ни в «Обзоре» и «Чего не хватает».
  5. Поиск по имени, коду, MQTT ID и серийнику находит элементы схемы и точки; пустой запрос
     возвращает дерево.
  6. Учётные группы — отдельный раздел под схемой.

Запуск: python tests/browser/test_b09_structure_tree.py
"""
from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from wb_energy_meter.group_repo_v2 import GroupRepoV2  # noqa: E402
from wb_energy_meter.model import ControlState  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []
PLACE_WORDS = ("нет места", "без места", "Без места", "место точки", "Места", "Место",
               "Новое место", "Деревья мест", "дерево мест", "Дерево мест")


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def api(page, path):
    return page.evaluate(f"fetch({path!r}).then(r=>r.json())")


def place(h, plan_id, node, x, y):
    r = h.client.post(f"/api/v2/plans/{plan_id}/items", json={
        "kind": "node", "node_id": node["id"], "geometry": {"x": x, "y": y},
        "coord_space": "canvas_xy_v2", "label": node["name"]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def view_edge(h, plan_id, edge, fi, ti):
    r = h.client.post(f"/api/v2/plans/{plan_id}/edges", json={
        "edge_id": edge["id"], "from_item_id": fi["id"], "to_item_id": ti["id"],
        "view_kind": "structural"})
    assert r.status_code == 201, r.get_json()


def expected_rows(nodes, edges):
    """Тот же обход, что у схемы: корни — элементы без входящей линии; дети по порядку типов и имён."""
    by_id = {n["id"]: n for n in nodes}
    children, has_in = {}, set()
    for e in edges:
        if e["from_node_id"] in by_id and e["to_node_id"] in by_id:
            children.setdefault(e["from_node_id"], []).append(e["to_node_id"])
            has_in.add(e["to_node_id"])
    order = {"source": 0, "panel": 1, "junction": 2, "load": 3}

    def key(i):
        n = by_id[i]
        return (order.get(n["kind"], 0), n["name"])
    rows = []

    def walk(i, d):
        rows.append((by_id[i]["name"], d))
        for c in sorted(children.get(i, []), key=key):
            walk(c, d + 1)
    for r in sorted((i for i in by_id if i not in has_in), key=key):
        walk(r, 0)
    return rows


def main() -> int:
    with Harness() as h:
        m1 = h.see_in_mqtt("dev-1", "Счётчик ввода")
        h.see_in_mqtt("dev-2", "Счётчик станка")
        h.see_in_mqtt("dev-off", "Забытый прибор")
        h.see_in_mqtt("dev-free", "Свободный прибор")
        now = time.time()
        m1.controls["Total P"] = ControlState(name="Total P", value=2500.0, last_update_ts=now,
                                              meta={"type": "power"})
        m1.controls["Serial"] = ControlState(name="Serial", value="SN-777", last_update_ts=now)
        m1.last_any_ts = now

        vvod = h.make_node("vvod", "Ввод", "source")
        shr1 = h.make_node("shr1", "ЩР-1", "panel")
        shr2 = h.make_node("shr2", "ЩР-2", "panel")
        e1 = h.connect(vvod["id"], shr1["id"])
        e3 = h.connect(vvod["id"], shr2["id"])
        c2 = h.add_consumer(shr1["id"], "Станок")
        c4 = h.add_consumer(shr1["id"], "Свет")
        stanok, e2 = c2["node"], c2["edge"]
        svet, e4 = c4["node"], c4["edge"]
        p1 = h.make_point("p-in", "Счётчик ввода")
        h.bind_meter(p1["id"], "dev-1", "Счётчик ввода")
        h.meters_repo.update("dev-1", serial_number="SN-777")   # серийник из базы — по нему ищет «Структура»
        h.set_meter_on_edge(e1["id"], p1["id"])
        p2 = h.make_point("p-st", "Счётчик станка")
        h.bind_meter(p2["id"], "dev-2", "Счётчик станка")
        h.set_meter_on_edge(e2["id"], p2["id"])
        p3 = h.make_point("p-off", "Забытая точка")
        h.bind_meter(p3["id"], "dev-off", "Забытый прибор")
        grp = GroupRepoV2(h.db).add("Цех")
        GroupRepoV2(h.db).add_member(grp.id, p1["id"], valid_from=0)

        plan = h.create_plan("Смоук")
        items = {n["name"]: place(h, plan["id"], n, 300 + 220 * i, 260 + 160 * (i % 3))
                 for i, n in enumerate([vvod, shr1, shr2, stanok, svet])}
        for e, a, b in ((e1, "Ввод", "ЩР-1"), (e3, "Ввод", "ЩР-2"),
                        (e2, "ЩР-1", "Станок"), (e4, "ЩР-1", "Свет")):
            view_edge(h, plan["id"], e, items[a], items[b])

        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1000)
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(300)
        page.locator("nav button:visible", has_text="Структура").click()
        page.wait_for_timeout(1800)

        # ===== 1. дерево = схема Плана v3 =====
        ui_rows = page.locator("[data-testid=scheme-node]").evaluate_all(
            "els => els.map(e => [e.querySelector('[data-testid=scheme-node-name]').textContent.trim(),"
            " Number(e.dataset.depth)])")
        ui_rows = [tuple(r) for r in ui_rows]
        nodes = api(page, "/api/v2/topology/nodes")
        edges = api(page, "/api/v2/topology/edges?state=published")
        want = expected_rows(nodes, edges)
        check(ui_rows == want, f"дерево «Структуры» = действующие линии API: {ui_rows}")
        plan_now = api(page, f"/api/v2/plans/{plan['id']}")
        item_node = {i["id"]: i["node_id"] for i in plan_now["items"] if i["kind"] == "node"}
        plan_edges = [{"from_node_id": item_node[pe["from_item_id"]], "to_node_id": item_node[pe["to_item_id"]]}
                      for pe in plan_now["edges"]]
        plan_rows = expected_rows(nodes, plan_edges)
        check(ui_rows == plan_rows,
              f"…и тому же, что нарисовано на плане «Смоук» (тот же состав и вложенность): {plan_rows}")
        check(ui_rows[0] == ("Ввод", 0) and ("Станок", 2) in ui_rows and ("ЩР-2", 1) in ui_rows,
              "Ввод — корень, ЩР-1/ЩР-2 на уровне 1, Станок и Свет на уровне 2")

        # ===== 2. счётчик один раз; живая мощность =====
        meters = [norm(t) for t in page.locator("[data-testid=scheme-meter]").all_inner_texts()]
        check(len(meters) == 2, f"в дереве два счётчика, каждый один раз ({meters})")
        check(any("Счётчик ввода" in t and "ввод щита" in t for t in meters)
              and any("Счётчик станка" in t and "вход нагрузки" in t for t in meters),
              "роли на языке схемы: «ввод щита», «вход нагрузки»")
        snap = api(page, "/api/v2/snapshot")
        p_api = next(p["power_w"] for p in snap["points"] if p["point_id"] == p1["id"])
        shown = f"{p_api / 1000:.2f} кВт"
        check(p_api == 2500.0 and any(shown in t for t in meters),
              f"живая мощность счётчика ввода = снимку API: {shown!r} (строка: {meters[0]!r})")
        chips = page.locator("[data-testid=scheme-node] .stc:visible").all_inner_texts()
        check(chips == ["1", "1"], f"значок «1 счётчик» только у ЩР-1 и Станка ({chips})")
        bp.screenshot("b09_structure_tree")

        # ===== 3. «Не на схеме» =====
        off = page.locator("[data-testid=struct-off-scheme]")
        check(off.count() == 1 and norm(off.inner_text()).lower().startswith("не на схеме (2)"),
              f"«Не на схеме (2)»: {norm(off.inner_text())[:80]!r}")
        first_top = page.evaluate(
            "(()=>{const o=document.querySelector('[data-testid=struct-off-scheme]'),"
            "t=document.querySelector('[data-testid=scheme-tree]');"
            "return !!(o.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING);})()")
        check(first_top, "«Не на схеме» стоит над деревом")
        check(page.locator("[data-testid=off-scheme-point]", has_text="Забытая точка").count() == 1,
              "в «Не на схеме»: точка без линии «Забытая точка»")
        dev_row = page.locator("[data-testid=off-scheme-device]", has_text="dev-free")
        check(dev_row.count() == 1, "в «Не на схеме»: прибор без точки dev-free")
        dev_row.locator("select").select_option(label="ЩР-2 (Щит)")
        page.wait_for_timeout(1500)
        form_dev = page.input_value("[data-testid=node-meter-device]:visible")
        check(form_dev == "dev-free" and page.evaluate(
            "(()=>{const a=" + APP + "; return a.structInspector && a.structInspector.data.name;})()") == "ЩР-2",
            f"«+ Счётчик → куда?» открыл карточку ЩР-2 с формой «+ Счётчик» и прибором {form_dev!r}")
        page.locator("[data-testid=node-meter-form]:visible button", has_text="Отмена").click()
        page.evaluate(APP + ".closeInspector()")

        # ===== 4. «мест» нигде нет =====
        def place_words():
            txt = page.locator("body").inner_text()
            return [w for w in PLACE_WORDS if w in txt]
        check(not place_words(), f"«Структура»: слов про места нет ({place_words()})")
        page.locator("[data-testid=scheme-node]", has_text="ЩР-1").first.click()
        page.wait_for_timeout(900)
        check(not place_words(), f"«Структура», карточка элемента: слов про места нет ({place_words()})")
        page.locator("[data-testid=scheme-meter]", has_text="Счётчик ввода").click()
        page.wait_for_selector("[data-testid=point-card-title]", timeout=8000)
        check(not place_words(), f"«Структура», карточка точки: слов про места нет ({place_words()})")
        page.evaluate(APP + ".closeInspector()")
        for tab_name in ("Обзор", "Отчёты", "План v3"):
            page.locator("nav button:visible", has_text=tab_name).click()
            page.wait_for_timeout(900)
            check(not place_words(), f"«{tab_name}»: слов про места нет ({place_words()})")

        # ===== 5. поиск =====
        page.locator("nav button:visible", has_text="Структура").click()
        page.wait_for_timeout(900)
        box = page.locator("input[placeholder^='Поиск']")

        def search(q):
            box.fill(q)
            page.wait_for_timeout(1200)
            res = page.locator("[data-testid=struct-search-results]")
            return norm(res.inner_text()).lower() if res.count() else ""  # заголовки рисуются капсом (CSS)

        r = search("Станок")
        check("элементы схемы (1)" in r and "станок" in r, f"поиск по имени элемента: {r[:90]!r}")
        r = search("p-off")
        check("точки учёта (1)" in r and "забытая точка" in r, f"поиск по коду точки: {r[:90]!r}")
        r = search("dev-2")
        check("счётчик станка" in r, f"поиск по MQTT ID прибора: {r[:90]!r}")
        r = search("SN-777")
        check("счётчик ввода" in r, f"поиск по серийному номеру: {r[:90]!r}")
        r = search("щр")
        check("элементы схемы (2)" in r, f"поиск находит элементы схемы без учёта регистра: {r[:90]!r}")
        box.fill("")
        page.wait_for_timeout(1000)
        check(page.locator("[data-testid=scheme-tree]").count() == 1
              and page.locator("[data-testid=struct-search-results]").count() == 0,
              "пустой запрос возвращает дерево")

        # ===== 6. группы — отдельным разделом =====
        groups_title = page.locator(".structure-sec-title", has_text="Учётные группы")
        check(groups_title.count() == 1 and page.locator("[data-testid=struct-group-row]", has_text="Цех").count() == 1,
              "раздел «Учётные группы» с группой «Цех»")
        after = page.evaluate(
            "(()=>{const t=document.querySelector('[data-testid=scheme-tree]'),"
            "g=document.querySelector('[data-testid=struct-group-row]');"
            "return !!(t.compareDocumentPosition(g) & Node.DOCUMENT_POSITION_FOLLOWING);})()")
        check(after, "группы стоят под схемой, а не внутри дерева")
        check(page.locator("[data-testid=scheme-tree] [data-testid=struct-group-row]").count() == 0,
              "в дереве схемы групп нет")

        check(not bp.errors, f"консоль без ошибок ({len(bp.errors)}): " + "; ".join(sorted(set(bp.errors))[:6]))
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
