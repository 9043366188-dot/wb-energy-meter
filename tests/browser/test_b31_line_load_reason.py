"""A32 (ТЗ §13): «нет тока/номинала, у соседней линии есть мощность» — на экране нет процента
и «нормальной нагрузки» по подставленной мощности, а причина видна.

Серверная часть (tests/test_step68_line_load.py) проверяет код причины в ответе
GET /api/v2/topology/edge-loads. Здесь — то, что видит человек: настоящий Chromium, настоящее
Flask-приложение, однолинейная схема. Положительный случай («40 % от номинала») в
test_b17_checklist_plan_v3.py берёт плоскую линию «Плана v3»; здесь он — контроль рядом с двумя
линиями без процента.

Схема (чистая БД): один щит и три отходящие линии со счётчиками. Живые значения кладутся прямо
в реестр приборов (как делает MQTT), статус приборов «в порядке».

  А  номинал 100 А, токи 40 / 30 / 20 А, мощность 8,8 кВт  → процент есть (40 %) — контроль
  Б  номинала нет, мощность 5 кВт, тока нет               → процента нет, причина no_rating
  В  номинал 63 А, мощность 7 кВт, тока нет               → процента нет, причина no_current

Где причина видна на экране (по коду, static/js/core.js::_planLoadSuffix):
  * подпись линии на самой схеме (single-line.js::_slLineLabelHtml) — «P · процент»: у линий Б и В
    только мощность, без процента и без слов «норма»/«нормальная нагрузка»; причины в подписи нет;
  * карточка линии (клик по линии, панель «Плана v3», поле «Ток / загрузка / мощность») —
    «ток неизвестен · загрузка неизвестна: <причина> · P кВт». Текст причины на экране равен
    load_unavailable_text ответа edge-loads, а тот — REASON_TEXT модуля line_load.

Проверки:
  1. API: у А load_percent == 40.0, у Б и В — null, причины no_rating / no_current, тексты из line_load.
  2. Схема: процент (`%`) в подписи только у А; у Б и В — мощность без процента.
  3. Карточка линий Б и В: «загрузка неизвестна: <текст из API>», нет «% от номинала», мощность показана;
     карточка А: «40% от номинала», нет «загрузка неизвестна».
  4. Живой тик (общий 5-секундный) ничего не меняет: у Б и В по-прежнему нет процента, причина на месте.
  5. Консоль без ошибок.

Запуск: python tests/browser/test_b31_line_load_reason.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from waits import wait_map_v3_ready, wait_card_open, close_card  # noqa: E402
from scheme_tools import line_hit_point  # noqa: E402
from wb_energy_meter.line_load import REASON_TEXT  # noqa: E402
from wb_energy_meter.model import ControlState, MeterStatus  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
CARD_FIELD = "Ток / загрузка / мощность"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def wait_state(page, expr: str, timeout: int = 15000) -> None:
    page.wait_for_function(f"() => {{ const a = {APP}; return !!({expr}); }}", timeout=timeout)


def live(h, device_id: str, name: str, power_w: float, irms: dict | None = None) -> None:
    """Прибор «на связи»: как после свежего MQTT-сообщения. irms=None — токов в MQTT нет совсем."""
    now = time.time()
    m = h.see_in_mqtt(device_id, name)
    m.controls["Total P"] = ControlState(name="Total P", value=power_w, raw_value=str(power_w),
                                         last_update_ts=now, meta={"type": "power"})
    for ph, val in (irms or {}).items():
        m.controls["Irms " + ph] = ControlState(name="Irms " + ph, value=val, raw_value=str(val),
                                                last_update_ts=now, meta={"type": "current"})
    m.last_any_ts = now
    m.last_measurement_ts = now
    m.status = MeterStatus.OK


def edge_load(h, edge_id: int) -> dict:
    r = h.client.get(f"/api/v2/topology/edge-loads?edge_ids={edge_id}")
    assert r.status_code == 200, r.get_json()
    items = r.get_json()["edges"]
    assert len(items) == 1 and items[0]["edge_id"] == edge_id, items
    return items[0]


def label_texts(page, edge_id: int) -> dict:
    """Подпись линии на схеме: вторая строка («P · процент») и всё содержимое."""
    return page.evaluate("""(id) => {
      const el = document.querySelector('.sl-ll[data-sl-line="' + id + '"]');
      if (!el) return null;
      const b = el.querySelector('.sl-ll-b');
      return {second: b ? b.textContent.trim() : '', all: el.textContent.trim(), title: el.getAttribute('title') || ''};
    }""", edge_id)


def open_line_card(page, edge_id: int) -> None:
    """Открыть карточку линии настоящим кликом по линии на схеме."""
    page.locator("#site-plan-v3-map").scroll_into_view_if_needed()
    wait_map_v3_ready(page)
    p = line_hit_point(page, edge_id)
    check(p is not None, f"линия {edge_id}: на схеме есть точка, в которую попадает клик")
    page.mouse.click(p["x"], p["y"])
    wait_state(page, f"a.structInspector && a.structInspector.kind==='edge' && a.structInspector.id=={edge_id}")
    wait_card_open(page)


def card_field(page) -> str:
    """Значение поля «Ток / загрузка / мощность» карточки линии (панель «Плана v3»)."""
    loc = page.locator("[data-testid=planv3-panel] .structure-field").filter(
        has=page.locator(".sf-lbl", has_text=CARD_FIELD)).locator(".sf-val")
    return loc.first.inner_text().strip() if loc.count() else ""


def main() -> int:
    with Harness() as h:
        # ---- 1. топология: щит и три линии со счётчиками ---------------------------------
        panel = h.make_node("P1", "Щит", "panel")
        lines = {}
        for key, (load_name, rated) in {
                "A": ("Нагрузка А", 100.0), "B": ("Нагрузка Б", None), "V": ("Нагрузка В", 63.0)}.items():
            extra = {"rated_current_a": rated} if rated is not None else {}
            c = h.add_consumer(panel["id"], load_name, **extra)
            pt = h.make_point("PT-" + key, "Счётчик " + key)
            live_args = {
                "A": (8800.0, {"L1": 40.0, "L2": 30.0, "L3": 20.0}),
                "B": (5000.0, None),
                "V": (7000.0, None),
            }[key]
            live(h, "dev-" + key, "Счётчик " + key, *live_args)
            h.bind_meter(pt["id"], "dev-" + key, "Счётчик " + key)
            h.set_meter_on_edge(c["edge"]["id"], pt["id"])
            lines[key] = c["edge"]["id"]
        eA, eB, eV = lines["A"], lines["B"], lines["V"]

        # ---- 2. сервер: что он отдаёт по трём линиям ------------------------------------
        a, b, v = edge_load(h, eA), edge_load(h, eB), edge_load(h, eV)
        check(a["load_percent"] == 40.0 and a["load_unavailable_reason"] is None,
              f"API, линия А (контроль): load_percent == 40.0, причины нет ({a['load_percent']}, {a['load_unavailable_reason']})")
        check(b["load_percent"] is None and b["load_unavailable_reason"] == "no_rating",
              f"API, линия Б (нет номинала): процента нет, причина no_rating ({b['load_percent']}, {b['load_unavailable_reason']})")
        check(b["power_w"] == 5000.0 and b["rated_current_a"] is None,
              f"API, линия Б: мощность 5000 Вт есть, номинала нет ({b['power_w']}, {b['rated_current_a']})")
        check(v["load_percent"] is None and v["load_unavailable_reason"] == "no_current",
              f"API, линия В (нет тока): процента нет, причина no_current ({v['load_percent']}, {v['load_unavailable_reason']})")
        check(v["power_w"] == 7000.0 and v["rated_current_a"] == 63.0 and v["current_a"] is None,
              f"API, линия В: мощность 7000 Вт и номинал 63 А есть, тока нет ({v['power_w']}, {v['rated_current_a']}, {v['current_a']})")
        check(b["load_unavailable_text"] == REASON_TEXT["no_rating"] and v["load_unavailable_text"] == REASON_TEXT["no_current"],
              f"API: тексты причин — из line_load.REASON_TEXT ({b['load_unavailable_text']!r}, {v['load_unavailable_text']!r})")

        # ---- 3. схема на экране -----------------------------------------------------------
        bp = open_browser(h.base_url, viewport={"width": 1400, "height": 1000}, timezone_id="UTC")
        page = bp.page
        wait_state(page, "true")
        if page.evaluate(APP + ".uiSet") != "v2":
            page.evaluate(APP + ".toggleUiSet()")
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_function("() => typeof _planV3Map !== 'undefined' && !!_planV3Map", timeout=15000)
        page.fill("input[placeholder='Название плана']", "Схема нагрузки")
        page.click("[data-testid=sl-empty-create]")
        wait_state(page, "a.planV3Current && a.planV3Current.plan_kind==='single_line' && a.structNodes.length===4")
        wait_state(page, "a.slUnplacedCount()===4")
        page.click("[data-testid=sl-auto-unplaced]")
        wait_state(page, "a.planV3Current.items.filter(i=>i.kind==='node').length===4 "
                         "&& a.planV3Current.edges.length===3 && !a.slAutoBusy")
        wait_state(page, "Object.keys(a.slLoads).length===3")
        page.wait_for_function("() => document.querySelectorAll('.sl-ll').length === 3", timeout=15000)
        wait_map_v3_ready(page)

        la, lb, lv = (label_texts(page, e) for e in (eA, eB, eV))
        check(la and "40%" in la["second"] and "8.80 кВт" in la["second"],
              f"схема, линия А: в подписи мощность и «40%» ({la and la['second']!r})")
        for name, lab, power in (("Б", lb, "5.00 кВт"), ("В", lv, "7.00 кВт")):
            check(lab is not None and "%" not in lab["all"] and "%" not in lab["title"],
                  f"схема, линия {name}: процента в подписи нет ({lab and lab['second']!r})")
            check(lab is not None and power in lab["second"],
                  f"схема, линия {name}: видна измеренная мощность {power}, она не превращена в процент")
            check(lab is not None and "норм" not in (lab["all"] + lab["title"]).lower(),
                  f"схема, линия {name}: нет слов о «нормальной нагрузке»")
        pcts = page.evaluate("[...document.querySelectorAll('.sl-ll-b')].filter(e => e.textContent.includes('%')).length")
        check(pcts == 1, f"на всей схеме ровно одна подпись с процентом — у линии А ({pcts})")

        # ---- 4. карточки линий: причина видна ---------------------------------------------
        open_line_card(page, eB)
        fb = card_field(page)
        reason_b = REASON_TEXT["no_rating"]
        check(f"загрузка неизвестна: {b['load_unavailable_text']}" in fb,
              f"карточка линии Б: «загрузка неизвестна: {b['load_unavailable_text']}» ({fb!r})")
        check(fb.startswith("ток неизвестен") and "5.00 кВт" in fb and "от номинала" not in fb,
              "карточка линии Б: ток неизвестен, мощность 5.00 кВт показана, «% от номинала» нет")
        check(f"загрузка неизвестна: {reason_b}" in fb, "причина на экране — тот же текст, что в line_load.REASON_TEXT['no_rating']")
        close_card(page)

        open_line_card(page, eV)
        fv = card_field(page)
        check(f"загрузка неизвестна: {v['load_unavailable_text']}" in fv,
              f"карточка линии В: «загрузка неизвестна: {v['load_unavailable_text']}» ({fv!r})")
        check(fv.startswith("ток неизвестен") and "7.00 кВт" in fv and "от номинала" not in fv,
              "карточка линии В: ток неизвестен, мощность 7.00 кВт показана, «% от номинала» нет")
        rated_v = page.locator("[data-testid=planv3-panel] input[aria-label='Номинальный ток, А']").input_value()
        check(rated_v in ("63", "63.0"), f"карточка линии В: номинал 63 А указан (поле {rated_v!r}) — причина именно в токе, не в номинале")
        close_card(page)

        open_line_card(page, eA)
        fa = card_field(page)
        check("40% от номинала" in fa and "загрузка неизвестна" not in fa,
              f"карточка линии А (контроль): «40% от номинала», причины нет ({fa!r})")
        check(fa.startswith("40.0 А"), f"карточка линии А: ток 40.0 А ({fa!r})")
        close_card(page)

        # ---- 5. живой тик ничего не меняет --------------------------------------------------
        live(h, "dev-B", "Счётчик Б", 5200.0)             # мощность изменилась, тока и номинала по-прежнему нет
        page.evaluate(APP + "._pollTick()")
        page.wait_for_function("() => { const e = document.querySelector('.sl-ll[data-sl-line=\"%d\"] .sl-ll-b');"
                               " return !!e && e.textContent.includes('5.20 кВт'); }" % eB, timeout=15000)
        lb2 = label_texts(page, eB)
        check("%" not in lb2["all"], f"после тика мощность Б обновилась до 5.20 кВт, процента по-прежнему нет ({lb2['second']!r})")
        open_line_card(page, eB)
        page.evaluate(APP + "._pollTick()")
        page.wait_for_function("() => { const l = document.querySelector('[data-testid=planv3-panel]');"
                               " return !!l && l.innerText.includes('5.20 кВт'); }", timeout=15000)
        fb2 = card_field(page)
        check(f"загрузка неизвестна: {reason_b}" in fb2 and "от номинала" not in fb2,
              f"карточка линии Б после тика: причина на месте, процента нет ({fb2!r})")
        close_card(page)

        check(not bp.errors, f"консоль без ошибок ({bp.errors[:3]})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
