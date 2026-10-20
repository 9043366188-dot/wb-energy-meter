"""Партия 13, 13.5 (§10): дисциплина опроса — настоящий Chromium, настоящее Flask-приложение.

  1. Один общий таймер: за обход всех вкладок набора v2 живых setInterval с периодом
     ≤ 10 с ровно один (тик 5 с); новых таймеров экраны не заводят.
  2. Скрытая вкладка не опрашивает сервер; при возвращении — ровно одно обновление.
  3. Один незавершённый запрос каждого типа: пока ответ на снимок не пришёл, новые тики
     запрос не повторяют (ни «Обзор», ни «Структура», ни карточка узла).
  4. Цифры не мигают: пока идут обновления, плитки «Обзора» не пустеют и не прыгают
     по высоте, а прежние значения остаются до прихода новых.
  5. Вкладка «Настройки» набора v2 не опрашивает снимок (только статус раз в 30 с).
  6. ТЗ 13.7 (A33/A34): 20 переключений «План v3 → Обзор → План v3» — ровно один
     `.leaflet-container`, число слоёв карты и живых таймеров не растёт, открытая карточка
     и зум сохраняются; скрытая вкладка 12 с — ни одного запроса `/api/status`.

Запуск: python tests/browser/test_b15_polling.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from demo_seed import seed_demo  # noqa: E402
from waits import wait_map_v3_ready, wait_card_open  # noqa: E402
from fetch_tools import FETCH_WRAP  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
SNAP = "/api/v2/snapshot"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def js(page, code: str):
    return page.evaluate(code)


def calls(page, needle: str, since: int = 0) -> int:
    """Сколько запросов, адрес которых содержит needle, начато после журнала длины since."""
    return js(page, "window.__fetchLog.slice(%d).filter(e => e.url.includes(%r)).length" % (since, needle))


def log_len(page) -> int:
    return js(page, "window.__fetchLog.length")


def live_timers(page) -> list:
    return js(page, "window.__intervals.filter(i => !i.cleared && i.ms <= 10000).map(i => i.ms)")


def set_hidden(page, hidden: bool) -> None:
    js(page, """(()=>{
        Object.defineProperty(document, 'hidden', {configurable: true, get: () => %s});
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => %r});
        document.dispatchEvent(new Event('visibilitychange'));
    })()""" % ("true" if hidden else "false", "hidden" if hidden else "visible"))


def main() -> int:
    with Harness() as h:
        info = seed_demo(h)
        shr1 = info["shr1"]["id"]
        bp = open_browser(h.base_url, init_script=FETCH_WRAP)
        page = bp.page
        page.wait_for_timeout(1200)
        js(page, APP + ".toggleUiSet()")
        page.wait_for_timeout(500)

        # ===== 1. один общий таймер =====
        for name in ("Обзор", "Отчёты", "Структура", "План v3", "Настройки"):
            page.locator("nav button:visible", has_text=name).click()
            page.wait_for_timeout(700)
        timers = live_timers(page)
        check(timers == [5000], f"один общий таймер 5 с за обход всех вкладок v2 (живые: {timers})")

        # ===== 2. скрытая вкладка =====
        page.locator("nav button:visible", has_text="Обзор").click()
        page.wait_for_timeout(1500)
        set_hidden(page, True)
        page.wait_for_timeout(300)
        since = log_len(page)
        page.wait_for_timeout(6500)          # больше периода тика (5 с)
        n_hidden = calls(page, "/api/", since)
        check(n_hidden == 0, f"скрытая вкладка не опрашивает сервер за 6,5 с ({n_hidden} запросов)")
        since = log_len(page)
        set_hidden(page, False)
        page.wait_for_timeout(900)
        n_back = calls(page, SNAP, since)
        check(n_back == 1, f"при возвращении на вкладку — одно обновление снимка ({n_back})")
        since = log_len(page)
        page.wait_for_timeout(5600)
        check(calls(page, SNAP, since) >= 1, "после возврата обычный опрос продолжается")

        # ===== 3. один незавершённый запрос каждого типа =====
        js(page, "window.__delay = {'%s': 2500}; window.__maxInflight = {}" % SNAP)
        since = log_len(page)
        for _ in range(5):
            js(page, APP + "._pollTick()")
            page.wait_for_timeout(60)
        page.wait_for_timeout(300)
        started = calls(page, SNAP, since)
        check(started == 1, f"«Обзор»: 5 тиков при не пришедшем ответе — один запрос снимка ({started})")
        page.wait_for_timeout(2600)
        js(page, "window.__delay = {}")

        page.locator("nav button:visible", has_text="Структура").click()
        page.wait_for_timeout(1200)
        js(page, "(()=>{ %s.openInspector('node', %d); })()" % (APP, shr1))
        page.wait_for_timeout(1200)
        # живые числа карточки узла — запрос с перечнем точек в адресе: берём его из журнала
        live_url = js(page, "(window.__fetchLog.filter(e => e.url.includes('%s?point_ids=')).pop() || {}).url" % SNAP)
        check(bool(live_url), f"карточка узла запрашивает живые числа ({live_url})")
        js(page, "window.__delay = {'%s': 2500, %r: 2500}; window.__maxInflight = {}" % (SNAP, live_url))
        since = log_len(page)
        for _ in range(5):
            js(page, APP + "._pollTick()")
            page.wait_for_timeout(60)
        page.wait_for_timeout(300)
        mx = js(page, "window.__maxInflight")
        worst = max(mx.values()) if mx else 0
        started = calls(page, SNAP, since)
        # дерево («Структура») и живые числа карточки — два разных типа запроса: по одному
        check(worst <= 1 and 1 <= started <= 2,
              f"«Структура»: по одному незавершённому запросу каждого типа (макс. {worst}, начато {started})")
        page.wait_for_timeout(2600)
        js(page, "window.__delay = {}")

        # ===== 4. цифры не мигают =====
        page.locator("nav button:visible", has_text="Обзор").click()
        page.wait_for_timeout(1500)
        js(page, """(()=>{
            window.__texts = [];
            const tiles = document.querySelector('.tiles');
            window.__h0 = tiles.getBoundingClientRect().height;
            window.__heights = [];
            const rec = () => {
                const v = Array.from(tiles.querySelectorAll('.val')).map(e => e.textContent.trim());
                window.__texts.push(v.join('|'));
                window.__heights.push(tiles.getBoundingClientRect().height);
            };
            window.__mo = new MutationObserver(rec);
            window.__mo.observe(tiles, {subtree: true, childList: true, characterData: true, attributes: true});
            rec();
        })()""")
        for _ in range(4):
            js(page, APP + "._pollTick()")
            page.wait_for_timeout(500)
        res = js(page, "({texts: window.__texts, heights: window.__heights, h0: window.__h0})")
        blink = [t for t in res["texts"] if any(p in ("", "—") for p in t.split("|")[:2])]
        check(not blink, f"плитки «Обзора» не пустеют при обновлении ({len(res['texts'])} наблюдений, пустых: {len(blink)})")
        check(all(abs(x - res["h0"]) < 1 for x in res["heights"]),
              "высота блока плиток не прыгает при обновлении")

        # ===== 5. «Настройки» не опрашивают снимок =====
        page.locator("nav button:visible", has_text="Настройки").click()
        page.wait_for_timeout(1200)
        since = log_len(page)
        for _ in range(3):
            js(page, APP + "._pollTick()")
            page.wait_for_timeout(100)
        page.wait_for_timeout(300)
        check(calls(page, SNAP, since) == 0, "«Настройки» v2: снимок не запрашивается")
        check(live_timers(page) == [5000], f"в конце сценария по-прежнему один таймер ({live_timers(page)})")

        # ===== 6. ТЗ 13.7: 20 переключений «План v3 → Обзор → План v3» =====
        page.locator("nav button:visible", has_text="План v3").click()
        wait_map_v3_ready(page)
        js(page, "%s.openInspectorInPlace('node', %d)" % (APP, shr1))
        wait_card_open(page)
        js(page, "_planV3Map.setZoom(-1.5, {animate: false})")
        zoom0 = js(page, "_planV3Map.getZoom()")
        layers0 = js(page, "Object.keys(_planV3Map._layers).length")
        timers0 = js(page, "window.__intervals.filter(i => !i.cleared).length")
        # контейнер карты «Плана v3» — #site-plan-v3-map; у классического «Плана» и «Плана v2»
        # свои карты на странице, их число тоже не должно меняться
        V3C = "document.querySelectorAll('#site-plan-v3-map.leaflet-container, #site-plan-v3-map .leaflet-container').length"
        ALLC = "document.querySelectorAll('.leaflet-container').length"
        containers0, all0 = js(page, V3C), js(page, ALLC)
        check(containers0 == 1, f"«План v3»: один .leaflet-container до переключений ({containers0})")
        for _ in range(20):
            page.locator("nav button:visible", has_text="Обзор").click()
            page.wait_for_function("() => !!document.querySelector('.tiles') && document.querySelector('.tiles').offsetHeight > 0")
            page.locator("nav button:visible", has_text="План v3").click()
            wait_map_v3_ready(page)
        containers, all_c = js(page, V3C), js(page, ALLC)
        layers = js(page, "Object.keys(_planV3Map._layers).length")
        timers = js(page, "window.__intervals.filter(i => !i.cleared).length")
        zoom = js(page, "_planV3Map.getZoom()")
        card = js(page, "(%s.structInspector ? [%s.structInspector.kind, %s.structInspector.id] : null)" % (APP, APP, APP))
        check(containers == 1, f"после 20 переключений у «Плана v3» ровно один .leaflet-container ({containers})")
        check(all_c == all0, f"число .leaflet-container на странице не растёт ({all0} → {all_c})")
        check(layers == layers0, f"число слоёв карты не растёт: было {layers0}, стало {layers}")
        check(timers == timers0, f"число живых таймеров не растёт: было {timers0}, стало {timers}")
        check(abs(zoom - zoom0) < 1e-6, f"зум сохранился ({zoom0} → {zoom})")
        check(card == ["node", shr1], f"открытая карточка элемента сохранилась ({card})")

        # скрытая вкладка 12 с: ни одного запроса /api/status (и вообще ни одного /api/)
        set_hidden(page, True)
        page.wait_for_timeout(300)
        since = log_len(page)
        page.wait_for_timeout(12000)
        n_status = calls(page, "/api/status", since)
        n_all = calls(page, "/api/", since)
        check(n_status == 0 and n_all == 0,
              f"скрытая вкладка 12 с: /api/status — {n_status}, всех запросов /api/ — {n_all}")
        set_hidden(page, False)

        check(not bp.errors, f"консоль без ошибок ({bp.errors[:3]})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
