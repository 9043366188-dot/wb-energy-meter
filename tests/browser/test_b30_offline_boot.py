"""Партия 15, A37 — «без интернета; нет Alpine; нет MQTT» в настоящем Chromium.

Матрица приёмки (ТЗ §13, A37). Раньше проверялось статически (вендорные файлы лежат в пакете,
в `index.html` нет внешних адресов, заглушка есть в разметке). Здесь — настоящий браузер, у
которого интернета нет вообще: каждый запрос не на адрес стенда отклоняется и записывается.

Сценарии:
  1. Нет Alpine. Файл `/static/vendor/alpine.min.js` не отдаётся. Ожидание: вместо белого экрана
     показана заглушка «Интерфейс не запустился» с именем файла, командой `curl` по этому файлу и
     подсказкой `journalctl`; заглушка появляется сразу по ошибке загрузки, а не через 1,5 секунды.
  2. Нет MQTT. Брокера нет, приборы привязаны к точкам, но ни разу не появлялись в сети. Ожидание:
     в шапке «MQTT offline»; заглушки нет (интерфейс запустился); на «Обзоре» плитка «Требует
     внимания» равна числу точек, каждая точка названа в списке с причиной «устройство ещё не
     подключалось» и бейджем «Не подключалось», в таблице «Все точки учёта» все точки на месте, раздел
     «Приборы» говорит, что в MQTT пока никого нет; числа мощности и энергии — «—», а не нули;
     вкладки «Структура» и «Отчёты» открываются, точки в них видны.
  3. Оба сценария: ни одного запроса за пределы стенда (то есть интернет интерфейсу не нужен) и
     ни одной ошибки приложения в консоли (кроме нарочно отклонённого файла в сценарии 1).

Запуск: python tests/browser/test_b30_offline_boot.py
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright  # noqa: E402

from harness import BrowserPage, Harness  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
ALPINE = "/static/vendor/alpine.min.js"
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


def open_offline(base_url: str, block: tuple = ()):
    """Chromium без интернета: всё, что не адрес стенда, отклоняется и попадает в `external`;
    пути из `block` (внутри стенда) отклоняются как «файл не отдаётся»."""
    pw = sync_playwright().start()
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1000}, timezone_id="UTC")
    bp = BrowserPage(pw, browser, page)
    external: list[str] = []
    blocked: list[str] = []

    def handler(route):
        url = route.request.url
        if not url.startswith(base_url):
            external.append(url)
            route.abort()
        elif any(urlparse(url).path.endswith(b) for b in block):  # у файлов есть ?v=версия
            blocked.append(url)
            route.abort()
        else:
            route.continue_()

    page.route("**/*", handler)
    page.goto(base_url)
    return bp, external, blocked


# ---------------------------------------------------------------- 1. нет Alpine
def scenario_no_alpine(h) -> None:
    print("\n== A37.1: нет Alpine — заглушка вместо белого экрана ==")
    bp, external, blocked = open_offline(h.base_url, block=(ALPINE,))
    page = bp.page
    shown = wait_for(page, "() => { const b = document.getElementById('boot-error');"
                           " return b && getComputedStyle(b).display !== 'none'; }", ms=1200)
    check(blocked, "файл Alpine отклонён (сценарий настроен верно)")
    check(shown, "заглушка #boot-error показана раньше, чем сработал таймер 1,5 с (по ошибке загрузки)")
    box = page.locator("#boot-error").inner_text()
    check("Интерфейс не запустился" in box, f"заголовок заглушки ({box[:40]!r})")
    check(ALPINE in page.locator("#boot-error-missing").inner_text(),
          "в заглушке названа потерянная пара: " + ALPINE)
    check(ALPINE in page.locator("#boot-error-curl").inner_text(), "команда curl указывает на тот же файл")
    check("journalctl -u wb-energy-meter" in box, "есть подсказка с journalctl")
    covers = page.evaluate("""() => { const r = document.getElementById('boot-error').getBoundingClientRect();
        return r.width >= innerWidth - 1 && r.height >= innerHeight - 1; }""")
    check(covers, "заглушка закрывает всё окно")
    check(js(page, "typeof window.Alpine") == "undefined", "Alpine действительно не загружен")
    check(not external, f"запросов за пределы стенда нет ({external[:3]})")
    bp.screenshot("b30_no_alpine_stub")
    bp.close()


# ---------------------------------------------------------------- 2. нет MQTT
def seed_unseen(h) -> list:
    vv = h.make_node("vv", "Ввод", "source")
    sh = h.make_node("sh", "ЩР-1", "panel")
    e_in = h.connect(vv["id"], sh["id"])
    names = ["Ввод объекта"]
    pin = h.make_point("pt-in", "Ввод объекта")
    h.bind_meter(pin["id"], "dev-in", "Счётчик ввода")
    h.set_meter_on_edge(e_in["id"], pin["id"])
    for dev, name in (("dev-light", "Освещение"), ("dev-sock", "Розетки")):
        c = h.add_consumer(sh["id"], name)
        pt = h.make_point("pt-" + dev, name)
        h.bind_meter(pt["id"], dev, name)
        h.set_meter_on_edge(c["edge"]["id"], pt["id"])
        names.append(name)
    return names


def scenario_no_mqtt(h, names) -> None:
    print("\n== A37.2: нет MQTT — точки видны, у каждой названа причина ==")
    bp, external, blocked = open_offline(h.base_url)
    page = bp.page
    page.wait_for_timeout(1200)
    check(js(page, "typeof window.Alpine") == "object", "Alpine загружен и интерфейс запустился")
    check(page.locator("#boot-error").count() == 0, "заглушка снята — интерфейс стартовал")
    js(page, APP + ".uiSet!=='v2' && " + APP + ".toggleUiSet()")
    page.wait_for_timeout(300)
    page.locator("nav button:visible", has_text="Обзор").click()
    wait_for(page, "() => document.querySelector('[data-testid=ov-devices]') !== null")
    wait_for(page, "() => document.body.innerText.includes('MQTT offline')")
    page.wait_for_timeout(1200)

    head = page.locator(".conn").inner_text()
    check("MQTT offline" in head, f"в шапке «MQTT offline» ({head!r})")
    devices_empty = page.locator("[data-testid=ov-devices-empty]")
    check(devices_empty.count() > 0 and "Ни один прибор пока не появился в MQTT" in devices_empty.first.inner_text(),
          "раздел «Приборы»: «Ни один прибор пока не появился в MQTT»")
    body = page.locator("body").inner_text()
    n = len(names)
    tile = page.evaluate("""() => { const t = [...document.querySelectorAll('.tile')]
        .find(x => x.querySelector('.lbl') && x.querySelector('.lbl').innerText.trim().toLowerCase() === 'требует внимания');
        return t ? t.querySelector('.val').innerText.trim() : null; }""")
    check(tile == str(n), f"плитка «Требует внимания» = {n} (по числу точек) ({tile!r})")
    for name in names:
        check(name in body, f"точка «{name}» видна на «Обзоре»")
    check(body.count("устройство ещё не подключалось") == n,
          f"у каждой из {n} точек названа причина «устройство ещё не подключалось» "
          f"(встречается {body.count('устройство ещё не подключалось')} раз)")
    badges = page.evaluate("() => [...document.querySelectorAll('.badge')].map(b => b.innerText.trim().toLowerCase())")
    check(badges.count("не подключалось") >= n, f"бейджи «Не подключалось» у всех точек ({badges})")
    rows = page.evaluate("""() => { const t = [...document.querySelectorAll('table.rep-table')]
        .find(x => x.innerText.toLowerCase().includes('обновлено'));
        return t ? [...t.querySelectorAll('tbody tr')].map(tr => [...tr.children].map(td => td.innerText.trim())) : null; }""")
    check(rows is not None and len(rows) == n, f"«Все точки учёта»: {n} строк ({0 if rows is None else len(rows)})")
    if rows:
        zeros = [r for r in rows if any(c in ("0", "0.0", "0.00", "0.000") for c in r[3:5])]
        check(not zeros, f"мощность и энергия без данных — «—», а не нули ({rows[0]})")
    for label in ("Мощность сейчас", "Энергия (снимок)"):
        val = page.evaluate("""(lbl) => { const t = [...document.querySelectorAll('.tile')]
            .find(x => x.querySelector('.lbl') && x.querySelector('.lbl').innerText.trim().toLowerCase() === lbl.toLowerCase());
            return t ? t.querySelector('.val').innerText.trim() : null; }""", label)
        check(val is not None and val.startswith("—"), f"плитка «{label}» — «—» ({val!r})")
    bp.screenshot("b30_no_mqtt_overview")

    page.locator("nav button:visible", has_text="Структура").click()
    page.wait_for_timeout(1200)
    tree = page.locator("body").inner_text()
    check(all(name in tree for name in names), "«Структура»: все точки видны в дереве схемы")
    page.locator("nav button:visible", has_text="Отчёты").click()
    page.wait_for_timeout(800)
    check(page.locator("body").inner_text().count("Показать") > 0, "«Отчёты» открываются")

    check(not external, f"запросов за пределы стенда нет ({external[:3]})")
    errs = [e for e in bp.errors if "Failed to load resource" not in e]
    check(not errs, f"консоль без ошибок приложения ({errs[:3]})")
    bp.close()


def main() -> int:
    with Harness() as h:
        scenario_no_alpine(h)
    with Harness() as h:
        names = seed_unseen(h)
        scenario_no_mqtt(h, names)
    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проверок не прошли")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
