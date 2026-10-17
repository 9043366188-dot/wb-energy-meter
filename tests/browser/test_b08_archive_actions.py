"""Партия 13 (находка пилота П1): архивировать и убирать можно из интерфейса —
настоящий Chromium, настоящее Flask-приложение.

Схема стенда: Ввод → ЩР-1 (счётчик dev-1, линия e1) → Станок (счётчик dev-2, линия e2);
Ввод → ЩР-2 (счётчик dev-3, линия e3). Все элементы размещены на плане «Смоук».
За контрольные сутки dev-1 дал 100 кВт·ч, dev-3 — 20: итог объекта = 120.

Сценарий (каждый шаг — действие в интерфейсе, результат сверен с API):
  1. «Убрать с плана» (с подтверждением): значок ушёл с плана, элемент остался в схеме и
     в списке «Не размещены на плане».
  2. «Убрать счётчик» в карточке щита: точка архивирована, линия осталась без счётчика,
     прибор освободился (стал выбираемым в «+ Счётчик»).
  3. «Архивировать точку» на «Структуре»: в окне подтверждения слово в слово
     «История сохраняется; из текущего состава, итогов и балансов точка уходит с этого
     момента»; после — точки нет в «Структуре», в «Обзоре» (итог объекта перестал быть 100),
     она есть под «Показать архивные», а в её карточке «История изменений» записала архивацию.
  4. Читаемый отказ 409: элемент с линиями, о которых интерфейс ещё не знал, — окно не
     закрывается, показан человеческий текст (без служебных слов) и предложено
     «Вывести линии из работы и архивировать».
  5. «Архивировать узел» у элемента с линиями: окно перечисляет линии; одним действием
     элемент архивирован, его линии выведены из работы (решение «вместе» — см. ТЗ П1).
  6. «Вывести из работы» у линии (подтверждение): линия ушла из действующих.
  7. «Удалить план» (подтверждение): план удалён, схема и точки на месте.

Запуск: python tests/browser/test_b08_archive_actions.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser, HOUR  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []
FORBIDDEN = ("scope", "channel_profile", "valid_from", "meter_source_id", "retire_edges",
             "expected_revision", "cross_point", "HTTP 4")


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def api(page, path):
    return page.evaluate(f"fetch({path!r}).then(r=>r.json())")


def card(page):
    return page.locator("[data-testid=node-card]:visible")


def modal(page):
    return page.locator("[data-testid=ui-confirm]")


def open_node(page, node_id):
    page.evaluate(APP + f".openInspectorInPlace('node', {node_id})")
    page.wait_for_timeout(900)


def place(h, plan_id, node, x, y):
    r = h.client.post(f"/api/v2/plans/{plan_id}/items", json={
        "kind": "node", "node_id": node["id"], "geometry": {"x": x, "y": y},
        "coord_space": "canvas_xy_v2", "label": node["name"]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def view_edge(h, plan_id, edge, from_item, to_item):
    r = h.client.post(f"/api/v2/plans/{plan_id}/edges", json={
        "edge_id": edge["id"], "from_item_id": from_item["id"],
        "to_item_id": to_item["id"], "view_kind": "structural"})
    assert r.status_code == 201, r.get_json()


def main() -> int:
    with Harness() as h:
        h.see_in_mqtt("dev-1", "Счётчик 1")
        h.see_in_mqtt("dev-2", "Счётчик 2")
        h.see_in_mqtt("dev-3", "Счётчик 3")
        vvod = h.make_node("vvod", "Ввод", "source")
        shr1 = h.make_node("shr1", "ЩР-1", "panel")
        shr2 = h.make_node("shr2", "ЩР-2", "panel")
        e1 = h.connect(vvod["id"], shr1["id"])
        e3 = h.connect(vvod["id"], shr2["id"])
        c = h.add_consumer(shr1["id"], "Станок")
        stanok, e2 = c["node"], c["edge"]
        p1 = h.make_point("p-1", "Точка щита")
        h.bind_meter(p1["id"], "dev-1", "Счётчик 1")
        h.set_meter_on_edge(e1["id"], p1["id"])
        p2 = h.make_point("p-2", "Точка станка")
        h.bind_meter(p2["id"], "dev-2", "Счётчик 2")
        h.set_meter_on_edge(e2["id"], p2["id"])
        p3 = h.make_point("p-3", "Точка ЩР-2")
        h.bind_meter(p3["id"], "dev-3", "Счётчик 3")
        h.set_meter_on_edge(e3["id"], p3["id"])

        plan = h.create_plan("Смоук")
        it = {n["name"]: place(h, plan["id"], n, 300 + 250 * i, 300 + 200 * (i % 2))
              for i, n in enumerate([vvod, shr1, shr2, stanok])}
        view_edge(h, plan["id"], e1, it["Ввод"], it["ЩР-1"])
        view_edge(h, plan["id"], e3, it["Ввод"], it["ЩР-2"])
        view_edge(h, plan["id"], e2, it["ЩР-1"], it["Станок"])

        day = (datetime.now().date() + timedelta(days=1))
        ov_from, ov_to = day.strftime("%Y-%m-%d"), (day + timedelta(days=1)).strftime("%Y-%m-%d")
        day_start = int(datetime.strptime(ov_from, "%Y-%m-%d").timestamp())
        for i in range(24):
            ps = day_start + i * HOUR
            h.seed_energy("dev-1", 100.0 if i == 0 else 0.0, period_start=ps, period_end=ps + HOUR)
            h.seed_energy("dev-2", 30.0 if i == 0 else 0.0, period_start=ps, period_end=ps + HOUR)
            h.seed_energy("dev-3", 20.0 if i == 0 else 0.0, period_start=ps, period_end=ps + HOUR)

        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1000)
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(300)
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(1200)

        def total_api():
            return page.evaluate(
                "fetch('/api/v2/overview/summary', {method:'POST', headers:{'Content-Type':'application/json'}, "
                f"body: JSON.stringify({{from:'{ov_from}', to:'{ov_to}', timezone:'UTC'}})}}).then(r=>r.json())"
            ).get("object_total", {}).get("value")

        check(total_api() is not None and abs(total_api() - 120.0) < 1e-6,
              f"старт: итог объекта по API = 120 ({total_api()})")

        # ===== 1. «Убрать с плана» =====
        open_node(page, shr1["id"])
        check(card(page).locator("button", has_text="Убрать с плана").count() == 1
              and card(page).locator("button", has_text="Архивировать узел").count() == 1,
              "в карточке щита есть «Убрать с плана» и «Архивировать узел»")
        card(page).locator("button", has_text="Убрать с плана").click()
        page.wait_for_timeout(300)
        check(modal(page).count() == 1 and "Убрать «ЩР-1» с плана?" in modal(page).inner_text(),
              "«Убрать с плана» просит подтверждение")
        modal(page).locator("button", has_text="Отмена").click()
        page.wait_for_timeout(200)
        check(modal(page).count() == 0 and len(api(page, f"/api/v2/plans/{plan['id']}")["items"]) == 4,
              "«Отмена» ничего не меняет: на плане по-прежнему 4 значка")
        card(page).locator("button", has_text="Убрать с плана").click()
        page.wait_for_timeout(300)
        page.click("[data-testid=ui-confirm-ok]")
        page.wait_for_timeout(1500)
        plan_now = api(page, f"/api/v2/plans/{plan['id']}")
        placed = {i["node_id"] for i in plan_now["items"] if i["kind"] == "node"}
        nodes = {n["name"]: n for n in api(page, "/api/v2/topology/nodes")}
        check(shr1["id"] not in placed and len(placed) == 3,
              f"значок ЩР-1 убран с плана, остальные на месте (на плане {len(placed)})")
        check("ЩР-1" in nodes and nodes["ЩР-1"].get("archived_at") in (None, 0),
              "элемент «ЩР-1» остался в схеме, не архивирован")
        page.evaluate(APP + ".closeInspector()")
        page.wait_for_timeout(300)
        check(page.locator("[data-testid=planv3-unplaced]:visible", has_text="ЩР-1").count() == 1,
              "«ЩР-1» появился в списке «Не размещены на плане»")
        bp.screenshot("b08_removed_from_plan")

        # ===== 2. «Убрать счётчик» =====
        open_node(page, shr1["id"])
        rows = page.locator("[data-testid=node-card]:visible [data-testid=node-meter]")
        check(rows.count() == 2, f"в карточке ЩР-1 два счётчика ({rows.count()})")
        rows.filter(has_text="Точка станка").locator("button", has_text="Убрать счётчик").click()
        page.wait_for_timeout(300)
        check("Убрать счётчик «Точка станка»?" in modal(page).inner_text(),
              "«Убрать счётчик» просит подтверждение")
        page.click("[data-testid=ui-confirm-ok]")
        page.wait_for_timeout(1500)
        pts = {p["name"] for p in api(page, "/api/v2/points")}
        edge2 = api(page, f"/api/v2/topology/edges/{e2['id']}")
        check("Точка станка" not in pts and "Точка щита" in pts,
              f"точка станка архивирована, точка щита на месте ({sorted(pts)})")
        check(edge2.get("primary_point_id") is None and edge2.get("state") == "published",
              "линия ЩР-1 → Станок осталась в схеме, без счётчика")
        occupied = api(page, "/api/v2/structure/points")["occupied_devices"]
        check(sorted(o["device_id"] for o in occupied) == ["dev-1", "dev-3"],
              f"dev-2 освободился, dev-1 и dev-3 заняты ({sorted(o['device_id'] for o in occupied)})")
        check(card(page).locator("[data-testid=node-meter]").count() == 1,
              "в карточке остался один счётчик")

        # ===== 3. «Архивировать точку» на «Структуре» =====
        page.locator("nav button:visible", has_text="Структура").click()
        page.wait_for_timeout(1200)
        check(page.locator("[data-testid=scheme-meter]", has_text="Точка щита").count() == 1,
              "«Структура»: счётчик «Точка щита» виден в дереве схемы")
        page.locator("[data-testid=scheme-meter]", has_text="Точка щита").click()
        page.wait_for_selector("[data-testid=point-card-title]", timeout=8000)
        page.locator("[data-testid=point-actions] button", has_text="Архивировать точку").click()
        page.wait_for_timeout(300)
        text = " ".join(modal(page).inner_text().split())
        check("История сохраняется; из текущего состава, итогов и балансов точка уходит с этого момента" in text,
              f"окно подтверждения: нужные слова про историю и «с этого момента» ({text!r})")
        page.click("[data-testid=ui-confirm-ok]")
        page.wait_for_timeout(1500)
        pts_active = {p["name"] for p in api(page, "/api/v2/points")}
        pts_all = {p["name"]: p for p in api(page, "/api/v2/points?include_archived=1")}
        check("Точка щита" not in pts_active
              and pts_all.get("Точка щита", {}).get("archived_at"),
              "API: точка щита архивирована (есть в include_archived, нет в действующих)")
        check(page.locator("[data-testid=scheme-meter]", has_text="Точка щита").count() == 0
              and page.locator("[data-testid=scheme-meter]", has_text="Точка ЩР-2").count() == 1
              and page.locator("[data-testid=off-scheme-point]").count() == 0,
              "«Структура»: архивной точки нет в дереве и в «Не на схеме», действующая на месте")
        check(page.locator("[data-testid=point-card-title]").count() == 0, "карточка архивированной точки закрылась")
        tot = total_api()
        check(tot is None or abs(tot - 120.0) > 1e-6,
              f"API: итог объекта больше не включает архивную точку ({tot})")
        page.locator("nav button:visible", has_text="Обзор").click()
        page.wait_for_timeout(1200)
        page.fill("input[x-model='ovFrom']", ov_from)
        page.fill("input[x-model='ovTo']", ov_to)
        page.click("text=Обновить")
        page.wait_for_timeout(1200)
        ov_text = page.locator("body").inner_text()
        check("120.0" not in ov_text and "Точка щита" not in ov_text and "Точка ЩР-2" in ov_text,
              "«Обзор»: нет ни архивной точки, ни прежнего итога 120.0; действующая «Точка ЩР-2» на месте")

        page.locator("nav button:visible", has_text="Структура").click()
        page.wait_for_timeout(800)
        page.locator("[data-testid=struct-show-archived]").check()
        page.wait_for_timeout(800)
        arch = page.locator("[data-testid=archived-point]")
        check(arch.count() == 2 and "Точка щита — в архиве" in " ".join(arch.all_inner_texts()),
              f"«Показать архивные»: обе архивные точки в списке ({arch.all_inner_texts()})")
        arch.filter(has_text="Точка щита").click()
        page.wait_for_selector("[data-testid=point-archived-note]", timeout=8000)
        check("История сохраняется" in page.locator("[data-testid=point-archived-note]").inner_text(),
              "карточка архивной точки: пометка «в архиве… История сохраняется»")
        page.locator("#struct-history-toggle").click()
        page.wait_for_selector(".struct-history-item", timeout=8000)
        titles = page.locator(".struct-history-item").all_inner_texts()
        check(any("точка #%d — архивировано" % p1["id"] in t for t in titles),
              f"«История изменений» записала архивацию точки ({[t.splitlines()[0] for t in titles][:4]})")
        page.locator("[data-testid=struct-show-archived]").uncheck()
        bp.screenshot("b08_after_archive_point")

        # ===== 4. читаемый отказ 409 =====
        jun = h.make_node("jun", "Ответвление-1", "junction")
        page.evaluate(APP + "._loadStructureLists()")
        page.wait_for_timeout(500)
        h.connect(shr2["id"], jun["id"])  # интерфейс про эту линию пока не знает
        open_node(page, jun["id"])
        # имитируем устаревший список линий: окно подтверждения строится без них
        page.evaluate(
            "(()=>{const a=" + APP + f"; a.structEdges=a.structEdges.filter(e=>e.to_node_id!=={jun['id']});}})()")
        card(page).locator("button", has_text="Архивировать узел").click()
        page.wait_for_timeout(300)
        check("Элемент уйдёт из схемы" in modal(page).inner_text(),
              "окно без известных линий предлагает обычную архивацию")
        page.click("[data-testid=ui-confirm-ok]")
        page.wait_for_timeout(1200)
        err = modal(page).locator("[data-testid=ui-confirm-error]")
        err_text = err.inner_text() if err.count() else ""
        check(modal(page).count() == 1 and err_text != "",
              f"отказ сервера: окно не закрылось, текст ошибки на месте ({err_text!r})")
        check(not any(w in err_text for w in FORBIDDEN),
              "текст отказа человеческий, без служебных слов")
        extra = modal(page).locator("button", has_text="Вывести линии из работы и архивировать")
        check(extra.count() == 1, "предложено «Вывести линии из работы и архивировать»")
        extra.click()
        page.wait_for_timeout(1500)
        nodes = {n["name"]: n for n in api(page, "/api/v2/topology/nodes")}
        check(modal(page).count() == 0 and "Ответвление-1" not in nodes,
              "после «Вывести линии… и архивировать» элемент архивирован, окно закрыто")

        # ===== 5. «Архивировать узел» у элемента с линиями =====
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(800)
        open_node(page, shr1["id"])
        card(page).locator("button", has_text="Архивировать узел").click()
        page.wait_for_timeout(300)
        lines = modal(page).locator("li").all_inner_texts()
        check(len(lines) == 2 and any("Станок" in ln for ln in lines),
              f"окно перечисляет линии ЩР-1 ({lines})")
        check(modal(page).locator("[data-testid=ui-confirm-ok]").inner_text().strip()
              == "Вывести линии из работы и архивировать",
              "кнопка подтверждения честно называет, что будет: «Вывести линии из работы и архивировать»")
        page.click("[data-testid=ui-confirm-ok]")
        page.wait_for_timeout(1800)
        nodes = {n["name"]: n for n in api(page, "/api/v2/topology/nodes")}
        published = api(page, "/api/v2/topology/edges?state=published")
        left = {(e["from_node_id"], e["to_node_id"]) for e in published}
        check("ЩР-1" not in nodes, "«ЩР-1» архивирован (нет среди действующих элементов)")
        check((vvod["id"], shr1["id"]) not in left and (shr1["id"], stanok["id"]) not in left,
              f"обе линии ЩР-1 выведены из работы одновременно с ним (действующие линии: {len(published)})")
        check((vvod["id"], shr2["id"]) in left, "линия Ввод → ЩР-2 не затронута")
        check(modal(page).count() == 0, "окно подтверждения закрыто")

        # ===== 6. «Вывести из работы» у линии =====
        page.locator("nav button:visible", has_text="Структура").click()
        page.wait_for_timeout(800)
        page.evaluate(APP + f".openInspector('edge', {e3['id']})")
        page.wait_for_timeout(800)
        page.locator("[data-testid=edge-actions]:visible button", has_text="Вывести из работы").click()
        page.wait_for_timeout(300)
        check("С этого момента" in modal(page).inner_text() or "линию" in modal(page).inner_text().lower(),
              "«Вывести из работы» просит подтверждение")
        page.click("[data-testid=ui-confirm-ok]")
        page.wait_for_timeout(1500)
        published = api(page, "/api/v2/topology/edges?state=published")
        check(all(e["id"] != e3["id"] for e in published),
              f"линия Ввод → ЩР-2 выведена из работы (действующих линий: {len(published)})")

        # ===== 7. «Удалить план» =====
        n_nodes = len(api(page, "/api/v2/topology/nodes"))
        n_points = len(api(page, "/api/v2/points?include_archived=1"))
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(800)
        page.locator("[data-testid=plan-delete]").click()
        page.wait_for_timeout(300)
        check("Смоук" in modal(page).inner_text() and "схема" in modal(page).inner_text().lower(),
              "«Удалить план» просит подтверждение и говорит, что схема останется")
        page.click("[data-testid=ui-confirm-ok]")
        page.wait_for_timeout(1500)
        plans = api(page, "/api/v2/plans")
        check(plans == [] or plans.get("plans") == [] if isinstance(plans, dict) else plans == [],
              f"план удалён ({plans})")
        check(len(api(page, "/api/v2/topology/nodes")) == n_nodes
              and len(api(page, "/api/v2/points?include_archived=1")) == n_points,
              "схема и точки на месте после удаления плана")

        errs = [e for e in bp.errors if "status of 409" not in e]
        check(not errs, f"консоль без ошибок ({len(errs)}): " + "; ".join(sorted(set(errs))[:8]))
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
