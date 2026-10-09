"""Страховочная сетка для разбивки index.html на файлы (партия 9, F5).

Снимает поверхность Alpine-компонента — `Object.keys(app())`, то есть
все поля состояния и методы, — и сверяет с эталоном
`tests/fixtures/app_keys_baseline.json`, снятым ДО разбивки (26.09.2026,
версия 0.18.0, единый index.html).

Зачем именно так. При переносе ~7000 строк JS в отдельные файлы самая
дорогая ошибка — молча потерять метод: Alpine не ругается на
несуществующий метод в разметке, кнопка просто перестаёт работать, и
никакой `node --check`, баланс тегов или юнит-тест этого не увидят. Один
такой промах уже стоил проекту двух белых экранов на контроллере (см.
docs/agent-guides/frontend.md). Сверка множества ключей до и после ловит и потерю, и
случайный дубликат имени между частями — механически, без ручного
обхода интерфейса.

Правила работы с эталоном:

* поменять `app_keys_baseline.json` можно ТОЛЬКО когда метод или поле
  добавлены/удалены осознанно, и в том же коммите, что и само
  изменение, с объяснением в сообщении коммита;
* во время разбивки (F5) эталон не трогается вообще: перенос кода не
  меняет поверхность компонента. Если тест покраснел — потерялся метод
  или появился дубликат, это и есть тот баг, ради которого тест написан.

Запуск: python tests/browser/test_b04_app_surface.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from browser.harness import Harness, open_browser  # noqa: E402

BASELINE = os.path.join(os.path.dirname(HERE), "fixtures", "app_keys_baseline.json")


def test_app_surface_matches_baseline():
    with open(BASELINE, "r", encoding="utf-8") as f:
        baseline = sorted(json.load(f))

    with Harness() as h:
        bp = open_browser("http://127.0.0.1:%d/" % h.port)
        try:
            bp.page.wait_for_timeout(1500)
            keys = sorted(bp.page.evaluate("Object.keys(app()).sort()"))

            lost = [k for k in baseline if k not in keys]
            added = [k for k in keys if k not in baseline]

            assert not lost, (
                "ПОТЕРЯНЫ поля/методы компонента (%d шт.) — в разметке они "
                "молча перестанут работать: %s" % (len(lost), lost[:20]))
            assert not added, (
                "ПОЯВИЛИСЬ новые поля/методы (%d шт.), которых нет в эталоне: "
                "%s. Если это осознанное изменение — обновите "
                "tests/fixtures/app_keys_baseline.json в том же коммите."
                % (len(added), added[:20]))

            # Дубликат имени между частями не виден по Object.keys (второй
            # тихо перетирает первый), поэтому отдельно требуем, чтобы
            # сборщик частей сам сообщал о конфликтах, если он уже есть.
            conflicts = bp.page.evaluate(
                "(window.WBEM && window.WBEM.conflicts) ? window.WBEM.conflicts : []")
            assert not conflicts, (
                "Сборщик частей интерфейса сообщил о конфликте имён: %s"
                % (conflicts,))

            assert not bp.errors, "Ошибки в консоли: %s" % (bp.errors,)
            print("OK   поверхность app() совпадает с эталоном (%d полей и "
                  "методов), конфликтов имён нет, консоль чистая"
                  % len(keys))
        finally:
            bp.close()


if __name__ == "__main__":
    test_app_surface_matches_baseline()
    print("\nСценарий b04 (поверхность компонента) пройден.")
