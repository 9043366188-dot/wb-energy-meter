"""Этап 3 (партия 7), сквозной сценарий «Плана v3»: docs/
TZ-batch7-review-fixes.md, §Этап 3, «Браузер: ... Новый test_b03_planv3_flow.py
(Haiku, H1): чистая база, два прибора в registry -> план -> ввод, щит,
два потребителя -> линии -> "новая точка из прибора" на входящую и одну
отходящую -> засеять агрегаты -> карточка щита показывает баланс из
ручки Э2.5 -> "Обзор" показывает тот же итог и небаланс. Числа на экране
сверять с ответом API, а не с константой».

Топология (Ввод -> ЩР-1 -> {Станок, Свет}) заводится напрямую через
harness (make_node/connect/add_consumer, как и в test_step39/test_step40) —
клик-по-карте для СОЗДАНИЯ узла уже покрыт test_b02. Здесь под проверкой —
то, что test_b02 не трогает и что появилось только в Этапе 3:
  - B4: «Разместить на карте» для уже заведённого, но ещё не нанесённого
    узла (топология создана в обход карты — все 4 узла первоначально
    "неразмещены"), и списки «Линии»/«Приборы без точки учёта» в правой
    панели, когда ничего не выбрано;
  - B5/B8: карточка линии и инлайн-форма «+ новая точка из прибора» —
    реальный клик по кнопке, реальный ввод device_id, реальный POST
    /attach-new-point из браузера (не HTTP-хелпер);
  - B7: «Баланс щита» в карточке узла, посчитанный ручкой Э2.5, и что он
    остаётся согласован с /api/v2/overview/summary для того же периода
    (A43 — то же число, а не своя вторая формула);
  - B10: тайл «Требует внимания» отражает найденный "прибор без точки"
    ДО того, как он привязан (после привязки его в списке уже нет).

Запуск: python tests/browser/test_b03_planv3_flow.py
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser, HOUR  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def main() -> int:
    with Harness() as h:
        # --- топология в обход карты (то, что test_b02 не проверяет) ---
        vvod = h.make_node("vvod", "Ввод", "source")
        shr1 = h.make_node("shr1", "ЩР-1", "panel")
        edge_in = h.connect(vvod["id"], shr1["id"])
        c1 = h.add_consumer(shr1["id"], "Станок")
        c2 = h.add_consumer(shr1["id"], "Свет")
        edge_stanok = c1["edge"]
        node_stanok = c1["node"]
        node_svet = c2["node"]

        # «Два прибора в registry» (видны в MQTT, ещё не привязаны) — то
        # же условие, что и у "Приборы без точки учёта" (Э3/B4/B10).
        h.see_in_mqtt("dev-vvod", "Ввод-1")
        h.see_in_mqtt("dev-stanok", "Станок-1")

        plan = h.create_plan("Смоук")

        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1000)
        page.evaluate("document.querySelector('[x-data]')._x_dataStack[0].uiSet='v2'")
        page.wait_for_timeout(300)
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(1000)

        # --- B10: тайл "Требует внимания" уже видит оба прибора без точки ---
        page.locator("nav button:visible", has_text="Обзор").click()
        page.wait_for_timeout(1000)
        attn_text = page.locator(".tile", has_text="Требует внимания").inner_text()
        check("Обнаружено 2 приборов без точек учёта" in attn_text,
              f"плитка «Требует внимания» видит 2 прибора без точки (текст: {attn_text!r})")

        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(500)

        # --- B4: "Приборы без точки учёта" в панели "ничего не выбрано" ---
        # .sf-lbl рендерится через CSS text-transform:uppercase — inner_text()
        # отдаёт ВИЗУАЛЬНЫЙ текст (с учётом CSS), а не сырой textContent,
        # поэтому сравниваем без учёта регистра.
        panel_text = page.locator(".plan-side:visible").inner_text()
        check("приборы без точки учёта (2)" in panel_text.lower(),
              f"панель «ничего не выбрано» видит 2 прибора без точки (текст: {panel_text!r})")
        check("Ввод" in panel_text and "ЩР-1" in panel_text and "Станок" in panel_text and "Свет" in panel_text,
              "панель «ничего не выбрано» видит все 4 узла (пока не размещены)")

        # --- B4: "Разместить на карте" для всех 4 узлов ---
        box = page.locator(".leaflet-container:visible").first.bounding_box()
        positions = [(0.2, 0.2), (0.5, 0.2), (0.2, 0.6), (0.8, 0.6)]
        for (name, (fx, fy)) in zip(["Ввод", "ЩР-1", "Станок", "Свет"], positions):
            # Кнопка «Разместить на карте» в ТОЙ ЖЕ строке, что и имя узла
            # (structure-field -> div со span-именем и кнопкой рядом,
            # см. разметку в index.html, Э3/B4).
            row = page.locator(".plan-side:visible div", has_text=name).filter(
                has=page.locator("button", has_text="Разместить на карте")
            ).last
            row.locator("button", has_text="Разместить на карте").click()
            page.wait_for_timeout(200)
            page.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
            page.wait_for_timeout(600)
            # planV3PlaceExistingNode() открывает карточку размещённого
            # узла (openInspectorInPlace) — закрываем её, иначе на
            # следующей итерации панель "ничего не выбрано" со списком
            # "Узлы"/кнопками "Разместить на карте" не видна вообще.
            page.evaluate("document.querySelector('[x-data]')._x_dataStack[0].closeInspector()")
            page.wait_for_timeout(200)

        n_items = page.evaluate(
            f"fetch('/api/v2/plans/{plan['id']}').then(r=>r.json()).then(p=>p.items.length)"
        )
        check(n_items == 4, f"все 4 узла размещены на плане (items: {n_items})")

        # --- B5/B8: карточка линии + "+ новая точка из прибора" (входящая) ---
        page.locator("text=Ввод → ЩР-1").first.click()
        page.wait_for_timeout(500)
        page.click("text=＋ новая точка из прибора…")
        page.fill("input[placeholder='device_id прибора']", "dev-vvod")
        page.click("text=Подключить")
        page.wait_for_timeout(800)
        edge_in_after = page.evaluate(f"fetch('/api/v2/topology/edges/{edge_in['id']}').then(r=>r.json())")
        check(edge_in_after.get("primary_point_id") is not None,
              f"входящая линия получила точку измерения через UI (edge: {edge_in_after})")

        # --- то же самое для одной отходящей линии (ЩР-1 -> Станок) ---
        # Список "Линии" виден только в панели "ничего не выбрано" —
        # после открытия карточки предыдущей линии её нужно закрыть,
        # иначе "text=ЩР-1 → Станок" не найти (тот же нюанс, что и в
        # цикле "Разместить на карте" выше).
        page.evaluate("document.querySelector('[x-data]')._x_dataStack[0].closeInspector()")
        page.wait_for_timeout(300)
        page.locator("text=ЩР-1 → Станок").first.click()
        page.wait_for_timeout(500)
        page.click("text=＋ новая точка из прибора…")
        page.fill("input[placeholder='device_id прибора']", "dev-stanok")
        page.click("text=Подключить")
        page.wait_for_timeout(800)
        edge_stanok_after = page.evaluate(
            f"fetch('/api/v2/topology/edges/{edge_stanok['id']}').then(r=>r.json())"
        )
        check(edge_stanok_after.get("primary_point_id") is not None,
              f"линия ЩР-1 -> Станок получила точку измерения через UI (edge: {edge_stanok_after})")
        # Линия ЩР-1 -> Свет остаётся БЕЗ счётчика намеренно (неизмеренная
        # ветвь — реалистичный сценарий и дополнительная проверка B7).
        # Закрываем карточку линии — иначе после возврата с "Обзора" на
        # "План v3" будет всё ещё открыта карточка "ЩР-1 -> Станок", а не
        # панель "ничего не выбрано" со строкой "ЩР-1 (Щит)".
        page.evaluate("document.querySelector('[x-data]')._x_dataStack[0].closeInspector()")
        page.wait_for_timeout(200)

        # --- засеять агрегаты (harness, как test_step39/test_step40) ---
        # ВАЖНО (найдено при написании этого теста): "+ новая точка из
        # прибора" (Э3/B8, attach-new-point) открывает привязку с
        # valid_from=сейчас (в отличие от harness.bind_meter(), которым
        # пользуются test_step39/test_step40 и который явно просит
        # valid_from=0) — реальная форма в браузере "at" не передаёт,
        # это осознанное поведение ручки (см. её докстринг в api_v2.py),
        # не баг. period_start=0 (1970) здесь не сработает: агрегат "до"
        # открытия привязки не засчитывается точке (valid_count=0).
        #
        # Дальше: <input type="date"> (ovFrom/ovTo) даёт окно не мельче
        # целых календарных суток — 24 часовых слота. Чтобы это окно
        # получило "availability":"ok" (а не "partial"/"gap"), нужно
        # закрыть агрегатом КАЖДЫЙ час окна, а само окно должно целиком
        # лежать ПОСЛЕ valid_from обеих привязок — берём ЗАВТРАШНИЕ сутки
        # целиком (гарантированно позже "сейчас", когда обе привязки уже
        # открыты) и засеваем полные 24 часа: весь расход — одним часом,
        # остальные 23 часа — нулевые, но существующие ("ok") агрегаты.
        now_local = datetime.now()
        ov_from = (now_local.date() + timedelta(days=1)).strftime("%Y-%m-%d")
        ov_to = (now_local.date() + timedelta(days=2)).strftime("%Y-%m-%d")
        day_start = int(datetime.strptime(ov_from, "%Y-%m-%d").timestamp())

        def seed_full_day(device_id, total_kwh):
            for k in range(24):
                ps = day_start + k * HOUR
                kwh = total_kwh if k == 0 else 0.0
                h.seed_energy(device_id, kwh, period_start=ps, period_end=ps + HOUR)

        seed_full_day("dev-vvod", 100.0)
        seed_full_day("dev-stanok", 30.0)

        # --- B10: приборы без точки учёта теперь ни одного (оба привязаны) ---
        page.locator("nav button:visible", has_text="Обзор").click()
        page.wait_for_timeout(500)
        attn_text2 = page.locator(".tile", has_text="Требует внимания").inner_text()
        check("Обнаружено" not in attn_text2,
              f"после привязки обоих приборов тайл больше не просит завести точки (текст: {attn_text2!r})")

        # --- период, включающий засеянные агрегаты, тот же на «Обзоре» и
        #     в карточке узла (ovFrom/ovTo — общее состояние, A43/Э3/B7) ---
        page.fill("input[x-model='ovFrom']", ov_from)
        page.fill("input[x-model='ovTo']", ov_to)
        page.click("text=Обновить")
        page.wait_for_timeout(1000)

        api_summary = page.evaluate(
            "fetch('/api/v2/overview/summary', {method:'POST', "
            "headers:{'Content-Type':'application/json'}, "
            f"body: JSON.stringify({{from:'{ov_from}', to:'{ov_to}', timezone:'UTC'}})}})"
            ".then(r=>r.json())"
        )
        expected_total = api_summary.get("object_total", {}).get("value")
        expected_imbalance = api_summary.get("imbalance_value")
        ov_text = page.locator("body").inner_text()
        if expected_total is not None:
            check(f"{expected_total:.1f}" in ov_text,
                  f"«Обзор» показывает object_total из API ({expected_total:.1f})")
        else:
            check(False, f"ожидался числовой object_total от API, получено: {api_summary}")
        if expected_imbalance is not None:
            check(f"{expected_imbalance:.1f}" in ov_text,
                  f"«Обзор» показывает imbalance_value из API ({expected_imbalance:.1f})")
        else:
            check(False, f"ожидался числовой imbalance_value от API, получено: {api_summary}")

        # --- B7: карточка узла ЩР-1 показывает "Баланс щита" из Э2.5,
        #     согласованный с тем же расчётом, что и «Обзор» выше ---
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(500)
        # "ЩР-1 (Щит)" — конкретная строка узла в списке "Узлы" панели
        # "ничего не выбрано". Обработчик клика (planV3NodePlaced(n.id) &&
        # openInspectorInPlace(...)) висит на <span class="sf-val">, НЕ на
        # обёртывающем <div> строки — клик по внешнему div не долетает до
        # него (playwright кликает в центр bbox, который у div шире, чем
        # реально кликабельный span) — найдено при отладке этого теста.
        page.locator(".plan-side:visible span.sf-val", has_text="ЩР-1 (Щит)").click()
        page.wait_for_timeout(1500)

        api_balance = page.evaluate(
            f"fetch('/api/v2/topology/nodes/{shr1['id']}/balance', "
            "{method:'POST', headers:{'Content-Type':'application/json'}, "
            f"body: JSON.stringify({{from:'{ov_from}', to:'{ov_to}', timezone:'UTC'}})}})"
            ".then(r=>r.json())"
        )
        node_card_text = page.locator(".plan-side:visible").inner_text()
        # "Баланс щита за период" — тоже .sf-lbl (text-transform:uppercase),
        # тот же нюанс inner_text(), что и у "Приборы без точки учёта" выше.
        check("баланс щита" in node_card_text.lower(), "карточка узла показывает блок «Баланс щита»")
        if api_balance.get("value") is not None:
            check(f"{api_balance['value']:.1f}" in node_card_text,
                  f"«Баланс щита» в карточке совпадает с ответом Э2.5 ({api_balance['value']:.1f})")
        else:
            check(False, f"ожидался числовой баланс щита от API, получено: {api_balance}")
        check(api_balance.get("boundary_coverage") == "has_unmetered_branches",
              "баланс щита честно отмечает неизмеренную ветвь (Свет без счётчика)")
        check("неизмеренные ветви" in node_card_text or "unmetered" not in node_card_text,
              "карточка не показывает сырой код причины вместо текста")

        check(not bp.errors, f"консоль без ошибок ({len(bp.errors)}): " + "; ".join(bp.errors[:5]))
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
