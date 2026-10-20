"""Партия 13, 13.1/13.3, A36: адаптивность набора v2 — настоящий Chromium,
настоящее Flask-приложение, демо-данные (demo_seed.py).

  1. viewport: в разметке (то, что отдаёт сервер) — `width=device-width,
     initial-scale=1`; в новом наборе так и остаётся, у классического набора JS
     возвращает прежний `width=1100` (классика на телефоне не стала хуже).
  2. Ширины 360 / 768 / 1280 / 1920 × две темы × экраны v2 («Обзор», «Отчёты»,
     «Структура», «План v3», «Настройки»): у страницы нет горизонтального
     скролла (`documentElement.scrollWidth <= innerWidth`); при провале —
     список элементов, выпирающих за правый край. Снимки — в tests/browser/_shots/.
  3. Карточка: на ≥1280 — рядом (не выдвижная), на 768–1279 — выдвижная панель
     справа, на <768 — нижняя; открытая панель целиком внутри окна, фокус внутри
     неё, «Закрыть» закрывает; «Структура»: поиск достижим, панель закрыта, пока
     ничего не выбрано; «План v3»: кнопка «Панель» есть только на узких экранах.
  4. Смена ширины не сбрасывает выбранную карточку и положение карты (зум, центр).
  5. Зоны касания на <768: кнопки шапки и навигации, «Панель», «Закрыть», строки
     дерева — не меньше 44×44 CSS px.
  6. Классика при 1280: шапка в одну строку (как раньше), вкладки на месте.

Запуск: python tests/browser/test_b14_responsive.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from demo_seed import seed_demo  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
WIDTHS = (360, 768, 1280, 1920)
HEIGHT = {360: 740, 768: 1024, 1280: 860, 1920: 1000}
SCREENS = (("Обзор", "overview"), ("Отчёты", "reports"), ("Структура", "structure"),
           ("План v3", "planv3"), ("Настройки", "settings"))
fails: list[str] = []

OFFENDERS_JS = """
(() => {
  const W = window.innerWidth, out = [];
  const clipped = (el) => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox !== 'visible' && p.getBoundingClientRect().right <= W + 1) return true;
      if (getComputedStyle(p).position === 'fixed') return true;
    }
    return false;
  };
  for (const el of document.querySelectorAll('body *')) {
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > W + 1 && !clipped(el) && getComputedStyle(el).position !== 'fixed')
      out.push(el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ?
        '.' + el.className.trim().split(/\\s+/).join('.') : '') + ' right=' + Math.round(r.right));
  }
  return out.slice(0, 8);
})()
"""


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def goto_tab(page, label):
    page.locator("nav button:visible", has_text=label).first.click()
    page.wait_for_timeout(1200)


def set_theme(page, theme):
    if page.evaluate(APP + ".theme") != theme:
        page.evaluate(APP + ".toggleTheme()")
        page.wait_for_timeout(150)


def to_v2(page):
    if page.evaluate(APP + ".uiSet") != "v2":
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(500)


def box(page, sel):
    return page.evaluate(
        "(s)=>{const e=document.querySelector(s); if(!e) return null; const r=e.getBoundingClientRect();"
        "const cs=getComputedStyle(e); return {l:r.left,t:r.top,r:r.right,b:r.bottom,w:r.width,h:r.height,"
        "pos:cs.position,vis:cs.visibility,disp:cs.display, open:e.classList.contains('open')};}", sel)


def main() -> int:
    with Harness() as h:
        info = seed_demo(h)
        shr1 = info["shr1"]["id"]

        # ===== 1. viewport =====
        html = h.client.get("/").get_data(as_text=True)
        check('<meta name="viewport" content="width=device-width, initial-scale=1">' in html,
              "в разметке: viewport = width=device-width, initial-scale=1")

        # ===== 2. нет горизонтального скролла: ширина × тема × экран =====
        for width in WIDTHS:
            for theme in ("dark", "light"):
                bp = open_browser(h.base_url, viewport={"width": width, "height": HEIGHT[width]})
                page = bp.page
                page.wait_for_timeout(1000)
                set_theme(page, theme)
                to_v2(page)
                for label, key in SCREENS:
                    goto_tab(page, label)
                    sw = page.evaluate("document.documentElement.scrollWidth")
                    iw = page.evaluate("window.innerWidth")
                    ok = sw <= iw
                    extra = "" if ok else " выпирают: " + "; ".join(page.evaluate(OFFENDERS_JS))
                    check(ok, f"{width}px {theme} «{label}»: scrollWidth {sw} ≤ innerWidth {iw}{extra}")
                    bp.screenshot(f"b14_{key}_{theme}_{width}")
                check(not bp.errors, f"{width}px {theme}: консоль без ошибок ({bp.errors[:2]})")
                bp.close()

        # ===== 3–5. панель карточки, зоны касания, смена ширины =====
        for width in (360, 768, 1280):
            bp = open_browser(h.base_url, viewport={"width": width, "height": HEIGHT[width]})
            page = bp.page
            page.wait_for_timeout(1000)
            to_v2(page)
            narrow, phone = width < 1280, width < 768

            # --- План v3
            goto_tab(page, "План v3")
            page.wait_for_timeout(800)
            toggle = box(page, "[data-testid=planv3-panel-toggle]")
            check((toggle is not None and toggle["disp"] != "none") == narrow,
                  f"{width}px: кнопка «☰ Панель» {'есть' if narrow else 'скрыта'} ({toggle and toggle['disp']})")
            d = box(page, "[data-drawer=plan]")
            if narrow:
                check(d["vis"] == "hidden" and not d["open"],
                      f"{width}px: пока карточка не выбрана и панель не открыта — панель скрыта")
            else:
                check(d["pos"] != "fixed" and d["vis"] == "visible",
                      f"{width}px: панель стоит рядом с картой (position={d['pos']})")
                check(d["l"] > 0 and page.evaluate("document.querySelector('#site-plan-v3-map').getBoundingClientRect().right")
                      <= d["l"] + 1, "…справа от карты, не поверх неё")
            zoom0 = page.evaluate("_planV3Map.getZoom()")
            page.evaluate(f"{APP}.openInspectorInPlace('node', {shr1})")
            page.wait_for_timeout(1500)
            d = box(page, "[data-drawer=plan]")
            check(d["vis"] == "visible" and (d["open"] or not narrow), f"{width}px: карточка щита открыта в панели")
            if narrow:
                inside = d["l"] >= -1 and d["r"] <= width + 1 and d["b"] <= HEIGHT[width] + 1 and d["t"] >= -1
                check(inside, f"{width}px: панель целиком в окне ({round(d['l'])},{round(d['t'])})–({round(d['r'])},{round(d['b'])})")
                if phone:
                    check(abs(d["w"] - width) <= 1 and abs(d["b"] - HEIGHT[width]) <= 1,
                          f"{width}px: панель — нижняя на всю ширину ({round(d['w'])}×{round(d['h'])})")
                    check(d["h"] <= HEIGHT[width] * 0.65, "…и не выше ~62% окна, карта остаётся видна")
                else:
                    check(d["r"] >= width - 1 and d["w"] <= 460, f"{width}px: панель справа, ширина ≤ 460 ({round(d['w'])})")
                focus_in = page.evaluate("(()=>{const d=document.querySelector('[data-drawer=plan]');"
                                         "return d.contains(document.activeElement)})()")
                check(focus_in, f"{width}px: фокус перенесён в открывшуюся панель")
            # зум не сброшен открытием карточки
            zoom1 = page.evaluate("_planV3Map.getZoom()")
            check(abs(zoom1 - zoom0) < 1e-6, f"{width}px: открытие карточки не меняет зум карты ({zoom0:.3f} → {zoom1:.3f})")

            # --- смена ширины при открытой карточке: карточка и зум на месте
            if width == 1280:
                page.set_viewport_size({"width": 768, "height": 1024})
                page.wait_for_timeout(700)
                sel_id = page.evaluate(APP + ".structInspector && " + APP + ".structInspector.id")
                zoom2 = page.evaluate("_planV3Map.getZoom()")
                dd = box(page, "[data-drawer=plan]")
                check(sel_id == shr1 and dd["vis"] == "visible" and dd["pos"] == "fixed",
                      "1280 → 768 при открытой карточке: карточка осталась, панель стала выдвижной")
                check(abs(zoom2 - zoom0) < 1e-6, f"…зум карты не сбросился ({zoom0:.3f} → {zoom2:.3f})")
                page.set_viewport_size({"width": 1280, "height": 860})
                page.wait_for_timeout(700)
                dd = box(page, "[data-drawer=plan]")
                check(page.evaluate(APP + ".structInspector && " + APP + ".structInspector.id") == shr1
                      and dd["pos"] != "fixed", "768 → 1280: карточка осталась, панель снова рядом с картой")

            if narrow:
                if phone:
                    for sel, name in (("[data-drawer=plan] [data-testid=drawer-close]", "«Закрыть»"),):
                        b = box(page, sel)
                        check(b and b["h"] >= 44 and b["w"] >= 44, f"{width}px: {name} ≥ 44×44 ({b and (round(b['w']), round(b['h']))})")
                page.locator("[data-drawer=plan] [data-testid=drawer-close]").click()
                page.wait_for_timeout(500)
                d = box(page, "[data-drawer=plan]")
                check(d["vis"] == "hidden" and page.evaluate(APP + ".structInspector") is None,
                      f"{width}px: «Закрыть» убрало панель и сняло выбор")
                # «Панель» открывает обзор элементов без карточки
                page.locator("[data-testid=planv3-panel-toggle]").click()
                page.wait_for_timeout(500)
                d = box(page, "[data-drawer=plan]")
                check(d["vis"] == "visible" and page.locator("[data-testid=planv3-nodes]").is_visible(),
                      f"{width}px: «☰ Панель» показывает список элементов плана")
                page.locator("[data-drawer=plan] [data-testid=drawer-close]").click()
                page.wait_for_timeout(400)
            else:
                page.evaluate(APP + ".closeInspector()")

            # --- Структура
            goto_tab(page, "Структура")
            page.wait_for_timeout(1200)
            search = box(page, "[data-testid=struct-left] input[type=text]")
            check(search is not None and search["r"] <= width + 1 and search["h"] > 0,
                  f"{width}px: поле поиска «Структуры» на экране")
            d = box(page, "[data-drawer=struct]")
            if narrow:
                check(d["vis"] == "hidden", f"{width}px: «Структура» — карточка скрыта, пока ничего не выбрано")
            row = page.locator("[data-testid=scheme-node]", has_text="ЩР-1").first
            if phone:
                rb = row.bounding_box()
                check(rb["height"] >= 44, f"{width}px: строка дерева ≥ 44 px по высоте ({round(rb['height'])})")
            row.click()
            page.wait_for_timeout(1500)
            d = box(page, "[data-drawer=struct]")
            check(d["vis"] == "visible" and page.locator("[data-drawer=struct] [data-testid=node-card]").count() == 1,
                  f"{width}px: клик по дереву открывает карточку щита")
            if narrow:
                check(d["pos"] == "fixed" and d["r"] <= width + 1 and d["l"] >= -1,
                      f"{width}px: карточка «Структуры» — выдвижная панель внутри окна")
                page.locator("[data-drawer=struct] [data-testid=drawer-close]").click()
                page.wait_for_timeout(400)
                check(box(page, "[data-drawer=struct]")["vis"] == "hidden", f"{width}px: «Закрыть» скрывает карточку «Структуры»")
            else:
                check(d["pos"] != "fixed" and d["l"] > search["r"] - 1,
                      f"{width}px: карточка «Структуры» стоит справа от дерева")

            # --- зоны касания на телефоне
            if phone:
                small = page.evaluate("""() => {
                  const bad = [];
                  const sel = 'header .ico-btn, nav button, main .btn, main select, main input[type=text], main input[type=date]';
                  for (const e of document.querySelectorAll(sel)) {
                    if (e.offsetParent === null) continue;
                    const r = e.getBoundingClientRect();
                    if (r.width > 0 && (r.height < 43.5 || r.width < 43.5))
                      bad.push((e.textContent||e.className).trim().slice(0,24) + ' ' + Math.round(r.width) + '×' + Math.round(r.height));
                  }
                  return bad.slice(0, 6);
                }""")
                check(not small, f"{width}px: кнопки/поля на «Структуре» ≥ 44×44 {small}")
            check(not bp.errors, f"{width}px: консоль без ошибок ({bp.errors[:2]})")
            bp.close()

        # ===== 1б. классика: прежний viewport и прежняя шапка =====
        bp = open_browser(h.base_url, viewport={"width": 1280, "height": 860})
        page = bp.page
        page.wait_for_timeout(1000)
        if page.evaluate(APP + ".uiSet") != "classic":
            page.evaluate(APP + ".toggleUiSet()")
            page.wait_for_timeout(500)
        content = page.evaluate("document.querySelector('meta[name=viewport]').content")
        check(content == "width=1100", f"классический набор: viewport возвращён к width=1100 ({content!r})")
        hdr = box(page, "header")
        check(hdr["h"] < 60, f"классика при 1280: шапка в одну строку, как раньше ({round(hdr['h'])} px)")
        labels = page.locator("nav button:visible").all_inner_texts()
        check(labels == ["Дашборд", "План", "Расход", "Настройки", "Отчёты"], f"классика: вкладки на месте {labels}")
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(500)
        content = page.evaluate("document.querySelector('meta[name=viewport]').content")
        check(content == "width=device-width, initial-scale=1", f"новый набор: viewport device-width ({content!r})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
