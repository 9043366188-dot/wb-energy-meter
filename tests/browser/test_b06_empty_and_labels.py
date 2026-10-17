"""Партия 13 (находки пилота П2, П3): чистая база и одни слова — настоящий
Chromium, настоящее Flask-приложение, ПУСТАЯ БД.

  П3. В новом интерфейсе на пустой базе нет «Перенести легаси-данные» (ни в «Обзоре»,
      ни на вкладке «План v2» — самой вкладки в наборе нет); «Обзор» говорит
      «Начните со схемы…». Классический «Настройки» кнопку сохраняет — с предупреждением.
  П2. Один словарь типов элементов схемы — одни и те же слова («Ввод / Щит /
      Ответвление / Нагрузка») в списке типов «Плана v3», в форме «+ Элемент
      схемы» на «Структуре» и в карточке элемента; подсказка пустой схемы и
      фраза «Счётчик ставится на линию» — те же слова на «Структуре» и «Плане v3».

Запуск: python tests/browser/test_b06_empty_and_labels.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
KINDS = ["Ввод", "Щит", "Ответвление", "Нагрузка"]
HINT = ("Схема — это вводы, щиты и нагрузки, соединённые линиями. "
        "Счётчик ставится на линию: тогда видно, сколько прошло по ней.")
OLD_WORDS = ["Узел разветвления", "Промежуточный узел", "Потребитель", "Узел сети",
             "+ Узел", "+ узел"]
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def option_labels(page, selector):
    return page.locator(selector + " option").evaluate_all(
        "els => els.map(e => e.textContent.trim())")


def main() -> int:
    with Harness() as h:
        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1000)

        # ===== П3. классический набор: кнопка на месте, с предупреждением =====
        page.locator("nav button:visible", has_text="Настройки").click()
        page.wait_for_timeout(500)
        check(page.locator("button:visible", has_text="Перенести легаси-данные").count() == 1,
              "классический набор: «Перенести легаси-данные» в «Настройках» сохранена")
        warn = norm(page.locator("[data-testid=migrate-warning]:visible").inner_text())
        check("Для пилота не нужно" in warn,
              f"…с предупреждением «Для пилота не нужно» (текст: {warn!r})")

        # ===== новый набор вкладок =====
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(500)
        tabs = [t.strip() for t in page.locator("nav button:visible").all_inner_texts()]
        check("План v2" not in tabs,
              f"П3: вкладки «План v2» в новом наборе нет (вкладки: {tabs})")
        check(all(t in tabs for t in ("Обзор", "Отчёты", "Структура", "План v3")),
              "в новом наборе есть «Обзор», «Отчёты», «Структура», «План v3»")

        # ===== П3. «Обзор» на пустой базе =====
        page.locator("nav button:visible", has_text="Обзор").click()
        page.wait_for_timeout(1000)
        ov = norm(page.locator("[data-testid=overview-empty]").inner_text())
        check("Начните со схемы" in ov and "Точек учёта пока нет" in ov,
              f"«Обзор» пуст и говорит «Начните со схемы…» ({ov!r})")
        check("легаси" not in ov.lower() and "перенести" not in ov.lower(),
              "в «Обзоре» ни слова про перенос легаси-данных")
        check(page.locator("button:visible", has_text="Перенести легаси-данные").count() == 0,
              "в «Обзоре» нет кнопки «Перенести легаси-данные»")

        # ===== П2. «Структура» на пустой базе =====
        page.locator("nav button:visible", has_text="Структура").click()
        page.wait_for_timeout(1000)
        st_hint = norm(page.locator("[data-testid=struct-empty-hint]").inner_text())
        check(st_hint.startswith(HINT), f"«Структура»: подсказка пустой схемы — слово в слово ({st_hint!r})")
        intro = norm(page.locator("[data-testid=struct-intro]").inner_text())
        check("Счётчик ставится на линию" in intro, f"«Структура»: во введении «Счётчик ставится на линию» ({intro!r})")
        page.click("[data-testid=struct-add-node]")
        page.wait_for_timeout(300)
        struct_kinds = option_labels(page, "[data-testid=struct-new-node] select")
        check(struct_kinds == KINDS, f"«Структура»: «+ Элемент схемы» → типы {struct_kinds}")
        check(page.locator("[data-testid=struct-add-node]").inner_text().strip() == "+ Элемент схемы",
              "кнопка называется «+ Элемент схемы» (не «+ Узел»)")

        # ===== П2. «План v3» на пустой базе =====
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(800)
        h.create_plan("Смоук")
        page.evaluate(APP + ".loadPlanV3Plans && " + APP + ".loadPlanV3Plans()")
        page.reload()
        page.wait_for_timeout(1000)
        page.locator("nav button:visible", has_text="План v3").click()
        page.wait_for_timeout(1000)
        body = norm(page.locator("body").inner_text())
        check(HINT in body, "«План v3»: подсказка пустой схемы — те же слова, что на «Структуре»")
        page.click("button:has-text('+ Элемент схемы')")
        page.wait_for_timeout(300)
        pv3_kinds = option_labels(page, "[data-testid=planv3-kind-select]")
        check(pv3_kinds == KINDS, f"«План v3»: «+ Элемент схемы» → типы {pv3_kinds}")
        check("Счётчик ставится на линию" in norm(page.locator("[data-testid=planv3-node-form]").inner_text()),
              "«План v3»: форма элемента повторяет «Счётчик ставится на линию»")
        page.click("text=✕ Отменить инструмент")

        # ===== П2. карточка элемента: те же типы =====
        node = h.make_node("shr1", "ЩР-1", "panel")
        page.evaluate(APP + "._loadStructureLists()")
        page.wait_for_timeout(500)
        page.evaluate(APP + f".openInspectorInPlace('node', {node['id']})")
        page.wait_for_timeout(800)
        card_kinds = option_labels(page, "[data-testid=node-card]:visible [data-testid=node-card-kind-select]")
        check(card_kinds == KINDS, f"карточка элемента: типы {card_kinds}")
        check(page.locator("[data-testid=node-card-kind]:visible").inner_text().strip() == "Щит",
              "в карточке тип крупно: «Щит»")

        # ===== П2. «Структура» с элементом: дерево и «Не на схеме» — те же слова =====
        page.locator("nav button:visible", has_text="Структура").click()
        page.wait_for_timeout(800)
        tree = norm(page.locator("[data-testid=scheme-tree]").inner_text())
        check("ЩР-1" in tree and "щит" in tree.lower(),  # тип рисуется капсом (CSS)
              f"«Структура»: в дереве «ЩР-1» с типом «Щит» ({tree!r})")

        # ===== П2/П3. нигде в новом наборе нет старых слов и переноса легаси =====
        for tab_name in ("Обзор", "Отчёты", "Структура", "План v3"):
            page.locator("nav button:visible", has_text=tab_name).click()
            page.wait_for_timeout(700)
            txt = norm(page.locator("body").inner_text())
            bad = [w for w in OLD_WORDS if w in txt]
            check(not bad, f"«{tab_name}»: старых слов нет ({bad})")
            check("легаси" not in txt.lower(), f"«{tab_name}»: про легаси-данные ни слова")

        check(not bp.errors, f"консоль без ошибок ({len(bp.errors)}): " + "; ".join(bp.errors[:5]))
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
