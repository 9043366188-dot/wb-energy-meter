"""Партия находок 0.22.3, П9: счётчик «по фазам» — от выбора прибора до баланса
щита (настоящий Chromium, настоящее Flask-приложение).

Сценарий (трёхфазный WB-MAP3E питает три разные нагрузки щита):

  1. «План v3»: «Ввод объекта» и «Щит 1», на ввод щита — обычный счётчик
     `wb-in_1` (весь прибор одной линией).
  2. Щит → «+ Счётчик» → прибор без пофазных счётчиков (`wb-plain_1`): «по фазам»
     недоступно, рядом сказано почему. Прибор с пофазными счётчиками
     (`wb-map3e_16`): «по фазам» доступно.
  3. «Отходящую линию» → «по фазам» → три строки L1/L2/L3: L1 → новая нагрузка
     «Освещение», L2 → «Розетки», L3 → «не используется». Одной операцией: две линии
     с `phase_count = 1`, две точки с профилями `phase_l1` / `phase_l2`, обе
     нагрузки на плане в РАЗНЫХ местах, обе линии нарисованы.
  4. Карточка щита: «wb-map3e_16 · L1 — линия к «Освещение»» и «… L2 — линия к
     «Розетки»»; в списке приборов `wb-map3e_16` НЕ серый: «свободны L3».
  5. Снова «+ Счётчик» → тот же прибор: «все три фазы» недоступно, L1 и L2 — «занята
     точкой «…»», L3 → существующая нагрузка «Вентиляция». После этого прибор в
     списке серый («занят точкой…»).
  6. Данные: каждой фазе засеян свой пофазный итог (L1 = 20, L2 = 10, L3 = 5 кВт·ч),
     вводу щита — 100. Баланс щита в карточке = ответу API = 100 − 35 = 65:
     по итогу всего прибора получилось бы 100 (итогов у него нет). «Обзор»: итог
     объекта = 100, все четыре точки учёта в списке.

Запуск: python tests/browser/test_b13_phase_meter.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser, HOUR  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []
DEV = "wb-map3e_16"
FORBIDDEN = ("scope", "channel_profile", "valid_from", "meter_source_id", "phase_l1",
             "total_3p")


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def api(page, path):
    return page.evaluate(f"fetch({path!r}).then(r=>r.json())")


def card(page):
    return page.locator("[data-testid=node-card]:visible")


def place_on_map(page, kind_value, name, fx, fy):
    box = page.locator(".leaflet-container:visible").first.bounding_box()
    page.click("button:has-text('+ Элемент схемы')")
    page.wait_for_timeout(200)
    page.select_option("[data-testid=planv3-kind-select]", kind_value)
    page.fill("input[placeholder='Название']", name)
    page.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
    page.wait_for_timeout(1200)


def open_form(page):
    card(page).locator("button", has_text="+ Счётчик").click()
    page.wait_for_timeout(300)


def device_options(page):
    return page.locator("[data-testid=node-meter-device]:visible option").evaluate_all(
        "els => els.filter(e => e.value).map(e => ({t: e.textContent.trim(), v: e.value, d: e.disabled}))")


def pick_outgoing(page):
    page.locator("[data-testid=node-meter-form]:visible label",
                 has_text="Отходящую линию").locator("input").check()
    page.wait_for_timeout(200)


def main() -> int:
    with Harness() as h:
        h.see_in_mqtt("wb-in_1", "Ввод щита")
        h.see_in_mqtt("wb-plain_1", "Одна линия")
        h.see_in_mqtt(DEV, "Трёхфазный", phases=True)
        plan = h.create_plan("Смоук")

        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1000)
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(300)
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(1000)

        # ===== 1. ввод, щит, счётчик на ввод щита =====
        place_on_map(page, "source", "Ввод объекта", 0.3, 0.3)
        place_on_map(page, "panel", "Щит 1", 0.3, 0.5)
        open_form(page)
        page.select_option("[data-testid=node-meter-device]:visible", "wb-in_1")
        page.select_option("[data-testid=node-meter-fed-from]:visible", label="Ввод объекта (Ввод)")
        page.click("[data-testid=node-meter-submit]:visible")
        page.wait_for_timeout(1500)
        nodes = {n["name"]: n for n in api(page, "/api/v2/topology/nodes")}
        shield = nodes["Щит 1"]
        check(len(api(page, "/api/v2/points")) == 1, "на ввод щита поставлен обычный счётчик")

        # ===== 2. прибор без пофазных счётчиков: «по фазам» недоступно =====
        open_form(page)
        pick_outgoing(page)
        phases_radio = page.locator("[data-testid=node-meter-mode-phases]:visible")
        check(phases_radio.is_disabled(), "прибор не выбран → «по фазам» недоступно")
        page.select_option("[data-testid=node-meter-device]:visible", "wb-plain_1")
        page.wait_for_timeout(200)
        note = page.locator("[data-testid=node-meter-mode-note]:visible").inner_text()
        check(phases_radio.is_disabled() and "нет пофазных счётчиков энергии" in note,
              f"у «wb-plain_1» нет AP energy L1–L3: «по фазам» недоступно и сказано почему ({note!r})")
        check(not any(w in note for w in FORBIDDEN), "в пояснении нет служебных слов")
        page.select_option("[data-testid=node-meter-device]:visible", DEV)
        page.wait_for_timeout(200)
        check(phases_radio.is_enabled(), "у «wb-map3e_16» пофазные счётчики есть → «по фазам» доступно")

        # ===== 3. «по фазам»: L1 → «Освещение», L2 → «Розетки», L3 не используется =====
        phases_radio.check()
        page.wait_for_timeout(200)
        rows = page.locator("[data-testid=node-meter-phases]:visible > div[data-testid^=node-meter-phase-]")
        check(rows.count() == 3, f"три строки фаз L1/L2/L3 ({rows.count()})")
        check(page.input_value("[data-testid=node-meter-phase-target-L1]:visible") == "skip",
              "по умолчанию фаза «не используется»")
        check(page.locator("[data-testid=node-meter-submit]:visible").is_disabled(),
              "пока ни одной фазы не выбрано, «Поставить счётчик» недоступна")
        page.select_option("[data-testid=node-meter-phase-target-L1]:visible", "new")
        page.fill("[data-testid=node-meter-phase-new-L1]:visible", "Освещение")
        page.select_option("[data-testid=node-meter-phase-target-L2]:visible", "new")
        check(page.locator("[data-testid=node-meter-submit]:visible").is_disabled(),
              "фаза «новая нагрузка» без названия → отправить нельзя")
        page.fill("[data-testid=node-meter-phase-new-L2]:visible", "Розетки")
        check(page.locator("[data-testid=node-meter-submit]:visible").is_enabled(),
              "L1 и L2 заполнены, L3 не используется → можно отправить")
        bp.screenshot("b13_phase_form")

        rev0 = api(page, "/api/v2/revision")["configuration_revision"]
        n_points0 = len(api(page, "/api/v2/points"))
        page.click("[data-testid=node-meter-submit]:visible")
        page.wait_for_timeout(2200)

        nodes = {n["name"]: n for n in api(page, "/api/v2/topology/nodes")}
        check("Освещение" in nodes and "Розетки" in nodes
              and all(nodes[n]["kind"] == "load" for n in ("Освещение", "Розетки")),
              "созданы нагрузки «Освещение» и «Розетки»")
        check(api(page, "/api/v2/revision")["configuration_revision"] == rev0 + 1,
              "все фазы — одна ревизия конфигурации")
        points = api(page, "/api/v2/points")
        check(len(points) == n_points0 + 2, f"две новые точки (L1, L2); третья фаза не создана ({len(points)})")
        edges = api(page, "/api/v2/topology/edges?state=published")
        by_to = {e["to_node_id"]: e for e in edges}
        e1, e2 = by_to[nodes["Освещение"]["id"]], by_to[nodes["Розетки"]["id"]]
        check(e1["phase_count"] == 1 and e2["phase_count"] == 1
              and e1["from_node_id"] == shield["id"] and e2["from_node_id"] == shield["id"],
              "обе линии идут от щита и имеют одну фазу (phase_count = 1)")
        profiles = {}
        for e in (e1, e2):
            b = api(page, f"/api/v2/points/{e['primary_point_id']}/bindings")
            items = b if isinstance(b, list) else b.get("bindings", b)
            profiles[e["to_node_id"]] = items[0]["channel_profile"]
        check(profiles[nodes["Освещение"]["id"]] == "phase_l1"
              and profiles[nodes["Розетки"]["id"]] == "phase_l2",
              f"привязки по фазам: Освещение — L1, Розетки — L2 ({profiles})")

        plan_now = api(page, f"/api/v2/plans/{plan['id']}")
        geom = {it["node_id"]: it["geometry"] for it in plan_now["items"] if it["kind"] == "node"}
        g1, g2 = geom.get(nodes["Освещение"]["id"]), geom.get(nodes["Розетки"]["id"])
        check(g1 is not None and g2 is not None and (round(g1["x"]), round(g1["y"])) != (round(g2["x"]), round(g2["y"])),
              f"обе нагрузки на плане в разных местах: {g1} / {g2}")
        check(len(plan_now["edges"]) == 3, f"на плане три линии: ввод щита и две фазы ({len(plan_now['edges'])})")

        # ===== 4. карточка щита и список приборов =====
        rows_text = " | ".join(card(page).locator("[data-testid=node-meter]").all_inner_texts())
        check(f"{DEV} · L1 — линия к «Освещение»" in rows_text
              and f"{DEV} · L2 — линия к «Розетки»" in rows_text,
              f"карточка называет прибор и фазу ({rows_text!r})")
        check(not any(w in rows_text for w in FORBIDDEN), "в карточке нет служебных слов")
        open_form(page)
        opts = {o["v"]: o for o in device_options(page)}
        check(not opts[DEV]["d"] and "свободны L3" in opts[DEV]["t"],
              f"прибор с занятыми L1/L2 не серый: «{opts[DEV]['t']}»")
        check(opts["wb-in_1"]["d"] and "занят точкой" in opts["wb-in_1"]["t"],
              f"прибор на вводе серый: «{opts['wb-in_1']['t']}»")
        page.locator("[data-testid=node-meter-form]:visible button", has_text="Отмена").click()
        page.wait_for_timeout(200)

        # ===== 5. третья фаза на существующую нагрузку =====
        h.make_node("ventilation", "Вентиляция", "load")
        page.evaluate(APP + "._loadStructureLists()")
        page.wait_for_timeout(600)
        open_form(page)
        page.select_option("[data-testid=node-meter-device]:visible", DEV)
        pick_outgoing(page)
        check(page.locator("[data-testid=node-meter-mode-total]:visible").is_disabled(),
              "L1 и L2 заняты → «все три фазы» недоступно")
        check(page.locator("[data-testid=node-meter-mode-phases]:visible").is_checked(),
              "выбран «по фазам» сам")
        busy1 = page.locator("[data-testid=node-meter-phase-busy-L1]:visible").inner_text()
        busy2 = page.locator("[data-testid=node-meter-phase-busy-L2]:visible").inner_text()
        check(f"занята точкой «{DEV} · L1»" in busy1 and f"занята точкой «{DEV} · L2»" in busy2,
              f"занятые фазы названы по точке-владельцу ({busy1!r}, {busy2!r})")
        check(page.locator("[data-testid=node-meter-phase-target-L1]").count() == 0
              and page.locator("[data-testid=node-meter-phase-target-L3]:visible").count() == 1,
              "у занятых фаз нет выбора цели, у L3 — есть")
        target_opts = page.locator("[data-testid=node-meter-phase-target-L3]:visible option").evaluate_all(
            "els => els.map(e => ({t: e.textContent.trim(), v: e.value}))")
        vent = [o for o in target_opts if "Вентиляция" in o["t"]]
        check(len(vent) == 1, f"существующая нагрузка «Вентиляция» есть среди целей: {target_opts}")
        page.select_option("[data-testid=node-meter-phase-target-L3]:visible", vent[0]["v"])
        page.click("[data-testid=node-meter-submit]:visible")
        page.wait_for_timeout(2200)
        edges = api(page, "/api/v2/topology/edges?state=published")
        nodes = {n["name"]: n for n in api(page, "/api/v2/topology/nodes")}
        e3 = next((e for e in edges if e["to_node_id"] == nodes["Вентиляция"]["id"]), None)
        check(e3 is not None and e3["phase_count"] == 1 and e3["primary_point_id"] is not None,
              "L3 подключена к «Вентиляция» (линия с phase_count = 1)")
        open_form(page)
        opts = {o["v"]: o for o in device_options(page)}
        check(opts[DEV]["d"] and "занят точкой" in opts[DEV]["t"],
              f"все три фазы заняты → прибор серый: «{opts[DEV]['t']}»")
        page.locator("[data-testid=node-meter-form]:visible button", has_text="Отмена").click()
        page.wait_for_timeout(200)

        # ===== 6. баланс щита и «Обзор» считают по фазам =====
        now_local = datetime.now()
        ov_from = (now_local.date() + timedelta(days=1)).strftime("%Y-%m-%d")
        ov_to = (now_local.date() + timedelta(days=2)).strftime("%Y-%m-%d")
        day_start = int(datetime.strptime(ov_from, "%Y-%m-%d").timestamp())
        for i in range(24):
            ps = day_start + i * HOUR
            h.seed_energy("wb-in_1", 100.0 if i == 0 else 0.0, period_start=ps, period_end=ps + HOUR)
            for ph, kwh in (("L1", 20.0), ("L2", 10.0), ("L3", 5.0)):
                h.seed_phase_energy(DEV, ph, kwh if i == 0 else 0.0,
                                    period_start=ps, period_end=ps + HOUR)

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
              f"«Обзор»: итог объекта = 100 (API {total})")
        snap = api(page, "/api/v2/snapshot")
        names = [p["name"] for p in snap["points"]]
        check(len(names) == 4 and all(n in ov_text for n in names)
              and sum(1 for n in names if n.startswith(DEV + " · L")) == 3,
              f"в «Обзоре» все четыре точки: ввод и три фазы прибора (API: {names})")

        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(500)
        page.evaluate(APP + f".openInspectorInPlace('node', {shield['id']})")
        page.wait_for_timeout(1800)
        api_balance = page.evaluate(
            f"fetch('/api/v2/topology/nodes/{shield['id']}/balance', "
            "{method:'POST', headers:{'Content-Type':'application/json'}, "
            f"body: JSON.stringify({{from:'{ov_from}', to:'{ov_to}', timezone:Intl.DateTimeFormat().resolvedOptions().timeZone}})}}).then(r=>r.json())")
        shown = page.locator("[data-testid=node-card]:visible [data-testid=node-balance-value]").first.inner_text()
        check(api_balance.get("value") is not None and abs(api_balance["value"] - 65.0) < 1e-6,
              f"API: баланс щита = 100 − (20 + 10 + 5) = 65 ({api_balance.get('value')})")
        check(f"{api_balance['value']:.1f}" in shown,
              f"«Баланс щита» в карточке = ответу API ({shown!r})")
        bp.screenshot("b13_balance")

        check(not bp.errors, f"консоль без ошибок ({len(bp.errors)}): " + "; ".join(bp.errors[:5]))
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
