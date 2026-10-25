"""A34 (ТЗ §13): 20 переключений «План v3 → Обзор → План v3» с открытой карточкой и живыми тиками —
один экземпляр карты; карточка, масштаб и выделение сохраняются; редактор не теряет черновик.

Дополняет test_b15_polling.py, раздел 6 (карта, слои, таймеры, масштаб, открытая карточка узла без
правок). Здесь — две части критерия, которые b15 не видит.

Что в «Плане v3» считается «черновиком редактора» (по коду, не по догадке). Единого «черновика схемы»
на этом экране нет: правки уходят на сервер сразу (перетаскивание значка, «Положение», «Сохранить»).
Несохранённое состояние живёт только в полях карточки справа от карты, пока человек не нажал
«Сохранить»; оно хранится в состоянии Alpine и переживает всё, что не пересоздаёт карточку:
  * карточка элемента (structure.js::_loadNodeInspector, plan-v3.js::planV3SaveNodeCard):
    «Название» (nameDraft), «Тип» (kindDraft), «Где стоит» (locationTextDraft);
  * поле «что питает линия» формы «Добавить отходящую линию» (planV3AddConsumerName);
  * «Положение на плане» X/Y (planV3XyDraft, применяется кнопкой «Переместить»);
  * карточка линии (structure.js::_loadEdgeInspector, plan-v3.js::planV3SaveEdgeCard):
    «Название» (nameDraft), «Номинальный ток, А» (ratedDraft), «Кабель / примечание» (cableDraft).
Это то, что человек теряет, если вкладка сбросит карточку или живой тик (5 с) перечитает её заново
(openInspectorInPlace пересоздаёт structInspector вместе с черновиками). «Нарисовать трассу» (Geoman)
и «+ Элемент схемы» — режимы инструмента, а не черновик; их Escape/смена вкладки не затрагивают.

Что такое «выделение». Подсветки выбранного значка на самой карте в «Плане v3» в приложении НЕТ:
проверено сравнением DOM — классы, стили, содержимое значков, подписей и линий до и после клика
по значку совпадают (`NOTE` ниже печатается, если это так). Выделение — открытая карточка:
structInspector.kind/id и сама карточка в панели. Поэтому тест проверяет и состояние, и то, что у
выбранного значка (линии) и у всех остальных DOM после 20 переключений тот же, что до них: если
подсветку когда-нибудь добавят, она окажется в снимке «после выбора» и обязана пережить переключения.

Сценарий (демо-схема tests/browser/demo_seed.py, Chromium, настоящее Flask-приложение):
  Часть 1 — карточка элемента: клик по значку «ЩР-1» мышью, масштаб и положение карты меняем вручную,
    вводим несохранённые Название / Тип / Где стоит / «что питает линия» / X; 20 раз «План v3 → Обзор →
    План v3»; на каждом круге — живой тик на «Обзоре» и на «Плане v3»; после каждого круга черновики
    на месте (и в состоянии, и в полях). В конце: одна карта, слои и таймеры не растут, масштаб и центр
    те же, карточка та же, DOM значков тот же; «Сохранить» отправляет введённое (проверка по API),
    «Переместить» применяет X.
  Часть 2 — карточка линии (клик по линии мышью): то же с Название / Номинальный ток / Кабель; «Сохранить»
    записывает введённое.

Запуск: python tests/browser/test_b32_planv3_switch_draft.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from demo_seed import seed_demo  # noqa: E402
from waits import wait_map_v3_ready, wait_card_open, close_card  # noqa: E402
from fetch_tools import FETCH_WRAP  # noqa: E402
from scheme_tools import line_hit_point  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
PANEL = "[data-testid=planv3-panel]"
CYCLES = 20
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def js(page, code: str):
    return page.evaluate(code)


def wait_state(page, expr: str, timeout: int = 15000) -> None:
    page.wait_for_function(f"() => {{ const a = {APP}; return !!({expr}); }}", timeout=timeout)


def log_len(page) -> int:
    return js(page, "window.__fetchLog.length")


def calls(page, since: int, pred: str) -> int:
    """Сколько запросов после журнала длины since удовлетворяют условию над адресом `u`."""
    return js(page, "window.__fetchLog.slice(%d).filter(e => { const u = e.url; return %s; }).length" % (since, pred))


# ---- черновики ---------------------------------------------------------------------------------
NODE_DRAFTS = {
    "name": "ЩР-1 (черновик)",
    "kind": "junction",
    "loc": "эт. 3, помещение 14",
    "add": "Вентилятор (черновик)",
    "x": "777",
}
EDGE_DRAFTS = {
    "name": "Линия света (черновик)",
    "rated": "16",
    "cable": "ВВГнг 3×2,5 (черновик)",
}

NODE_READ = """() => {
  const a = %s, i = a.structInspector, p = document.querySelector('%s');
  if (!i || !p) return null;
  const q = s => p.querySelector(s);
  const v = e => e ? e.value : null;
  return {
    model: {name: i.nameDraft, kind: i.kindDraft, loc: i.locationTextDraft,
            add: a.planV3AddConsumerName, x: String(a.planV3XyDraft.x)},
    dom: {name: v(q('[data-testid=node-card] input[aria-label="Название элемента"]')),
          kind: v(q('[data-testid=node-card-kind-select]')),
          loc: v(q('[data-testid=node-card] input[placeholder="например, ЩР-1, эт. 2"]')),
          add: v(q('[data-testid=node-card] input[placeholder="что питает линия"]')),
          x: v(q('[data-testid=planv3-xy-x]'))}
  };
}""" % (APP, PANEL)

EDGE_READ = """() => {
  const a = %s, i = a.structInspector, p = document.querySelector('%s');
  if (!i || !p) return null;
  const q = s => p.querySelector(s);
  const v = e => e ? e.value : null;
  return {
    model: {name: i.nameDraft, rated: String(i.ratedDraft), cable: i.cableDraft},
    dom: {name: v(q('input[aria-label="Название линии"]')),
          rated: v(q('input[aria-label="Номинальный ток, А"]')),
          cable: v(q('input[aria-label="Кабель или заметка"]'))}
  };
}""" % (APP, PANEL)


def drafts_intact(page, read_js: str, want: dict) -> tuple[bool, object]:
    got = js(page, read_js)
    ok = bool(got) and got["model"] == want and got["dom"] == want
    return ok, got


# ---- снимки карты ------------------------------------------------------------------------------
LOOK = """() => {
  const out = {items: {}, edges: {}};
  _planV3ItemLayers.forEach((l, id) => {
    const n = l._icon, t = l.getTooltip && l.getTooltip();
    out.items[id] = {cls: n.className, style: n.getAttribute('style'), inner: n.innerHTML,
                     tip: t && t._container ? t._container.className : null};
  });
  _planV3EdgeLayers.forEach((l, id) => {
    const el = l.getElement(), at = {};
    for (const x of el.attributes) if (x.name !== 'd') at[x.name] = x.value;   // d зависит от окна отрисовки
    out.edges[id] = at;
  });
  return out;
}"""

STATE = """() => {
  const a = %s;
  const c = _planV3Map.getCenter(), s = _planV3Map.getSize();
  const cp = _planV3Map.latLngToContainerPoint(_planV3Map.getCenter());
  return {zoom: _planV3Map.getZoom(), center: [c.lat, c.lng], size: [s.x, s.y], cp: [cp.x, cp.y],
          containers: document.querySelectorAll('#site-plan-v3-map.leaflet-container, #site-plan-v3-map .leaflet-container').length,
          allContainers: document.querySelectorAll('.leaflet-container').length,
          layers: Object.keys(_planV3Map._layers).length,
          timers: window.__intervals.filter(i => !i.cleared).length,
          card: a.structInspector ? [a.structInspector.kind, a.structInspector.id] : null,
          sameMap: window.__map0 === _planV3Map};
}""" % APP


def cycle(page) -> None:
    """«План v3 → Обзор → План v3» с живым тиком на каждом из двух экранов."""
    page.locator("nav button:visible", has_text="Обзор").click()
    page.wait_for_function("() => !!document.querySelector('.tiles') && document.querySelector('.tiles').offsetHeight > 0")
    js(page, APP + "._pollTick()")
    page.locator("nav button:visible", has_text="План v3").click()
    wait_map_v3_ready(page)
    js(page, APP + "._pollTick()")


def run_switches(page, read_js: str, want: dict) -> tuple[list, int, dict]:
    """20 кругов; после каждого — черновики на месте. → (сбои по кругам, число запросов, журнал-счётчики)."""
    broken = []
    since = log_len(page)
    for n in range(1, CYCLES + 1):
        cycle(page)
        ok, got = drafts_intact(page, read_js, want)
        if not ok:
            broken.append((n, got))
    counts = {
        "overview": calls(page, since, "u.endsWith('/api/v2/snapshot')"),
        "card_node": calls(page, since, "u.includes('/api/v2/snapshot?point_ids=')"),
        "card_edge": calls(page, since, r"/edge-loads\?edge_ids=\d+$/.test(u)"),
        "scheme": calls(page, since, r"/edge-loads\?edge_ids=\d+(,\d+)+$/.test(u)"),
    }
    return broken, since, counts


def main() -> int:
    with Harness() as h:
        info = seed_demo(h)
        shr1 = info["shr1"]
        e_light = info["e_light"]["id"]
        plan_id = info["plan"]["id"]

        bp = open_browser(h.base_url, init_script=FETCH_WRAP)
        page = bp.page
        page.wait_for_timeout(1200)
        js(page, APP + ".toggleUiSet()")
        page.locator("nav button:visible", has_text="План v3").click()
        wait_map_v3_ready(page)
        wait_state(page, "a.planV3Current && a.planV3Current.id===%d && a.planV3Current.items.length===6" % plan_id)
        page.locator("#site-plan-v3-map").scroll_into_view_if_needed()
        js(page, "window.__map0 = _planV3Map")

        # масштаб и положение, выбранные человеком (не «вписанные» по умолчанию)
        js(page, "_planV3Map.setView([500, 800], -1, {animate: false})")
        wait_map_v3_ready(page)

        # ============================ Часть 1. Карточка элемента ============================
        before_sel = js(page, LOOK)
        ico = page.locator('.sl-ico[aria-label$="ЩР-1"]')
        check(ico.count() == 1, "на схеме один значок «ЩР-1»")
        ico.first.click()
        wait_state(page, "a.structInspector && a.structInspector.kind==='node' && a.structInspector.id===%d" % shr1["id"])
        wait_card_open(page)
        page.wait_for_selector(f"{PANEL} [data-testid=planv3-xy-x]", state="visible")
        page.wait_for_function("() => { const a = %s; return a.planV3XyDraft.x !== '' && !a.structInspector.balanceLoading; }" % APP)
        after_sel = js(page, LOOK)
        marks = {k: (before_sel["items"][k], after_sel["items"][k]) for k in before_sel["items"]
                 if before_sel["items"][k] != after_sel["items"][k]}
        marks_e = [k for k in before_sel["edges"] if before_sel["edges"][k] != after_sel["edges"][k]]
        if not marks and not marks_e:
            print("NOTE подсветки выбранного значка на карте «Плана v3» нет: DOM значков, подписей и линий "
                  "до и после выбора совпадает; выделение = открытая карточка (structInspector + панель)")
        card_title = page.locator(f"{PANEL} [data-testid=node-card] h3").inner_text()
        check(card_title == "ЩР-1", f"открыта карточка выбранного элемента: заголовок {card_title!r}")

        card = page.locator(f"{PANEL} [data-testid=node-card]")
        card.locator('input[aria-label="Название элемента"]').fill(NODE_DRAFTS["name"])
        card.locator("[data-testid=node-card-kind-select]").select_option(NODE_DRAFTS["kind"])
        card.locator('input[placeholder="например, ЩР-1, эт. 2"]').fill(NODE_DRAFTS["loc"])
        card.locator('input[placeholder="что питает линия"]').fill(NODE_DRAFTS["add"])
        page.locator(f"{PANEL} [data-testid=planv3-xy-x]").fill(NODE_DRAFTS["x"])
        ok, got = drafts_intact(page, NODE_READ, NODE_DRAFTS)
        check(ok, f"до переключений черновик введён и в состоянии, и в полях ({got})")
        saved_name = h.client.get(f"/api/v2/topology/nodes/{shr1['id']}").get_json()["name"]
        check(saved_name == "ЩР-1", f"на сервере имя прежнее — правка не сохранена ({saved_name!r})")

        st0 = js(page, STATE)
        check(st0["containers"] == 1, f"«План v3»: один .leaflet-container до переключений ({st0['containers']})")
        broken, _since, counts = run_switches(page, NODE_READ, NODE_DRAFTS)
        check(not broken, f"черновик карточки элемента на месте после каждого из {CYCLES} кругов "
                          f"(сбои: {[(n, g and g['model']) for n, g in broken[:2]]})")
        print(f"     живые запросы за {CYCLES} кругов: Обзор {counts['overview']}, карточка узла {counts['card_node']}, "
              f"схема {counts['scheme']}")
        check(counts["overview"] >= CYCLES // 2 and counts["card_node"] >= CYCLES // 2 and counts["scheme"] >= CYCLES // 2,
              "живые тики шли на всех трёх путях (Обзор, карточка узла, подписи схемы) — не менее половины кругов")

        st1 = js(page, STATE)
        check(st1["sameMap"], "карта — тот же экземпляр Leaflet")
        check(st1["containers"] == 1, f"после {CYCLES} кругов ровно один .leaflet-container у «Плана v3» ({st1['containers']})")
        check(st1["allContainers"] == st0["allContainers"], f"число .leaflet-container на странице не растёт ({st0['allContainers']} → {st1['allContainers']})")
        check(st1["layers"] == st0["layers"], f"число слоёв карты не растёт: {st0['layers']} → {st1['layers']}")
        check(st1["timers"] == st0["timers"], f"число живых таймеров не растёт: {st0['timers']} → {st1['timers']}")
        check(abs(st1["zoom"] - st0["zoom"]) < 1e-6, f"масштаб сохранился ({st0['zoom']} → {st1['zoom']})")
        shift = max(abs(st1["cp"][0] - st1["size"][0] / 2), abs(st1["cp"][1] - st1["size"][1] / 2))
        check(abs(st1["center"][0] - st0["center"][0]) < 0.5 and abs(st1["center"][1] - st0["center"][1]) < 0.5,
              f"положение карты (центр) сохранилось: {st0['center']} → {st1['center']}, сдвиг {shift:.2f} px")
        check(st1["card"] == ["node", shr1["id"]], f"выделение: открыта та же карточка ({st1['card']})")
        check(page.locator(f"{PANEL} [data-testid=node-card] h3").inner_text() == "ЩР-1",
              "в панели по-прежнему карточка «ЩР-1»")
        after_loop = js(page, LOOK)
        diff_items = [k for k in after_sel["items"] if after_sel["items"][k] != after_loop["items"].get(k)]
        diff_edges = [k for k in after_sel["edges"] if after_sel["edges"][k] != after_loop["edges"].get(k)]
        check(not diff_items and not diff_edges and len(after_loop["items"]) == len(after_sel["items"]),
              f"значки, подписи и линии на карте выглядят как до переключений — выделение не потеряно и не «приклеилось» к чужим "
              f"(отличаются значки {diff_items}, линии {diff_edges})")

        # «Сохранить» отправляет то, что введено (а не прежнее значение из уже потерянного состояния)
        card.locator('input[aria-label="Название элемента"]').locator("xpath=following-sibling::button").click()
        page.wait_for_function("(n) => [...document.querySelectorAll('.toast')].some(t => t.textContent.includes(n))", arg="Сохранено")
        node = h.client.get(f"/api/v2/topology/nodes/{shr1['id']}").get_json()
        check(node["name"] == NODE_DRAFTS["name"] and node["kind"] == NODE_DRAFTS["kind"],
              f"после 20 переключений «Сохранить» записал введённые Название и Тип ({node['name']!r}, {node['kind']!r})")
        # после «Сохранить» экран сам перечитывает план и карточку: ждём новую карточку (данные с сервера уже
        # с новым именем), иначе перечитывание сбросит поле X, введённое посреди него
        wait_state(page, "a.structInspector && a.structInspector.kind==='node' && !a.structInspectorLoading "
                         "&& a.structInspector.data && a.structInspector.data.name==='%s'" % NODE_DRAFTS["name"])
        page.locator(f"{PANEL} [data-testid=planv3-xy-x]").fill(NODE_DRAFTS["x"])
        page.click(f"{PANEL} [data-testid=planv3-xy-apply]")
        page.wait_for_function("(n) => [...document.querySelectorAll('.toast')].some(t => t.textContent.includes(n))", arg="Положение сохранено")
        items = h.client.get(f"/api/v2/plans/{plan_id}").get_json()["items"]
        it = next(i for i in items if i.get("node_id") == shr1["id"])
        check(it["geometry"]["x"] == int(NODE_DRAFTS["x"]),
              f"«Переместить» применил введённый X после переключений ({it['geometry']['x']})")
        locs = h.client.get("/api/v2/locations").get_json()
        locs = locs if isinstance(locs, list) else locs.get("locations", locs.get("items", []))
        loc_name = next((l["name"] for l in locs if l["id"] == node.get("location_id")), None)
        check(loc_name == NODE_DRAFTS["loc"], f"«Сохранить» записал и введённое «Где стоит» ({loc_name!r})")
        close_card(page)

        # ============================ Часть 2. Карточка линии ===============================
        wait_map_v3_ready(page)
        page.locator("#site-plan-v3-map").scroll_into_view_if_needed()
        js(page, "_planV3Map.setView([450, 900], -1.25, {animate: false})")
        wait_map_v3_ready(page)
        before_edge = js(page, LOOK)
        pt = line_hit_point(page, e_light)
        check(pt is not None, "линия «ЩР-1 → Освещение» на схеме: есть точка, в которую попадает клик")
        page.mouse.click(pt["x"], pt["y"])
        wait_state(page, "a.structInspector && a.structInspector.kind==='edge' && a.structInspector.id===%d" % e_light)
        wait_card_open(page)
        page.wait_for_function("() => { const a = %s; return a.structInspector.data && a.structInspector.currentA !== undefined; }" % APP)
        after_edge_sel = js(page, LOOK)
        if before_edge == after_edge_sel:
            print("NOTE подсветки выбранной линии на карте нет: DOM значков и линий до и после клика по линии совпадает")
        p = page.locator(PANEL)
        p.locator('input[aria-label="Название линии"]').fill(EDGE_DRAFTS["name"])
        p.locator('input[aria-label="Номинальный ток, А"]').fill(EDGE_DRAFTS["rated"])
        p.locator('input[aria-label="Кабель или заметка"]').fill(EDGE_DRAFTS["cable"])
        ok, got = drafts_intact(page, EDGE_READ, EDGE_DRAFTS)
        check(ok, f"до переключений черновик линии введён и в состоянии, и в полях ({got})")
        api_edge = h.client.get(f"/api/v2/topology/edges/{e_light}").get_json()
        check(api_edge.get("name") != EDGE_DRAFTS["name"] and api_edge.get("rated_current_a") is None,
              "на сервере линия прежняя — правка не сохранена")

        st0 = js(page, STATE)
        broken, _since, counts = run_switches(page, EDGE_READ, EDGE_DRAFTS)
        check(not broken, f"черновик карточки линии на месте после каждого из {CYCLES} кругов "
                          f"(сбои: {[(n, g and g['model']) for n, g in broken[:2]]})")
        print(f"     живые запросы за {CYCLES} кругов: Обзор {counts['overview']}, карточка линии {counts['card_edge']}, "
              f"схема {counts['scheme']}")
        check(counts["overview"] >= CYCLES // 2 and counts["card_edge"] >= CYCLES // 2 and counts["scheme"] >= CYCLES // 2,
              "живые тики шли на всех трёх путях (Обзор, карточка линии, подписи схемы) — не менее половины кругов")
        st1 = js(page, STATE)
        check(st1["sameMap"] and st1["containers"] == 1 and st1["allContainers"] == st0["allContainers"],
              f"карта та же и одна ({st1['containers']} контейнер)")
        check(st1["layers"] == st0["layers"] and st1["timers"] == st0["timers"],
              f"слои и таймеры не растут: слои {st0['layers']} → {st1['layers']}, таймеры {st0['timers']} → {st1['timers']}")
        check(abs(st1["zoom"] - st0["zoom"]) < 1e-6 and abs(st1["center"][0] - st0["center"][0]) < 0.5
              and abs(st1["center"][1] - st0["center"][1]) < 0.5,
              f"масштаб и положение карты сохранились: {st0['zoom']} → {st1['zoom']}")
        check(st1["card"] == ["edge", e_light], f"выделение: открыта та же карточка линии ({st1['card']})")
        after_edge_loop = js(page, LOOK)
        check(after_edge_loop == after_edge_sel,
              "значки, подписи и линии на карте выглядят как до переключений (выделение линии не потеряно)")

        p.locator('input[aria-label="Название линии"]').locator("xpath=following-sibling::button").click()
        page.wait_for_function("(n) => [...document.querySelectorAll('.toast')].some(t => t.textContent.includes(n))", arg="Линия обновлена")
        api_edge = h.client.get(f"/api/v2/topology/edges/{e_light}").get_json()
        check(api_edge["name"] == EDGE_DRAFTS["name"] and float(api_edge["rated_current_a"]) == 16.0
              and api_edge["cable_note"] == EDGE_DRAFTS["cable"],
              f"«Сохранить» записал введённые Название, Номинал и Кабель ({api_edge['name']!r}, "
              f"{api_edge['rated_current_a']}, {api_edge['cable_note']!r})")
        wait_state(page, "a.structInspector && a.structInspector.kind==='edge' && !a.structInspectorLoading "
                         "&& a.structInspector.data && a.structInspector.data.name==='%s'" % EDGE_DRAFTS["name"])

        errs = [e for e in bp.errors if "Failed to load resource" not in e]
        check(not errs, f"консоль без ошибок ({errs[:3]})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
