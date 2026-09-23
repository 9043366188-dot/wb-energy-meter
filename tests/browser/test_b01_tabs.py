"""E1.2 (партия 7, этап 1, докс/TZ-batch7-review-fixes.md): обход всех
вкладок обоих наборов («classic» и «v2») в обеих темах — 0 ошибок
консоли/pageerror. Полностью новый прогон (в прошлых партиях браузер не
открывался вживую — см. AGENTS.md → «Что в работе»).

Запуск: python tests/browser/test_b01_tabs.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def main() -> int:
    with Harness() as h:
        # Немного данных, чтобы вкладки не были совсем пустыми (пустые
        # состояния — своя проверка, не эта).
        vv = h.make_node("vvod", "Ввод", "source")
        sh = h.make_node("shr1", "ЩР-1", "panel")
        e_in = h.connect(vv["id"], sh["id"])
        p = h.make_point("vvod-pt", "Ввод")
        h.bind_meter(p["id"], "dev-v", "Ввод")
        h.set_meter_on_edge(e_in["id"], p["id"])
        h.seed_energy("dev-v", 42.0)
        h.see_in_mqtt("dev-unbound", "Неизвестный прибор")

        bp = open_browser(h.base_url)
        bp.page.wait_for_timeout(1200)

        for theme in ("dark", "light"):
            if bp.page.evaluate(APP + ".theme") != theme:
                bp.page.evaluate(APP + ".toggleTheme()")
                bp.page.wait_for_timeout(200)
            for ui_set in ("classic", "v2"):
                if bp.page.evaluate(APP + ".uiSet") != ui_set:
                    bp.page.evaluate(APP + ".toggleUiSet()")
                    bp.page.wait_for_timeout(400)
                buttons = bp.page.locator("nav button:visible")
                n = buttons.count()
                for i in range(n):
                    btn = bp.page.locator("nav button:visible").nth(i)
                    label = btn.inner_text().strip()
                    btn.click()
                    bp.page.wait_for_timeout(900)
                    bp.screenshot(f"b01_{theme}_{ui_set}_{label}")
        check(not bp.errors,
              f"консоль без ошибок при обходе вкладок обоих наборов "
              f"в обеих темах ({len(bp.errors)} ошибок): "
              + "; ".join(bp.errors[:5]))
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
