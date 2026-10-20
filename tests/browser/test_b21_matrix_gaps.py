"""Партия 13, 13.8 — пробелы матрицы приёмки A02, A35, A45 в настоящем Chromium.

  A02  Первый поиск «Структуры» после загрузки страницы показывает результаты; поиск
       находит точку по имени её учётной группы и по имени узла питания (бэкенд —
       tests/test_step32_structure_search.py, здесь — путь до экрана).
  A35  Два браузера правят один план. Конфликт 409 при сохранении «Плана v2»: черновик
       остаётся на экране, «Закрыть» его не трогает, «Загрузить чужую версию, мой черновик
       оставить» подгружает чужую ревизию и оставляет черновик поверх неё, повторное
       сохранение проходит и в плане есть обе правки; «Обновить план» черновик отбрасывает;
       чужая версия убрала элемент, к которому был черновик, — такая правка отбрасывается
       (и об этом сказано).
  A45  Подсказки Leaflet, подписи и списки не исполняют HTML из названий: точка, узел,
       линия и группа с именем `<img src=x onerror=alert(1)>"'=1+1` видны как ТЕКСТ.
       Перехвачены `dialog`, ошибки консоли и страницы; в DOM нет ни одного <img src="x">,
       `window.__xss` не появился. Отсутствие «Failed to load resource» тоже проверяется:
       попытка загрузить `x` значила бы, что тег попал в DOM.

Запуск: python tests/browser/test_b21_matrix_gaps.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from demo_seed import seed_demo  # noqa: E402

from wb_energy_meter.group_repo_v2 import GroupRepoV2  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
HOSTILE = '<img src=x onerror=alert(1)>"\'=1+1'
HOSTILE_B = '<svg onload="window.__xss=(window.__xss||0)+1"><b>жирный</b>&amp;'
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def js(page, code: str):
    return page.evaluate(code)


def vis(page, testid: str) -> bool:
    loc = page.locator(f"[data-testid={testid}]")
    return loc.count() > 0 and loc.first.is_visible()


def to_v2_set(page) -> None:
    js(page, APP + ".toggleUiSet()")
    page.wait_for_timeout(400)


def nav(page, title: str) -> None:
    page.locator("nav button:visible", has_text=title).click()
    page.wait_for_timeout(900)


# ---------------------------------------------------------------- A02
def scenario_search(h, info) -> None:
    print("\n== A02: первый поиск «Структуры» после загрузки страницы ==")
    bp = open_browser(h.base_url)
    page = bp.page
    page.wait_for_timeout(1000)
    to_v2_set(page)
    nav(page, "Структура")
    # самый первый ввод на этой странице: ничего до него не искали
    check(js(page, APP + "._structSearchResults") is None,
          "до первого поиска результатов нет (_structSearchResults объявлено и равно null)")
    page.fill("[data-testid=struct-search-input]", "освещ")
    page.wait_for_timeout(900)
    res = page.locator("[data-testid=struct-search-results]").inner_text()
    check(vis(page, "struct-search-results") and "Освещение" in res,
          f"первый же поиск «освещ» показал точку «Освещение» ({res[:60]!r})")
    check(not bp.errors, f"консоль без ошибок ({bp.errors})")

    page.fill("[data-testid=struct-search-input]", "цех")
    page.wait_for_timeout(900)
    res = page.locator("[data-testid=struct-search-results]").inner_text()
    check("Освещение" in res and "Розетки" in res and "Ввод" not in res.replace("Вводе", ""),
          "A02: поиск по имени учётной группы «цех» находит обе точки группы, а не «Ввод»")

    page.fill("[data-testid=struct-search-input]", "щр-1")
    page.wait_for_timeout(900)
    res = page.locator("[data-testid=struct-search-results]").inner_text()
    check("Освещение" in res and "Розетки" in res,
          "A02: поиск по имени узла питания «щр-1» находит точки, которые от него питаются")
    bp.close()


# ---------------------------------------------------------------- A35
def foreign_move(h, plan_id: int, item_id: int, x: int, y: int) -> dict:
    """Другой браузер: сохраняет раскладку с актуальной ревизией (как это делает второй редактор)."""
    rev = h.client.get(f"/api/v2/plans/{plan_id}").get_json()["canvas_revision"]
    r = h.client.post(f"/api/v2/plans/{plan_id}/layout", json={
        "expected_revision": rev,
        "item_ops": [{"op": "upsert", "id": item_id, "geometry": {"x": x, "y": y},
                      "coord_space": "canvas_xy_v2"}],
        "edge_view_ops": []})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def plan_geoms(h, plan_id: int) -> dict:
    d = h.client.get(f"/api/v2/plans/{plan_id}").get_json()
    return {i["id"]: i["geometry"] for i in d["items"]}


def stage_move(page, item_id: int, x: float, y: float) -> None:
    """Черновик «перетащили значок» — тем же путём, что и drag-end карты."""
    js(page, """(()=>{ const a=%s; const H=a._planV2EffectiveDims(a.planV2Current)[1];
        const it=a._planV2EffectiveItems().find(i=>i.id===%d);
        a._planV2OnItemDragEnd(it, {lat:H-%f, lng:%f}, H); })()""" % (APP, item_id, y, x))
    page.wait_for_timeout(150)


def scenario_conflict(h, info) -> None:
    print("\n== A35: конфликт сохранения «Плана v2», черновик остаётся ==")
    plan = info["plan"]
    pid = plan["id"]
    it = info["plan_items"]
    light, sock, stan = it["Освещение"]["id"], it["Розетки"]["id"], it["Станок"]["id"]
    bp = open_browser(h.base_url)
    page = bp.page
    page.wait_for_timeout(1000)
    js(page, "(async()=>{ const a=%s; a.tab='planv2'; await a.openPlanV2Tab(); await a.selectPlanV2(%d); })()"
       % (APP, pid))
    page.wait_for_timeout(900)
    check(js(page, APP + ".planV2Current && " + APP + ".planV2Current.id") == pid, "план v2 открыт")
    rev0 = js(page, APP + ".planV2Current.canvas_revision")

    # --- 1. «Освещение» двигаем в черновике; другой браузер двигает «Розетки» и сохраняет
    stage_move(page, light, 900, 150)
    check(js(page, APP + ".planV2PendingCount()") == 1, "в черновике одна правка («Освещение»)")
    foreign_move(h, pid, sock, 1400, 800)
    page.locator("[data-testid=planv2-save]").click()
    page.wait_for_timeout(600)
    check(vis(page, "planv2-conflict"), "сохранение со старой ревизией → окно «План изменён в другом месте» (409)")
    check(js(page, APP + ".planV2PendingCount()") == 1, "после 409 черновик остался (1 правка)")
    check(plan_geoms(h, pid)[light] == it["Освещение"]["geometry"],
          "на сервере «Освещение» не тронуто: устаревшая запись не применилась")

    # --- 2. «Закрыть» — черновик на месте, окно закрыто
    page.locator("[data-testid=planv2-conflict-close]").click()
    page.wait_for_timeout(300)
    check(not vis(page, "planv2-conflict") and js(page, APP + ".planV2PendingCount()") == 1,
          "«Закрыть (черновик останется)»: окно закрылось, черновик цел")
    check(vis(page, "planv2-save"), "кнопка «Сохранить» всё ещё на экране")

    # --- 3. Загрузить чужую версию, черновик оставить → повторное сохранение проходит
    page.locator("[data-testid=planv2-save]").click()
    page.wait_for_timeout(500)
    check(vis(page, "planv2-conflict"), "повторное сохранение на старой ревизии → снова 409")
    page.locator("[data-testid=planv2-conflict-keep]").click()
    page.wait_for_timeout(700)
    check(not vis(page, "planv2-conflict"), "«Загрузить чужую версию…»: окно закрылось")
    check(js(page, APP + ".planV2Current.canvas_revision") > rev0, "ревизия плана на экране обновилась до чужой")
    seen = js(page, "(()=>{ const a=%s; const g=id=>a._planV2EffectiveItems().find(i=>i.id===id).geometry;"
                    " return {sock:g(%d), light:g(%d)}; })()" % (APP, sock, light))
    check(round(seen["sock"]["x"]) == 1400 and round(seen["sock"]["y"]) == 800,
          f"чужая правка «Розеток» видна на экране ({seen['sock']})")
    check(round(seen["light"]["x"]) == 900 and round(seen["light"]["y"]) == 150,
          f"наш черновик «Освещения» остался поверх чужой версии ({seen['light']})")
    check(js(page, APP + ".planV2PendingCount()") == 1 and vis(page, "planv2-save"),
          "черновик (1 правка) доступен для сохранения")
    page.locator("[data-testid=planv2-save]").click()
    page.wait_for_timeout(900)
    check(not vis(page, "planv2-conflict"), "повторное сохранение прошло без конфликта")
    check(js(page, APP + ".planV2PendingCount()") == 0, "черновик очищен после сохранения")
    g = plan_geoms(h, pid)
    check(round(g[light]["x"]) == 900 and round(g[light]["y"]) == 150
          and round(g[sock]["x"]) == 1400 and round(g[sock]["y"]) == 800,
          f"на сервере обе правки: наша «Освещение» и чужая «Розетки» ({g[light]}, {g[sock]})")

    # --- 4. «Обновить план» (черновик теряется)
    stage_move(page, light, 300, 700)
    foreign_move(h, pid, sock, 1300, 700)
    page.locator("[data-testid=planv2-save]").click()
    page.wait_for_timeout(500)
    check(vis(page, "planv2-conflict"), "второй конфликт получен")
    page.locator("[data-testid=planv2-conflict-discard]").click()
    page.wait_for_timeout(800)
    check(not vis(page, "planv2-conflict") and js(page, APP + ".planV2PendingCount()") == 0,
          "«Обновить план»: окно закрыто, черновик отброшен")
    g = plan_geoms(h, pid)
    check(round(g[light]["x"]) == 900, "на сервере «Освещение» осталось как было: отброшенный черновик не записан")

    # --- 5. чужая версия убрала элемент, к которому у нас черновик
    stage_move(page, stan, 700, 900)
    stage_move(page, light, 500, 400)
    check(js(page, APP + ".planV2PendingCount()") == 2, "в черновике две правки («Станок», «Освещение»)")
    rev = h.client.get(f"/api/v2/plans/{pid}").get_json()["canvas_revision"]
    r = h.client.post(f"/api/v2/plans/{pid}/layout", json={
        "expected_revision": rev, "item_ops": [{"op": "remove", "id": stan}], "edge_view_ops": []})
    check(r.status_code == 200, f"другой браузер убрал «Станок» с плана ({r.status_code})")
    page.locator("[data-testid=planv2-save]").click()
    page.wait_for_timeout(500)
    check(vis(page, "planv2-conflict"), "конфликт получен")
    page.locator("[data-testid=planv2-conflict-keep]").click()
    page.wait_for_timeout(700)
    check(js(page, APP + ".planV2PendingCount()") == 1,
          "правка к убранному «Станку» отброшена, правка «Освещения» осталась")
    toast = js(page, "(()=>{ const t=document.querySelector('.toast'); return t?t.textContent:''; })()")
    check("отброшены" in toast, f"об отброшенной правке сказано словами ({toast.strip()[:110]!r})")
    page.locator("[data-testid=planv2-save]").click()
    page.wait_for_timeout(900)
    g = plan_geoms(h, pid)
    check(stan not in g and round(g[light]["x"]) == 500 and round(g[light]["y"]) == 400,
          "после повторного сохранения на сервере «Станка» нет, «Освещение» там, где поставили")
    own = [e for e in bp.errors if "status of 409" not in e]
    check(not own, f"ошибок страницы нет; в консоли только ожидаемые ответы 409 ({own})")
    bp.close()


# ---------------------------------------------------------------- A45
def seed_hostile(h, info) -> None:
    """Точка, узел, линия, нагрузка и группа с враждебными названиями — всё на плане."""
    evil = h.make_node("evil", HOSTILE, "panel")
    e_to_evil = h.connect(info["vvod"]["id"], evil["id"])
    rev = h.current_revision()
    r = h.client.patch(f"/api/v2/topology/edges/{e_to_evil['id']}", json={
        "name": HOSTILE_B, "expected_revision": rev})
    assert r.status_code == 200, r.get_json()
    cons = h.add_consumer(evil["id"], HOSTILE_B)
    h.see_in_mqtt("dev-evil", HOSTILE)
    pt = h.make_point("pt-evil", HOSTILE)
    h.bind_meter(pt["id"], "dev-evil", HOSTILE)
    h.set_meter_on_edge(cons["edge"]["id"], pt["id"])
    repo = GroupRepoV2(h.db)
    grp = repo.add(HOSTILE)
    repo.add_member(grp.id, pt["id"], valid_from=0)

    pid = info["plan"]["id"]
    items = {}
    for name, node, xy in (("evil", evil, (620, 800)), ("cons", cons["node"], (1000, 800))):
        r = h.client.post(f"/api/v2/plans/{pid}/items", json={
            "kind": "node", "node_id": node["id"], "geometry": {"x": xy[0], "y": xy[1]},
            "coord_space": "canvas_xy_v2"})
        assert r.status_code == 201, r.get_json()
        items[name] = r.get_json()
    r = h.client.post(f"/api/v2/plans/{pid}/items", json={
        "kind": "group", "group_id": grp.id,
        "geometry": [[100, 900], [400, 900], [400, 1100], [100, 1100]], "coord_space": "canvas_xy_v2"})
    assert r.status_code == 201, r.get_json()
    vvod_item = info["plan_items"]["Ввод"]["id"]
    for edge, a, b in ((e_to_evil, vvod_item, items["evil"]["id"]),
                       (cons["edge"], items["evil"]["id"], items["cons"]["id"])):
        r = h.client.post(f"/api/v2/plans/{pid}/edges", json={
            "edge_id": edge["id"], "from_item_id": a, "to_item_id": b, "view_kind": "structural"})
        assert r.status_code == 201, r.get_json()


def inert(page, dialogs, label: str) -> None:
    """Ничего не исполнилось и не попало в DOM как элемент."""
    bad = js(page, "({xss: window.__xss===undefined ? 0 : window.__xss,"
                   " imgs: document.querySelectorAll('img[src=\"x\"]').length,"
                   " svgs: document.querySelectorAll('svg[onload]').length})")
    check(not dialogs and bad == {"xss": 0, "imgs": 0, "svgs": 0},
          f"{label}: ни dialog, ни window.__xss, ни <img src=x>/<svg onload> в DOM ({dialogs}, {bad})")


def scenario_xss(h, info) -> None:
    print("\n== A45: HTML в названиях виден как текст, ничего не исполняется ==")
    seed_hostile(h, info)
    bp = open_browser(h.base_url)
    page = bp.page
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.wait_for_timeout(1200)
    to_v2_set(page)

    # --- Обзор
    nav(page, "Обзор")
    body = page.locator("body").inner_text()
    check("<img src=x onerror=alert(1)>" in body, "Обзор: название показано буквами, тег виден как текст")
    inert(page, dialogs, "Обзор")

    # --- Структура: поиск, список, карточка точки
    nav(page, "Структура")
    page.fill("[data-testid=struct-search-input]", "img src")
    page.wait_for_timeout(900)
    res = page.locator("[data-testid=struct-search-results]").inner_text()
    check(HOSTILE in res, f"Структура: поиск находит точку и показывает её название текстом ({res[:80]!r})")
    inert(page, dialogs, "Структура (поиск)")
    page.locator("[data-testid=struct-search-results] [data-kb-row]").first.click()
    page.wait_for_timeout(900)
    card = page.locator("[data-testid=struct-right]").inner_text()
    check(HOSTILE in card or "<img src=x" in card, "Структура: карточка показывает название текстом")
    inert(page, dialogs, "Структура (карточка)")
    page.fill("[data-testid=struct-search-input]", "")

    # --- План v3: подписи узлов (постоянные подсказки), подсказки линий, зона, карточка
    nav(page, "План v3")
    page.wait_for_timeout(900)
    tips = js(page, """(()=>{ const out=[];
        document.querySelectorAll('.leaflet-tooltip').forEach(t=>out.push({text:t.textContent, html:t.innerHTML,
            kids:t.querySelectorAll('img,svg,b').length}));
        return out; })()""")
    node_tip = [t for t in tips if "<img src=x" in t["text"]]
    check(bool(node_tip) and "&lt;img" in node_tip[0]["html"] and node_tip[0]["kids"] == 0,
          f"План v3: подпись узла — текст, а не разметка ({node_tip[:1]})")
    hover = js(page, """(()=>{ const out=[];
        document.querySelectorAll('.leaflet-overlay-pane path.leaflet-interactive').forEach(p=>{
          p.dispatchEvent(new MouseEvent('mouseover',{bubbles:true,clientX:300,clientY:300}));
          document.querySelectorAll('.leaflet-tooltip').forEach(t=>{
            if(!t.classList.contains('planv3-node-tip')) out.push({text:t.textContent, html:t.innerHTML,
              kids:t.querySelectorAll('img,svg,b').length}); });
          p.dispatchEvent(new MouseEvent('mouseout',{bubbles:true}));
        });
        return out; })()""")
    line_tip = [t for t in hover if "<svg onload" in t["text"]]
    check(bool(line_tip) and line_tip[0]["kids"] == 0 and "&lt;svg" in line_tip[0]["html"],
          f"План v3: подсказка линии — текст, а не разметка ({line_tip[:1]})")
    inert(page, dialogs, "План v3 (подсказки)")
    # зона-группа: клик по контуру показывает название в уведомлении (x-text)
    js(page, """(()=>{ const p=[...document.querySelectorAll('.leaflet-overlay-pane path.leaflet-interactive')]
        .find(x=>x.getAttribute('fill-opacity')==='0.18'); if(p) p.dispatchEvent(new MouseEvent('click',{bubbles:true})); })()""")
    page.wait_for_timeout(400)
    toast = js(page, "(()=>{ const t=document.querySelector('.toast'); return t?t.textContent:''; })()")
    check("<img src=x" in toast or toast == "", f"План v3: название группы в уведомлении — текст ({toast.strip()[:60]!r})")
    inert(page, dialogs, "План v3 (зона)")

    # --- Отчёты: подписи срезов
    nav(page, "Отчёты")
    for dim, what in (("point", "точки"), ("group", "группы")):
        js(page, APP + ".rv2Dimension=" + repr(dim))
        page.wait_for_timeout(300)
        opts = js(page, "[...document.querySelectorAll('select[multiple] option')].map(o=>o.textContent)")
        check(any(HOSTILE == o for o in opts), f"Отчёты: название {what} в списке «Состав» — текст ({opts[:3]})")
        inert(page, dialogs, "Отчёты (" + dim + ")")

    errs = bp.errors
    check(not errs, f"консоль и страница без ошибок и без «Failed to load resource» ({errs})")
    bp.close()


def main() -> int:
    with Harness() as h:
        info = seed_demo(h)
        scenario_search(h, info)
        scenario_conflict(h, info)
        scenario_xss(h, info)
    print("\nпровалов: %d" % len(fails))
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
