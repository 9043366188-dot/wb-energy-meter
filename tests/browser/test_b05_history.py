"""Партия 12, этап 12.3: интерфейс истории — «История» в карточке «Структуры» и
переключатель «Структура: текущая / как было» в «Отчётах v2».

Сценарий (настоящий Chromium, настоящее Flask-приложение, время журнала под
контролем теста — `change_journal._clock`):

  Шкала времени (UTC): журнал начат 30.12.2025; данные — трое суток с 01.01.2026,
  P1 даёт 1 кВт·ч/ч, P2 — 2 кВт·ч/ч. Точки состоят в группе «А»; с 02.01.2026
  P1 перенесена из «А» в «Б».

  Числа посчитаны вручную. Отчёт по группам за 01.01–03.01 (48 ч):
    структура «текущая» (состав — на начало периода):  А = (1+2)·48 = 144,
                                                         Б = состава на начало нет → «—»;
    структура «как было» (состав по интервалам):       А = 24·3 + 24·2 = 120,
                                                         Б = 24·1 = 24.

  1. Отчёт в обоих режимах: числа на экране совпадают с ответом API на ТОТ ЖЕ запрос,
     который отправил интерфейс (тело запроса перехвачено), и с ручным расчётом;
     при «как было» выбор состава группы недоступен; в шапке результата подписан режим.
  2. A23: период до начала журнала — на экране пометка «история ведётся с версии 0.22.0».
  3. CSV содержит режим структуры в заголовке и в колонке, число «как было» = 120.
  4. «Структура»: карточка P1 → «История изменений» показывает те же записи, что
     GET /api/v2/change-log?related=1; правка описания в интерфейсе добавляет запись сверху
     (время записи — управляемые часы журнала), а API подтверждает её содержимое.

Запуск: python tests/browser/test_b05_history.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser, HOUR  # noqa: E402
from wb_energy_meter import change_journal  # noqa: E402
from wb_energy_meter.group_repo_v2 import GroupRepoV2  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def utc(y, m, d, hh=0, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp())


D1 = utc(2026, 1, 1)
D2 = D1 + 24 * HOUR
JOURNAL_START = utc(2025, 12, 30)
SETUP_AT = utc(2025, 12, 31, 12)
EDIT_AT = utc(2026, 1, 5, 12)

CLK = {"t": JOURNAL_START}


def table_rows(page):
    """Строки таблицы отчёта как списки текстов ячеек."""
    return page.evaluate(
        "() => [...document.querySelectorAll('table.rep-table tbody tr')]"
        ".map(tr => [...tr.children].map(td => td.innerText.trim()))")


def run_report(page, h, expect_mode):
    """Нажать «Показать», вернуть (тело запроса интерфейса, ответ API на то же тело)."""
    with page.expect_request(lambda r: r.url.endswith("/api/v2/reports/query")) as ri:
        page.locator("button.btn-primary", has_text="Показать").click()
    body = json.loads(ri.value.post_data)
    page.wait_for_selector("#rv2-result-structure", timeout=8000)
    page.wait_for_function(
        "m => document.querySelector('#rv2-result-structure').innerText.trim() === m",
        arg=expect_mode, timeout=8000)
    page.wait_for_timeout(200)
    api = h.client.post("/api/v2/reports/query", json=body)
    assert api.status_code == 200, api.get_json()
    return body, api.get_json()


def main() -> int:
    # Даты «ГГГГ-ММ-ДД» из полей отчёта сервер разбирает в часовом поясе СВОЕГО процесса
    # (periods.parse_user_datetime), поле `timezone` запроса на это не влияет. Чтобы границы
    # суток в сценарии не зависели от пояса песочницы, процесс теста (а с ним и сервер
    # стенда — он в том же процессе) работает в UTC.
    orig_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    orig_clock = change_journal._clock
    change_journal._clock = lambda: CLK["t"]       # журнал начинается 30.12.2025
    try:
        with Harness() as h:
            # --- данные: две точки, две группы, перенос P1 из «А» в «Б» с 02.01 ---
            CLK["t"] = SETUP_AT
            p1 = h.make_point("P1-HIST", "Станок 1")
            p2 = h.make_point("P2-HIST", "Станок 2")
            h.bind_meter(p1["id"], "dev-hist-1", "Счётчик 1")
            h.bind_meter(p2["id"], "dev-hist-2", "Счётчик 2")
            for i in range(48):
                h.seed_energy("dev-hist-1", 1.0, D1 + i * HOUR, D1 + (i + 1) * HOUR)
                h.seed_energy("dev-hist-2", 2.0, D1 + i * HOUR, D1 + (i + 1) * HOUR)
            groups = GroupRepoV2(h.db)
            ga = groups.add("Группа А")
            gb = groups.add("Группа Б")
            groups.add_member(ga.id, p1["id"], valid_from=0)
            groups.add_member(ga.id, p2["id"], valid_from=0)
            CLK["t"] = D2
            groups.remove_member(ga.id, p1["id"], at=D2)
            groups.add_member(gb.id, p1["id"], valid_from=D2)
            CLK["t"] = EDIT_AT

            bp = open_browser(h.base_url, timezone_id="UTC")
            page = bp.page
            page.wait_for_timeout(1000)
            page.evaluate("document.querySelector('[x-data]')._x_dataStack[0].uiSet='v2'")
            page.wait_for_timeout(300)

            # ===== 1. «Отчёты v2»: две структуры =====
            page.locator("nav button:visible", has_text="Отчёты").click()
            page.wait_for_timeout(800)
            page.select_option("select[x-model='rv2Dimension']", "group")
            page.wait_for_timeout(200)
            page.select_option("select[x-model='rv2ScopeIds']", [str(ga.id), str(gb.id)])
            page.fill("input[x-model='rv2From']", "2026-01-01")
            page.fill("input[x-model='rv2To']", "2026-01-03")

            check(page.input_value("#rv2-structure-mode") == "current",
                  "по умолчанию «Структура сети» = текущая (поведение не изменилось)")

            body, api = run_report(page, h, "текущая")
            check(body.get("structure_mode") == "current",
                  f"в запросе интерфейса structure_mode=current (тело: {body})")
            ui = {r[0]: r for r in table_rows(page)}
            api_rows = {r["name"]: r for r in api["rows"]}
            a_cur = api_rows["Группа А"]["result"]["value"]
            check(abs(a_cur - 144.0) < 1e-6, f"API: «текущая», группа А = 144 (получено {a_cur})")
            check(ui["Группа А"][1] == "144.000",
                  f"экран: «текущая», группа А = 144.000 (на экране {ui['Группа А'][1]!r})")
            b_cur = api_rows["Группа Б"]["result"]
            check(b_cur is None or b_cur["value"] is None,
                  "API: «текущая», у группы Б на начало периода состава нет → значения нет")
            check(ui["Группа Б"][1] == "—", f"экран: группа Б «—» (на экране {ui['Группа Б'][1]!r})")
            check(api["structure_mode"] == "current" and api["assumed_legacy"] is False,
                  "API: ответ подписан структурой current, допущения нет")
            check(not page.locator("#rv2-assumed-legacy").is_visible(),
                  "пометки «история с версии 0.22.0» нет, когда период целиком в истории")

            page.select_option("#rv2-structure-mode", "as_was")
            page.wait_for_timeout(200)
            check(page.locator("select[x-model='rv2CompositionMode']").is_disabled(),
                  "при «как было» выбор «Состав группы» недоступен (состав берётся по интервалам)")
            body, api = run_report(page, h, "как было")
            check(body.get("structure_mode") == "as_was" and body.get("composition_mode") == "as_was",
                  f"в запросе structure_mode=as_was и composition_mode=as_was (тело: {body})")
            ui = {r[0]: r for r in table_rows(page)}
            api_rows = {r["name"]: r for r in api["rows"]}
            a_was = api_rows["Группа А"]["result"]["value"]
            b_was = api_rows["Группа Б"]["result"]["value"]
            check(abs(a_was - 120.0) < 1e-6 and abs(b_was - 24.0) < 1e-6,
                  f"API: «как было», А = 120, Б = 24 (получено {a_was}, {b_was})")
            check(ui["Группа А"][1] == "120.000" and ui["Группа Б"][1] == "24.000",
                  f"экран: «как было», А = 120.000, Б = 24.000 "
                  f"(на экране {ui['Группа А'][1]!r}, {ui['Группа Б'][1]!r})")
            check(a_was != a_cur,
                  "число группы А в двух режимах различается там, где обязано (144 ≠ 120)")
            # structure_intervals описывает схему сети (узлы и линии): она в периоде не менялась,
            # а состав групп делится собственными границами членства — на число не влияет
            check(api["structure_mode"] == "as_was" and len(api["structure_intervals"]) == 1,
                  f"API: ответ подписан as_was; схема сети в периоде не менялась — 1 интервал "
                  f"({len(api['structure_intervals'])})")
            check(page.inner_text("#rv2-result-structure").strip() == "как было",
                  "в шапке результата подписано «структура: как было»")
            bp.screenshot("b05_reports_as_was")

            # ===== 3. CSV =====
            with page.expect_download() as dl:
                page.locator("button", has_text="Скачать CSV").click()
            csv_path = dl.value.path()
            text = open(csv_path, "rb").read().decode("utf-8-sig")
            check("структура: как было" in text, "CSV: в заголовке подписана структура «как было»")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            head = next(ln for ln in lines if ln.startswith("Срез;")).split(";")
            col = head.index("Режим структуры")
            row_a = next(ln.split(";") for ln in lines if ln.startswith("group;%d;" % ga.id))
            check(row_a[col] == "as_was", f"CSV: колонка «Режим структуры» = as_was ({row_a[col]!r})")
            check(abs(float(row_a[4]) - 120.0) < 1e-6, f"CSV: значение группы А = 120 ({row_a[4]!r})")

            # ===== 2. A23: период до начала журнала =====
            page.fill("input[x-model='rv2From']", "2025-12-28")
            page.fill("input[x-model='rv2To']", "2025-12-30")
            body, api = run_report(page, h, "как было")
            check(api["assumed_legacy"] is True and api["journal_started_at"] == JOURNAL_START,
                  f"API: период до начала журнала → assumed_legacy, начало истории {JOURNAL_START}")
            check(page.locator("#rv2-assumed-legacy").is_visible()
                  and "0.22.0" in page.inner_text("#rv2-assumed-legacy"),
                  "на экране пометка «История структуры ведётся с версии 0.22.0…»")

            # ===== 4. «Структура»: история карточки =====
            page.locator("nav button:visible", has_text="Структура").click()
            page.wait_for_timeout(1000)
            # точка без линии — в разделе «Не на схеме» (партия 13, П6: дерево мест
            # убрано, «Структура» строится по схеме)
            page.locator("[data-testid=off-scheme-point]", has_text="dev-hist-1").click()
            page.wait_for_selector("#struct-history-toggle", timeout=8000)
            check(page.locator(".struct-history-item").count() == 0,
                  "история не грузится, пока блок не раскрыт")
            page.locator("#struct-history-toggle").click()
            page.wait_for_selector(".struct-history-item", timeout=8000)
            page.wait_for_timeout(300)

            url = f"/api/v2/change-log?entity_type=metering_point&entity_id={p1['id']}&related=1&limit=20"
            before = h.client.get(url).get_json()["items"]
            ui_n = page.locator(".struct-history-item").count()
            check(ui_n == len(before) and ui_n >= 4,
                  f"в карточке {ui_n} записей = ответу API related=1 ({len(before)}), не меньше 4 "
                  "(точка, привязка, членство в «А», перенос в «Б»)")
            titles = [t for t in page.locator(".struct-history-item").all_inner_texts()]
            check(any("состав группы" in t and "создано" in t for t in titles),
                  "среди записей — «состав группы … создано» (членство, подчинённая сущность)")

            # правка описания в интерфейсе → новая запись сверху
            ta = page.locator("textarea[data-testid=point-description]")
            ta.fill("Щитовая, 2 этаж")
            ta.blur()
            page.wait_for_function(
                "() => { const e = document.querySelector('.struct-history-item');"
                " return e && e.innerText.includes('изменено'); }", timeout=8000)
            page.wait_for_timeout(300)
            top = page.locator(".struct-history-item").first.inner_text()
            check("точка #%d" % p1["id"] in top and "изменено" in top,
                  f"сверху новая запись «точка #{p1['id']} — изменено» (на экране {top!r})")
            check("description" in top and "Щитовая, 2 этаж" in top,
                  "в записи видно, что изменилось: поле description и новое значение")
            check("05.01.2026" in top and "интерфейс" in top,
                  "время записи — по часам журнала (05.01.2026), происхождение — интерфейс")

            top_api = h.client.get(
                f"/api/v2/change-log?entity_type=metering_point&entity_id={p1['id']}&limit=1"
            ).get_json()["items"][0]
            check(top_api["action"] == "update" and top_api["recorded_at"] == EDIT_AT
                  and top_api["new_value"].get("description") == "Щитовая, 2 этаж"
                  and top_api["origin"] == "web",
                  "API подтверждает: update точки, recorded_at = управляемые часы, "
                  "description записан, origin=web")
            after_n = page.locator(".struct-history-item").count()
            check(after_n == len(before) + 1,
                  f"после правки записей стало на одну больше ({len(before)} → {after_n})")
            bp.screenshot("b05_struct_history")

            check(not bp.errors, f"консоль браузера чиста (ошибки: {bp.errors})")
            bp.close()
    finally:
        change_journal._clock = orig_clock
        if orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = orig_tz
        time.tzset()

    if fails:
        print("\nПровалено:", *fails, sep="\n  - ")
        return 1
    print("\nИстория в интерфейсе (партия 12, этап 12.3) — все проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
