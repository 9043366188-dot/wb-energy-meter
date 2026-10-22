"""Партия 15, A44: даты периода «Обзора» и «Отчётов» — в часовом поясе браузера, сервер читает их по
тому же `timezone` — настоящий Chromium, настоящее Flask-приложение.

Что проверяется:
  1. Период по умолчанию (с 1-го числа месяца по сегодня) совпадает с календарём ПОЯСА браузера —
     для поясов восточнее UTC (Москва +03, Киритимати +14), западнее (Лос-Анджелес) и UTC. Раньше
     даты брались через toISOString() (UTC): в поясе восточнее UTC местная полночь 1-го числа
     превращалась в последнее число прошлого месяца.
  2. Запрос «Обзора» уходит с `timezone` браузера и датами из полей.
  3. Сутки перехода на летнее время (Europe/Berlin, 29.03.2026): сервер вернул подписи периода
     `from_local = …T00:00:00+01:00`, `to_local = …T00:00:00+02:00` — границы построены по
     поясу браузера, а не по поясу процесса службы.
  4. Консоль чистая.

Запуск: python tests/browser/test_b28_period_timezone.py
"""
from __future__ import annotations

import json
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


# Календарь пояса, посчитанный независимо от кода приложения: Intl без toISOString().
EXPECTED = """(tz) => {
  const f = new Intl.DateTimeFormat('en-CA', {timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit'});
  const today = f.format(new Date());
  return {from: today.slice(0, 8) + '01', to: today};
}"""


def main() -> int:
    with Harness() as h:
        for tz in ("Europe/Moscow", "Pacific/Kiritimati", "America/Los_Angeles", "UTC"):
            bp = open_browser(h.base_url, timezone_id=tz)
            page = bp.page
            page.wait_for_timeout(800)
            got = page.evaluate("(code) => { const a = eval(code); return a._ovDefaultDates(); }", APP)
            want = page.evaluate(EXPECTED, tz)
            check(got == want, f"{tz}: период по умолчанию = календарь пояса ({got} против {want})")
            check(got["from"].endswith("-01") and got["from"][:7] == got["to"][:7],
                  f"{tz}: начало — 1-е число текущего месяца пояса ({got['from']})")
            with page.expect_request(lambda r: r.url.endswith("/api/v2/overview/summary")) as req:
                page.evaluate("(code) => { eval(code).initOvAccounting(); }", APP)
            body = json.loads(req.value.post_data)
            check(body.get("timezone") == tz and body.get("from") == want["from"] and body.get("to") == want["to"],
                  f"{tz}: запрос «Обзора» — timezone браузера и даты из календаря пояса ({body})")
            bp.close()

        # ===== сутки перехода часов: сервер строит границы по поясу браузера =====
        bp = open_browser(h.base_url, timezone_id="Europe/Berlin")
        page = bp.page
        page.wait_for_timeout(800)
        page.evaluate("(code) => { const a = eval(code); a.ovFrom = '2026-03-29'; a.ovTo = '2026-03-30'; }", APP)
        with page.expect_response(lambda r: r.url.endswith("/api/v2/overview/summary")) as resp:
            page.evaluate("(code) => { eval(code).loadOvSummary(); }", APP)
        period = resp.value.json().get("period", {})
        check(period.get("timezone") == "Europe/Berlin"
              and period.get("from_local") == "2026-03-29T00:00:00+01:00"
              and period.get("to_local") == "2026-03-30T00:00:00+02:00",
              f"Berlin 29.03.2026: подписи периода со смещением ({period})")
        check(int(float(period.get("to", 0))) - int(float(period.get("from", 0))) == 23 * 3600,
              "Berlin 29.03.2026: сутки длятся 23 часа")
        errs = [e for e in bp.errors if "Failed to load resource" not in e]
        check(not errs, f"консоль без ошибок приложения ({errs[:3]})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
