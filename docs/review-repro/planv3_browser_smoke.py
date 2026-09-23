"""Браузерный смоук «Плана v3» на чистой базе (Playwright + Chromium).

В облачной песочнице агента Chromium и Playwright уже установлены
(PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers), поэтому «браузера нет» —
больше не причина пропускать проверку UI.

Подготовка (пример):
    # mosquitto: apt-get install -y mosquitto && mosquitto -d
    # сервис на чистой БД, без агрегатора и самообновления:
    python -c "import sys; from wb_energy_meter.main import main; sys.exit(main(sys.argv[1:]))" \
        --config /tmp/conf.yaml --db-path /tmp/state.db --no-log-file &
    python planv3_browser_smoke.py http://127.0.0.1:8090/

Что проверяет:
  1. ни одной ошибки консоли/pageerror при обходе всех вкладок обоих наборов;
  2. после «Создать план» на чистой базе холст виден в нормальном масштабе
     (на 0.17.0: zoom = minZoom −4, холст 2000×1200 выглядит точкой в центре);
  3. клик в левую верхнюю четверть карты с инструментом «+ Узел» создаёт
     узел (на 0.17.0: тост «Точка вне границ плана», узел не создаётся);
  4. масштаб не сбрасывается после размещения узла (на 0.17.0 каждое
     действие вызывает selectPlanV3 -> _planV3RenderBase -> fitBounds).
Скриншоты — в ./shots/.
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090/"
APP = "document.querySelector('[x-data]')._x_dataStack[0]"
os.makedirs("shots", exist_ok=True)

errors = []
fails = []


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE)
    page.wait_for_timeout(1500)

    # 1. обход вкладок обоих наборов
    for ui_set in ("classic", "v2"):
        if page.evaluate(APP + ".uiSet") != ui_set:
            page.evaluate(APP + ".toggleUiSet()")
            page.wait_for_timeout(500)
        for btn in page.locator("nav button:visible").all():
            label = btn.inner_text().strip()
            btn.click()
            page.wait_for_timeout(1200)
            page.screenshot(path=f"shots/tab_{ui_set}_{label}.png")
    check(not errors, f"консоль без ошибок при обходе вкладок ({len(errors)} ошибок)")

    # 2. «План v3» на чистой базе
    page.locator("nav button:visible", has_text="План v3").click()
    page.wait_for_timeout(1200)
    if page.locator("text=Создать план").count():
        page.fill("input[placeholder='Название плана']", "Смоук")
        page.click("text=Создать план")
        page.wait_for_timeout(2000)
    zoom = page.evaluate("_planV3Map.getZoom()")
    check(zoom > -4, f"холст пустого плана виден в нормальном масштабе (zoom={zoom})")
    page.screenshot(path="shots/planv3_after_create.png")

    # 3. узел кликом в левую верхнюю четверть карты
    box = page.locator(".leaflet-container:visible").first.bounding_box()
    page.click("text=+ Узел")
    page.wait_for_timeout(300)
    page.fill("input[placeholder='Название узла']", "Ввод")
    page.mouse.click(box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.3)
    page.wait_for_timeout(1500)
    n_nodes = len(page.evaluate("fetch('/api/v2/topology/nodes').then(r=>r.json())"))
    check(n_nodes == 1, f"узел создан кликом по карте (узлов: {n_nodes})")

    # 4. масштаб не сбрасывается действием
    # пользователь приблизил центр холста (2000×1200) до zoom 0 и ставит узел
    page.evaluate("_planV3Map.setView([600, 1000], 0)")
    page.wait_for_timeout(500)
    z_before = page.evaluate("_planV3Map.getZoom()")
    page.click("text=+ Узел")
    page.fill("input[placeholder='Название узла']", "ЩР-1")
    page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    page.wait_for_timeout(1500)
    z_after = page.evaluate("_planV3Map.getZoom()")
    check(z_before == z_after, f"масштаб сохраняется после действия ({z_before} -> {z_after})")
    page.screenshot(path="shots/planv3_after_nodes.png")

    browser.close()

print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
sys.exit(1 if fails else 0)
