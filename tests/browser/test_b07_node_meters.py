"""Партия 13 (находки пилота П4, П5): «+ Счётчик» в карточке узла — настоящий
Chromium, настоящее Flask-приложение.

Сценарий Кира («создал ввод и первый щит — добавил в щит живой счётчик»):

  1. «План v3»: «+ Элемент схемы» → Ввод «Ввод объекта» (клик по карте) → Щит «Щит 1».
  2. Карточка щита → «+ Счётчик» → прибор wb-map3e_16 → «Ввод щита» → «Откуда
     питается?» = «Ввод объекта» → одним действием появились линия, точка, привязка,
     «измеряет линию». Карточка показывает «wb-map3e_16 — ввод щита», значок щита
     на плане — «счётчик», линия нарисована между значками.
  3. «+ Счётчик» → прибор wb-map12e_3 → «Отходящую линию» → «+ новая нагрузка…»
     «Станок»: нагрузка создана и поставлена на план, линия нарисована, счётчик
     показан как «линия к «Станок»».
  4. П4: в списке приборов оба занятых прибора серые («занят точкой «…»»), выбрать
     их нельзя; свободных нет.
  5. Баланс щита в карточке и «Обзор» видят точки: числа на экране = ответам API
     (баланс щита = 100 − 30 = 70, итог объекта = 100).
  6. Отказ при занятой линии: человеческий текст без служебных слов, черновик
     формы не потерян.

Запуск: python tests/browser/test_b07_node_meters.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser, HOUR  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def api(page, path, method="GET", body=None):
    if body is None:
        return page.evaluate(f"fetch({path!r}).then(r=>r.json())")
    import json
    return page.evaluate(
        f"fetch({path!r}, {{method:{method!r}, headers:{{'Content-Type':'application/json'}}, "
        f"body: JSON.stringify({json.dumps(body)})}}).then(async r=>({{status:r.status, body:await r.json()}}))")


def place_on_map(page, kind_value, name, fx, fy):
    box = page.locator(".leaflet-container:visible").first.bounding_box()
    page.click("button:has-text('+ Элемент схемы')")
    page.wait_for_timeout(200)
    page.select_option("[data-testid=planv3-kind-select]", kind_value)
    page.fill("input[placeholder='Название']", name)
    page.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
    page.wait_for_timeout(1200)


def card(page):
    return page.locator("[data-testid=node-card]:visible")


def main() -> int:
    with Harness() as h:
        h.see_in_mqtt("wb-map3e_16", "Тестовый счётчик 1")
        h.see_in_mqtt("wb-map12e_3", "Счётчик 12")
        plan = h.create_plan("Смоук")

        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1000)
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(300)
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(1000)

        # ===== 1. ввод и щит кликом по карте =====
        place_on_map(page, "source", "Ввод объекта", 0.3, 0.4)
        place_on_map(page, "panel", "Щит 1", 0.3, 0.65)
        nodes = api(page, "/api/v2/topology/nodes")
        kinds = {n["name"]: n["kind"] for n in nodes}
        check(kinds == {"Ввод объекта": "source", "Щит 1": "panel"},
              f"создан ввод и щит: {kinds}")
        check(card(page).count() == 1 and
              page.evaluate("(()=>{const a=" + APP + "; return a.structInspector && a.structInspector.data.name;})()") == "Щит 1",
              "после размещения открыта карточка щита")
        check(page.locator("[data-testid=node-card-kind]:visible").inner_text().strip() == "Щит",
              "в карточке тип крупно: «Щит»")
        check(page.locator("[data-testid=node-meter-note]:visible").inner_text().startswith(
            "Счётчик ставится на линию"), "в карточке сказано, что счётчик ставится на линию")

        # ===== 2. «+ Счётчик» → ввод щита (линии нет — «Откуда питается?») =====
        n_points0 = len(api(page, "/api/v2/points"))
        card(page).locator("button", has_text="+ Счётчик").click()
        page.wait_for_timeout(300)
        form = page.locator("[data-testid=node-meter-form]:visible")
        opts = page.locator("[data-testid=node-meter-device]:visible option").evaluate_all(
            "els => els.map(e => ({t: e.textContent.trim(), v: e.value, d: e.disabled}))")
        check([o["v"] for o in opts if o["v"]] == ["wb-map12e_3", "wb-map3e_16"] and not any(o["d"] for o in opts),
              f"оба прибора свободны и выбираемы: {opts}")
        page.select_option("[data-testid=node-meter-device]:visible", "wb-map3e_16")
        check(page.input_value("[data-testid=node-meter-name]:visible") == "wb-map3e_16",
              "название счётчика по умолчанию — имя прибора")
        check(form.locator("[data-testid=node-meter-fed-from]").count() == 1,
              "у щита нет входящей линии → спрашивает «Откуда питается?»")
        check(page.locator("[data-testid=node-meter-submit]:visible").is_disabled(),
              "без «Откуда питается?» кнопка «Поставить счётчик» недоступна")
        page.select_option("[data-testid=node-meter-fed-from]:visible", label="Ввод объекта (Ввод)")
        page.click("[data-testid=node-meter-submit]:visible")
        page.wait_for_timeout(1500)

        edges = api(page, "/api/v2/topology/edges?state=published")
        points = api(page, "/api/v2/points")
        check(len(edges) == 1 and len(points) == n_points0 + 1,
              f"одной операцией: линия и точка (линий {len(edges)}, точек {len(points)})")
        e_in = edges[0]
        check(e_in["primary_point_id"] == points[-1]["id"]
              and nodes[0]["id"] in (e_in["from_node_id"], e_in["to_node_id"]),
              "линия Ввод → Щит измеряется новой точкой")
        meter_rows = page.locator("[data-testid=node-card]:visible [data-testid=node-meter]").all_inner_texts()
        check(len(meter_rows) == 1 and "wb-map3e_16 — ввод щита" in meter_rows[0],
              f"карточка показывает «wb-map3e_16 — ввод щита» ({meter_rows})")
        tip = page.locator(".planv3-node-tip", has_text="Щит 1").first.inner_text()
        check("счётчик" in tip, f"значок щита на плане показывает счётчик (подпись: {tip!r})")
        plan_after = api(page, f"/api/v2/plans/{plan['id']}")
        check(len(plan_after["edges"]) == 1, "линия нарисована на плане между двумя значками")
        bp.screenshot("b07_after_input_meter")

        # ===== 3. «+ Счётчик» → отходящая линия к новой нагрузке =====
        card(page).locator("button", has_text="+ Счётчик").click()
        page.wait_for_timeout(300)
        page.select_option("[data-testid=node-meter-device]:visible", "wb-map12e_3")
        page.locator("[data-testid=node-meter-form]:visible label", has_text="Отходящую линию").locator("input").check()
        page.wait_for_timeout(200)
        check(page.input_value("[data-testid=node-meter-out-target]:visible") == "new",
              "по умолчанию — «+ новая нагрузка…» (отходящих линий ещё нет)")
        page.fill("[data-testid=node-meter-new-load]:visible", "Станок")
        page.click("[data-testid=node-meter-submit]:visible")
        page.wait_for_timeout(1800)

        nodes2 = {n["name"]: n for n in api(page, "/api/v2/topology/nodes")}
        check("Станок" in nodes2 and nodes2["Станок"]["kind"] == "load",
              "создана нагрузка «Станок»")
        plan_after = api(page, f"/api/v2/plans/{plan['id']}")
        placed = {it["node_id"] for it in plan_after["items"] if it["kind"] == "node"}
        check(nodes2["Станок"]["id"] in placed and len(plan_after["edges"]) == 2,
              f"нагрузка поставлена на план рядом со щитом, линий на плане: {len(plan_after['edges'])}")
        meter_rows = page.locator("[data-testid=node-card]:visible [data-testid=node-meter]").all_inner_texts()
        joined = " | ".join(meter_rows)
        check(len(meter_rows) == 2 and "wb-map12e_3 — линия к «Станок»" in joined,
              f"карточка щита: два счётчика, «wb-map12e_3 — линия к «Станок»» ({joined!r})")
        tip = page.locator(".planv3-node-tip", has_text="Щит 1").first.inner_text()
        check("счётчиков: 2" in tip, f"значок щита показывает число счётчиков (подпись: {tip!r})")

        # ===== 4. П4: занятые приборы — серые, выбрать нельзя =====
        card(page).locator("button", has_text="+ Счётчик").click()
        page.wait_for_timeout(300)
        opts = page.locator("[data-testid=node-meter-device]:visible option").evaluate_all(
            "els => els.map(e => ({t: e.textContent.trim(), v: e.value, d: e.disabled}))")
        busy = [o for o in opts if o["v"]]
        check(len(busy) == 2 and all(o["d"] and "занят точкой «" in o["t"] for o in busy),
              f"оба прибора заняты: серые, «занят точкой «…»» ({busy})")
        check(page.locator("[data-testid=node-meter-form]:visible").inner_text().count(
            "Свободных приборов нет") == 1, "форма прямо говорит, что свободных приборов нет")
        forbidden = ("scope", "channel_profile", "valid_from", "meter_source_id")
        check(not any(w in " ".join(o["t"] for o in busy) for w in forbidden),
              "в подписях приборов нет служебных слов")

        # ===== 6. входящая линия уже измеряется — человеческий отказ, черновик цел =====
        h.see_in_mqtt("wb-map-extra", "Третий")
        page.evaluate(APP + "._loadKnownDevices()")
        page.wait_for_timeout(500)
        page.select_option("[data-testid=node-meter-device]:visible", "wb-map-extra")
        page.locator("[data-testid=node-meter-form]:visible label", has_text="Ввод щита").locator("input").check()
        page.wait_for_timeout(200)
        check(page.locator("[data-testid=node-meter-form]:visible").inner_text().count(
            "уже измеряется другим счётчиком") == 1,
            "ввод щита уже измеряется — форма предупреждает и не даёт отправить")
        check(page.locator("[data-testid=node-meter-submit]:visible").is_disabled(),
              "кнопка «Поставить счётчик» недоступна, пока входящая линия занята")
        # отказ сервера в самом запросе: тот же сценарий через API → понятный текст
        rev = api(page, "/api/v2/revision")["configuration_revision"]
        r = api(page, f"/api/v2/topology/nodes/{nodes2['Щит 1']['id']}/meters", "POST",
                {"device_id": "wb-map-extra", "measures": "input", "expected_revision": rev})
        check(r["status"] == 409 and "уже измеряется" in r["body"]["message"]
              and not any(w in r["body"]["message"] for w in forbidden),
              f"отказ API: 409, человеческий текст без служебных слов ({r['body'].get('message')!r})")
        page.fill("[data-testid=node-meter-name]:visible", "Мой черновик")
        check(page.input_value("[data-testid=node-meter-name]:visible") == "Мой черновик",
              "черновик формы на месте")
        page.locator("[data-testid=node-meter-form]:visible button", has_text="Отмена").click()
        page.wait_for_timeout(200)

        # ===== 5. баланс щита и «Обзор» видят точки =====
        now_local = datetime.now()
        ov_from = (now_local.date() + timedelta(days=1)).strftime("%Y-%m-%d")
        ov_to = (now_local.date() + timedelta(days=2)).strftime("%Y-%m-%d")
        day_start = int(datetime.strptime(ov_from, "%Y-%m-%d").timestamp())

        def seed_full_day(device_id, total_kwh):
            for i in range(24):
                ps = day_start + i * HOUR
                h.seed_energy(device_id, total_kwh if i == 0 else 0.0,
                              period_start=ps, period_end=ps + HOUR)
        seed_full_day("wb-map3e_16", 100.0)
        seed_full_day("wb-map12e_3", 30.0)

        page.locator("nav button:visible", has_text="Обзор").click()
        page.wait_for_timeout(1000)
        page.fill("input[x-model='ovFrom']", ov_from)
        page.fill("input[x-model='ovTo']", ov_to)
        page.click("text=Обновить")
        page.wait_for_timeout(1200)
        api_summary = page.evaluate(
            "fetch('/api/v2/overview/summary', {method:'POST', headers:{'Content-Type':'application/json'}, "
            f"body: JSON.stringify({{from:'{ov_from}', to:'{ov_to}', timezone:Intl.DateTimeFormat().resolvedOptions().timeZone}})}}).then(r=>r.json())")
        total = api_summary.get("object_total", {}).get("value")
        ov_text = page.locator("body").inner_text()
        check(total is not None and abs(total - 100.0) < 1e-6 and f"{total:.1f}" in ov_text,
              f"«Обзор» видит ввод щита: итог объекта = 100 (API {total}, на экране есть {total and f'{total:.1f}'})")
        snap = api(page, "/api/v2/snapshot")
        names = [p["name"] for p in snap["points"]]
        check("все точки учёта" in ov_text.lower() and all(n in ov_text for n in names) and len(names) == 2,
              f"в «Обзоре» обе новые точки учёта в списке «Все точки учёта» (API: {names})")

        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(500)
        page.evaluate(APP + f".openInspectorInPlace('node', {nodes2['Щит 1']['id']})")
        page.wait_for_timeout(1800)
        api_balance = page.evaluate(
            f"fetch('/api/v2/topology/nodes/{nodes2['Щит 1']['id']}/balance', "
            "{method:'POST', headers:{'Content-Type':'application/json'}, "
            f"body: JSON.stringify({{from:'{ov_from}', to:'{ov_to}', timezone:Intl.DateTimeFormat().resolvedOptions().timeZone}})}}).then(r=>r.json())")
        shown = page.locator("[data-testid=node-card]:visible [data-testid=node-balance-value]").first.inner_text()
        check(api_balance.get("value") is not None and abs(api_balance["value"] - 70.0) < 1e-6,
              f"API: баланс щита = 100 − 30 = 70 ({api_balance.get('value')})")
        check(f"{api_balance['value']:.1f}" in shown,
              f"«Баланс щита» в карточке = ответу API ({shown!r})")

        # 409 — намеренный отказ API-пробы выше; браузер пишет его в консоль сам.
        errs = [e for e in bp.errors if "status of 409" not in e]
        check(not errs, f"консоль без ошибок ({len(errs)}): " + "; ".join(errs[:5]))
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
