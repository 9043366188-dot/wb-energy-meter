"""E1.2 (партия 7, этап 1): перенос docs/review-repro/planv3_browser_smoke.py
на harness.py. На этапе 1 (до этапа 3, задачи B1/B2) этот тест
**ОЖИДАЕМО КРАСНЫЙ ровно по трём известным причинам** (см. docs/
review-2026-09-23.md п.2 и docs/TZ-batch7-review-fixes.md, «Этап 1»,
«Готово, когда»):
    1. холст пустого плана открывается в масштабе −4 (minZoom);
    2. клик по карте с инструментом «+ Элемент схемы» не создаёт узел
       («Точка вне границ плана»);
    3. масштаб/центр карты сбрасываются после действия.
Чинит эти три пункта этап 3 (задачи B1/B2) — после него все проверки
здесь обязаны стать зелёными.

Запуск: python tests/browser/test_b02_planv3_clean.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from waits import wait_map_v3_ready  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def main() -> int:
    with Harness() as h:
        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1000)

        # Переключиться на новый набор и открыть «План v3».
        page.evaluate("document.querySelector('[x-data]')._x_dataStack[0].uiSet='v2'")
        page.wait_for_timeout(300)
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(800)

        # Чистая база — создать пустой план.
        if page.locator("text=Создать план").count():
            page.fill("input[placeholder='Название плана']", "Смоук")
            page.click("text=Создать план")
        # Партия 13, 13.8: ждём готовности карты (вписана, размер = размеру контейнера), а не
        # фиксированную паузу — клик раньше этого давал «Точка вне границ плана» (плавающий тест).
        wait_map_v3_ready(page)

        zoom = page.evaluate("_planV3Map.getZoom()")
        check(zoom > -4, f"холст пустого плана виден в нормальном масштабе (zoom={zoom})")
        bp.screenshot("b02_after_create")

        # Клик в левую верхнюю четверть карты с инструментом «+ Элемент схемы».
        page.click("button:has-text('+ Элемент схемы')")
        page.wait_for_selector("[data-testid=planv3-node-form]")
        page.fill("input[placeholder='Название']", "Ввод")
        # П11: форма инструмента сдвигает карту вниз — координаты клика берём
        # из свежего замера (после того как карта подогнана под новый размер контейнера).
        wait_map_v3_ready(page)
        box = page.locator(".leaflet-container:visible").first.bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.3)
        page.wait_for_function("fetch('/api/v2/topology/nodes').then(r=>r.json()).then(n=>n.length>=1)")
        n_nodes = len(page.evaluate("fetch('/api/v2/topology/nodes').then(r=>r.json())"))
        check(n_nodes == 1, f"узел создан кликом по карте (узлов: {n_nodes})")

        # Масштаб не должен сбрасываться следующим действием.
        page.evaluate("_planV3Map.setView([600, 1000], 0, {animate: false})")
        wait_map_v3_ready(page)
        z_before = page.evaluate("_planV3Map.getZoom()")
        page.click("button:has-text('+ Элемент схемы')")
        page.wait_for_selector("[data-testid=planv3-node-form]")
        page.fill("input[placeholder='Название']", "ЩР-1")
        wait_map_v3_ready(page)
        box = page.locator(".leaflet-container:visible").first.bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
        page.wait_for_function("fetch('/api/v2/topology/nodes').then(r=>r.json()).then(n=>n.length>=2)")
        z_after = page.evaluate("_planV3Map.getZoom()")
        check(z_before == z_after, f"масштаб сохраняется после действия ({z_before} -> {z_after})")
        bp.screenshot("b02_after_nodes")

        check(not bp.errors, f"консоль без ошибок ({len(bp.errors)}): " + "; ".join(bp.errors[:5]))
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
