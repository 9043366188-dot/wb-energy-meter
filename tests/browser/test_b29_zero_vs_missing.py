"""Партия 15, A10 — «нет данных» и «исправный ноль» не путаются на экране и в CSV — настоящий
Chromium, настоящее Flask-приложение.

Матрица приёмки (ТЗ §13, A10): «Нет данных → `null`; исправная точка с нулём → `0`». В ответах API
это проверено давно (`test_step27_overview_summary.py::test_a10_no_data_vs_zero_distinguished`);
здесь — то, что видит человек: таблицы «Обзора» и «Отчётов» и выгрузка CSV.

Схема стенда (всё в UTC, браузер тоже в UTC):

    Ввод ──(счётчик ввода)──▶ Щит ──▶ «Нулевая»      счётчик есть, сутки по часам, расход 0 кВт·ч
                                  ├──▶ «Без данных»   счётчик привязан, показаний за период нет
                                  └──▶ «Без счётчика» счётчика нет вовсе

  Сутки 01.03.2026 — ввод 1 кВт·ч в час (итого 24), «Нулевая» — нули, «Без данных» — ничего.
  Сутки 03.03.2026 — ввод и «Нулевая» — нули: итог объекта РАВЕН нулю.
  Сутки 05.03.2026 — показаний нет ни у кого: итог объекта НЕ ИЗВЕСТЕН.

Ожидания посчитаны до запуска.

  «Обзор», 01.03:  итог объекта «24.0»; ветвь «Нулевая» — «0.0» и «0.0%»; «Без данных» — «—» и «—»;
                    «Неизмеренная ветвь: Без счётчика» — «—» и «—».
  «Обзор», 03.03:  итог объекта «0.0» (а не «—»), блока «нет показаний» нет.
  «Обзор», 05.03:  итога-числа нет, стоит «—» и пояснение «За выбранный период нет показаний».
  «Отчёты» (точки), 01.03: «Нулевая» — «0.000», доступность complete; «Без данных» — «—», missing.
  CSV того же отчёта: у «Нулевой» в колонке «Значение» стоит `0`, у «Без данных» — ПУСТАЯ ячейка
                    (а не 0 и не «null»), в колонке «Доступность» — `missing`.

Решение по CSV (запись в отчёте партии): неизвестное значение выгружается ПУСТОЙ ячейкой. Ноль —
настоящее число и пишется как `0`. Это стандартное для табличных программ различие (пустая ячейка
не участвует в СУММ и СРЗНАЧ, ноль — участвует), а колонка «Доступность» называет причину словами.
Слово «null» или «—» в числовой колонке сломало бы формулы у людей, которые считают по выгрузке.

Запуск: python tests/browser/test_b29_zero_vs_missing.py
"""
from __future__ import annotations

import calendar
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402

HOUR = 3600
APP = "document.querySelector('[x-data]')._x_dataStack[0]"
DAY1 = calendar.timegm((2026, 3, 1, 0, 0, 0))
DAY3 = calendar.timegm((2026, 3, 3, 0, 0, 0))
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def js(page, code: str):
    return page.evaluate(code)


def wait_for(page, expr: str, ms: int = 8000) -> bool:
    try:
        page.wait_for_function(expr, timeout=ms)
        return True
    except Exception:
        return False


def text(page, testid: str) -> str:
    loc = page.locator(f"[data-testid={testid}]")
    return loc.first.inner_text().strip() if loc.count() and loc.first.is_visible() else ""


def table_rows(page, selector: str) -> dict:
    """Строки таблицы: первая ячейка → список текстов всех ячеек."""
    rows = page.evaluate(
        "(sel) => [...document.querySelectorAll(sel + ' tbody tr')]"
        ".map(tr => [...tr.children].map(td => td.innerText.trim()))", selector)
    return {r[0]: r for r in rows if r}


def seed(h) -> dict:
    vv = h.make_node("vv", "Ввод", "source")
    sh = h.make_node("sh", "Щит", "panel")
    e_in = h.connect(vv["id"], sh["id"])
    pts = {}
    pin = h.make_point("pt-in", "Ввод объекта")
    h.bind_meter(pin["id"], "dev-in", "Счётчик ввода")
    h.set_meter_on_edge(e_in["id"], pin["id"])
    pts["Ввод объекта"] = pin
    for dev, name in (("dev-zero", "Нулевая"), ("dev-nodata", "Без данных")):
        c = h.add_consumer(sh["id"], name)
        pt = h.make_point("pt-" + dev, name)
        h.bind_meter(pt["id"], dev, name)
        h.set_meter_on_edge(c["edge"]["id"], pt["id"])
        pts[name] = pt
    h.add_consumer(sh["id"], "Без счётчика")
    for i in range(24):
        h.seed_energy("dev-in", 1.0, DAY1 + i * HOUR, DAY1 + (i + 1) * HOUR)
        h.seed_energy("dev-zero", 0.0, DAY1 + i * HOUR, DAY1 + (i + 1) * HOUR)
        h.seed_energy("dev-in", 0.0, DAY3 + i * HOUR, DAY3 + (i + 1) * HOUR)
        h.seed_energy("dev-zero", 0.0, DAY3 + i * HOUR, DAY3 + (i + 1) * HOUR)
    return pts


def load_overview(page, d_from: str, d_to: str) -> None:
    with page.expect_response(lambda r: r.url.endswith("/api/v2/overview/summary")) as resp:
        js(page, """(()=>{ const a=%s; a.ovFrom=%r; a.ovTo=%r; a.loadOvSummary(); })()""" % (APP, d_from, d_to))
    assert resp.value.status == 200
    page.wait_for_timeout(500)


def no_garbage(page, where: str) -> None:
    body = page.locator("body").inner_text()
    bad = [w for w in ("NaN", "undefined", "null", "Infinity") if w in body]
    check(not bad, f"{where}: на экране нет служебных слов {bad}")


def main() -> int:
    with Harness() as h:
        pts = seed(h)
        bp = open_browser(h.base_url, timezone_id="UTC")
        page = bp.page
        page.wait_for_timeout(1000)
        js(page, APP + ".uiSet!=='v2' && " + APP + ".toggleUiSet()")
        page.wait_for_timeout(300)

        # ===== «Обзор» =====
        print("\n== «Обзор»: нет данных и ноль ==")
        page.locator("nav button:visible", has_text="Обзор").click()
        wait_for(page, "() => document.querySelector('[data-testid=ov-devices]') !== null")

        load_overview(page, "2026-03-01", "2026-03-02")
        total = text(page, "ov-object-total")
        check(total.startswith("24.0"), f"01.03: итог объекта «24.0» ({total!r})")
        rows = table_rows(page, "[data-testid=ov-branches]")
        zero = rows.get("Нулевая", [])
        check(zero[1:3] == ["0.0", "0.0%"], f"01.03: ветвь «Нулевая» — «0.0» и «0.0%» ({zero})")
        nod = rows.get("Без данных", [])
        check(nod[1:3] == ["—", "—"], f"01.03: ветвь «Без данных» — «—» и «—», не нули ({nod})")
        unm = rows.get("Неизмеренная ветвь: Без счётчика", [])
        check(unm[1:3] == ["—", "—"], f"01.03: неизмеренная ветвь — «—» и «—» ({unm})")
        no_garbage(page, "«Обзор» 01.03")
        bp.screenshot("b29_overview_zero_vs_missing")

        load_overview(page, "2026-03-03", "2026-03-04")
        total = text(page, "ov-object-total")
        check(total.startswith("0.0"), f"03.03: итог объекта — настоящий ноль «0.0», а не «—» ({total!r})")
        body = page.locator("body").inner_text()
        check("За выбранный период нет показаний" not in body,
              "03.03: пояснения «нет показаний» нет — показания есть, они нулевые")
        rows = table_rows(page, "[data-testid=ov-branches]")
        check(rows.get("Нулевая", [])[1:2] == ["0.0"],
              f"03.03: «Нулевая» — «0.0» ({rows.get('Нулевая')})")
        no_garbage(page, "«Обзор» 03.03")

        load_overview(page, "2026-03-05", "2026-03-06")
        total = text(page, "ov-object-total")
        body = page.locator("body").inner_text()
        check(total == "", f"05.03: числа итога объекта нет ({total!r})")
        check("За выбранный период нет показаний" in body or "Не все вводы" in body,
              "05.03: вместо числа — пояснение, что показаний нет")
        check(not any(c == "0.0" for r in table_rows(page, "[data-testid=ov-branches]").values() for c in r[1:2]),
              "05.03: ни одна ветвь не показывает 0.0 — данных нет")
        no_garbage(page, "«Обзор» 05.03")

        # ===== «Отчёты» =====
        print("\n== «Отчёты»: нет данных и ноль ==")
        page.locator("nav button:visible", has_text="Отчёты").click()
        page.wait_for_timeout(800)
        ids = [pts["Ввод объекта"]["id"], pts["Нулевая"]["id"], pts["Без данных"]["id"]]
        js(page, """(()=>{ const a=%s; a.rv2Dimension='point'; a.rv2ScopeIds=%s.map(String);
            a.rv2From='2026-03-01'; a.rv2To='2026-03-02'; })()""" % (APP, ids))
        page.wait_for_timeout(200)
        with page.expect_response(lambda r: r.url.endswith("/api/v2/reports/query")) as resp:
            page.locator("button.btn-primary", has_text="Показать").click()
        check(resp.value.status == 200, "отчёт посчитан (200)")
        page.wait_for_selector("table.rep-table tbody tr", timeout=8000)
        page.wait_for_timeout(300)
        rows = table_rows(page, "table.rep-table")
        z = rows.get("Нулевая", [])
        n = rows.get("Без данных", [])
        i = rows.get("Ввод объекта", [])
        check(z[1:2] == ["0.000"] and z[3:4] == ["complete"],
              f"отчёт: «Нулевая» — «0.000», доступность complete ({z[:4]})")
        check(n[1:2] == ["—"] and n[3:4] == ["missing"],
              f"отчёт: «Без данных» — «—», доступность missing ({n[:4]})")
        check(i[1:2] == ["24.000"], f"отчёт: «Ввод объекта» — «24.000» ({i[:2]})")
        no_garbage(page, "«Отчёты»")

        # ===== CSV =====
        with page.expect_download() as dl:
            page.locator("button", has_text="Скачать CSV").click()
        with open(dl.value.path(), encoding="utf-8-sig", newline="") as f:
            lines = [ln for ln in f.read().split("\r\n") if ln]
        head_i = next((k for k, ln in enumerate(lines) if ln.startswith("Срез;")), -1)
        check(head_i >= 0, "CSV: строка заголовков найдена")
        head = lines[head_i].split(";")
        col_name, col_val, col_known, col_avail = (head.index(x) for x in
                                                   ("Название", "Значение", "Известная частичная сумма", "Доступность"))
        csv_rows = {ln.split(";")[col_name]: ln.split(";") for ln in lines[head_i + 1:]}
        cz, cn, ci = csv_rows.get("Нулевая", []), csv_rows.get("Без данных", []), csv_rows.get("Ввод объекта", [])
        check(cz and cz[col_val] == "0" and cz[col_avail] == "complete",
              f"CSV: «Нулевая» — значение «0», complete ({cz[col_val:col_val + 1]}, {cz[col_avail:col_avail + 1]})")
        check(cn and cn[col_val] == "" and cn[col_known] == "" and cn[col_avail] == "missing",
              f"CSV: «Без данных» — ПУСТАЯ ячейка значения и «missing» "
              f"({cn[col_val:col_val + 1]}, {cn[col_known:col_known + 1]}, {cn[col_avail:col_avail + 1]})")
        check(ci and ci[col_val] == "24", f"CSV: «Ввод объекта» — «24» ({ci[col_val:col_val + 1]})")
        whole = "\n".join(lines)
        check("null" not in whole and "NaN" not in whole and "undefined" not in whole,
              "CSV: в файле нет слов null / NaN / undefined")

        # попутно A22: режим состава назван на экране явно — и «как было», и «текущий»
        label_now = page.evaluate("document.body.innerText").lower()
        check("режим состава: как было на начало периода" in label_now,
              "отчёт: подпись «режим состава: как было на начало периода» (режим по умолчанию)")
        js(page, "%s.rv2CompositionMode='current'" % APP)
        page.wait_for_timeout(200)
        with page.expect_response(lambda r: r.url.endswith("/api/v2/reports/query")) as resp2:
            page.locator("button.btn-primary", has_text="Показать").click()
        check(resp2.value.status == 200 and resp2.value.request.post_data_json.get("composition_mode") == "current",
              "отчёт в режиме «текущий»: запрос несёт composition_mode=current, ответ 200")
        page.wait_for_selector("table.rep-table tbody tr", timeout=8000)
        page.wait_for_timeout(300)
        label_cur = page.evaluate("document.body.innerText").lower()
        check("режим состава: текущий" in label_cur,
              "отчёт: на экране подпись «режим состава: текущий» (A22)")

        errs = [e for e in bp.errors if "Failed to load resource" not in e]
        check(not errs, f"консоль без ошибок приложения ({errs[:3]})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
