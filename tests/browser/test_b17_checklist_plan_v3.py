"""Партия 13, 13.7б (решение R15): автопроход чек-листа `docs/browser-checklist-plan-v3.md`
(61 шаг, 10 разделов) в настоящем Chromium по настоящему Flask-приложению.

Тест идёт ПО ШАГАМ ФАЙЛА В ТОМ ЖЕ ПОРЯДКЕ. У каждого шага — номер из файла, действие тем же
способом, каким его делает человек (клик по кнопке, по маркеру, по линии на карте, ввод в поле),
и проверка «Должно получиться»: текст на экране, а где есть — ответ API. Шаг, у которого
в файле стоит **[БЫЛО ИНАЧЕ]**, проверяется по ТЕКУЩЕМУ поведению (оно описано в файле рядом).
Расхождения «файл ↔ программа» решены не подгонкой теста: каждое — отметка в файле и строка
в отчёте партии (см. список «РАСХОЖДЕНИЯ» в конце вывода).

Подготовка данных (то, на что чек-лист ссылается «из другого раздела»): шаг 6 просит план
«с хотя бы одним узлом» (узлы в файле заводятся только в разделе 4) — перед ним схема-основа
заводится через API; приборы для счётчиков (шаги 27–30, 45–46) — через `h.see_in_mqtt` с живыми
значениями; это те же приёмы, что в test_b03 и test_b06…b13.

Таблица «шаг → проверка»:

 Раздел 1. Работа без загруженного плана
  1  «Планов пока нет», поле «Название плана», «Создать план»; карты и панели инструментов нет
  2  пустое название → план «План v3»; карта и три кнопки инструментов
  3  F5 → план открывается сам, «Планов пока нет» нет
 Раздел 2. Загрузка плана
  4  в списке планов оба плана; переключение → на карту грузятся узлы выбранного плана
  5  [БЫЛО ИНАЧЕ] «+ План» → «Создать план» → третий пустой план, прежние планы целы
 Раздел 3. Панель «ничего не выбрано»
  6  подсказка и блоки «Элементы схемы / Линии / Зоны / Точки учёта»
  7  клик по имени узла в списке → карточка именно этого узла
  8  [БЫЛО ИНАЧЕ] новый элемент со «Структуры» → блок «Не размещены на плане» с кнопкой
  9  [БЫЛО ИНАЧЕ] «Разместить на карте» → режим размещения без формы типа/имени
 10  клик по карте → «размещён на плане», маркер, карточка, строки в списке
 11  клик по строке линии → карточка линии; по строке зоны → карточка группы; контур — только подсказка
 12  «— не стоит на линии» только у точек вне линий
 13  [БЫЛО ИНАЧЕ] «Приборы без точки учёта» с подсказкой про «+ Счётчик»; без приборов блока нет
 Раздел 4. Узлы
 14  «+ Элемент схемы» → форма типа/названия и подсказка
 15  «Щит» «ЩР-1» → маркер с постоянной подписью, уведомление, карточка; [инструмент остаётся]
 16  [БЫЛО ИНАЧЕ] порядок полей карточки
 17  «Где стоит» → «Сохранить»; после повторного открытия поле хранит текст; один PATCH с тремя полями
 18  новое название → подпись на карте и в карточке обновились
 19  клик по маркеру открывает карточку, дубль не создаётся
 20  перетаскивание маркера сохраняется на сервере
 Раздел 5. Линии
 21  второй узел «Ввод»
 22  [БЫЛО ИНАЧЕ] «+ Линия» → подсказка «элемент-источник»
 23  клик «Ввод», клик «ЩР-1 (главный)» → оранжевая линия, «Линия проведена»
 24  [БЫЛО ИНАЧЕ] строка «← от Ввод» с «— без счётчика —», «Карточка линии»
 25  «Добавить отходящую линию» «Розетки» → узел «Нагрузка» и линия одним действием
 26  то же для «Освещение»
 27  счётчик на входящую линию → «Счётчик назначен», линия зелёная
 28  счётчик на отходящую линию того же узла: назначены оба, «Освещение» без счётчика
 29  «— без счётчика —» → «Счётчик снят», линия оранжевая
 30  [БЫЛО ИНАЧЕ] «＋ новая точка из прибора…» — в карточке ЛИНИИ, прибор выбирается из списка свободных
 31  [БЫЛО ИНАЧЕ] клик по линии на карте → собственная карточка линии
 32  номинальный ток → «Линия обновлена», хранится; «% от номинала» при живом токе
 Раздел 6. Смена типа узла
 33  «Нагрузка»: только серая подсказка вместо формы
 34  тип «Щит» → «Сохранено», «(Щит)» в списке; [БЫЛО ИНАЧЕ] цвет маркера зависит от типа
 35  форма «Добавить отходящую линию» появилась без перезагрузки
 36  отходящая линия действительно проводится
 37  «Ввод» для узла с входящей линией → отказ с именем узла и причиной; тип прежний
 38  «Ввод» для узла без входящей линии сохраняется
 Раздел 7. Зоны
 39  [БЫЛО ИНАЧЕ] группа на «Структуре» только отображается; заведена через «＋ Новая группа»
 40  «+ Зона» → список групп, «Нарисовать контур» (неактивна), «＋ Новая группа»
 41  «Нарисовать контур» → подсказка рисования
 42  контур кликами и двойной клик → «Зона нанесена», закрашенная область
 43  клик по контуру → только «Учётная группа: …»
 44  клик по имени зоны в списке → полная карточка группы
 Раздел 8. Числа
 45  схема Ввод → Щит → два потребителя со счётчиками + один без; на щите две измеряющие точки
 46  показания приборов: на вводе больше суммы потребителей
 47  «Баланс щита за период»: значение со знаком, процент, неизмеренные ветви; период «как на Обзоре»
 48  «Обзор»: итог объекта — по прибору на вводе
 49  небаланс объекта со знаком и процентом
 50  один щит → баланс щита = небаланс объекта
 51  [БЫЛО ИНАЧЕ] «Незавершённая конфигурация»: размещённые узлы не «не на плане», изолированный — «не подключён»
 Раздел 9. Простой режим удалён
 52  на «Структуре» нет «Подробная схема»; формы схемы показываются всегда
 53  карточка точки без линии: «Описание», «Точка не назначена измерением…»; нет «Это ввод»
 54  после назначения на линию: «Измеряет связь», «Питается от», «Питает»
 55  [БЫЛО ИНАЧЕ] таблицы с «Чего не хватает» больше нет; «не стоит на линии» — в списке «Плана v3»
 56  [БЫЛО ИНАЧЕ] «нет места» в наборе v2 не показывается (решение R11)
 57  PATCH is_input → 400 field_removed
 Раздел 10. Ошибочные ситуации
 58  обратная линия B → A отклонена с именами узлов
 59  второй источник у узла отклонён с обоими именами
 60  легитимная линия C → D проводится
 61  Esc прерывает рисование контура; рисование заново работает

Шаги, которые автомат проверить не может: нет — все 61 шаг имеют проверку; два места проверяются
частично и названы в выводе: «удобно ли» в шагах 14/22 (текст подсказки проверяется, оценка
понятности — нет), и цвет «красный/зелёный» в шаге 47 (проверяется знак значения и класс цвета).

Запуск: python tests/browser/test_b17_checklist_plan_v3.py [--until N]   (N — номер раздела 1…10)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser, HOUR  # noqa: E402
from waits import wait_map_v3_ready, wait_card_open, wait_card_closed, close_card  # noqa: E402
from wb_energy_meter.model import ControlState, MeterStatus  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []
DISCREPANCIES: list[str] = []
STEP = [0]


def step(n: int, text: str) -> None:
    STEP[0] = n
    print(f"\n--- шаг {n}: {text}")


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + f"[{STEP[0]}] {msg}")
    if not cond:
        fails.append(f"шаг {STEP[0]}: {msg}")


def note(msg: str) -> None:
    """Расхождение «файл ↔ программа» (идёт в отчёт партии)."""
    print(f"NOTE [{STEP[0]}] {msg}")
    DISCREPANCIES.append(f"шаг {STEP[0]}: {msg}")


def js(page, code: str):
    return page.evaluate(code)


def api(h, method: str, url: str, **kw):
    r = getattr(h.client, method)(url, **kw)
    return r.status_code, (r.get_json(silent=True) if r.data else None)


# ------------------------------------------------------------------ экран
def nav(page, title: str, pause: int = 700) -> None:
    page.locator("nav button:visible", has_text=title).click()
    page.wait_for_timeout(pause)


def panel(page) -> str:
    return page.locator("[data-testid=planv3-panel]").inner_text()


def toast_text(page) -> str:
    loc = page.locator(".toast")
    return loc.first.inner_text().strip() if loc.count() else ""


def wait_toast(page, sub: str, timeout: int = 8000) -> str:
    try:
        page.wait_for_function(
            "(s)=>[...document.querySelectorAll('.toast')].some(t=>t.textContent.includes(s))",
            arg=sub, timeout=timeout)
    except Exception:
        return toast_text(page)
    return page.evaluate("(s)=>{const t=[...document.querySelectorAll('.toast')].find(x=>x.textContent.includes(s)); return t?t.textContent.trim():''}", sub)


def plan_state(page) -> dict:
    return js(page, """(()=>{ const a=%s; const p=a.planV3Current; return {
        id:p?p.id:null, name:p?p.name:null, items:p?(p.items||[]).length:0,
        tool:a.planV3Tool, cardKind:a.structInspector?a.structInspector.kind:null,
        cardId:a.structInspector?a.structInspector.id:null,
        cardName:(a.structInspector&&a.structInspector.data)?a.structInspector.data.name:null}; })()""" % APP)


def node_by_name(h, name: str):
    s, nodes = api(h, "get", "/api/v2/topology/nodes")
    return next((n for n in nodes if n["name"] == name), None)


def edges(h):
    return api(h, "get", "/api/v2/topology/edges?state=published")[1]


def edge_between(h, a: str, b: str):
    na, nb = node_by_name(h, a), node_by_name(h, b)
    if not na or not nb:
        return None
    return next((e for e in edges(h) if e["from_node_id"] == na["id"] and e["to_node_id"] == nb["id"]), None)


def marker_xy(page, node_name: str) -> dict:
    """Экранные координаты маркера узла на карте «Плана v3»."""
    return js(page, """(()=>{ const a=%s; const n=a.structNodes.find(x=>x.name===%s);
        const it=a.planV3Current.items.find(i=>i.node_id===n.id);
        const l=_planV3ItemLayers.get(String(it.id)); const p=_planV3Map.latLngToContainerPoint(l.getLatLng());
        const c=_planV3Map.getContainer(); const r=c.getBoundingClientRect(); return {x:r.left+c.clientLeft+p.x, y:r.top+c.clientTop+p.y}; })()"""
              % (APP, json.dumps(node_name)))


def line_xy(page, edge_id: int) -> dict:
    """Точка ЛИНИИ (вида «структурная») на экране, в которую реально попадает клик.

    latLngToContainerPoint считает от внутренней границы контейнера (за рамкой в 1 px), поэтому к
    getBoundingClientRect() добавляются clientLeft/clientTop — иначе точка уходит мимо линии шириной 3 px.
    Линия может быть пунктирной (оранжевая «без счётчика») — клик ровно в середину способен
    попасть в просвет между штрихами, поэтому перебираем точки вдоль линии и берём ту, под которой
    действительно лежит её штрих (document.elementFromPoint)."""
    return js(page, """(()=>{ const a=%s; const v=a.planV3Current.edges.find(e=>e.edge_id===%d);
        const l=_planV3EdgeLayers.get(String(v.id)); const ll=l.getLatLngs();
        const c=_planV3Map.getContainer(); const r=c.getBoundingClientRect(); const ox=r.left+c.clientLeft, oy=r.top+c.clientTop; const el=l.getElement();
        let first=null;
        const A=_planV3Map.latLngToContainerPoint(ll[0]), B=_planV3Map.latLngToContainerPoint(ll[1]);
        const len=Math.hypot(B.x-A.x, B.y-A.y);
        // шаг 1.3 px, чтобы не «залипнуть» в одной фазе штрихового узора; идём от середины в обе стороны
        for (let d=0; d<len*0.4; d+=1.3) {
          for (const sgn of [1,-1]) {
            const t=0.5+sgn*d/len;
            const pt={x:ox+A.x+(B.x-A.x)*t, y:oy+A.y+(B.y-A.y)*t}; if(!first) first=pt;
            const hit=document.elementFromPoint(pt.x, pt.y);
            if (hit && el && (hit===el || el.contains(hit))) return pt;
          }
        }
        return first; })()"""
              % (APP, edge_id))


def line_color(page, edge_id: int) -> str:
    return js(page, """(()=>{ const a=%s; const v=a.planV3Current.edges.find(e=>e.edge_id===%d);
        return v ? _planV3EdgeLayers.get(String(v.id)).options.color : null; })()""" % (APP, edge_id))


def css_ok(page) -> str:
    return js(page, "_planCssVar('--ok')")


def marker_color(page, node_name: str) -> str:
    return js(page, """(()=>{ const a=%s; const n=a.structNodes.find(x=>x.name===%s);
        const it=a.planV3Current.items.find(i=>i.node_id===n.id);
        return _planV3ItemLayers.get(String(it.id)).options.color; })()""" % (APP, json.dumps(node_name)))


def free_spot(page, fx: float, fy: float) -> dict:
    box = page.locator("#site-plan-v3-map").bounding_box()
    return {"x": box["x"] + box["width"] * fx, "y": box["y"] + box["height"] * fy}


def click_map(page, fx: float, fy: float) -> None:
    wait_map_v3_ready(page)
    p = free_spot(page, fx, fy)
    page.mouse.click(p["x"], p["y"])


def place_by_tool(page, kind_label: str, name: str, fx: float, fy: float) -> None:
    """«+ Элемент схемы» → тип → название → клик по карте."""
    page.locator("button.btn-sm", has_text="+ Элемент схемы").first.click()
    page.wait_for_selector("[data-testid=planv3-node-form]")
    page.select_option("[data-testid=planv3-kind-select]", label=kind_label)
    page.fill("[data-testid=planv3-node-form] input[placeholder='Название']", name)
    click_map(page, fx, fy)
    page.wait_for_function("(n)=>%s.structNodes.some(x=>x.name===n)" % APP, arg=name)
    wait_card_open(page)


def live(h, device_id: str, name: str, power_w: float, energy: float, irms: float | None = None) -> None:
    now = time.time()
    m = h.see_in_mqtt(device_id, name)
    m.controls["Total P"] = ControlState(name="Total P", value=power_w, raw_value=str(power_w),
                                          last_update_ts=now, meta={"type": "power"})
    m.controls["Total AP energy"] = ControlState(name="Total AP energy", value=energy, raw_value=str(energy),
                                                 last_update_ts=now, meta={"type": "value"})
    if irms is not None:
        for ph in ("L1", "L2", "L3"):
            m.controls["Irms " + ph] = ControlState(name="Irms " + ph, value=irms, raw_value=str(irms),
                                                    last_update_ts=now, meta={"type": "value"})
    m.last_any_ts = now
    m.last_measurement_ts = now
    m.status = MeterStatus.OK


def make_point_with_meter(h, code: str, name: str, device_id: str):
    pt = h.make_point(code, name)
    h.bind_meter(pt["id"], device_id, name)
    return pt


def reload_app(page, base_url: str) -> None:
    page.goto(base_url)
    page.wait_for_function("()=>!!document.querySelector('[x-data]') && !!document.querySelector('[x-data]')._x_dataStack")
    page.wait_for_timeout(900)


def pick_plan(page, label: str, items: int | None = None) -> None:
    """Выбрать план в выпадающем списке и дождаться, пока карта отрисует его элементы."""
    page.select_option("select[aria-label='План']:visible", label=label)
    page.wait_for_function("(n)=>{const p=%s.planV3Current; return !!p && p.name===n;}" % APP, arg=label)
    wait_map_v3_ready(page)
    if items is not None:
        page.wait_for_function("(k)=>_planV3ItemLayers.size===k", arg=items)


def to_plan_v3(page) -> None:
    nav(page, "План v3", 900)


def plan_px(page, x: float, y: float) -> dict:
    """Экранные координаты точки плана (x, y в единицах плана)."""
    return js(page, """(()=>{ const a=%s; const H=a._planV3EffectiveDims(a.planV3Current)[1];
        const p=_planV3Map.latLngToContainerPoint([H-%f, %f]);
        const c=_planV3Map.getContainer(); const r=c.getBoundingClientRect(); return {x:r.left+c.clientLeft+p.x, y:r.top+c.clientTop+p.y}; })()"""
              % (APP, y, x))


def click_plan(page, x: float, y: float) -> None:
    wait_map_v3_ready(page)
    p = plan_px(page, x, y)
    page.mouse.click(p["x"], p["y"])


def click_marker(page, name: str) -> None:
    wait_map_v3_ready(page)
    p = marker_xy(page, name)
    page.mouse.click(p["x"], p["y"])


def node_card(page):
    return page.locator("[data-testid=planv3-panel] [data-testid=node-card]")


def edge_card_text(page) -> str:
    return page.locator("[data-testid=planv3-panel]").inner_text()


def line_select(page, row_start: str):
    """Список «Счётчик на линии» в строке карточки узла, начинающейся с row_start («← от Ввод»)."""
    return page.locator(
        "xpath=//div[@data-testid='planv3-panel']//div[@data-testid='node-card']"
        "//div[contains(@class,'sf-val') and starts-with(normalize-space(.),%s)]/parent::div//select"
        % json.dumps(row_start, ensure_ascii=False).replace('"', "'"))


def choose_in(select_loc, label: str) -> None:
    select_loc.select_option(label=label)


class S:  # общие данные сценария
    h = None
    bp = None
    page = None
    plan1 = None   # «План v3» из шага 2
    plan_v2 = None
    plan3 = None


# ================================================================== Раздел 1
def section1() -> None:
    h, page = S.h, S.page
    print("\n================ Раздел 1. Работа без загруженного плана")
    js(page, APP + ".toggleUiSet()")      # новый набор вкладок, выбор запоминается (F5 в шаге 3)
    page.wait_for_timeout(400)

    step(1, "«План v3» на чистой базе: сообщение «Планов пока нет»")
    to_plan_v3(page)
    txt = page.locator("main").inner_text()
    check("Планов пока нет" in txt, "вместо карты — «Планов пока нет»")
    check(page.locator("input[placeholder='Название плана']:visible").count() == 1, "поле «Название плана» видно")
    check(page.locator("button:visible", has_text="Создать план").count() == 1, "кнопка «Создать план» видна")
    check(not page.locator("#site-plan-v3-map").is_visible(), "карты не видно")
    check(page.locator("button:visible", has_text="+ Элемент схемы").count() == 0
          and page.locator("button:visible", has_text="+ Линия").count() == 0,
          "панели инструментов не видно")

    step(2, "«Создать план» без названия → план «План v3», карта и панель инструментов")
    page.locator("button:visible", has_text="Создать план").click()
    page.wait_for_selector("#site-plan-v3-map", state="visible")
    wait_map_v3_ready(page)
    s, plans = api(h, "get", "/api/v2/plans")
    check(len(plans) == 1 and plans[0]["name"] == "План v3", f"создан один план «План v3» ({[p['name'] for p in plans]})")
    S.plan1 = plans[0]
    check(page.locator("#site-plan-v3-map").is_visible(), "появилась карта")
    for label in ("+ Элемент схемы", "+ Линия", "+ Зона (учётная группа)"):
        check(page.locator("button:visible", has_text=label).count() >= 1, f"кнопка инструмента «{label}»")

    step(3, "F5 и снова «План v3»: план открывается сам")
    reload_app(page, h.base_url)
    to_plan_v3(page)
    wait_map_v3_ready(page)
    txt = page.locator("main").inner_text()
    check("Планов пока нет" not in txt, "после F5 «Планов пока нет» нет")
    st = plan_state(page)
    check(st["id"] == S.plan1["id"] and st["name"] == "План v3", f"открыт тот же план ({st['name']!r})")


# ================================================================== Раздел 2
def section2() -> None:
    h, page = S.h, S.page
    print("\n================ Раздел 2. Загрузка плана (когда планов несколько)")
    step(4, "два плана в выпадающем списке; переключение меняет карту")
    # «план из Плана v2» — общий список /api/v2/plans; на нём один узел
    S.plan_v2 = h.create_plan("План из v2")
    n_v2 = h.make_node("v2-node", "Узел из v2", "panel")
    s, item = api(h, "post", f"/api/v2/plans/{S.plan_v2['id']}/items", json={
        "kind": "node", "node_id": n_v2["id"], "geometry": {"x": 500, "y": 400},
        "coord_space": "canvas_xy_v2", "label": "Узел из v2"})
    check(s == 201, "на втором плане стоит узел «Узел из v2»")
    reload_app(page, h.base_url)
    to_plan_v3(page)
    wait_map_v3_ready(page)
    opts = js(page, "[...document.querySelectorAll('select[aria-label=\"План\"]')].filter(s=>s.offsetParent!==null)[0].options.length ? [...[...document.querySelectorAll('select[aria-label=\"План\"]')].filter(s=>s.offsetParent!==null)[0].options].map(o=>o.textContent.trim()) : []")
    check(set(opts) == {"План v3", "План из v2"}, f"в списке видны оба плана ({opts})")
    pick_plan(page, "План из v2", items=1)
    st = plan_state(page)
    check(st["items"] == 1, f"на карту загружены узлы выбранного плана (элементов: {st['items']})")
    tips = js(page, "[...document.querySelectorAll('.leaflet-tooltip')].map(t=>t.textContent)")
    check(any("Узел из v2" in t for t in tips), f"подпись «Узел из v2» на карте ({tips})")
    pick_plan(page, "План v3", items=0)
    # подпись гаснет ~200 мс (плавное исчезновение Leaflet) — ждём, а не угадываем паузой
    page.wait_for_function("()=>![...document.querySelectorAll('.leaflet-tooltip')].some(t=>t.textContent.includes('Узел из v2'))")
    tips = js(page, "[...document.querySelectorAll('.leaflet-tooltip')].map(t=>t.textContent)")
    check(not any("Узел из v2" in t for t in tips), "после возврата на «План v3» узлов чужого плана нет")

    step(5, "[БЫЛО ИНАЧЕ] «+ План» открывает форму; «Создать план» → второй пустой план")
    before = api(h, "get", f"/api/v2/plans/{S.plan_v2['id']}")[1]
    page.locator("[data-testid=planv3-new-plan]").click()
    page.wait_for_selector("[data-testid=planv3-new-plan-form]")
    page.fill("[data-testid=planv3-new-plan-form] input[placeholder='Название плана']", "Третий план")
    page.locator("[data-testid=planv3-new-plan-form] button", has_text="Создать план").click()
    page.wait_for_function("()=>%s.planV3Current && %s.planV3Current.name==='Третий план'" % (APP, APP))
    s, plans = api(h, "get", "/api/v2/plans")
    names = sorted(p["name"] for p in plans)
    check(names == ["План v3", "План из v2", "Третий план"], f"появился план с указанным именем ({names})")
    S.plan3 = next(p for p in plans if p["name"] == "Третий план")
    check(plan_state(page)["items"] == 0, "новый план пуст")
    after = api(h, "get", f"/api/v2/plans/{S.plan_v2['id']}")[1]
    check(len(after["items"]) == len(before["items"]) == 1, "предыдущий план и его элементы не пострадали")
    note("шаг 5: после первого плана форма «Создать план» открывается кнопкой «+ План» — в файле это описано")
    pick_plan(page, "План v3", items=0)


# ================================================================== Раздел 3
def section3() -> None:
    h, page = S.h, S.page
    print("\n================ Раздел 3. Когда ничего не выбрано: списки узлов, линий, зон и точек")
    # --- подготовка: схема-основа (шаг 6 ссылается на раздел 4, где узлы заводятся лишь позже)
    shit_a = h.make_node("shit-a", "Щит-А", "panel")
    load_b = h.make_node("load-b", "Нагрузка-Б", "load")
    e_ab = h.connect(shit_a["id"], load_b["id"])
    live(h, "dev-lineA", "Счётчик линии А", 900.0, 100.0)
    live(h, "dev-lone", "Одинокий счётчик", 100.0, 10.0)
    pt_line = make_point_with_meter(h, "pt-line", "Точка-на-линии", "dev-lineA")
    pt_lone = make_point_with_meter(h, "pt-lone", "Точка-без-линии", "dev-lone")
    h.set_meter_on_edge(e_ab["id"], pt_line["id"])
    from wb_energy_meter.group_repo_v2 import GroupRepoV2
    grepo = GroupRepoV2(h.db)
    grp = grepo.add("Зона-1")
    grepo.add_member(grp.id, pt_line["id"], valid_from=0)
    S.grp = grp
    for node, xy in ((shit_a, (700, 300)),):
        api(h, "post", f"/api/v2/plans/{S.plan1['id']}/items", json={
            "kind": "node", "node_id": node["id"], "geometry": {"x": xy[0], "y": xy[1]},
            "coord_space": "canvas_xy_v2", "label": node["name"]})
    api(h, "post", f"/api/v2/plans/{S.plan1['id']}/items", json={
        "kind": "node", "node_id": load_b["id"], "geometry": {"x": 1200, "y": 300},
        "coord_space": "canvas_xy_v2", "label": "Нагрузка-Б"})
    s, items = api(h, "get", f"/api/v2/plans/{S.plan1['id']}")
    ia = next(i for i in items["items"] if i["node_id"] == shit_a["id"])
    ib = next(i for i in items["items"] if i["node_id"] == load_b["id"])
    api(h, "post", f"/api/v2/plans/{S.plan1['id']}/edges", json={
        "edge_id": e_ab["id"], "from_item_id": ia["id"], "to_item_id": ib["id"], "view_kind": "structural"})
    S.shit_a, S.load_b, S.e_ab, S.pt_line, S.pt_lone = shit_a, load_b, e_ab, pt_line, pt_lone
    reload_app(page, h.base_url)
    to_plan_v3(page)
    wait_map_v3_ready(page)

    step(6, "панель «ничего не выбрано»: подсказка и списки")
    txt = panel(page)
    check("Кликните элемент схемы на карте, чтобы открыть его карточку, либо выберите инструмент выше." in txt,
          "текст-подсказка")
    low = txt.lower()
    for blk in ("элементы схемы (", "линии (", "зоны (", "точки учёта ("):
        check(blk in low, f"блок «{blk.strip(' (')}»")
    check("приборы без точки учёта" not in low, "блока «Приборы без точки учёта» нет, пока таких приборов нет")
    check("кликните элемент-источник" not in low, "подсказка инструмента не показывается, пока он не выбран")
    page.locator("button.btn-sm", has_text="+ Линия").click()
    page.wait_for_timeout(200)
    check("Кликните элемент-источник, затем элемент-получатель, чтобы провести линию." in panel(page),
          "с выбранным инструментом подсказка меняется на инструмент-специфичную")
    page.locator("button", has_text="Отменить инструмент").click()
    note("шаг 6: текст подсказки теперь про «элемент схемы» (слово «узел» заменено словарём интерфейса)")

    step(7, "клик по имени узла в списке «Элементы схемы» → карточка именно этого узла")
    page.locator("[data-testid=planv3-node-row]", has_text="Щит-А (Щит)").click()
    wait_card_open(page)
    st = plan_state(page)
    check(st["cardKind"] == "node" and st["cardName"] == "Щит-А", f"открыта карточка «Щит-А» ({st['cardName']!r})")
    close_card(page)

    step(8, "[БЫЛО ИНАЧЕ] новый элемент со «Структуры», не на плане → «Не размещены на плане» с кнопкой")
    nav(page, "Структура", 1200)
    page.locator("button.btn-sm", has_text="+ Элемент схемы").first.click()
    page.wait_for_timeout(300)
    page.select_option("select[aria-label='Тип нового элемента']", label="Нагрузка")
    page.fill("input[placeholder='Название']", "Склад")
    page.locator("button", has_text="Добавить в схему").click()
    page.wait_for_function("(n)=>%s.structNodes.some(x=>x.name===n)" % APP, arg="Склад")
    to_plan_v3(page)
    wait_map_v3_ready(page)
    if plan_state(page)["cardKind"]:
        close_card(page)
    txt = panel(page)
    unplaced = page.locator("[data-testid=planv3-unplaced]").inner_text()
    check("Склад (Нагрузка)" in unplaced and "не размещены на плане (2)" in unplaced.lower(),
          f"«Склад» появился в блоке «Не размещены на плане» (вместе с «Узел из v2» — он стоит на другом плане) ({unplaced[:80]!r})")
    row = page.locator("[data-testid=planv3-unplaced] div", has_text="Склад").filter(
        has=page.locator("button", has_text="Разместить на карте")).last
    check(row.count() == 1, "у «Склад» есть кнопка «Разместить на карте»")
    note("шаг 8: ненанесённый элемент показан отдельным блоком «Не размещены на плане (N)» (а также строкой в «Элементы схемы»)")

    step(9, "[БЫЛО ИНАЧЕ] «Разместить на карте» → режим размещения без формы типа/названия")
    row.locator("button", has_text="Разместить на карте").click()
    wait_toast(page, "Кликните на карте")
    check("Кликните на карте, чтобы разместить элемент схемы" in toast_text(page), f"уведомление ({toast_text(page)!r})")
    check(page.locator("#site-plan-v3-map.planv3-placing").count() == 1, "курсор карты — режим размещения")
    check(page.locator("[data-testid=planv3-node-form]").count() == 0, "формы типа/названия нет — элемент уже существует")
    check("Кликните на карте, чтобы разместить выбранный элемент." in panel(page), "подсказка в панели")
    note("шаг 9: уведомление говорит «элемент схемы», а не «узел»")

    step(10, "клик по свободному месту → «размещён на плане», маркер, карточка, строка кликабельна")
    click_map(page, 0.2, 0.7)
    wait_card_open(page)
    check("«Склад» размещён на плане" in wait_toast(page, "размещён"), f"уведомление ({toast_text(page)!r})")
    st = plan_state(page)
    check(st["cardKind"] == "node" and st["cardName"] == "Склад", "карточка «Склад» открылась сама")
    check(marker_xy(page, "Склад") is not None, "на карте появился маркер")
    close_card(page)
    unplaced = page.locator("[data-testid=planv3-unplaced]").inner_text()
    check("Склад" not in unplaced, "«Склад» из блока «Не размещены на плане» исчез")
    check(page.locator("[data-testid=planv3-node-row]", has_text="Склад (Нагрузка)").count() == 1,
          "строка «Склад» в «Элементы схемы» кликабельна")

    step(11, "строка линии → карточка линии; строка зоны → карточка группы; контур на карте — подсказка")
    page.locator("[data-testid=planv3-panel] .sf-val", has_text="Щит-А → Нагрузка-Б").first.click()
    wait_card_open(page)
    st = plan_state(page)
    check(st["cardKind"] == "edge" and st["cardId"] == S.e_ab["id"], "открылась карточка именно этой линии")
    close_card(page)
    page.locator("[data-testid=planv3-panel] .sf-val", has_text="Зона-1").first.click()
    wait_card_open(page)
    ctext = page.locator("[data-testid=planv3-panel]").inner_text()
    check(plan_state(page)["cardKind"] == "group", "открылась карточка учётной группы")
    check("Точка-на-линии" in ctext and "расход за период" in ctext.lower(), f"в карточке состав и «Расход за период» ({ctext[:160]!r})")
    close_card(page)

    step(12, "«— не стоит на линии» только у точек вне линий")
    rows = page.locator("[data-testid=planv3-point-row]")
    texts = [rows.nth(i).inner_text() for i in range(rows.count())]
    lone = next(t for t in texts if "Точка-без-линии" in t)
    onl = next(t for t in texts if "Точка-на-линии" in t)
    check("не стоит на линии" in lone, f"у точки вне линий пометка есть ({lone!r})")
    check("не стоит на линии" not in onl, f"у точки на линии пометки нет ({onl!r})")

    step(13, "[БЫЛО ИНАЧЕ] «Приборы без точки учёта» — подсказка про «+ Счётчик»; без таких приборов блока нет")
    check("приборы без точки учёта" not in panel(page).lower(), "без свободных приборов блока нет")
    h.see_in_mqtt("dev-free-1", "Свободный прибор")
    reload_app(page, h.base_url)
    to_plan_v3(page)
    wait_map_v3_ready(page)
    txt = panel(page)
    check("приборы без точки учёта (1)" in txt.lower(), "блок появился сам")
    check("Поставьте счётчик: «+ Счётчик» в карточке ввода, щита или нагрузки." in txt, "подсказка про «+ Счётчик»")
    note("шаг 13: подсказка ведёт в «+ Счётчик» карточки элемента (в файле раньше — «новая точка из прибора» на карточке линии)")


# ================================================================== Раздел 4
def section4() -> None:
    h, page = S.h, S.page
    print("\n================ Раздел 4. Узлы")
    requests: list[tuple[str, str, str]] = []
    page.on("request", lambda r: requests.append((r.method, r.url, r.post_data or "")))
    js(page, "window.__noReload = 1")

    step(14, "«+ Элемент схемы»: кнопка подсвечена, строка с типом, названием и подсказкой")
    btn = page.locator("button.btn-sm", has_text="+ Элемент схемы").first
    btn.click()
    page.wait_for_selector("[data-testid=planv3-node-form]")
    check("btn-primary" in (btn.get_attribute("class") or ""), "кнопка подсвечена")
    kinds = js(page, "[...document.querySelectorAll('[data-testid=planv3-kind-select] option')].map(o=>o.textContent.trim())")
    check(kinds == ["Ввод", "Щит", "Ответвление", "Нагрузка"], f"типы: Ввод/Щит/Ответвление/Нагрузка ({kinds})")
    check(page.locator("[data-testid=planv3-node-form] input[placeholder='Название']").count() == 1, "поле названия")
    check("Кликните на карте, чтобы разместить" in page.locator("[data-testid=planv3-node-form]").inner_text(),
          "подсказка «Кликните на карте, чтобы разместить»")

    step(15, "«Щит» «ЩР-1» + клик по карте → маркер с постоянной подписью, уведомление, карточка")
    nodes_before = len(api(h, "get", "/api/v2/topology/nodes")[1])
    page.select_option("[data-testid=planv3-kind-select]", label="Щит")
    page.fill("[data-testid=planv3-node-form] input[placeholder='Название']", "ЩР-1")
    click_plan(page, 1000, 900)
    page.wait_for_function("(n)=>%s.structNodes.some(x=>x.name===n)" % APP, arg="ЩР-1")
    wait_card_open(page)
    check("«ЩР-1» размещён на плане" in wait_toast(page, "размещён"), f"уведомление ({toast_text(page)!r})")
    check(len(api(h, "get", "/api/v2/topology/nodes")[1]) == nodes_before + 1, "узел создан")
    tips = js(page, "[...document.querySelectorAll('.leaflet-tooltip.planv3-node-tip')].map(t=>t.textContent)")
    check(any("ЩР-1" in t for t in tips), f"постоянная подпись «ЩР-1» на карте ({tips})")
    st = plan_state(page)
    check(st["cardKind"] == "node" and st["cardName"] == "ЩР-1", "справа открылась карточка узла")
    if st["tool"] is not None:
        note("шаг 15: после размещения инструмент «+ Элемент схемы» остаётся включённым (форма сохраняет тип и "
             "очищает название — можно ставить подряд); файл говорил «сам отключается» — исправлен в файле")
    check(st["tool"] in ("node", None), f"состояние инструмента после размещения: {st['tool']!r}")

    step(16, "[БЫЛО ИНАЧЕ] порядок полей карточки узла")
    labels = [t.strip().lower() for t in js(page,
        "[...document.querySelectorAll('[data-testid=planv3-panel] [data-testid=node-card] .sf-lbl')].map(e=>e.innerText)")]
    want = ["название", "тип", "где стоит", "счётчики (0)", "линии этого элемента", "баланс щита за период",
            "добавить отходящую линию", "действия"]
    pos = []
    for w in want:
        idx = next((i for i, l in enumerate(labels) if l.startswith(w)), -1)
        pos.append(idx)
    check(all(i >= 0 for i in pos) and pos == sorted(pos), f"поля по порядку: {labels}")
    kind_opts = js(page, "[...document.querySelectorAll('[data-testid=planv3-panel] [data-testid=node-card-kind-select] option')].map(o=>o.textContent.trim())")
    check(kind_opts == ["Ввод", "Щит", "Ответвление", "Нагрузка"], f"«Тип» — выпадающий список ({kind_opts})")
    card_text = node_card(page).inner_text()
    check("Линий нет." in card_text, "«Линии этого элемента»: «Линий нет.»")
    check(card_text.count("Сохранить") >= 3, "у «Названия», «Типа» и «Где стоит» своя кнопка «Сохранить»")
    note("шаг 16: между «Типа» и «Линий» в карточке появился блок «Счётчики (N)» с «+ Счётчик»; «узла» → «элемента»")

    step(17, "«Где стоит» + «Сохранить»; один PATCH с тремя полями; значение хранится")
    requests.clear()
    loc_input = node_card(page).locator("input[placeholder='например, ЩР-1, эт. 2']")
    loc_input.fill("эт. 2, у входа")
    loc_input.locator("xpath=following-sibling::button").click()
    wait_toast(page, "Сохранено")
    check("Сохранено" in toast_text(page), f"уведомление ({toast_text(page)!r})")
    patches = [r for r in requests if r[0] == "PATCH" and "/api/v2/topology/nodes/" in r[1]]
    check(len(patches) == 1 and {"name", "kind", "location_text"} <= set(json.loads(patches[0][2])),
          f"один PATCH со всеми тремя полями ({[json.loads(p[2]).keys() for p in patches]})")
    close_card(page)
    click_marker(page, "ЩР-1")
    wait_card_open(page)
    check(node_card(page).locator("input[placeholder='например, ЩР-1, эт. 2']").input_value() == "эт. 2, у входа",
          "после повторного открытия «Где стоит» показывает сохранённый текст")
    node = node_by_name(h, "ЩР-1")
    loc = api(h, "get", f"/api/v2/locations/{node['location_id']}")[1] if node.get("location_id") else None
    check(bool(loc) and loc.get("name") == "эт. 2, у входа", f"в API место узла сохранено ({loc and loc.get('name')!r})")
    note("шаг 17: уведомление после «Сохранить» — «Сохранено» (в файле было «Узел обновлён»)")

    step(18, "новое «Название» → подпись на карте и в карточке обновились без перезагрузки")
    name_input = node_card(page).locator("input[aria-label='Название элемента']")
    name_input.fill("ЩР-1 (главный)")
    name_input.locator("xpath=following-sibling::button").click()
    page.wait_for_function("()=>[...document.querySelectorAll('.leaflet-tooltip.planv3-node-tip')].some(t=>t.textContent.includes('ЩР-1 (главный)'))")
    page.wait_for_function("()=>{const h=document.querySelector('[data-testid=planv3-panel] [data-testid=node-card] h3'); return !!h && h.textContent.trim()==='ЩР-1 (главный)';}")
    check(node_card(page).locator("h3").inner_text().strip().lower() == "щр-1 (главный)", f"название в карточке обновилось ({node_card(page).locator('h3').all_inner_texts()})")
    check(js(page, "window.__noReload") == 1, "страница не перезагружалась")

    step(19, "клик по маркеру без инструмента → карточка этого же узла, дубликата нет")
    close_card(page)
    page.locator("button", has_text="Отменить инструмент").click()
    nodes_before = len(api(h, "get", "/api/v2/topology/nodes")[1])
    click_marker(page, "ЩР-1 (главный)")
    wait_card_open(page)
    st = plan_state(page)
    check(st["cardName"] == "ЩР-1 (главный)", f"открылась карточка этого узла ({st['cardName']!r})")
    check(len(api(h, "get", "/api/v2/topology/nodes")[1]) == nodes_before, "новый узел не создан")

    step(20, "перетащить маркер → остаётся на новом месте после F5")
    close_card(page)
    n = node_by_name(h, "ЩР-1 (главный)")
    item_before = next(i for i in api(h, "get", f"/api/v2/plans/{S.plan1['id']}")[1]["items"] if i["node_id"] == n["id"])
    p0 = marker_xy(page, "ЩР-1 (главный)")
    page.mouse.move(p0["x"], p0["y"])
    page.mouse.down()
    page.mouse.move(p0["x"] + 90, p0["y"] - 50, steps=12)
    page.mouse.up()
    item_after = item_before
    for _ in range(60):
        item_after = next(i for i in api(h, "get", f"/api/v2/plans/{S.plan1['id']}")[1]["items"] if i["id"] == item_before["id"])
        if item_after["geometry"] != item_before["geometry"]:
            break
        page.wait_for_timeout(150)
    check(item_after["geometry"] != item_before["geometry"],
          f"позиция изменилась и сохранена на сервере: {item_before['geometry']} → {item_after['geometry']}")
    reload_app(page, h.base_url)
    to_plan_v3(page)
    wait_map_v3_ready(page)
    p1 = marker_xy(page, "ЩР-1 (главный)")
    plan_pos = plan_px(page, item_after["geometry"]["x"], item_after["geometry"]["y"])
    check(abs(p1["x"] - plan_pos["x"]) < 2 and abs(p1["y"] - plan_pos["y"]) < 2,
          "после F5 маркер стоит на новом месте")
    page.on("request", lambda r: None)


# ================================================================== Раздел 5
def section5() -> None:
    h, page = S.h, S.page
    print("\n================ Раздел 5. Линии (включая несколько счётчиков в одном узле)")
    live(h, "dev-in", "Прибор ввода", 4800.0, 1000.0, irms=10.0)
    live(h, "dev-sock", "Прибор розеток", 1200.0, 300.0, irms=5.0)
    live(h, "dev-light", "Прибор освещения", 500.0, 120.0)
    live(h, "dev-free-2", "Свободный прибор 2", 100.0, 5.0)
    pt_in = make_point_with_meter(h, "pt-in", "Счётчик-ввод", "dev-in")
    pt_sock = make_point_with_meter(h, "pt-sock", "Счётчик-розетки", "dev-sock")
    S.pt_in, S.pt_sock = pt_in, pt_sock
    reload_app(page, h.base_url)
    to_plan_v3(page)
    wait_map_v3_ready(page)

    step(21, "второй узел типа «Ввод»")
    page.locator("button.btn-sm", has_text="+ Элемент схемы").first.click()
    page.wait_for_selector("[data-testid=planv3-node-form]")
    page.select_option("[data-testid=planv3-kind-select]", label="Ввод")
    page.fill("[data-testid=planv3-node-form] input[placeholder='Название']", "Ввод")
    click_plan(page, 300, 900)
    page.wait_for_function("(n)=>%s.structNodes.some(x=>x.name===n)" % APP, arg="Ввод")
    wait_card_open(page)
    check((node_by_name(h, "Ввод") or {}).get("kind") == "source", "узел «Ввод» типа «Ввод» создан")
    close_card(page)
    page.locator("button", has_text="Отменить инструмент").click()

    step(22, "[БЫЛО ИНАЧЕ] «+ Линия» → подсказка про элемент-источник и элемент-получатель")
    page.locator("button.btn-sm", has_text="+ Линия").click()
    page.wait_for_timeout(200)
    check("Кликните элемент-источник, затем элемент-получатель" in page.locator("main").inner_text(),
          "подсказка «Кликните элемент-источник, затем элемент-получатель»")

    step(23, "клик «Ввод», затем «ЩР-1 (главный)» → линия оранжевая, «Линия проведена»")
    click_marker(page, "Ввод")
    page.wait_for_function("()=>!!%s.planV3EdgeFirstItem" % APP)
    check("Откуда: «Ввод»" in page.locator("main").inner_text(), "после первого клика: «Откуда: «Ввод» — кликните … получатель»")
    click_marker(page, "ЩР-1 (главный)")
    e_in = None
    for _ in range(40):
        e_in = edge_between(h, "Ввод", "ЩР-1 (главный)")
        if e_in:
            break
        page.wait_for_timeout(150)
    check(e_in is not None, "линия «Ввод → ЩР-1 (главный)» создана")
    check("Линия проведена" in wait_toast(page, "Линия проведена"), f"уведомление ({toast_text(page)!r})")
    page.wait_for_function("(id)=>%s.planV3Current.edges.some(v=>v.edge_id===id)" % APP, arg=e_in["id"])
    check(line_color(page, e_in["id"]) == "#e0995f", f"цвет «без счётчика» ({line_color(page, e_in['id'])})")
    check(js(page, APP + ".planV3Tool") is None, "инструмент «+ Линия» сам отключился")
    S.e_in = e_in

    step(24, "[БЫЛО ИНАЧЕ] карточка «ЩР-1 (главный)»: «← от Ввод» с «— без счётчика —»")
    click_marker(page, "ЩР-1 (главный)")
    wait_card_open(page)
    card = node_card(page).inner_text()
    check("← от Ввод" in card, "строка «← от Ввод»")
    sel = line_select(page, "← от Ввод")
    check(sel.count() == 1 and sel.evaluate("s=>s.options[s.selectedIndex].textContent.trim()") == "— без счётчика —",
          "выпадающий список «— без счётчика —»")
    check(node_card(page).locator("button", has_text="Карточка линии").count() == 1, "кнопка «Карточка линии» в строке")
    check("новая точка из прибора" not in card, "«＋ новая точка из прибора…» в карточке УЗЛА больше нет")
    note("шаг 24: «＋ новая точка из прибора…» теперь только в карточке ЛИНИИ (строка узла даёт «Карточка линии»)")

    step(25, "«Добавить отходящую линию» «Розетки» → узел «Нагрузка» и линия одним действием")
    inp = node_card(page).locator("input[placeholder='что питает линия']")
    inp.fill("Розетки")
    node_card(page).locator("button", has_text="Добавить").last.click()
    check("Отходящая линия добавлена" in wait_toast(page, "Отходящая линия добавлена"), f"уведомление ({toast_text(page)!r})")
    page.wait_for_function("(n)=>%s.structNodes.some(x=>x.name===n)" % APP, arg="Розетки")
    s_node = node_by_name(h, "Розетки")
    check(s_node and s_node["kind"] == "load", "узел «Розетки» типа «Нагрузка»")
    e_sock = edge_between(h, "ЩР-1 (главный)", "Розетки")
    check(e_sock is not None, "линия «ЩР-1 (главный) → Розетки» создана тем же действием")
    page.wait_for_function("()=>%s.structInspector && (%s.structInspector.lines||[]).length>=2" % (APP, APP))
    check("→ к Розетки" in node_card(page).inner_text(), "карточка осталась открытой, в списке линий «→ к Розетки»")
    page.wait_for_function("(n)=>%s.planV3Current.items.some(i=>i.node_id===n)" % APP, arg=s_node["id"])
    check(marker_xy(page, "Розетки") is not None, "рядом на карте появился узел «Розетки»")
    S.e_sock = e_sock

    step(26, "то же с именем «Освещение»")
    inp = node_card(page).locator("input[placeholder='что питает линия']")
    inp.fill("Освещение")
    node_card(page).locator("button", has_text="Добавить").last.click()
    wait_toast(page, "Отходящая линия добавлена")
    page.wait_for_function("()=>%s.structInspector && (%s.structInspector.lines||[]).length>=3" % (APP, APP))
    e_light = edge_between(h, "ЩР-1 (главный)", "Освещение")
    check(e_light is not None and "→ к Освещение" in node_card(page).inner_text(), "второй потребитель от того же узла")
    S.e_light = e_light

    step(27, "счётчик на входящую линию → «Счётчик назначен», линия зелёная")
    choose_in(line_select(page, "← от Ввод"), "Счётчик-ввод")
    check("Счётчик назначен" in wait_toast(page, "Счётчик назначен"), f"уведомление ({toast_text(page)!r})")
    page.wait_for_function("(id)=>%s.planV3Current.edges.length>0" % APP, arg=e_in["id"])
    page.wait_for_function("(c)=>{const a=%s; const v=a.planV3Current.edges.find(e=>e.edge_id===%d); return _planV3EdgeLayers.get(String(v.id)).options.color===c;}" % (APP, e_in["id"]),
                           arg=css_ok(page))
    check(line_color(page, e_in["id"]) == css_ok(page), "линия «Ввод → ЩР-1» зелёная («со счётчиком»)")

    step(28, "[БАГ §5] счётчик и на отходящую линию того же узла: назначены оба")
    choose_in(line_select(page, "→ к Розетки"), "Счётчик-розетки")
    wait_toast(page, "Счётчик назначен")
    page.wait_for_function("(id)=>%s.structEdges.find(e=>e.id===id).primary_point_id!=null" % APP, arg=e_sock["id"])
    ein = api(h, "get", f"/api/v2/topology/edges/{e_in['id']}")[1]
    esock = api(h, "get", f"/api/v2/topology/edges/{e_sock['id']}")[1]
    elight = api(h, "get", f"/api/v2/topology/edges/{e_light['id']}")[1]
    check(ein["primary_point_id"] == pt_in["id"] and esock["primary_point_id"] == pt_sock["id"],
          "назначены ОБА счётчика — на входящую и на отходящую линии одного узла")
    check(elight.get("primary_point_id") is None, "«ЩР-1 → Освещение» осталась без счётчика")
    check(line_color(page, e_light["id"]) == "#e0995f", "линия «Освещение» жёлто-оранжевая")

    step(29, "«— без счётчика —» на «← от Ввод» → «Счётчик снят», линия снова оранжевая")
    choose_in(line_select(page, "← от Ввод"), "— без счётчика —")
    check("Счётчик снят" in wait_toast(page, "Счётчик снят"), f"уведомление ({toast_text(page)!r})")
    page.wait_for_function("(id)=>%s.structEdges.find(e=>e.id===id).primary_point_id==null" % APP, arg=e_in["id"])
    page.wait_for_function("(c)=>{const a=%s; const v=a.planV3Current.edges.find(e=>e.edge_id===%d); return _planV3EdgeLayers.get(String(v.id)).options.color===c;}" % (APP, e_in["id"]),
                           arg="#e0995f")
    check(line_color(page, e_in["id"]) == "#e0995f", "линия снова оранжевая")

    step(30, "[БЫЛО ИНАЧЕ] «＋ новая точка из прибора…» в карточке ЛИНИИ «Освещение»: прибор из списка свободных")
    row = node_card(page).locator("xpath=.//div[contains(@class,'sf-val') and starts-with(normalize-space(.),'→ к Освещение')]/parent::div")
    row.locator("button", has_text="Карточка линии").click()
    page.wait_for_function("()=>%s.structInspector && %s.structInspector.kind==='edge'" % (APP, APP))
    wait_card_open(page)
    page.locator("[data-testid=planv3-panel] button", has_text="новая точка из прибора").click()
    dev_opts = js(page, "[...document.querySelectorAll('[data-testid=edge-attach-device] option')].map(o=>o.textContent.trim())")
    check(any("Свободный прибор 2" in o or "dev-free-2" in o for o in dev_opts), f"в списке свободных приборов есть dev-free-2 ({dev_opts})")
    page.select_option("[data-testid=edge-attach-device]", "dev-free-2")
    page.locator("[data-testid=planv3-panel] button", has_text="Подключить").click()
    page.wait_for_function("(id)=>%s.structEdges.find(e=>e.id===id).primary_point_id!=null" % APP, arg=e_light["id"])
    elight = api(h, "get", f"/api/v2/topology/edges/{e_light['id']}")[1]
    check(elight["primary_point_id"] is not None, "точка создана и сразу назначена измерением именно этой линии")
    note("шаг 30: прибор выбирается из списка свободных (поле для ввода device_id убрано)")
    close_card(page)

    step(31, "[БЫЛО ИНАЧЕ] клик по самой линии на карте → собственная карточка линии")
    # вернём счётчик на «Ввод → ЩР-1» через карточку линии — и убедимся, что карточка — именно линии
    click_marker(page, "Ввод")   # без инструмента клик по маркеру откроет карточку узла «Ввод»
    wait_card_open(page)
    close_card(page)
    p = line_xy(page, e_in["id"])
    page.mouse.click(p["x"], p["y"])
    wait_card_open(page, timeout=3000)
    st = plan_state(page)
    check(st["cardKind"] == "edge" and st["cardId"] == e_in["id"], f"открылась собственная карточка линии ({st['cardKind']})")
    ptxt = edge_card_text(page)
    for want in ("Название", "Номинальный ток, А", "Кабель / примечание", "Точка измерения"):
        check(want.lower() in ptxt.lower(), f"в карточке линии поле «{want}»")
    check("новая точка из прибора" in ptxt, "и «＋ новая точка из прибора…»")
    sel = page.locator("[data-testid=planv3-panel] select[aria-label='Точка измерения линии']")
    sel.select_option(label="Счётчик-ввод")
    wait_toast(page, "Счётчик назначен")
    page.wait_for_function("(id)=>%s.structEdges.find(e=>e.id===id).primary_point_id!=null" % APP, arg=e_in["id"])

    step(32, "номинальный ток 25 → «Линия обновлена», хранится; при живом токе «% от номинала»")
    close_card(page)
    page.mouse.click(p["x"], p["y"])
    wait_card_open(page)
    rated = page.locator("[data-testid=planv3-panel] input[aria-label='Номинальный ток, А']")
    rated.fill("25")
    rated.locator("xpath=following-sibling::button").click()
    check("Линия обновлена" in wait_toast(page, "Линия обновлена"), f"уведомление ({toast_text(page)!r})")
    eapi = api(h, "get", f"/api/v2/topology/edges/{e_in['id']}")[1]
    check(float(eapi["rated_current_a"]) == 25.0, "в API номинальный ток 25")
    close_card(page)
    page.mouse.click(p["x"], p["y"])
    wait_card_open(page)
    check(page.locator("[data-testid=planv3-panel] input[aria-label='Номинальный ток, А']").input_value() in ("25", "25.0"),
          "при повторном открытии значение сохранено")
    page.wait_for_function("()=>/от номинала/.test(document.querySelector('[data-testid=planv3-panel]').innerText)",
                           timeout=12000)
    check("40% от номинала" in edge_card_text(page), "при измеренном токе 10 А рядом «40% от номинала»")
    close_card(page)


# ================================================================== Раздел 6
def kind_label(sel) -> str:
    return sel.evaluate("s=>s.options[s.selectedIndex].textContent.trim()")


def save_kind(card) -> None:
    """«Сохранить» рядом с полем «Тип» (кнопка — сосед выпадающего списка)."""
    card.locator("[data-testid=node-card-kind-select]").locator("xpath=following-sibling::button").click()


def open_node_card(page, h, name: str) -> None:
    n = node_by_name(h, name)
    click_marker(page, name)
    wait_card_open(page)
    try:
        page.wait_for_function(
            "(id)=>{const i=%s.structInspector; return !!i && i.kind==='node' && i.id===id && !!i.data;}" % APP,
            arg=n["id"], timeout=6000)
    except Exception:
        print("DBG open_node_card", name, n["id"], js(page, "(()=>{const i=%s.structInspector; return i?{kind:i.kind,id:i.id,hasData:!!i.data,keys:Object.keys(i).slice(0,12)}:null})()" % APP))
        raise


def section6() -> None:
    h, page = S.h, S.page
    print("\n================ Раздел 6. Смена типа узла")

    step(33, "«Розетки» — «Нагрузка»: вместо формы отходящей линии только серая подсказка")
    open_node_card(page, h, "Розетки")
    card = node_card(page)
    ksel = card.locator("[data-testid=node-card-kind-select]")
    check(kind_label(ksel) == "Нагрузка", f"тип узла — «Нагрузка» ({kind_label(ksel)})")
    check("У нагрузки не может быть отходящих линий." in card.inner_text(), "серая подсказка про отходящие линии")
    check(card.locator("input[placeholder='что питает линия']").count() == 0, "поля «что питает линия» нет")
    check(card.get_by_role("button", name="Добавить", exact=True).count() == 0, "кнопки «Добавить» нет")
    note("шаг 33: подсказка «У нагрузки не может быть отходящих линий.» (в файле — «У потребителя…»: слово «нагрузка» из словаря интерфейса)")

    step(34, "[БЫЛО ИНАЧЕ] тип «Щит» → «Сохранено», «(Щит)» в списке; цвет маркера зависит от типа")
    color_before = marker_color(page, "Розетки")
    ksel.select_option(label="Щит")
    save_kind(card)
    check("Сохранено" in wait_toast(page, "Сохранено"), f"уведомление ({toast_text(page)!r})")
    page.wait_for_function("(n)=>{const x=%s.structNodes.find(q=>q.name===n); return !!x && x.kind==='panel';}" % APP, arg="Розетки")
    check((node_by_name(h, "Розетки") or {}).get("kind") == "panel", "в API тип «panel»")
    page.wait_for_function("(c)=>{const a=%s; const n=a.structNodes.find(x=>x.name==='Розетки'); const it=a.planV3Current.items.find(i=>i.node_id===n.id); const l=_planV3ItemLayers.get(String(it.id)); return !!l && l.options.color!==c;}" % APP,
                           arg=color_before)
    color_after = marker_color(page, "Розетки")
    check(color_before == "#f0a83c" and color_after == "#3dd6c0", f"цвет маркера сменился с «Нагрузки» на «Щит» ({color_before} → {color_after})")
    note("шаг 34: уведомление «Сохранено» (в файле — «Узел обновлён»); цвет маркера зависит от типа элемента (в файле — «не меняет цвет/форму»)")
    close_card(page)
    check("Розетки (Щит)" in panel(page), "в списке «Элементы схемы» рядом с именем «(Щит)»")

    step(35, "карточка открыта — форма «Добавить отходящую линию» без перезагрузки страницы")
    js(page, "window.__s6_noreload = 1")
    open_node_card(page, h, "Розетки")
    card = node_card(page)
    check(card.locator("input[placeholder='что питает линия']").count() == 1, "поле «что питает линия» есть")
    check(card.get_by_role("button", name="Добавить", exact=True).count() == 1, "кнопка «Добавить» есть")
    check("У нагрузки не может быть отходящих линий." not in card.inner_text(), "серой подсказки больше нет")
    check(js(page, "window.__s6_noreload") == 1, "страница не перезагружалась")

    step(36, "«Тест-ветка» + «Добавить» → отходящая линия проводится")
    card.locator("input[placeholder='что питает линия']").fill("Тест-ветка")
    card.get_by_role("button", name="Добавить", exact=True).click()
    check("Отходящая линия добавлена" in wait_toast(page, "Отходящая линия добавлена"), f"уведомление ({toast_text(page)!r})")
    page.wait_for_function("(n)=>%s.structNodes.some(x=>x.name===n)" % APP, arg="Тест-ветка")
    check(edge_between(h, "Розетки", "Тест-ветка") is not None, "линия «Розетки → Тест-ветка» создана")
    check((node_by_name(h, "Тест-ветка") or {}).get("kind") == "load", "«Тест-ветка» — «Нагрузка»")

    step(37, "«Ввод» для узла с входящей линией → отказ с именем узла и причиной; тип прежний")
    card = node_card(page)
    ksel = card.locator("[data-testid=node-card-kind-select]")
    ksel.select_option(label="Ввод")
    save_kind(card)
    want = "«Розетки» нельзя сделать вводом: в него уже входит линия. Сначала удалите входящую линию."
    page.wait_for_function("(s)=>document.querySelector('[data-testid=planv3-panel]').innerText.includes(s)", arg=want, timeout=8000)
    check(want in node_card(page).inner_text(), "предупреждение с именем узла и причиной (не код ошибки)")
    check((node_by_name(h, "Розетки") or {}).get("kind") == "panel", "тип узла на сервере прежний («Щит»)")
    check(edge_between(h, "ЩР-1 (главный)", "Розетки") is not None, "линия «ЩР-1 → Розетки» на месте")
    check(marker_xy(page, "Розетки") is not None, "узел и линия остаются на карте")
    if kind_label(ksel) != "Щит":
        note("шаг 37: после отказа выпадающий список остаётся на выбранном «Ввод» рядом с предупреждением (черновик не сбрасывается); "
             "на сервере и в списке элементов тип прежний — в файле было «остаётся «Щит»»")
    # вернуть черновик к сохранённому — не оставлять «Ввод» в форме
    ksel.select_option(label="Щит")
    close_card(page)

    step(38, "парный случай: «Ввод» для узла БЕЗ входящей линии сохраняется")
    # В файле парой назван «Освещение», но после шага 26 у него есть входящая линия «ЩР-1 → Освещение» —
    # правило шага 37 к нему применяется так же. Проверяем и это, и настоящий парный случай — «Склад».
    open_node_card(page, h, "Освещение")
    card = node_card(page)
    card.locator("[data-testid=node-card-kind-select]").select_option(label="Ввод")
    save_kind(card)
    want2 = "«Освещение» нельзя сделать вводом: в него уже входит линия."
    page.wait_for_function("(s)=>document.querySelector('[data-testid=planv3-panel]').innerText.includes(s)", arg=want2, timeout=8000)
    check(want2 in node_card(page).inner_text(), "«Освещение» (входящая линия есть) — отказ, как в шаге 37")
    check((node_by_name(h, "Освещение") or {}).get("kind") == "load", "тип «Освещения» прежний")
    card.locator("[data-testid=node-card-kind-select]").select_option(label="Нагрузка")
    close_card(page)
    sklad = node_by_name(h, "Склад")
    check(sklad is not None and not any(e["to_node_id"] == sklad["id"] for e in edges(h)),
          "«Склад» — узел без входящей линии")
    open_node_card(page, h, "Склад")
    card = node_card(page)
    card.locator("[data-testid=node-card-kind-select]").select_option(label="Ввод")
    save_kind(card)
    check("Сохранено" in wait_toast(page, "Сохранено"), f"уведомление ({toast_text(page)!r})")
    page.wait_for_function("(n)=>{const x=%s.structNodes.find(q=>q.name===n); return !!x && x.kind==='source';}" % APP, arg="Склад")
    check((node_by_name(h, "Склад") or {}).get("kind") == "source", "«Склад» сохранён как «Ввод»")
    note("шаг 38: в файле парой был «Освещение», но у него после шага 26 есть входящая линия — смена на «Ввод» отклоняется; "
         "парный случай проверяется на узле без входящей линии («Склад»), файл исправлен")
    close_card(page)


# ================================================================== Раздел 7
ZONE_POLY = [(1350, 700), (1850, 700), (1850, 1100), (1350, 1100)]


def draw_zone(page, pts=None, finish=True) -> None:
    pts = pts or ZONE_POLY
    for (x, y) in pts[:-1]:
        click_plan(page, x, y)
        page.wait_for_timeout(120)
    px = plan_px(page, *pts[-1])
    if finish:
        page.mouse.dblclick(px["x"], px["y"])
    else:
        page.mouse.click(px["x"], px["y"])


def section7() -> None:
    h, page = S.h, S.page
    print("\n================ Раздел 7. Зоны (учётные группы контуром на карте)")

    step(39, "[БЫЛО ИНАЧЕ] «Структура»: группы только отображаются; заводятся через «＋ Новая группа» на «Плане v3»")
    nav(page, "Структура", 900)
    main_txt = page.locator("main").inner_text()
    check("учётные группы (1)" in main_txt.lower() and page.locator("[data-testid=struct-group-row]").count() == 1,
          "на «Структуре» группы показаны списком («Зона-1»)")
    ctl = js(page, """[...document.querySelectorAll('main button, main input')].filter(e=>e.offsetParent!==null)
        .map(e=>(e.tagName==='INPUT'?(e.getAttribute('placeholder')||''):e.textContent).trim().toLowerCase())
        .filter(t=>/(созда|нов|добав).{0,20}групп|групп.{0,20}(созда|нов)|название групп/.test(t))""")
    check(ctl == [], f"формы создания группы на «Структуре» нет ({ctl})")
    note("шаг 39: на «Структуре» группа только отображается, формы «Создать» нет; группа заводится через «＋ Новая группа» на «Плане v3» (шаг 40)")

    step(40, "«+ Зона» → список групп, «Нарисовать контур» (неактивна), «＋ Новая группа»")
    to_plan_v3(page)
    wait_map_v3_ready(page)
    page.locator("button.btn-sm", has_text="+ Зона").click()
    page.wait_for_selector("select[aria-label='Учётная группа для зоны']")
    gsel = page.locator("select[aria-label='Учётная группа для зоны']")
    gopts = js(page, "[...document.querySelectorAll(\"select[aria-label='Учётная группа для зоны'] option\")].map(o=>o.textContent.trim())")
    check("Зона-1" in gopts, f"в списке учётных групп есть «Зона-1» ({gopts})")
    draw_btn = page.locator("button:visible", has_text="Нарисовать контур")
    check(draw_btn.count() == 1 and draw_btn.first.is_disabled(), "«Нарисовать контур» неактивна, пока группа не выбрана")
    page.locator("button", has_text="＋ Новая группа").click()
    page.fill("input[placeholder='название группы']", "Зона освещения")
    page.locator("button:visible", has_text="Создать").first.click()
    check("Группа «Зона освещения» создана" in wait_toast(page, "создана"), f"уведомление ({toast_text(page)!r})")
    page.wait_for_function("()=>!!%s.planV3ZoneGroupId" % APP)
    grp_api = next((g for g in api(h, "get", "/api/v2/groups")[1] if g["name"] == "Зона освещения"), None)
    check(grp_api is not None and gsel.input_value() == str(grp_api["id"]),
          "группа заведена, не уходя с «Плана v3», и сразу выбрана в списке")
    S.zone_group = grp_api

    step(41, "«Нарисовать контур» → подсказка рисования; курсор — прицел")
    draw_btn = page.locator("button:visible", has_text="Нарисовать контур").first
    check(draw_btn.is_enabled(), "«Нарисовать контур» стала активной")
    draw_btn.click()
    page.wait_for_function("()=>%s.planV3DrawingZone===true" % APP)
    check("Рисуйте контур на карте, двойной клик — завершить (Esc — отмена)" in page.locator("main").inner_text(),
          "подсказка «Рисуйте контур на карте, двойной клик — завершить (Esc — отмена)»")
    cur = js(page, "getComputedStyle(document.querySelector('#site-plan-v3-map')).cursor")
    check(cur == "crosshair", f"курсор над картой — прицел ({cur})")

    step(42, "контур кликами и двойной клик → «Зона нанесена», закрашенная область, инструмент отключился")
    draw_zone(page)
    check("Зона нанесена" in wait_toast(page, "Зона нанесена"), f"уведомление ({toast_text(page)!r})")
    page.wait_for_function("()=>%s.planV3Current.items.some(i=>i.kind==='group')" % APP)
    zone = next(i for i in api(h, "get", f"/api/v2/plans/{S.plan1['id']}")[1]["items"] if i["kind"] == "group")
    check(zone["group_id"] == grp_api["id"] and len(zone["geometry"]) >= 4, f"в API зона группы «Зона освещения» ({len(zone['geometry'])} вершин)")
    fo = js(page, "(()=>{const a=%s; const it=a.planV3Current.items.find(i=>i.kind==='group'); const l=_planV3ItemLayers.get(String(it.id)); return l.options.fillOpacity;})()" % APP)
    check(fo and fo > 0, f"на карте полупрозрачная закрашенная область (fillOpacity={fo})")
    check(js(page, "[%s.planV3Tool, %s.planV3DrawingZone]" % (APP, APP)) == [None, False], "инструмент «Зона» отключился сам")

    step(43, "клик по области зоны → только уведомление «Учётная группа: …», карточка не открывается")
    cx = sum(p[0] for p in ZONE_POLY) / 4
    cy = sum(p[1] for p in ZONE_POLY) / 4
    # контур закрашен, но «пустое» внутри кликабельно — Leaflet отдаёт клик полигону с заливкой
    click_plan(page, cx, cy)
    check("Учётная группа: Зона освещения" in wait_toast(page, "Учётная группа: Зона освещения"), f"уведомление ({toast_text(page)!r})")
    page.wait_for_timeout(300)
    check(plan_state(page)["cardKind"] is None, "карточка при этом не открылась")

    step(44, "клик по имени зоны в списке «Зоны» → полная карточка группы")
    gid = grp_api["id"]
    # состав: чтобы карточка показала не пустой список, добавим точку в группу (как это делает «Структура»)
    from wb_energy_meter.group_repo_v2 import GroupRepoV2
    GroupRepoV2(h.db).add_member(gid, S.pt_sock["id"], valid_from=0)
    page.locator("[data-testid=planv3-panel] .sf-val", has_text="Зона освещения").first.click()
    wait_card_open(page)
    page.wait_for_function("(id)=>{const i=%s.structInspector; return !!i && i.kind==='group' && i.id===id && !!i.data;}" % APP, arg=gid)
    gtxt = page.locator("[data-testid=planv3-panel]").inner_text()
    check("зона освещения" in gtxt.lower(), "открыта карточка группы «Зона освещения»")
    check("состав (рекурсивно" in gtxt.lower(), "блок «Состав (рекурсивно…)»")
    page.wait_for_function("()=>/Счётчик-розетки/.test(document.querySelector('[data-testid=planv3-panel]').innerText)", timeout=8000)
    check("Счётчик-розетки" in page.locator("[data-testid=planv3-panel]").inner_text(), "в составе — точка учёта «Счётчик-розетки»")
    check("расход за период" in gtxt.lower(), "блок «Расход за период»")
    check("Небаланс у зоны не показывается: баланс считается по щитам — выберите узел." in gtxt,
          "примечание: небаланс у зоны не показывается, баланс считается по щитам")
    close_card(page)



# ================================================================== Раздел 8
def seed_day(h, day_str: str, values: dict) -> None:
    """Засеять ЦЕЛЫЕ сутки (24 часовых агрегата, как test_b03): весь расход — в первом часу,
    остальные 23 — нулевые, но существующие. Границу суток берём тем же разбором, что и сервер
    (periods.parse_user_datetime — по поясу процесса, поле `timezone` запроса на неё не влияет)."""
    from wb_energy_meter.periods import parse_user_datetime
    day_start = int(parse_user_datetime(day_str))
    for device_id, kwh in values.items():
        for k in range(24):
            ps = day_start + k * HOUR
            h.seed_energy(device_id, kwh if k == 0 else 0.0, period_start=ps, period_end=ps + HOUR)


def set_overview_period(page, d_from: str, d_to: str) -> None:
    nav(page, "Обзор", 500)
    page.fill("input[x-model='ovFrom']", d_from)
    page.fill("input[x-model='ovTo']", d_to)
    page.click("text=Обновить")
    page.wait_for_function("()=>{const s=%s.ovSummary; return !!s && s.object_total!=null;}" % APP, timeout=15000)
    page.wait_for_timeout(500)


def balance_api(h, node_id: int, d_from: str, d_to: str) -> dict:
    return api(h, "post", f"/api/v2/topology/nodes/{node_id}/balance",
               json={"from": d_from, "to": d_to, "timezone": "UTC"})[1]


def summary_api(h, d_from: str, d_to: str) -> dict:
    return api(h, "post", "/api/v2/overview/summary", json={"from": d_from, "to": d_to, "timezone": "UTC"})[1]


def section8() -> None:
    from datetime import date, timedelta
    h, page = S.h, S.page
    print("\n================ Раздел 8. Проверка чисел (небаланс объекта и баланс щита)")

    step(45, "схема Ввод → Щит → два потребителя со счётчиками + один без; на щите несколько измеряющих точек")
    nav(page, "План v3", 700)
    wait_map_v3_ready(page)
    open_node_card(page, h, "ЩР-1 (главный)")
    card = node_card(page)
    card.locator("input[placeholder='что питает линия']").fill("Без счётчика")
    card.get_by_role("button", name="Добавить", exact=True).click()
    wait_toast(page, "Отходящая линия добавлена")
    page.wait_for_function("(n)=>%s.structNodes.some(x=>x.name===n)" % APP, arg="Без счётчика")
    close_card(page)
    shr = node_by_name(h, "ЩР-1 (главный)")
    es = edges(h)
    in_e = next(e for e in es if e["to_node_id"] == shr["id"])
    out_es = [e for e in es if e["from_node_id"] == shr["id"]]
    names = {n["id"]: n["name"] for n in api(h, "get", "/api/v2/topology/nodes")[1]}
    measured = [e for e in out_es if e["primary_point_id"] is not None]
    check(in_e["primary_point_id"] is not None, "на входящей линии «Ввод → ЩР-1» есть счётчик")
    check(len(measured) == 2, f"два потребителя со счётчиками ({[names[e['to_node_id']] for e in measured]})")
    check(len(out_es) == 3 and [names[e["to_node_id"]] for e in out_es if e["primary_point_id"] is None] == ["Без счётчика"],
          "третий потребитель — «Без счётчика» — без счётчика")
    check(js(page, "%s.structNodeMeterCount(%d)" % (APP, shr["id"])) == 3, "на щите одновременно три измеряющие точки (входящая + две отходящие)")
    S.shr, S.in_edge = shr, in_e

    step(46, "показания приборов: на вводе больше суммы потребителей")
    tomorrow = date.today() + timedelta(days=1)
    d1, d1_to = tomorrow.strftime("%Y-%m-%d"), (tomorrow + timedelta(days=1)).strftime("%Y-%m-%d")
    d2, d2_to = (tomorrow + timedelta(days=1)).strftime("%Y-%m-%d"), (tomorrow + timedelta(days=2)).strftime("%Y-%m-%d")
    seed_day(h, d1, {"dev-in": 100.0, "dev-sock": 30.0, "dev-free-2": 20.0})
    seed_day(h, d2, {"dev-in": 40.0, "dev-sock": 30.0, "dev-free-2": 20.0})
    S.d1, S.d1_to, S.d2, S.d2_to = d1, d1_to, d2, d2_to
    check(100.0 > 30.0 + 20.0, "ввод (100 кВт·ч) заметно больше суммы потребителей (30 + 20)")

    step(47, "«Баланс щита за период»: значение со знаком, процент, неизмеренные ветви; период «как на Обзоре»")
    set_overview_period(page, d1, d1_to)
    nav(page, "План v3", 700)
    wait_map_v3_ready(page)
    open_node_card(page, h, "ЩР-1 (главный)")
    page.wait_for_function("()=>!!document.querySelector('[data-testid=node-balance-value]')", timeout=15000)
    api_bal = balance_api(h, shr["id"], d1, d1_to)
    val_txt = page.locator("[data-testid=node-balance-value]").inner_text().strip()
    exp_val = ("+" if api_bal["value"] > 0 else "") + f"{api_bal['value']:.1f} кВт·ч"
    check(val_txt == exp_val == "+50.0 кВт·ч", f"значение баланса щита со знаком и единицей ({val_txt!r}; API {api_bal['value']})")
    bal_block = page.locator("[data-testid=node-balance]").inner_text()
    pct = api_bal["explanation"]["percentage"]
    check(f"(+{pct:.2f}%)" in bal_block, f"процент в скобках ({pct:.2f}%)")
    check(f"{d1} — {d1_to}, как на «обзоре»" in bal_block.lower(), "рядом с заголовком тот же период, что на «Обзоре»")
    check("есть неизмеренные ветви: Без счётчика" in bal_block, "неизмеренная ветвь названа по имени")
    style = page.locator("[data-testid=node-balance-value]").get_attribute("style") or ""
    check("var(--ok)" in style, "положительное значение — «зелёный» класс цвета (--ok)")
    S.bal_text, S.bal_value = val_txt, api_bal["value"]
    close_card(page)

    step(48, "«Обзор»: итог объекта — по прибору на вводе, а не сумма всех приборов")
    set_overview_period(page, d1, d1_to)
    summ = summary_api(h, d1, d1_to)
    ot = summ["object_total"]["value"]
    check(ot == 100.0, f"итог объекта = показание ввода (100), не сумма приборов 150 ({ot})")
    check(f"{ot:.1f}" in page.locator("main").inner_text(), "тот же итог показан на экране «Обзор»")

    step(49, "небаланс объекта со знаком и процентом (плюс отрицательный случай на втором периоде)")
    imb_txt = page.locator("[data-testid=ov-imbalance]").inner_text().strip()
    check(imb_txt == "+50.0 кВт·ч", f"небаланс объекта со знаком ({imb_txt!r})")
    ov_text = page.locator("main").inner_text()
    check("(+50.00% от итога объекта)" in ov_text, "процент со знаком")
    S.imb_value = summ["imbalance_value"]
    set_overview_period(page, d2, d2_to)
    summ2 = summary_api(h, d2, d2_to)
    imb2 = page.locator("[data-testid=ov-imbalance]").inner_text().strip()
    check(summ2["imbalance_value"] == -10.0 and imb2.replace("−", "-") == "-10.0 кВт·ч", f"отрицательный небаланс — со знаком минус ({imb2!r})")
    check("(-25.00% от итога объекта)" in page.locator("main").inner_text().replace("−", "-"), "процент отрицательного случая со знаком")

    step(50, "один щит — граница между вводом и потребителями: баланс щита = небаланс объекта")
    check(S.bal_value == S.imb_value == 50.0, f"баланс щита ({S.bal_value}) = небаланс объекта ({S.imb_value}) — один расчёт overview_service")
    check(S.bal_text == "+50.0 кВт·ч" and imb_txt == "+50.0 кВт·ч", "и на экране числа совпадают")

    step(51, "[БЫЛО ИНАЧЕ] «Незавершённая конфигурация»: размещённые не «не на плане», изолированный — «не подключён»")
    set_overview_period(page, d1, d1_to)
    page.wait_for_function("()=>!!%s.validationResult" % APP, timeout=15000)
    val = api(h, "get", "/api/v2/validation")[1]
    no_plan = sorted(p["name"] for p in val["points_without_plan"])
    check("Счётчик-ввод" not in no_plan and "Счётчик-розетки" not in no_plan,
          f"измеряющие линии размещённых узлов точки не числятся «не на плане» ({no_plan})")
    iso = sorted(n["name"] for n in val["nodes_without_edges"])
    check("Склад" in iso, f"изолированный узел «Склад» — в списке («{', '.join(iso)}»)")
    main_txt = page.locator("main").inner_text()
    check("незавершённая конфигурация" in main_txt.lower(), "блок «Незавершённая конфигурация» на «Обзоре»")
    row = page.locator("main div[role=button]", has_text="Склад").first
    check("элемент не подключён к линиям" in row.inner_text().lower(), f"строка «Склад»: ({row.inner_text().strip()!r})")
    note("шаг 51: подпись для изолированного узла — «элемент не подключён к линиям» (в файле — «узел изолирован»)")


# ================================================================== Раздел 9
def struct_open_point(page, name: str) -> None:
    """Карточка точки на «Структуре»: строка в «Не на схеме» или «●»-строка счётчика в дереве схемы."""
    row = page.locator("[data-testid=off-scheme-point], [data-testid=scheme-meter]", has_text=name).first
    row.click()
    page.wait_for_function("(n)=>{const i=%s.structInspector; return !!i && i.kind==='point' && !!i.data && (i.data.name===n);}" % APP,
                           arg=name, timeout=10000)


def struct_card_text(page) -> str:
    return page.locator(".structure-inspector, [data-testid=struct-inspector], .plan-side:visible, main").first.inner_text()


def section9() -> None:
    h, page = S.h, S.page
    print("\n================ Раздел 9. Простой режим удалён (партия 8, Этап A)")
    pt_lone = S.pt_lone

    step(52, "[БЫЛО ИНАЧЕ] «Структура»: переключателя «Подробная схема» нет; формы узлов и линий доступны всегда")
    nav(page, "Структура", 900)
    main_txt = page.locator("main").inner_text()
    check("подробная схема" not in main_txt.lower() and "⚙" not in main_txt, "переключателя «⚙ Подробная схема» нет на экране")
    check(page.locator("[data-testid=struct-add-node]").is_visible(), "кнопка «+ Элемент схемы» видна всегда")
    page.locator("[data-testid=struct-add-node]").click()
    page.wait_for_selector("[data-testid=struct-new-node]")
    check(page.locator("[data-testid=struct-new-node] select[aria-label='Тип нового элемента']").is_visible(), "форма нового элемента открывается без каких-либо переключателей")
    page.locator("[data-testid=struct-new-node] button", has_text="Отмена").click()
    check("+ Линия (черновик)" in main_txt and "Публикация линий" in main_txt, "блок «Линии: черновики и публикация» показан всегда")
    note("шаг 52: блока «Завести в структуре» больше нет — кнопка «+ Элемент схемы» над деревом схемы и блок «Линии: черновики и публикация» (оба видны всегда)")

    step(53, "карточка точки без линии: «Описание», «Точка не назначена измерением…»; нет «Это ввод» / «Питается от»")
    struct_open_point(page, "Точка-без-линии")
    card = page.locator("main").inner_text()
    check("Точка не назначена измерением ни одной опубликованной связи." in card, "«Точка не назначена измерением ни одной опубликованной связи.»")
    check("описание" in card.lower() and page.locator("textarea[placeholder^='Например: розетки цеха 2']").count() == 1, "поле «Описание» (текстовое)")
    check("Это ввод" not in card and "Питается от:" not in card and page.locator("select[aria-label*='Питается']").count() == 0,
          "чекбокса «Это ввод» и списка «Питается от» нет")
    ta = page.locator("textarea[placeholder^='Например: розетки цеха 2']")
    ta.fill("Тестовое описание точки")
    ta.blur()
    for _ in range(40):
        d = api(h, "get", f"/api/v2/points/{pt_lone['id']}")[1]
        if (d or {}).get("description") == "Тестовое описание точки":
            break
        page.wait_for_timeout(150)
    check((d or {}).get("description") == "Тестовое описание точки", "описание сохраняется по уходу фокуса")

    step(54, "точка назначена на линию на «Плане v3» → блок «Измеряет связь / Питается от / Питает» показывается всегда")
    nav(page, "План v3", 800)
    wait_map_v3_ready(page)
    e_tb = edge_between(h, "Розетки", "Тест-ветка")
    p = line_xy(page, e_tb["id"])
    page.mouse.click(p["x"], p["y"])
    wait_card_open(page)
    page.wait_for_function("(id)=>{const i=%s.structInspector; return !!i && i.kind==='edge' && i.id===id;}" % APP, arg=e_tb["id"])
    sel = page.locator("[data-testid=planv3-panel] select[aria-label='Точка измерения линии']")
    sel.select_option(label="Точка-без-линии")
    wait_toast(page, "Счётчик назначен")
    page.wait_for_function("(id)=>%s.structEdges.find(e=>e.id===id).primary_point_id!=null" % APP, arg=e_tb["id"])
    close_card(page)
    nav(page, "Структура", 900)
    struct_open_point(page, "Точка-без-линии")
    card = page.locator("main").inner_text()
    check("Точка не назначена измерением" not in card, "текст «Точка не назначена измерением…» ушёл")
    check("Измеряет связь: " in card, "«Измеряет связь: …»")
    check("Питается от: Ввод" in card or "Питается от: ЩР-1 (главный)" in card, "«Питается от: …» — имя узла выше по дереву")
    check("Измеряет связь: Розетки → Тест-ветка" in card, "линия названа по узлам «Розетки → Тест-ветка»")
    check("Питает: — (конечных потребителей на схеме нет)" in card, "«Питает: — (конечных потребителей на схеме нет)» — ниже линии потребителей нет")
    check("pv3-e-" not in card, "служебные коды линий («pv3-e-…») на экран не попадают")
    note("шаг 54: «Измеряет связь», «Питается от» и «Питает» называют УЗЛЫ схемы («Розетки → Тест-ветка»), а не служебный код линии «pv3-e-…» — "
         "раньше у безымянной линии на экран попадал код (исправлено в 13.3)")

    step(55, "[БЫЛО ИНАЧЕ] таблицы «Чего не хватает» больше нет; «не стоит на линии» — в списке «Плана v3»")
    main_txt = page.locator("main").inner_text().lower()
    check("чего не хватает" not in main_txt and "не указано питание" not in main_txt,
          "столбца «Чего не хватает» и «не указано питание» нет")
    pt_free = h.make_point("pt-free", "Точка-запас")
    S.pt_free = pt_free
    reload_app(page, h.base_url)
    to_plan_v3(page)
    wait_map_v3_ready(page)
    ptxt = panel(page)
    m_free = re.search(r"Точка-запас[^\n]*", ptxt)
    m_lone = re.search(r"Точка-без-линии[^\n]*", ptxt)
    check(bool(m_free) and "не стоит на линии" in m_free.group(0), f"точка вне линий: «не стоит на линии» ({m_free and m_free.group(0)!r})")
    check(bool(m_lone) and "не стоит на линии" not in m_lone.group(0), f"точка на линии без такой пометки ({m_lone and m_lone.group(0)!r})")
    note("шаг 55: таблицы точек с «Чего не хватает» на «Структуре» больше нет; «не стоит на линии» остаётся в списке «Точки учёта» на «Плане v3»")

    step(56, "[БЫЛО ИНАЧЕ] «нет места» в наборе v2 не показывается (решение R11); место точки выводится из узла линии")
    val = api(h, "get", "/api/v2/validation")[1]
    no_loc = sorted(x["name"] for x in val["points_without_location"])
    check("Точка-без-линии" in no_loc, f"API: у точки на линии «Розетки → Тест-ветка» места ещё нет ({no_loc})")
    nav(page, "Обзор", 900)
    page.wait_for_function("()=>!!%s.validationResult" % APP, timeout=15000)
    ov = page.locator("main").inner_text().lower()
    check("нет места" not in ov, "«нет места» в «Незавершённая конфигурация» набора v2 не показывается")
    nav(page, "План v3", 800)
    wait_map_v3_ready(page)
    open_node_card(page, h, "Розетки")
    loc_in = node_card(page).locator("input[placeholder='например, ЩР-1, эт. 2']")
    loc_in.fill("эт. 1, щитовая")
    loc_in.locator("xpath=following-sibling::button").click()
    wait_toast(page, "Сохранено")
    for _ in range(40):
        val = api(h, "get", "/api/v2/validation")[1]
        if "Точка-без-линии" not in [x["name"] for x in val["points_without_location"]]:
            break
        page.wait_for_timeout(150)
    check("Точка-без-линии" not in [x["name"] for x in val["points_without_location"]],
          "после «Где стоит» у узла линии точка пропадает из points_without_location")
    close_card(page)
    note("шаг 56: «нет места» в списке «Незавершённая конфигурация» набора v2 не показывается (решение R11); место точки по-прежнему выводится из узла её линии (API /api/v2/validation)")

    step(57, "PATCH is_input → 400 field_removed «Питание задаётся линиями на «Плане v3»»")
    nav(page, "Структура", 800)
    before = api(h, "get", f"/api/v2/points/{pt_lone['id']}")[1]
    res = page.evaluate("""(id)=>fetch('/api/v2/points/'+id, {method:'PATCH', headers:{'Content-Type':'application/json'},
        body:'{"is_input":true}'}).then(async r=>({status:r.status, body:await r.json()}))""", pt_lone["id"])
    check(res["status"] == 400 and res["body"].get("code") == "field_removed", f"ответ 400 field_removed ({res['status']}, {res['body'].get('code')})")
    check("Питание задаётся линиями на «Плане v3»" in res["body"].get("message", ""), f"понятный текст ({res['body'].get('message')!r})")
    after = api(h, "get", f"/api/v2/points/{pt_lone['id']}")[1]
    check(after["name"] == before["name"] and after.get("description") == before.get("description"), "имя и остальные поля точки не изменились")


# ================================================================== Раздел 10
def section10() -> None:
    h, page = S.h, S.page
    print("\n================ Раздел 10. Ошибочные ситуации (должны быть отклонены с понятным текстом)")
    nav(page, "План v3", 700)
    wait_map_v3_ready(page)
    # узлы A–D заведены инструментом «+ Элемент схемы» (как шаги 14–15), по одному кликом по карте
    for (nm, kind, x, y) in (("A", "Щит", 250, 120), ("B", "Щит", 600, 120), ("C", "Щит", 950, 120), ("D", "Нагрузка", 1300, 120)):
        page.locator("button.btn-sm", has_text="+ Элемент схемы").first.click()
        page.wait_for_selector("[data-testid=planv3-node-form]")
        page.select_option("[data-testid=planv3-kind-select]", label=kind)
        page.fill("[data-testid=planv3-node-form] input[placeholder='Название']", nm)
        click_plan(page, x, y)
        page.wait_for_function("(n)=>%s.structNodes.some(q=>q.name===n)" % APP, arg=nm)
        wait_card_open(page)
        close_card(page)
    page.locator("button", has_text="Отменить инструмент").click()

    def draw_edge(a: str, b: str) -> None:
        page.locator("button.btn-sm", has_text="+ Линия").click()
        click_marker(page, a)
        page.wait_for_function("()=>!!%s.planV3EdgeFirstItem" % APP)
        click_marker(page, b)

    step(58, "обратная линия B → A отклонена с именами узлов")
    draw_edge("A", "B")
    for _ in range(40):
        if edge_between(h, "A", "B"):
            break
        page.wait_for_timeout(150)
    check(edge_between(h, "A", "B") is not None, "цепочка A → B построена")
    n_edges = len(edges(h))
    draw_edge("B", "A")
    page.wait_for_function("()=>[...document.querySelectorAll('.toast')].some(t=>/циклическое питание/.test(t.textContent))", timeout=8000)
    t58 = toast_text(page)
    check("циклическое питание" in t58 and "A" in t58 and "B" in t58, f"отказ с понятным текстом и именами узлов ({t58!r})")
    check(len(edges(h)) == n_edges and edge_between(h, "B", "A") is None, "новая линия не появилась")
    check(edge_between(h, "A", "B") is not None, "старая A → B осталась")

    step(59, "второй источник у узла отклонён с обоими именами")
    n_edges = len(edges(h))
    page.wait_for_function("()=>!document.querySelector('.toast')", timeout=12000)
    draw_edge("C", "B")
    page.wait_for_function("()=>[...document.querySelectorAll('.toast')].some(t=>/только один источник/.test(t.textContent))", timeout=8000)
    t59 = toast_text(page)
    check("«B» уже получает питание от" in t59 and "A" in t59 and "C" in t59, f"отказ называет узлы ({t59!r})")
    check(len(edges(h)) == n_edges and edge_between(h, "C", "B") is None, "линия C → B не создана")
    check(edge_between(h, "A", "B") is not None, "A → B на карте не изменилась")

    step(60, "легитимная линия C → D проводится")
    page.wait_for_function("()=>!document.querySelector('.toast')", timeout=12000)
    draw_edge("C", "D")
    for _ in range(40):
        if edge_between(h, "C", "D"):
            break
        page.wait_for_timeout(150)
    check(edge_between(h, "C", "D") is not None, "линия C → D проведена — отказ шага 59 инструмент не сломал")
    check("Линия проведена" in wait_toast(page, "Линия проведена"), f"уведомление ({toast_text(page)!r})")

    step(61, "Esc прерывает рисование контура; рисование заново работает")
    grp = S.zone_group
    page.locator("button.btn-sm", has_text="+ Зона").click()
    page.wait_for_selector("select[aria-label='Учётная группа для зоны']")
    page.select_option("select[aria-label='Учётная группа для зоны']", label="Зона-1")
    page.locator("button:visible", has_text="Нарисовать контур").first.click()
    page.wait_for_function("()=>%s.planV3DrawingZone===true" % APP)
    zone_items_before = len([i for i in api(h, "get", f"/api/v2/plans/{S.plan1['id']}")[1]["items"] if i["kind"] == "group"])
    for (x, y) in ((300, 400), (500, 400), (500, 600)):
        click_plan(page, x, y)
        page.wait_for_timeout(120)
    page.keyboard.press("Escape")
    page.wait_for_function("()=>%s.planV3DrawingZone===false" % APP, timeout=5000)
    check(js(page, APP + ".planV3DrawingZone") is False, "рисование прекратилось")
    check("Рисуйте контур на карте" not in page.locator("main").inner_text(), "подсказка «Рисуйте контур…» исчезла")
    check(js(page, "!!(_planV3Map.pm && _planV3Map.pm.globalDrawModeEnabled && _planV3Map.pm.globalDrawModeEnabled())") is False,
          "режим рисования Geoman выключен")
    cur = js(page, "getComputedStyle(document.querySelector('#site-plan-v3-map')).cursor")
    check(cur != "crosshair", f"курсор вернулся в обычный режим ({cur})")
    check(len([i for i in api(h, "get", f"/api/v2/plans/{S.plan1['id']}")[1]["items"] if i["kind"] == "group"]) == zone_items_before,
          "недорисованный контур не сохранился")
    check(js(page, APP + ".planV3Tool") is None, "инструмент «Зона» после Esc выключен")
    # заново — тот же сценарий, что в шаге 42: «+ Зона» → группа → «Нарисовать контур»
    page.locator("button.btn-sm", has_text="+ Зона").click()
    page.wait_for_selector("select[aria-label='Учётная группа для зоны']")
    page.locator("select[aria-label='Учётная группа для зоны']").select_option(label="Зона-1")
    page.locator("button:visible", has_text="Нарисовать контур").first.click()
    page.wait_for_function("()=>%s.planV3DrawingZone===true" % APP)
    draw_zone(page, [(300, 400), (600, 400), (600, 600), (300, 600)])
    check("Зона нанесена" in wait_toast(page, "Зона нанесена"), f"после отмены рисование заново работает ({toast_text(page)!r})")
    page.wait_for_function("(k)=>%s.planV3Current.items.filter(i=>i.kind==='group').length===k" % APP, arg=zone_items_before + 1)


SECTIONS = [section1, section2, section3, section4, section5, section6, section7, section8, section9, section10]


def main() -> int:
    until = 99
    if "--until" in sys.argv:
        until = int(sys.argv[sys.argv.index("--until") + 1])
    with Harness() as h:
        S.h = h
        S.bp = open_browser(h.base_url, timezone_id="UTC")
        S.page = S.bp.page
        S.page.on("console", lambda m: print(f"CONSOLE[{STEP[0]}] {m.type}: {m.text[:300]}")
                  if (m.type in ("error", "warning") and "Alpine" in m.text) else None)
        S.page.wait_for_timeout(1000)
        for i, sec in enumerate(SECTIONS, start=1):
            if i > until:
                break
            sec()
        errs = [e for e in S.bp.errors if "Failed to load resource" not in e]
        STEP[0] = 0
        check(not errs, f"консоль и страница без исключений ({errs[:3]})")
        S.bp.close()
    print("\nРАСХОЖДЕНИЯ «файл ↔ программа»:")
    for d in DISCREPANCIES:
        print("  -", d)
    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
