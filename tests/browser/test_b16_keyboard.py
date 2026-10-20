"""Партия 13, 13.6 (§8.5): доступность — клавиатура, фокус, Escape, подписи.

Настоящий Chromium, настоящее Flask-приложение, три ширины (≥1280, 768–1279, <768):

  1. У всех видимых кнопок, ссылок и полей на всех экранах обоих наборов (и в карточках
     «Структуры» / «Плана v3») есть доступное имя — текст, aria-label или подпись.
  2. Видимый фокус: у элемента, на который встали клавишей Tab, есть контур (:focus-visible).
  3. Список «Структуры»: строка берётся Tab-ом, Enter открывает карточку, стрелки/Home/End
     ходят по строкам; Escape закрывает карточку. На узких ширинах карточка — выдвижная
     панель: при открытии фокус уходит в неё, при Escape — возвращается на строку.
  4. Escape из текстового поля карточки: первое нажатие только уводит фокус на панель
     (черновик не теряется), второе закрывает.
  5. «План v3»: строки панели доступны с клавиатуры; инструмент «+ Линия» отменяется Escape;
     положение элемента — поля X/Y вместо перетаскивания: ввод чисел сохраняет, неверные
     числа — понятная ошибка. Линия между существующими элементами — форма «От / К» в
     «Структуре» (кнопка, два списка, «Создать черновик линии») целиком с клавиатуры.
  6. prefers-reduced-motion: переходы и анимации сводятся к нулю.

Запуск: python tests/browser/test_b16_keyboard.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from demo_seed import seed_demo  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []

# Имя элемента: aria-label / aria-labelledby / <label> / текст с буквами или цифрами / title —
# в том же порядке, что и в расчёте доступного имени. Пустое имя у видимого элемента — нарушение.
UNNAMED = r"""
(() => {
  const out = [];
  const name = (el) => {
    const al = el.getAttribute('aria-label'); if (al && al.trim()) return al.trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(i => (document.getElementById(i) || {}).textContent || '').join(' ').trim(); if (t) return t; }
    if (el.labels && el.labels.length) { const t = Array.from(el.labels).map(l => l.textContent).join(' ').trim(); if (t) return t; }
    // текст — имя только у кнопок и ссылок; у полей и списков текст внутри (option) именем не служит
    if (!/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) {
      const t = (el.textContent || '').replace(/[\s ]+/g, ' ').trim();
      if (t.replace(/[^\p{L}\p{N}]/gu, '')) return t;
    }
    const ti = el.getAttribute('title'); if (ti && ti.trim()) return ti.trim();
    return '';
  };
  const vis = (el) => !!(el.offsetParent || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
  document.querySelectorAll('button, [role=button], a[href], input:not([type=hidden]), select, textarea').forEach(el => {
    if (!vis(el)) return;
    if (el.closest('.leaflet-container') && !el.closest('.leaflet-control')) return;
    if (name(el)) return;
    const ph = el.getAttribute('placeholder');
    if (ph && /^(INPUT|TEXTAREA)$/.test(el.tagName)) return;
    out.push((el.tagName + ' ' + (el.getAttribute('data-testid') || '') + ' ' + el.outerHTML.slice(0, 90)).trim());
  });
  return out;
})()
"""


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def js(page, code: str):
    return page.evaluate(code)


def fire(page, code: str) -> None:
    page.evaluate("(()=>{ " + code + "; })()")


def focused(page) -> str:
    """Краткое описание элемента в фокусе: data-testid / data-drawer / тег."""
    return js(page, """(() => { const a = document.activeElement; if (!a) return '';
        return (a.hasAttribute('data-drawer') ? 'drawer:' + a.getAttribute('data-drawer') : '') || a.getAttribute('data-testid')
               || a.tagName.toLowerCase(); })()""")


def nav(page, name: str) -> None:
    page.locator("nav button:visible", has_text=name).first.click()
    page.wait_for_timeout(1200)


def audit_names(page, label: str) -> None:
    bad = js(page, UNNAMED)
    check(not bad, f"{label}: у всех видимых кнопок и полей есть имя ({len(bad)} без имени {bad[:3]})")


def main() -> int:
    with Harness() as h:
        info = seed_demo(h)
        shr1 = info["shr1"]["id"]

        # ===== 1. доступные имена на всех экранах =====
        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1200)
        for name in ("Дашборд", "План", "Расход", "Настройки", "Отчёты"):
            nav(page, name)
            audit_names(page, f"классика «{name}»")
        fire(page, APP + ".toggleUiSet()")
        page.wait_for_timeout(500)
        for name in ("Обзор", "Отчёты", "Структура", "План v3", "Настройки"):
            nav(page, name)
            audit_names(page, f"v2 «{name}»")
        nav(page, "Структура")
        for kind, ident in (("node", shr1), ("point", info["points"]["dev-light"]["id"]),
                            ("edge", info["e_light"]["id"]), ("group", info["group"].id)):
            fire(page, APP + ".openInspector(%r, %d)" % (kind, ident))
            page.wait_for_timeout(1000)
            audit_names(page, f"v2 «Структура», карточка «{kind}»")
        fire(page, APP + ".openCreatePanel('edge')")
        page.wait_for_timeout(500)
        audit_names(page, "v2 «Структура», форма «+ Линия (черновик)»")
        fire(page, APP + ".closeCreatePanel()")
        fire(page, APP + ".openInspector('node', %d)" % shr1)
        page.wait_for_timeout(1000)
        fire(page, APP + ".nodeMeterOpen = true")
        page.wait_for_timeout(500)
        audit_names(page, "v2 «Структура», карточка узла с открытой формой «+ Счётчик»")
        fire(page, APP + ".nodeMeterOpen = false")
        nav(page, "План v3")
        for kind, ident in (("node", shr1), ("edge", info["e_light"]["id"])):
            fire(page, APP + ".openInspectorInPlace(%r, %d)" % (kind, ident))
            page.wait_for_timeout(1000)
            audit_names(page, f"v2 «План v3», карточка «{kind}»")
        fire(page, APP + ".closeInspector()")
        for tool in ("node", "zone"):
            fire(page, APP + ".planV3SetTool(%r)" % tool)
            page.wait_for_timeout(500)
            audit_names(page, f"v2 «План v3», инструмент «{tool}»")
        fire(page, APP + ".planV3CancelTool()")

        # ===== 2. видимый фокус =====
        nav(page, "Обзор")
        page.keyboard.press("Tab")
        outline = js(page, """(() => { const a = document.activeElement; const c = getComputedStyle(a);
            return {st: c.outlineStyle, w: parseFloat(c.outlineWidth), tag: a.tagName, vis: a.matches(':focus-visible')}; })()""")
        check(outline["vis"] and outline["st"] != "none" and outline["w"] >= 2,
              f"Tab: у элемента в фокусе виден контур ({outline})")
        bp.close()

        # ===== 3–4. клавиатура и Escape на трёх ширинах =====
        for label, vp, narrow in (("≥1280", {"width": 1400, "height": 1000}, False),
                                  ("768–1279", {"width": 900, "height": 900}, True),
                                  ("<768", {"width": 390, "height": 800}, True)):
            bp = open_browser(h.base_url, viewport=vp)
            page = bp.page
            page.wait_for_timeout(1200)
            fire(page, APP + ".toggleUiSet()")
            page.wait_for_timeout(500)
            nav(page, "Структура")
            rows = page.locator("[data-testid=scheme-node]")
            check(rows.count() >= 3, f"[{label}] дерево схемы: строк {rows.count()}")
            check(rows.first.get_attribute("tabindex") == "0" and rows.first.get_attribute("role") == "button",
                  f"[{label}] строка дерева берётся Tab-ом и объявлена кнопкой")

            rows.first.focus()
            page.keyboard.press("ArrowDown")
            second = js(page, "document.activeElement.getAttribute('data-node-id')")
            first_id = rows.first.get_attribute("data-node-id")
            check(second is not None and second != first_id, f"[{label}] ArrowDown переносит фокус на следующую строку")
            page.keyboard.press("End")
            last = js(page, "document.activeElement.getAttribute('data-node-id')")
            page.keyboard.press("Home")
            home = js(page, "document.activeElement.getAttribute('data-node-id')")
            check(home == first_id and last != first_id, f"[{label}] Home/End — первая и последняя строки")

            page.keyboard.press("ArrowDown")
            row_id = js(page, "document.activeElement.getAttribute('data-node-id')")
            page.keyboard.press("Enter")
            page.wait_for_timeout(900)
            opened = js(page, "(() => { const i = %s.structInspector; return i && [i.kind, String(i.id)]; })()" % APP)
            check(opened == ["node", row_id], f"[{label}] Enter на строке открывает карточку ({opened})")
            if narrow:
                check(focused(page) == "drawer:struct",
                      f"[{label}] на узкой ширине фокус ушёл в выдвижную панель ({focused(page)})")
            else:
                check(js(page, "document.activeElement.getAttribute('data-node-id')") == row_id,
                      f"[{label}] на широкой ширине фокус остался на строке")

            # Escape из поля: черновик цел, первое нажатие — на панель
            name_in = page.locator("[data-drawer=struct] input[aria-label='Название элемента']")
            if name_in.count():
                name_in.first.click()
                name_in.first.fill("Черновик названия")
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                still = js(page, "!!%s.structInspector" % APP)
                draft = js(page, "%s.structInspector && %s.structInspector.nameDraft" % (APP, APP))
                check(still and draft == "Черновик названия" and focused(page) == "drawer:struct",
                      f"[{label}] Escape из поля: карточка открыта, черновик цел, фокус на панели ({focused(page)})")
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            check(js(page, "%s.structInspector === null" % APP), f"[{label}] Escape закрывает карточку")
            back = js(page, "document.activeElement.getAttribute('data-node-id')")
            check(back == row_id, f"[{label}] после закрытия фокус вернулся на строку ({back})")
            check(not bp.errors, f"[{label}] консоль без ошибок ({bp.errors[:2]})")
            bp.close()

        # ===== 5. «План v3»: строки, значки на карте, X/Y =====
        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1200)
        fire(page, APP + ".toggleUiSet()")
        page.wait_for_timeout(500)
        nav(page, "План v3")
        page.wait_for_timeout(800)
        prow = page.locator("[data-testid=planv3-node-row]").first
        check(page.locator("[data-drawer=plan] [data-kb-row]").count() >= 5,
              "«План v3»: строки панели (элементы, линии, зоны, точки) доступны с клавиатуры")
        prow.locator("xpath=..").focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(1000)
        check(js(page, "%s.structInspector && %s.structInspector.kind" % (APP, APP)) == "node",
              "«План v3»: Enter на строке панели открывает карточку элемента")

        # Инструмент «+ Линия» на карте — по кликам; Escape его отменяет
        page.locator("button", has_text="+ Линия").click()
        check(js(page, "%s.planV3Tool" % APP) == "edge", "«План v3»: инструмент «+ Линия» включается кнопкой")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        check(js(page, "!%s.planV3Tool && !%s.planV3EdgeFirstItem" % (APP, APP)),
              "«План v3»: Escape отменяет инструмент «+ Линия»")

        # X/Y вместо перетаскивания
        fire(page, APP + ".openInspectorInPlace('node', %d)" % shr1)
        page.wait_for_timeout(1000)
        xy = page.locator("[data-testid=planv3-xy]")
        check(xy.count() == 1 and xy.is_visible(), "«План v3»: в карточке элемента есть поля X/Y")
        x0 = page.locator("[data-testid=planv3-xy-x]").input_value()
        check(x0 == "620", f"«План v3»: X подставлен из положения элемента ({x0!r})")
        page.fill("[data-testid=planv3-xy-x]", "700")
        page.fill("[data-testid=planv3-xy-y]", "340")
        page.locator("[data-testid=planv3-xy-apply]").click()
        page.wait_for_timeout(900)
        item = js(page, "(%s.planV3NodeItem(%d) || {}).geometry" % (APP, shr1))
        check(item and item["x"] == 700 and item["y"] == 340, f"«План v3»: X/Y сохраняют положение ({item})")
        saved = h.client.get("/api/v2/plans/%d" % info["plan"]["id"]).get_json()
        sv = [i for i in saved["items"] if i.get("node_id") == shr1][0]["geometry"]
        check(sv["x"] == 700 and sv["y"] == 340, f"«План v3»: положение сохранено на сервере ({sv})")
        page.fill("[data-testid=planv3-xy-x]", "999999")
        page.locator("[data-testid=planv3-xy-apply]").click()
        page.wait_for_timeout(300)
        err = page.locator("[data-testid=planv3-xy-error]")
        check(err.is_visible() and "от 0 до" in err.inner_text(), "«План v3»: неверный X — понятная ошибка, положение не менялось")
        page.fill("[data-testid=planv3-xy-x]", "")
        page.locator("[data-testid=planv3-xy-apply]").click()
        page.wait_for_timeout(300)
        check(err.is_visible(), "«План v3»: пустое поле — ошибка, а не 0")
        check(not bp.errors, f"«План v3»: консоль без ошибок ({bp.errors[:2]})")
        bp.close()

        # ===== 5б. линия между существующими элементами — форма «От / К» в «Структуре» =====
        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1200)
        fire(page, APP + ".toggleUiSet()")
        page.wait_for_timeout(500)
        nav(page, "Структура")
        btn = page.locator("button", has_text="+ Линия (черновик)")
        btn.focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        frm = page.locator("select[aria-label='От элемента']")
        to = page.locator("select[aria-label='К элементу']")
        check(frm.count() == 1 and to.count() == 1,
              "«Структура»: форма линии открывается с клавиатуры, поля «От элемента» / «К элементу» подписаны")
        frm.focus()
        page.keyboard.press("ArrowDown")
        to.focus()
        page.keyboard.press("ArrowDown")
        page.keyboard.press("ArrowDown")
        pick = js(page, "[%s.edgeForm.from_node_id, %s.edgeForm.to_node_id]" % (APP, APP))
        check(all(isinstance(v, int) for v in pick) and pick[0] != pick[1],
              f"«Структура»: концы линии выбираются стрелками ({pick})")
        submit = page.locator("button", has_text="Создать черновик линии")
        submit.focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(900)
        msg = js(page, "[%s.structCreateOk, %s.structCreateError]" % (APP, APP))
        check(bool(msg[0]) and not msg[1], f"«Структура»: «Создать черновик линии» с клавиатуры отработала ({msg})")
        bp.close()

        # ===== 6. prefers-reduced-motion =====
        bp = open_browser(h.base_url)
        page = bp.page
        page.emulate_media(reduced_motion="reduce")
        page.wait_for_timeout(1200)
        fire(page, APP + ".toggleUiSet()")
        page.wait_for_timeout(500)
        nav(page, "Обзор")
        dur = js(page, """(() => {
            const el = document.createElement('div'); el.className = 'is-stale is-loading'; document.body.appendChild(el);
            const sp = document.createElement('span'); sp.className = 'spin'; document.body.appendChild(sp);
            const out = {t: getComputedStyle(el).transitionDuration, a: getComputedStyle(sp).animationDuration};
            el.remove(); sp.remove(); return out; })()""")

        def tiny(v: str) -> bool:
            try:
                return all(float(p.strip().rstrip("s")) < 0.01 for p in v.split(","))
            except ValueError:
                return False

        check(tiny(dur["t"]) and tiny(dur["a"]),
              f"prefers-reduced-motion: переходы и анимации сведены к нулю ({dur})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
