"""Снимки экранов для отчёта по партии (партия 13, 13.0 и 13.9).

Поднимает настоящее приложение на демо-данных (demo_seed.seed_demo) и снимает
экраны в заданных темах и ширинах:

    python tests/browser/shots.py --out docs/screenshots/0.23.0 \
        --set v2 --widths 1280 --themes dark light

Имя файла: <экран>_<тема>_<ширина>.png (например `overview_dark_1280.png`).
Экраны: overview, reports, structure, planv3 (карта), planv3-node (карточка
щита), planv3-edge (карточка линии), scheme (однолинейная схема, партия 14:
создаётся и раскладывается автоматически), settings (v2: вкладка «Настройки» набора v2;
для снимков «до» --set classic снимает классические «Настройки»).

Скрипт не входит в `run_all`/`run.py` — это инструмент отчёта, не тест. Размер
каждого снимка проверяется: больше 300 КБ — предупреждение (TZ 13.0).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from demo_seed import seed_demo  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
HEIGHTS = {1280: 860, 768: 1024, 360: 740}
MAX_BYTES = 300 * 1024


def _nav(page, label):
    page.locator("nav button:visible", has_text=label).first.click()
    page.wait_for_timeout(1500)


def _open_menu_if_needed(page):
    """На узких экранах (после 13.1) навигация может прятаться за кнопкой меню."""
    if page.locator("nav button:visible").count() == 0:
        btn = page.locator("[data-testid=nav-toggle]:visible")
        if btn.count():
            btn.first.click()
            page.wait_for_timeout(300)


def _make_scheme(h):
    """Создать на демо-данных однолинейную схему и разложить её (как это делает человек). → id плана."""
    bp = open_browser(h.base_url, viewport={"width": 1400, "height": 900})
    page = bp.page
    try:
        page.wait_for_timeout(1200)
        if page.evaluate(APP + ".uiSet") != "v2":
            page.evaluate(APP + ".toggleUiSet()")
            page.wait_for_timeout(400)
        _nav(page, "План v3")
        page.evaluate(f"{APP}.slNewSchemeName = 'Схема объекта'")
        page.evaluate(f"{APP}.slCreateScheme()")
        page.wait_for_function(
            "() => { const a = %s; return a.planV3Current && a.planV3Current.plan_kind === 'single_line'; }" % APP,
            timeout=15000)
        page.evaluate(f"{APP}.slAutoLayout('unplaced')")
        page.wait_for_function(
            "() => { const a = %s; return a.planV3Current.items.length > 0 "
            "&& a.planV3Current.items.length === a.structNodes.length; }" % APP, timeout=15000)
        return page.evaluate(APP + ".planV3Current.id")
    finally:
        bp.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--set", default="v2", choices=["v2", "classic"])
    ap.add_argument("--widths", nargs="+", type=int, default=[1280])
    ap.add_argument("--themes", nargs="+", default=["dark", "light"])
    ap.add_argument("--full", action="store_true", help="снимок всей страницы (для отладки вёрстки)")
    ap.add_argument("--screens", nargs="+", default=[
        "overview", "reports", "structure", "planv3", "planv3-node", "planv3-edge", "settings"])
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    too_big = []
    with Harness() as h:
        info = seed_demo(h)
        scheme_id = _make_scheme(h) if "scheme" in args.screens else None
        for width in args.widths:
            for theme in args.themes:
                bp = open_browser(h.base_url, viewport={"width": width, "height": HEIGHTS.get(width, 900)})
                page = bp.page
                page.wait_for_timeout(1200)
                if page.evaluate(APP + ".theme") != theme:
                    page.evaluate(APP + ".toggleTheme()")
                    page.wait_for_timeout(200)
                want_set = args.set
                if page.evaluate(APP + ".uiSet") != want_set:
                    page.evaluate(APP + ".toggleUiSet()")
                    page.wait_for_timeout(400)
                for screen in args.screens:
                    tab_label = {"overview": "Обзор", "reports": "Отчёты", "structure": "Структура",
                                 "planv3": "План v3", "planv3-node": "План v3", "scheme": "План v3",
                                 "planv3-edge": "План v3", "settings": "Настройки"}[screen]
                    _open_menu_if_needed(page)
                    try:
                        _nav(page, tab_label)
                    except Exception as e:  # экран недоступен в этом наборе/ширине
                        print(f"SKIP {screen} {theme} {width}: {e.__class__.__name__}")
                        continue
                    if screen == "scheme":
                        page.evaluate(f"{APP}.selectPlanV3({scheme_id})")
                        page.wait_for_timeout(2500)
                        page.evaluate(f"{APP}.slFitContent()")   # кнопка «Вписать»
                        page.wait_for_timeout(500)
                    elif screen == "planv3-node":
                        page.evaluate(f"{APP}.openInspectorInPlace('node', {info['shr1']['id']!r})")
                        page.wait_for_timeout(1500)
                    elif screen == "planv3-edge":
                        page.evaluate(f"{APP}.openInspectorInPlace('edge', {info['e_light']['id']!r})")
                        page.wait_for_timeout(1500)
                    elif screen == "reports":
                        page.evaluate(f"{APP}.runRv2Query && {APP}.runRv2Query()")
                        page.wait_for_timeout(1500)
                    path = os.path.join(args.out, f"{screen}_{theme}_{width}.png")
                    page.screenshot(path=path, full_page=args.full)
                    size = os.path.getsize(path)
                    flag = "" if size <= MAX_BYTES else "  !!> 300 КБ"
                    print(f"{path}  {size // 1024} КБ{flag}")
                    if size > MAX_BYTES:
                        too_big.append(path)
                    if screen.startswith("planv3-"):
                        page.evaluate(APP + ".closeInspector()")
                        page.wait_for_timeout(300)
                if bp.errors:
                    print("КОНСОЛЬ:", bp.errors[:5])
                bp.close()
    if too_big:
        print("Файлы больше 300 КБ (уменьшите область или квантуйте палитру):", too_big)
    return 0


if __name__ == "__main__":
    sys.exit(main())
