"""Партия находок 0.22.3, П11: причины мигающих браузерных тестов — по одной
детерминированной проверке на причину (docs/pilot-findings.md, П11).

1. Каскад скрытия `x-show` в Alpine 3.13.0. Вложенный элемент переключается
   «скрыт → показан → скрыт» за один кадр, пока скрытие родителя ждёт кадра:
   вендорный каскад падал «u is not a function», а родитель оставался на
   экране. Так мигал `test_b08_archive_actions` после «Удалить план».
   Проверка: ни одной ошибки, оба элемента скрыты (`display: none`).
2. Вписывание карты «Плана v3» из продолжения обработчика. После
   `await` `this` принадлежит уже убранной из DOM кнопке («Создать план»), её
   `$refs` не видят `planV3Map` — раньше `_planV3TryFit()` молча возвращал
   `false`, карта оставалась в масштабе 0, клик по плану давал «Точка вне
   границ плана» (мигал `test_b02_planv3_clean`). Проверка: вписывание
   выполнилось, масштаб сменился, `_planV3NeedsFit` пуст.
3. Тот же путь целиком: создать план → «+ Элемент схемы» → клик по карте на
   процессоре, замедленном в 6 раз (воспроизводит медленный CI; до правки
   узел не создавался в 14 запусках из 16). Узел появляется в API.

4. «Отчёты»: результат показан дважды подряд, затем меняется «Срез». Alpine успевал
   вычислить выражения внутри убираемого блока результата при `rv2Result = null`, и в
   консоль сыпалось «Cannot read properties of null (reading 'compare_period')»
   (нашла репетиция пилота). Проверка: консоль без ошибок и после смены среза, и после
   перехода на другую вкладку и обратно.

Запуск: python tests/browser/test_b11_flaky_regressions.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser, HOUR  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


CASCADE_JS = """
async () => {
  document.body.insertAdjacentHTML('beforeend',
    '<div id="race" x-data="{p:true,c:true}">' +
    '<div x-show="p" id="race-parent"><span x-show="c" id="race-child">x</span></div></div>');
  await new Promise(r => setTimeout(r, 200));
  const d = Alpine.$data(document.getElementById('race'));
  const tick = () => new Promise(r => queueMicrotask(() => queueMicrotask(r)));
  d.c = false; await tick();   // вложенный скрыт
  d.c = true;  await tick();   // показан
  d.p = false; await tick();   // скрыт родитель
  d.c = false; await tick();   // вложенный скрыт снова — всё в пределах одного кадра
  await new Promise(r => setTimeout(r, 300));
  return [getComputedStyle(document.getElementById('race-child')).display,
          getComputedStyle(document.getElementById('race-parent')).display];
}
"""

# Продолжение обработчика после await, когда его кнопка уже убрана из DOM.
DETACHED_FIT_JS = """
async () => {
  const root = document.querySelector('[x-data]');
  const app = %s;
  app.__probeDetachedFit = async function () {
    await new Promise(r => setTimeout(r, 150));
    window.__probeFit = this._planV3TryFit();
  };
  root.insertAdjacentHTML('beforeend',
    '<button id="probe-btn" type="button" style="display:none" @click="__probeDetachedFit()">p</button>');
  await new Promise(r => setTimeout(r, 200));   // Alpine подхватил кнопку
  _planV3Map.setView([0, 0], 3);                 // карта не вписана
  _planV3NeedsFit = [[0, 0], [1200, 2000]];
  window.__probeFit = null;
  const btn = document.getElementById('probe-btn');
  btn.click();                                   // обработчик ушёл в await
  btn.remove();                                  // как «Создать план» после появления плана
  await new Promise(r => setTimeout(r, 500));
  return {fit: window.__probeFit, needsFit: _planV3NeedsFit, zoom: _planV3Map.getZoom()};
}
""" % APP


def open_planv3(h):
    bp = open_browser(h.base_url)
    page = bp.page
    page.wait_for_timeout(1000)
    page.evaluate(APP + ".uiSet='v2'")
    page.wait_for_timeout(300)
    page.locator("nav button:visible", has_text="План v3").click()
    page.wait_for_timeout(800)
    return bp, page


def main() -> int:
    with Harness() as h:
        # ---- 1. каскад x-show ------------------------------------------------
        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1000)
        display = page.evaluate(CASCADE_JS)
        check(display == ["none", "none"],
              f"вложенные x-show: оба элемента скрыты после «скрыт → показан → скрыт» ({display})")
        check(not bp.errors, f"вложенные x-show: консоль без ошибок ({len(bp.errors)}): "
              + "; ".join(bp.errors[:3]))
        bp.close()

        # ---- 2. вписывание карты из продолжения обработчика -------------------
        h.create_plan("Смоук")
        bp, page = open_planv3(h)
        page.wait_for_timeout(1200)
        res = page.evaluate(DETACHED_FIT_JS)
        check(res["fit"] is True and res["needsFit"] is None and res["zoom"] != 3,
              f"вписывание карты после await при убранной кнопке выполнено ({res})")
        check(not bp.errors, f"вписывание карты: консоль без ошибок ({len(bp.errors)}): "
              + "; ".join(bp.errors[:3]))
        bp.close()

    # ---- 3. создание плана и узла на медленном процессоре ---------------------
    old = os.environ.get("WBEM_BROWSER_CPU_THROTTLE")
    os.environ["WBEM_BROWSER_CPU_THROTTLE"] = "6"
    try:
        for attempt in range(1, 4):
            with Harness() as h:
                bp, page = open_planv3(h)
                if page.locator("text=Создать план").count():
                    page.fill("input[placeholder='Название плана']", "Смоук")
                    page.click("text=Создать план")
                    page.wait_for_timeout(2500)
                page.click("button:has-text('+ Элемент схемы')")
                page.wait_for_timeout(500)
                page.fill("input[placeholder='Название']", "Ввод")
                box = page.locator(".leaflet-container:visible").first.bounding_box()
                page.mouse.click(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.3)
                page.wait_for_timeout(2500)
                nodes = page.evaluate("fetch('/api/v2/topology/nodes').then(r=>r.json())")
                check(len(nodes) == 1,
                      f"процессор ×6, попытка {attempt}: узел создан кликом по карте (узлов: {len(nodes)})")
                check(not bp.errors, f"процессор ×6, попытка {attempt}: консоль без ошибок "
                      f"({len(bp.errors)}): " + "; ".join(bp.errors[:3]))
                bp.close()
    finally:
        if old is None:
            os.environ.pop("WBEM_BROWSER_CPU_THROTTLE", None)
        else:
            os.environ["WBEM_BROWSER_CPU_THROTTLE"] = old

    # ---- 4. «Отчёты»: смена среза при показанном результате --------------------------
    with Harness() as h:
        p1 = h.make_point("P1-RV2", "Станок 1")
        h.bind_meter(p1["id"], "dev-rv2-1", "Счётчик 1")
        for i in range(48):
            h.seed_energy("dev-rv2-1", 1.0, i * HOUR, (i + 1) * HOUR)
        bp = open_browser(h.base_url, timezone_id="UTC")
        page = bp.page
        page.wait_for_timeout(1000)
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(300)
        page.locator("nav button:visible", has_text="Отчёты").click()
        page.wait_for_timeout(800)
        page.select_option("select[x-model='rv2Dimension']", "point")
        page.select_option("select[x-model='rv2ScopeIds']", [str(p1["id"])])
        page.fill("input[x-model='rv2From']", "1970-01-01")
        page.fill("input[x-model='rv2To']", "1970-01-03")
        for _ in range(2):
            page.locator("button.btn-primary", has_text="Показать").click()
            page.wait_for_selector("table.rep-table tbody tr", timeout=10000)
            page.wait_for_timeout(500)
        check(page.locator("table.rep-table tbody tr").count() == 1
              and not bp.errors, f"отчёты: два запроса подряд, консоль чистая ({len(bp.errors)})")
        page.select_option("select[x-model='rv2Dimension']", "branch")
        page.wait_for_timeout(500)
        check(page.locator("table.rep-table").count() == 0,
              "отчёты: после смены среза старый результат убран")
        page.locator("nav button:visible", has_text="Обзор").click()
        page.wait_for_timeout(600)
        page.locator("nav button:visible", has_text="Отчёты").click()
        page.wait_for_timeout(600)
        check(not bp.errors, f"отчёты: смена среза и возврат на вкладку — консоль без ошибок "
              f"({len(bp.errors)}): " + "; ".join(sorted(set(bp.errors))[:3]))
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
