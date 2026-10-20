"""Партия 13, 13.5 (§10): дисциплина опроса — настоящий Chromium, настоящее Flask-приложение.

  1. Один общий таймер: за обход всех вкладок набора v2 живых setInterval с периодом
     ≤ 10 с ровно один (тик 5 с); новых таймеров экраны не заводят.
  2. Скрытая вкладка не опрашивает сервер; при возвращении — ровно одно обновление.
  3. Один незавершённый запрос каждого типа: пока ответ на снимок не пришёл, новые тики
     запрос не повторяют (ни «Обзор», ни «Структура», ни карточка узла).
  4. Цифры не мигают: пока идут обновления, плитки «Обзора» не пустеют и не прыгают
     по высоте, а прежние значения остаются до прихода новых.
  5. Вкладка «Настройки» набора v2 не опрашивает снимок (только статус раз в 30 с).

Запуск: python tests/browser/test_b15_polling.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from demo_seed import seed_demo  # noqa: E402
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

        check(not bp.errors, f"консоль без ошибок ({bp.errors[:3]})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
