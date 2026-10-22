"""Партия 15, П14: после обновления службы открытая страница остаётся старой — настоящий
Chromium, настоящее Flask-приложение.

Что проверяется:
  1. Все скрипты и стили, которые браузер реально загрузил, пришли с `?v=<версия службы>`
     (сервер дописывает версию при отдаче `/`), а страница знает версию загрузки
     (`<meta name="wbem-build">`).
  2. Версия в ответе `/api/status` совпала — плашки нет.
  3. Версия в ответе `/api/status` подменена (служба «обновилась») — плашка «Служба обновлена
     до X — перезагрузите страницу» с кнопкой; страница сама НЕ перезагружается (введённое в
     форму и состояние страницы на месте).
  4. Плашка появляется в обоих наборах вкладок и на вкладке без «Дашборда»: общий опрос раз в
     30 с берёт `/api/status` и там, где раньше его не брал (классика, «Расход»).
  5. Кнопка перезагружает страницу: после неё версия загрузки = текущая, плашки нет.
  6. Нет метки версии (страница отдана не сервером) — плашка не появляется (сравнивать не с чем).
  7. Узкий экран 390 px: плашка и кнопка целиком на экране, горизонтальной прокрутки нет.
  8. Консоль чистая.

Запуск: python tests/browser/test_b27_stale_page.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from fetch_tools import FETCH_WRAP  # noqa: E402
from wb_energy_meter import __version__  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
BANNER = "[data-testid=stale-page-banner]"
FAKE = "9.9.9"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def js(page, code: str):
    return page.evaluate(code)


def banner_visible(page) -> bool:
    loc = page.locator(BANNER)
    return loc.count() > 0 and loc.first.is_visible()


def wait_for(page, expr: str, ms: int = 8000) -> bool:
    try:
        page.wait_for_function(expr, timeout=ms)
        return True
    except Exception:
        return False


def spoof_status_version(page, version: str) -> None:
    """Ответ `/api/status` с другой версией — как после обновления службы (без обрыва сети)."""
    def handler(route):
        resp = route.fetch()
        try:
            data = resp.json()
            data["version"] = version
            route.fulfill(response=resp, body=json.dumps(data), headers={**resp.headers, "content-type": "application/json"})
        except Exception:
            route.fulfill(response=resp)
    page.route("**/api/status", handler)


def main() -> int:
    with Harness() as h:
        bp = open_browser(h.base_url, init_script=FETCH_WRAP)
        page = bp.page
        page.wait_for_timeout(1200)

        # ===== 1. Версия в адресах и в метке =====
        res = js(page, """performance.getEntriesByType('resource').map(e=>e.name)
                           .filter(n=>n.includes('/static/'))""")
        js_res = [n for n in res if "/static/js/" in n]
        check(len(js_res) >= 15 and all(n.endswith("?v=" + __version__) for n in js_res),
              f"все загруженные /static/js/ — с ?v={__version__} ({len(js_res)} шт.)")
        vend = [n for n in res if "/static/vendor/" in n]
        check(len(vend) >= 4 and all(n.endswith("?v=" + __version__) for n in vend),
              f"вендорные скрипты и стили — тоже ({len(vend)} шт.)")
        check(js(page, APP + ".pageVersion") == __version__,
              f"страница помнит версию загрузки ({js(page, APP + '.pageVersion')!r})")
        js(page, APP + ".loadStatus()")
        wait_for(page, "() => %s.version" % APP)
        check(js(page, APP + ".version") == __version__ and not banner_visible(page),
              "версия /api/status совпала — плашки нет")

        # ===== 2. Версия подменена: плашка, без автоперезагрузки =====
        js(page, "window.__marker = 'старая страница'")
        spoof_status_version(page, FAKE)
        js(page, APP + ".loadStatus()")
        check(wait_for(page, "() => %s.pageStale === true" % APP), "версия /api/status другая — страница считается устаревшей")
        page.wait_for_timeout(300)
        check(banner_visible(page), "плашка видна")
        txt = page.locator(BANNER).first.inner_text()
        check(f"Служба обновлена до {FAKE} — перезагрузите страницу" in txt.replace("\n", " "),
              f"текст плашки ({txt.strip()!r})")
        check(page.locator(BANNER + " button").first.is_visible()
              and page.locator("[data-testid=stale-page-reload]").count() == 1,
              "в плашке есть кнопка «Перезагрузить»")
        page.wait_for_timeout(1500)
        check(js(page, "window.__marker") == "старая страница",
              "страница сама не перезагрузилась (ввод в форме не стирается)")

        # ===== 3. Классика, вкладка «Расход»: общий опрос тоже берёт /api/status =====
        js(page, "(()=>{ const a=%s; if(a.uiSet!=='classic') a.toggleUiSet(); a.tab='consumption'; })()" % APP)
        page.wait_for_timeout(300)
        page.unroute("**/api/status")
        js(page, "(()=>{ const a=%s; a.version=%r; })()" % (APP, __version__))
        check(not banner_visible(page), "классика: версия совпала — плашки нет")
        spoof_status_version(page, FAKE)
        js(page, "(()=>{ const a=%s; a._pollN=5; a._pollTick(); })()" % APP)
        check(wait_for(page, "() => %s.pageStale === true" % APP, 5000),
              "классика, «Расход»: тик общего опроса увидел новую версию")
        page.wait_for_timeout(300)
        check(banner_visible(page), "классика: плашка видна")

        # ===== 4. Узкий экран (новый набор: классический на телефоне намеренно шириной 1100) =====
        js(page, "(()=>{ const a=%s; if(a.uiSet!=='v2') a.toggleUiSet(); })()" % APP)
        page.wait_for_timeout(500)
        check(banner_visible(page), "новый набор: плашка видна")
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        box = page.locator(BANNER).first.bounding_box()
        check(box is not None and box["x"] >= -1 and box["x"] + box["width"] <= 391,
              f"390 px: плашка целиком по ширине экрана ({box})")
        check(js(page, "document.documentElement.scrollWidth") <= 390, "390 px: горизонтальной прокрутки нет")
        btn = page.locator("[data-testid=stale-page-reload]").first.bounding_box()
        check(btn is not None and btn["x"] >= -1 and btn["x"] + btn["width"] <= 391, "390 px: кнопка на экране")
        page.set_viewport_size({"width": 1400, "height": 1000})

        # ===== 5. Кнопка перезагружает страницу =====
        page.unroute("**/api/status")
        with page.expect_navigation():
            page.locator("[data-testid=stale-page-reload]").first.click()
        page.wait_for_timeout(1200)
        check(js(page, "window.__marker") is None, "после кнопки страница загружена заново")
        check(js(page, APP + ".pageVersion") == __version__, "после перезагрузки версия загрузки = текущая")
        js(page, APP + ".loadStatus()")
        wait_for(page, "() => %s.version" % APP)
        check(not banner_visible(page) and js(page, APP + ".pageStale") is False,
              "после перезагрузки плашки нет")

        # ===== 6. Нет метки версии — сравнивать не с чем =====
        js(page, "(()=>{ const a=%s; a.pageVersion=''; })()" % APP)
        spoof_status_version(page, FAKE)
        js(page, APP + ".loadStatus()")
        wait_for(page, "() => %s.version === %r" % (APP, FAKE))
        page.wait_for_timeout(300)
        check(js(page, APP + ".pageStale") is False and not banner_visible(page),
              "без метки версии плашка не появляется")
        page.unroute("**/api/status")

        errs = [e for e in bp.errors if "Failed to load resource" not in e]
        check(not errs, f"консоль без ошибок приложения ({errs[:3]})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
