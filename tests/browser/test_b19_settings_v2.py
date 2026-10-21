"""Партия 13, 13.2: «Настройки» в наборе v2 — настоящий Chromium, настоящее
Flask-приложение.

Классический интерфейс уходит в партии 15, поэтому «Настройки» должны жить в
наборе v2 уже сейчас:

  1. Классический набор — без изменений: шесть разделов в прежнем порядке
     (Обновление, Перенос легаси-данных, Зоны, Зарегистрированные счётчики,
     Опрос канала Uptime, Найдены в MQTT, не в реестре), раздела «Интерфейс и
     версия» нет.
  2. Набор v2: в навигации пятая вкладка «Настройки»; открыта — видны «Обновление»,
     «Опрос канала Uptime», «Эмулятор счётчиков» (П12) и «Интерфейс и версия»; разделов старой структуры
     (перенос легаси, зоны, реестр счётчиков, найденные в MQTT) нет — ни видимых,
     ни скрытых в разметке.
  3. Самообновление доступно: «Проверить обновления» работает (ответ сервера
     подменён на `page.route`, настоящий GitHub не трогаем), показывает версию и
     кнопку «Обновить сейчас».
  4. Канал Uptime: раздел, его пояснение и «Как включить вручную» на месте.
  5. Тема переключается из «Настроек»; версия на экране = версии из /api/status.
  6. «Вернуться в классический интерфейс» возвращает классический набор.

Запуск: python tests/browser/test_b19_settings_v2.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from demo_seed import seed_demo  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list[str] = []
LEGACY_H2 = ["Обновление", "Перенос легаси-данных", "Зоны", "Зарегистрированные счётчики",
             "Опрос канала Uptime", "Найдены в MQTT, не в реестре"]
LEGACY_ONLY = ["Перенос легаси-данных", "Зарегистрированные счётчики", "Найдены в MQTT, не в реестре"]


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def settings_h2(page):
    return [t.strip() for t in page.locator("main h2:visible").all_inner_texts()]


def main() -> int:
    with Harness() as h:
        seed_demo(h, with_plan=False)
        status = h.client.get("/api/status").get_json()
        bp = open_browser(h.base_url)
        page = bp.page
        page.wait_for_timeout(1200)

        # ===== 1. классика без изменений =====
        check(page.evaluate(APP + ".uiSet") == "classic", "по умолчанию открыт классический набор")
        page.locator("nav button:visible", has_text="Настройки").click()
        page.wait_for_timeout(1500)
        h2 = settings_h2(page)
        check(h2 == LEGACY_H2, f"классика: разделы «Настроек» в прежнем порядке {h2}")
        check(page.locator("[data-testid=settings-v2-about]").count() == 0,
              "классика: раздела «Интерфейс и версия» нет")

        # ===== 2. набор v2 =====
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(600)
        labels = page.locator("nav button:visible").all_inner_texts()
        check(labels == ["Обзор", "Отчёты", "Структура", "План v3", "Настройки"],
              f"v2: вкладки набора {labels}")
        page.locator("nav button:visible", has_text="Настройки").click()
        page.wait_for_timeout(1500)
        h2 = settings_h2(page)
        check(h2 == ["Обновление", "Опрос канала Uptime", "Эмулятор счётчиков", "Интерфейс и версия"],
              f"v2: разделы «Настроек» {h2}")
        # живой DOM (textContent не заглядывает внутрь <template> невыполненных x-if),
        # то есть «нет ни видимых, ни скрытых»
        body = page.evaluate("document.querySelector('main').textContent")
        gone = [t for t in LEGACY_ONLY + ["Массовое назначение", "Создать зону"] if t in body]
        check(not gone and page.locator("main .reg-table").count() <= 1,
              f"v2: разделов старой структуры в разметке нет ({gone})")
        check(page.locator("main", has_text="Зоны").locator("h2", has_text="Зоны").count() == 0,
              "v2: раздела «Зоны» нет")

        # ===== 3. самообновление =====
        fake = {"current": {"version": status["version"], "commit": "a" * 40},
                "remote": {"version": "9.9.9", "commit": "b" * 40, "message": "тест", "date": None},
                "update_available": True, "allow_from_ui": True}
        page.route("**/api/update/check", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(fake)))
        page.locator("button", has_text="Проверить обновления").click()
        page.wait_for_timeout(800)
        check(page.locator("main", has_text="Доступно обновление").count() == 1
              and page.locator("button", has_text="Обновить сейчас").is_visible(),
              "v2: «Проверить обновления» показывает версию и кнопку «Обновить сейчас»")

        # ===== 4. Uptime =====
        check(page.locator("main", has_text="Как включить вручную").count() == 1
              and page.locator("main .wbs-intro", has_text="Uptime").count() >= 1,
              "v2: раздел Uptime с пояснением и «Как включить вручную» на месте")

        # ===== 5. тема и версия =====
        before = page.evaluate(APP + ".theme")
        page.locator("[data-testid=settings-theme-toggle]").click()
        page.wait_for_timeout(300)
        after = page.evaluate("document.documentElement.getAttribute('data-theme')")
        check(after != before, f"тема переключается из «Настроек»: {before} → {after}")
        info = page.locator("[data-testid=settings-version-info]").inner_text()
        check(("v" + status["version"]) in info, f"версия на экране = /api/status: {status['version']!r} в {info!r}")

        # ===== 6. возврат в классику =====
        page.locator("[data-testid=settings-to-classic]").click()
        page.wait_for_timeout(600)
        check(page.evaluate(APP + ".uiSet") == "classic" and "Перенос легаси-данных" in settings_h2(page),
              "«Вернуться в классический интерфейс» — классические «Настройки» целиком")
        check(not bp.errors, f"консоль без ошибок ({bp.errors[:3]})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
