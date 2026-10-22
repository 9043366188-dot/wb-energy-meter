"""Партия 15, П13: «Обзор» v2 — раздел «Приборы» (карточки всех приборов из MQTT) — настоящий
Chromium, настоящее Flask-приложение.

Карточка на КАЖДЫЙ прибор из `/api/status` (как у классического «Дашборда»): имя, MQTT ID,
статус, мощность, энергия, напряжения, аптайм, возраст данных, причина статуса и строка
«Точка учёта» (из поля `device_id` снимка). Проблемные приборы — первыми. Раздел живёт на своём
запросе `/api/status` (лента 'status') и не зависит от снимка, итога объекта и проверки схемы.

Сценарии: пустая БД; демо-схема + проблемные и эмулятор; порядок и сводка; единица мощности;
независимость от сбоя снимка/итога/проверки; «устарело» при сбое самого запроса; клик и
клавиатура (то же окно со всеми каналами, что в классике); три ширины без горизонтальной
прокрутки; первая проблемная карточка видна на экране 1280×800 без прокрутки; чистая консоль.

Запуск: python tests/browser/test_b26_overview_devices.py
"""
from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Harness, open_browser  # noqa: E402
from demo_seed import seed_demo  # noqa: E402
from fetch_tools import FETCH_WRAP  # noqa: E402
from wb_energy_meter.model import ControlState, MeterStatus  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
STALE_RE = re.compile(r"устарело, обновлено \d\d:\d\d")
CARD = "[data-testid=ov-device-card]"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def js(page, code: str):
    return page.evaluate(code)


def vis(page, testid: str) -> bool:
    loc = page.locator(f"[data-testid={testid}]")
    return loc.count() > 0 and loc.first.is_visible()


def text(page, testid: str) -> str:
    loc = page.locator(f"[data-testid={testid}]")
    return loc.first.inner_text().strip() if loc.count() else ""


def wait_for(page, expr: str, ms: int = 8000) -> bool:
    try:
        page.wait_for_function(expr, timeout=ms)
        return True
    except Exception:
        return False


def pw_errors(bp) -> list[str]:
    """Ошибки консоли приложения. Исключение — сообщение самого `loadStatus` о запросе, который
    сценарий нарочно «оборвал» через window.__fail (классический код пишет его в консоль)."""
    return [e for e in bp.errors
            if "Failed to load resource" not in e
            and not (e.startswith("TypeError: Failed to fetch") and "loadStatus" in e)]


def open_overview(page) -> None:
    js(page, APP + ".uiSet!=='v2' && " + APP + ".toggleUiSet()")
    page.wait_for_timeout(300)
    page.locator("nav button:visible", has_text="Обзор").click()
    wait_for(page, "() => document.querySelector('[data-testid=ov-devices]') !== null")


def card(page, device_id: str):
    return page.locator(f"{CARD}[data-device-id='{device_id}']")


def ids_in_dom(page) -> list[str]:
    return js(page, "[...document.querySelectorAll('%s')].map(c=>c.dataset.deviceId)" % CARD)


def seed_extra(h, now: float) -> None:
    """К демо-схеме: прибор с ошибкой канала, прибор «нет нагрузки», эмулятор в ваттах."""
    err = h.see_in_mqtt("dev-err", "Насос")
    err.controls["Total P"] = ControlState(name="Total P", value=0.0, raw_value="0",
                                           last_update_ts=now, meta={"type": "power"})
    err.controls["Urms L1"] = ControlState(name="Urms L1", value=None, raw_value="",
                                           last_update_ts=now, error="r", meta={})
    err.last_any_ts = now
    err.status = MeterStatus.DEVICE_ERROR
    err.status_reason = "Ошибки каналов: Urms L1"

    nm = h.see_in_mqtt("dev-idle", "Склад")
    nm.controls["Total P"] = ControlState(name="Total P", value=0.0, raw_value="0",
                                          last_update_ts=now, meta={"type": "power"})
    nm.last_any_ts = now
    nm.status = MeterStatus.NO_MEASUREMENT
    nm.status_reason = "Связь со счётчиком есть, нет нагрузки (измерений ещё не было)"

    emu = h.see_in_mqtt("emu-map3e-1", "Эмулятор 1")
    emu.controls["Total P"] = ControlState(name="Total P", value=1500.0, raw_value="1500",
                                           last_update_ts=now, meta={"type": "power", "units": "W"})
    emu.controls["Total AP energy"] = ControlState(name="Total AP energy", value=10.5, raw_value="10.5",
                                                   last_update_ts=now, meta={"type": "value"})
    emu.last_any_ts = now
    emu.last_measurement_ts = now
    emu.status = MeterStatus.OK

    sock = h.registry.get("dev-sock")
    sock.status = MeterStatus.WARNING
    sock.status_reason = "L1: пониженное 190 В"
    h.registry.get("dev-free").status = MeterStatus.UNKNOWN


def main() -> int:
    # ===== 1. Пустая БД: раздел есть, подсказка, карточек нет =====
    with Harness() as h:
        bp = open_browser(h.base_url, init_script=FETCH_WRAP)
        page = bp.page
        page.wait_for_timeout(1000)
        open_overview(page)
        check(wait_for(page, "() => document.querySelector('[data-testid=ov-devices-empty]') !== null"),
              "пустая БД: показана подсказка, что приборов в MQTT пока нет")
        check(page.locator(CARD).count() == 0, "пустая БД: карточек приборов нет")
        check(text(page, "ov-devices-summary") == "", "пустая БД: сводка в заголовке пуста")
        check(not vis(page, "ov-devices-error"), "пустая БД: ошибки нет")
        check(not pw_errors(bp), f"пустая БД: консоль чистая ({pw_errors(bp)[:3]})")
        bp.close()

    # ===== 2. Демо-схема + проблемные приборы + эмулятор =====
    with Harness() as h:
        info = seed_demo(h)
        now = info["now"]
        seed_extra(h, now)
        bp = open_browser(h.base_url, init_script=FETCH_WRAP)
        page = bp.page
        page.wait_for_timeout(1000)
        open_overview(page)
        check(wait_for(page, "() => document.querySelectorAll('%s').length > 0" % CARD),
              "карточки приборов появились")
        page.wait_for_timeout(600)

        status = h.client.get("/api/status").get_json()
        api_ids = {m["device_id"] for m in status["meters"]}
        dom_ids = ids_in_dom(page)
        check(set(dom_ids) == api_ids and len(dom_ids) == len(api_ids) == 7,
              f"карточка на каждый прибор из /api/status: {len(dom_ids)} из {len(api_ids)}")

        # --- порядок: проблемные первыми, внутри ранга — по имени ---
        expected = [m["device_id"] for m in sorted(
            status["meters"],
            key=lambda m: ({"device_error": 0, "no_connection": 1, "incomplete": 2, "warning": 3,
                            "unconfirmed": 4, "unknown": 5, "no_measurement": 6, "ok": 7}[m["status"]],
                            (m["display_name"] or "").lower()))]
        check(dom_ids[:2] == ["dev-err", "dev-sock"],
              f"порядок: сначала ошибка, затем предупреждение ({dom_ids[:2]})")
        statuses = [card(page, d).get_attribute("data-status") for d in dom_ids]
        rank = {"device_error": 0, "no_connection": 1, "incomplete": 2, "warning": 3,
                "unconfirmed": 4, "unknown": 5, "no_measurement": 6, "ok": 7}
        check([rank[s] for s in statuses] == sorted(rank[s] for s in statuses),
              f"порядок: статусы не убывают по важности ({statuses})")
        check(set(dom_ids[:4]) == set(expected[:4]),
              "порядок: первые четыре — приборы с проблемами (ошибка, предупреждение, неизвестно, нет нагрузки)")
        problems = [card(page, d).get_attribute("data-problem") for d in dom_ids]
        check(problems == ["1", "1", "1", "0", "0", "0", "0"],
              f"признак проблемы: ошибка, предупреждение, неизвестно — да; «нет нагрузки» и норма — нет ({problems})")

        # --- сводка ---
        summ = text(page, "ov-devices-summary")
        check(summ.startswith("7 приборов:") and "3 в норме" in summ and "1 ошибка" in summ
              and "1 нет нагрузки" in summ and "1 внимание" in summ,
              f"сводка в заголовке: {summ!r}")

        # --- содержимое карточки как в классике ---
        c_in = card(page, "dev-in")
        m_in = next(m for m in status["meters"] if m["device_id"] == "dev-in")
        check(c_in.locator("[data-testid=ov-device-id]").inner_text().strip() == "MQTT ID: dev-in",
              "карточка: MQTT ID")
        check(c_in.locator(".nm").inner_text().strip() == m_in["display_name"], "карточка: имя прибора")
        check(c_in.locator("[data-testid=ov-device-status]").inner_text().strip()
              == js(page, APP + ".statusLabel('ok')"),
              "карточка: подпись статуса — та же, что у классической карточки")
        check(c_in.locator("[data-testid=ov-device-power]").inner_text().strip()
              == "%.3f" % m_in["main_values"]["Total P"],
              "карточка: мощность — как у классической карточки (без meta единицы значение уже в кВт)")
        check(c_in.locator("[data-testid=ov-device-energy]").inner_text().strip()
              == "%.2f" % m_in["main_values"]["Total AP energy"], "карточка: энергия")
        check(c_in.locator("[data-testid=ov-device-age]").inner_text().strip() not in ("", "—"),
              "карточка: возраст данных")
        err_card = card(page, "dev-err")
        check("Ошибки каналов" in err_card.locator("[data-testid=ov-device-reason]").inner_text(),
              "проблемная карточка: причина статуса показана")
        check(c_in.locator("[data-testid=ov-device-reason]").count() == 0,
              "карточка «норма»: причины нет")

        # --- эмулятор: бейдж и перевод Вт → кВт по meta прибора ---
        emu = card(page, "emu-map3e-1")
        check(emu.locator("[data-testid=emu-badge-device]").is_visible(), "эмулятор: бейдж «эмулятор»")
        check(c_in.locator("[data-testid=emu-badge-device]").is_visible() is False,
              "реальный прибор: бейджа «эмулятор» нет")
        check(emu.locator("[data-testid=ov-device-power]").inner_text().strip() == "1.500",
              f"эмулятор: 1500 Вт показаны как 1.500 кВт ({emu.locator('[data-testid=ov-device-power]').inner_text()!r})")

        # --- точка учёта ---
        ptxt = lambda d: card(page, d).locator("[data-testid=ov-device-point]").inner_text().strip()
        check("«Ввод»" in ptxt("dev-in") and card(page, "dev-in").get_attribute("data-device-id") == "dev-in"
              and card(page, "dev-in").locator("[data-testid=ov-device-point]").get_attribute("data-point-kind") == "bound",
              f"точка учёта: привязанный прибор → название точки ({ptxt('dev-in')!r})")
        check("не привязан" in ptxt("dev-free")
              and card(page, "dev-free").locator("[data-testid=ov-device-add-meter]").is_visible(),
              f"точка учёта: свободный прибор → «не привязан» и «+ Счётчик» ({ptxt('dev-free')!r})")
        check(not card(page, "dev-in").locator("[data-testid=ov-device-add-meter]").is_visible(),
              "точка учёта: у привязанного прибора «+ Счётчик» нет")

        # --- сверка со снимком: у каждой точки снимка её прибор — привязан ---
        snap_dev = js(page, "(" + APP + ".overviewV2Snapshot.points||[]).map(p=>p.device_id).filter(Boolean)")
        check(set(snap_dev) >= {"dev-in", "dev-light", "dev-sock"},
              f"снимок: поле device_id у точек ({sorted(snap_dev)})")

        # ===== 3. Независимость от снимка / итога / проверки =====
        js(page, "window.__fail=['/api/v2/snapshot','/api/v2/overview/summary','/api/v2/validation']")
        js(page, "(async()=>{ const a=%s; a.overviewV2Snapshot=null; await a.loadOverviewV2Snapshot();"
                 " await a.loadOvSummary(); await a.loadValidation(); })()" % APP)
        page.wait_for_timeout(500)
        check(page.locator(CARD).count() == 7, "сбой снимка, итога и проверки: все 7 карточек на месте")
        check(not vis(page, "ov-devices-error"), "сбой снимка, итога и проверки: у раздела «Приборы» ошибки нет")
        check(not vis(page, "ov-devices-stale"), "сбой снимка, итога и проверки: «устарело» у приборов нет")
        check("не удалось определить" in ptxt("dev-in")
              and card(page, "dev-in").locator("[data-testid=ov-device-point]").get_attribute("data-point-kind") == "unknown",
              f"без снимка точка учёта «не удалось определить», а не «не привязан» ({ptxt('dev-in')!r})")
        check(not card(page, "dev-free").locator("[data-testid=ov-device-add-meter]").is_visible(),
              "без снимка «+ Счётчик» не предлагается (привязка неизвестна)")
        js(page, "window.__fail=[]")
        js(page, APP + ".loadOverviewV2Snapshot()")
        wait_for(page, "() => %s.overviewV2Snapshot && %s.overviewV2Snapshot.points" % (APP, APP))
        page.wait_for_timeout(300)
        check("«Ввод»" in ptxt("dev-in"), "снимок вернулся — точка учёта снова названа")

        # ===== 4. Сбой самого запроса /api/status: прежние карточки + «устарело» =====
        before = ids_in_dom(page)
        js(page, "window.__fail=['/api/status']")
        js(page, APP + ".loadStatus()")
        page.wait_for_timeout(500)
        check(vis(page, "ov-devices-stale") and STALE_RE.search(text(page, "ov-devices-stale")) is not None,
              f"сбой /api/status: «устарело, обновлено ЧЧ:ММ» ({text(page, 'ov-devices-stale')!r})")
        check(ids_in_dom(page) == before, "сбой /api/status: прежние карточки остались на экране")
        check(vis(page, "ov-devices-error"), "сбой /api/status: сообщение об ошибке рядом с карточками")
        js(page, "window.__fail=[]")
        js(page, APP + ".loadStatus()")
        page.wait_for_timeout(500)
        check(not vis(page, "ov-devices-stale") and not vis(page, "ov-devices-error"),
              "после восстановления «устарело» и ошибка ушли")

        # ===== 5. Живое обновление: прибор пропал связью → порядок и сводка меняются =====
        h.registry.get("dev-in").status = MeterStatus.NO_CONNECTION
        h.registry.get("dev-in").status_reason = "Нет сообщений от устройства 5.0 мин"
        ok = wait_for(page, "() => document.querySelector('%s').dataset.deviceId === 'dev-err'"
                            " && [...document.querySelectorAll('%s')][1].dataset.deviceId === 'dev-in'" % (CARD, CARD),
                      ms=12000)
        check(ok, "живое обновление: «нет связи» поднял прибор выше предупреждения (общий опрос без нажатий)")
        check("1 нет связи" in text(page, "ov-devices-summary"), f"сводка обновилась: {text(page, 'ov-devices-summary')!r}")
        h.registry.get("dev-in").status = MeterStatus.OK
        h.registry.get("dev-in").status_reason = ""

        # ===== 6. Клик и клавиатура: то же окно со всеми каналами, что в классике =====
        n_controls = next(m for m in h.client.get("/api/status").get_json()["meters"]
                          if m["device_id"] == "dev-in")["controls_count"]
        card(page, "dev-in").click()
        check(wait_for(page, "() => %s.detail && %s.detail.device_id==='dev-in'" % (APP, APP)),
              "клик по карточке открывает окно прибора")
        page.wait_for_timeout(300)
        n_ctrl = page.locator(".modal .ctrl").count()
        check(n_ctrl == n_controls and n_ctrl > 0,
              f"окно прибора: показаны все каналы ({n_ctrl} из {n_controls})")
        js(page, APP + ".closeDetail()")
        page.wait_for_timeout(200)
        card(page, "dev-light").focus()
        page.keyboard.press("Enter")
        check(wait_for(page, "() => %s.detail && %s.detail.device_id==='dev-light'" % (APP, APP)),
              "Enter на сфокусированной карточке открывает окно прибора")
        js(page, APP + ".closeDetail()")
        page.wait_for_timeout(200)
        check(card(page, "dev-in").get_attribute("tabindex") == "0" and card(page, "dev-in").get_attribute("role") == "button"
              and "Прибор" in (card(page, "dev-in").get_attribute("aria-label") or ""),
              "карточка доступна с клавиатуры: tabindex, role, aria-label")

        # «+ Счётчик» у свободного прибора ведёт в «Структуру»
        card(page, "dev-free").locator("[data-testid=ov-device-add-meter]").click()
        page.wait_for_timeout(500)
        check(js(page, APP + ".tab") == "structurev2" and js(page, APP + ".detail") is None,
              "«+ Счётчик» у свободного прибора открывает «Структуру» (окно прибора не открывается)")
        open_overview(page)
        page.wait_for_timeout(500)

        # ===== 7. Ширины: без горизонтальной прокрутки, текст читается =====
        for w, hgt in ((390, 800), (768, 900), (1280, 800)):
            page.set_viewport_size({"width": w, "height": hgt})
            page.wait_for_timeout(400)
            sw = js(page, "document.documentElement.scrollWidth")
            check(sw <= w, f"{w}px: нет горизонтальной прокрутки страницы (scrollWidth={sw})")
            over = js(page, """[...document.querySelectorAll('%s')].filter(c=>{
                const r=c.getBoundingClientRect(); return r.right>window.innerWidth+1 || r.left<-1; }).length""" % CARD)
            check(over == 0, f"{w}px: ни одна карточка не выходит за экран ({over})")
            if w == 1280:
                first = js(page, """(()=>{ const c=document.querySelector('%s'); c.scrollIntoView({block:'nearest'});
                    window.scrollTo(0,0); const r=c.getBoundingClientRect();
                    return {top:r.top, bottom:r.bottom, h:window.innerHeight, id:c.dataset.deviceId}; })()""" % CARD)
                check(first["bottom"] <= first["h"],
                      f"1280×800: первая проблемная карточка целиком видна без прокрутки ({first})")
                bp.screenshot("b26_overview_devices_1280")
        page.set_viewport_size({"width": 1400, "height": 1000})

        check(not pw_errors(bp), f"консоль без ошибок приложения ({pw_errors(bp)[:3]})")
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
