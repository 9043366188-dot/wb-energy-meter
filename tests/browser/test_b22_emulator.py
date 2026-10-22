"""Партия находок 0.23.1, П12: «Эмулятор счётчиков» в интерфейсе — от переключателя
в «Настройках» до стёртых топиков (настоящий Chromium, настоящее Flask-приложение).

Эмулятор подключён к стенду «петлёй» (tests/emu_rig.py): всё, что он публикует, попадает в
реестр приборов, как из брокера, и в журнал «истории», как будто её пишет wb-mqtt-db.
Часы управляются тестом и стоят в завтрашнем дне: привязки, которые создаёт интерфейс
(«сегодня»), действуют на весь период данных. Часовые итоги считает боевая
`compute_hourly_aggregate` из истории эмулятора, а не формула теста.

Сценарий:

  1. «Настройки» (v2): раздел «Эмулятор счётчиков» есть, переключатель выключен по умолчанию,
     приборов нет; баннера в «Обзоре» нет.
  2. «Добавить типовой набор» → 4 строки с меткой «эмулятор»; пока выключено, кнопки
     неисправностей недоступны. Включение: «Включён», кнопки доступны, приборы в реестре.
     Проба истории: wb-mqtt-db «молчит» → предупреждение в «Настройках» и «Обзоре», после
     появления истории — предупреждение уходит.
  3. «Обзор»: баннер «Включён эмулятор: 4 виртуальных прибора — цифры с ними не настоящие».
  4. «Структура»: схема Ввод → Щит; «+ Счётчик»: в списке приборов у каждого прибора
     эмулятора слово «эмулятор» в названии, у настоящего — нет. Ввод щита — эмулятор-ввод
     (сумма нагрузок + 3 %); один эмулятор «по фазам» на три нагрузки, два — на
     «Станок» и «Печь».
  5. Сутки данных. Баланс щита в карточке = ответу API = заданным 3 % от ввода
     (3 / 103 = 2,91 %); в «Обзоре» у точек эмулятора метка, у настоящей точки её нет.
  6. Неисправности кнопками: «Сброс» (журнал со временем), «Нет связи» (на каналах `r`,
     потом ошибка снимается).
  7. «Отчёты»: у строки эмулятора метка и флаг reset, над таблицей — предупреждение; у
     настоящей точки ни метки, ни предупреждения; CSV: колонка «Эмулятор» и строка-предупреждение.
  8. Выключение: «Выключен»; эмулятор стёр retained-топики всех своих приборов; прибор
     без точки исчез из списков, привязанные остались; баннера нет. «Архивировать точки
     эмулятора» — подтверждение, точки эмулятора в архиве, настоящая точка на месте.

Запуск: python tests/browser/test_b22_emulator.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from harness import Harness, open_browser, HOUR  # noqa: E402
from waits import wait_card_open  # noqa: E402
import emu_rig  # noqa: E402
from emu_rig import Clock, Loopback, run, fill_aggregates  # noqa: E402

from wb_energy_meter import emulator as em  # noqa: E402
from wb_energy_meter.mqtt_client import MqttService  # noqa: E402
from wb_energy_meter.repo import KvRepo  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
REAL = "wb-real_1"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class Recording(Loopback):
    """Петля, которая запоминает, какие retained-топики были стёрты (пустое сообщение)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.erased: set[str] = set()

    def publish(self, topic, payload, retain=True, qos=1):
        if payload == "" and retain:
            self.erased.add(topic)
        super().publish(topic, payload, retain=retain, qos=qos)


class Probe:
    """Что отвечает «wb-mqtt-db» на вопрос про историю эмулятора."""

    def __init__(self):
        self.points = 0

    def __call__(self, device, control, ts_from):
        return self.points


def api(page, path):
    return page.evaluate(f"fetch({path!r}).then(r=>r.json())")


def js(page, code):
    return page.evaluate(code)


def vis(page, testid, scope=""):
    loc = page.locator(f"{scope} [data-testid={testid}]".strip())
    return loc.count() > 0 and loc.first.is_visible()


def text(page, testid, scope=""):
    loc = page.locator(f"{scope} [data-testid={testid}]".strip())
    return loc.first.inner_text().strip() if loc.count() else ""


def nav(page, label):
    page.locator("nav button:visible", has_text=label).click()
    page.wait_for_timeout(500)


def wait_js(page, expr, ms=8000):
    try:
        page.wait_for_function(expr, timeout=ms)
        return True
    except Exception:
        return False


def card(page):
    return page.locator("[data-testid=node-card]:visible")


def device_options(page):
    return page.locator("[data-testid=node-meter-device]:visible option").evaluate_all(
        "els => els.filter(e => e.value).map(e => ({t: e.textContent.trim(), v: e.value, d: e.disabled}))")


def open_form(page):
    card(page).locator("button", has_text="+ Счётчик").click()
    page.wait_for_timeout(300)


def pick_outgoing(page):
    page.locator("[data-testid=node-meter-form]:visible label",
                 has_text="Отходящую линию").locator("input").check()
    page.wait_for_timeout(200)


def open_node(page, node_id):
    page.evaluate(APP + f".openInspectorInPlace('node', {node_id})")
    wait_card_open(page)
    page.wait_for_timeout(300)


def set_emulator_toggle(page, on: bool):
    box = page.locator("[data-testid=emu-toggle]")
    box.set_checked(on)
    want = "Включён" if on else "Выключен"
    page.wait_for_function(
        "w => document.querySelector('[data-testid=emu-toggle-label]').textContent.trim() === w",
        arg=want, timeout=8000)


def main() -> int:
    with Harness() as h:
        # Даты периода сервер читает по своему поясу (как в test_b13): сетка часов эмулятора
        # начинается с локальной полуночи завтрашнего дня, и «Обзор»/«Отчёты» видят те же сутки.
        tomorrow = datetime.now().date() + timedelta(days=1)
        ov_from = tomorrow.strftime("%Y-%m-%d")
        day_start = int(datetime.strptime(ov_from, "%Y-%m-%d").timestamp())
        ov_to = (tomorrow + timedelta(days=1)).strftime("%Y-%m-%d")
        d2_from = ov_to
        d2_to = (tomorrow + timedelta(days=2)).strftime("%Y-%m-%d")

        clock = Clock(day_start)
        mqtt = MqttService("127.0.0.1", 1883, h.registry, "wb-map3e_")
        loop = Recording(mqtt, clock)
        probe = Probe()
        emu = em.EmulatorService(
            KvRepo(h.db), publisher_factory=lambda: loop, registry=h.registry,
            meters_repo=h.meters_repo, history_probe=probe, clock=clock, utc_offset_s=0)
        h.state.emulator = emu

        h.see_in_mqtt(REAL, "Настоящий счётчик", phases=True)
        real_point = h.make_point("real-1", "Настоящая точка")
        h.bind_meter(real_point["id"], REAL, "Настоящий счётчик")
        src = h.make_node("in", "Ввод объекта", "source")
        panel = h.make_node("p1", "Щит 1", "panel")
        h.connect(src["id"], panel["id"])

        bp = open_browser(h.base_url, timezone_id="UTC")
        page = bp.page
        page.wait_for_timeout(1000)
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(300)

        # ===== 1. раздел есть, выключен, пуст; баннера нет =====
        nav(page, "Настройки")
        page.wait_for_selector("[data-testid=settings-emulator]", timeout=8000)
        check(page.locator("main h2:visible", has_text="Эмулятор счётчиков").count() == 1,
              "«Настройки»: раздел «Эмулятор счётчиков»")
        check(not page.locator("[data-testid=emu-toggle]").is_checked()
              and text(page, "emu-toggle-label") == "Выключен",
              "по умолчанию эмулятор выключен")
        check(vis(page, "emu-empty"), "приборов нет — сказано, что делать")
        nav(page, "Обзор")
        check(not vis(page, "emu-banner"), "«Обзор»: баннера эмулятора нет, пока он выключен")

        # ===== 2. типовой набор, включение, проба истории =====
        nav(page, "Настройки")
        page.click("[data-testid=emu-add-preset]")
        page.wait_for_function(
            "() => document.querySelectorAll('[data-testid^=emu-device-]').length === 4", timeout=8000)
        rows = page.locator("[data-testid^=emu-device-]")
        check(all("эмулятор" in rows.nth(i).inner_text().lower() for i in range(4)),
              "в каждой из четырёх строк — метка «эмулятор»")
        check(page.locator("[data-testid=emu-fault-reset-1]").is_disabled(),
              "пока эмулятор выключен, кнопки неисправностей недоступны")
        st = api(page, "/api/v2/emulator")
        check([d["device_id"] for d in st["devices"]]
              == ["emu-map3e-1", "emu-map3e-2", "emu-map3e-3", "emu-map3e-4"]
              and st["devices"][3]["role"] == "input" and st["devices"][3]["pct"] == 3.0,
              "типовой набор: три нагрузки и ввод над ними (+3 %)")

        set_emulator_toggle(page, True)
        check(page.locator("[data-testid=emu-fault-reset-1]").is_enabled(),
              "включён — кнопки неисправностей доступны")
        check(all(h.registry.get(d["device_id"]) is not None for d in st["devices"]),
              "приборы эмулятора появились в реестре (как из брокера)")
        check(api(page, "/api/v2/emulator")["enabled"] is True, "API: эмулятор включён")

        page.click("[data-testid=emu-probe-history]")
        page.wait_for_selector("[data-testid=emu-history-warning]:visible", timeout=8000)
        warn = text(page, "emu-history-warning")
        check("wb-mqtt-db не пишет историю эмулятора" in warn and "группы каналов" in warn,
              f"wb-mqtt-db молчит → предупреждение про группы каналов ({warn!r})")
        nav(page, "Обзор")
        page.wait_for_selector("[data-testid=emu-banner-history]:visible", timeout=8000)
        check("wb-mqtt-db не пишет историю эмулятора" in text(page, "emu-banner-history"),
              "то же предупреждение — в «Обзоре»")
        probe.points = 3
        nav(page, "Настройки")
        page.click("[data-testid=emu-probe-history]")
        page.wait_for_function(
            "() => !document.querySelector('[data-testid=emu-history-warning]').offsetParent", timeout=8000)
        check("история эмулятора в wb-mqtt-db есть" in text(page, "emu-history-state"),
              "история появилась → предупреждение ушло, состояние «история есть»")

        # ===== 3. баннер в «Обзоре» =====
        nav(page, "Обзор")
        page.wait_for_selector("[data-testid=emu-banner]:visible", timeout=8000)
        check(text(page, "emu-banner")
              == "Включён эмулятор: 4 виртуальных прибора — цифры с ними не настоящие",
              f"«Обзор»: баннер ({text(page, 'emu-banner')!r})")
        check(not vis(page, "emu-banner-history"), "предупреждения про историю в «Обзоре» больше нет")

        # ===== 4. «Структура»: приборы эмулятора в «+ Счётчик» =====
        nav(page, "Структура")
        page.wait_for_timeout(1200)
        open_node(page, panel["id"])
        open_form(page)
        opts = device_options(page)
        emu_opts = [o for o in opts if o["v"].startswith("emu-")]
        real_opts = [o for o in opts if o["v"] == REAL]
        check(len(emu_opts) == 4 and all("эмулятор" in o["t"].lower() for o in emu_opts),
              f"в «+ Счётчик» у всех приборов эмулятора «эмулятор» в названии: {[o['t'] for o in emu_opts]}")
        check(len(real_opts) == 1 and "эмулятор" not in real_opts[0]["t"].lower(),
              f"у настоящего прибора метки нет: {[o['t'] for o in real_opts]}")

        # ввод щита: эмулятор-ввод (линия Ввод → Щит уже есть)
        page.select_option("[data-testid=node-meter-device]:visible", "emu-map3e-4")
        page.click("[data-testid=node-meter-submit]:visible")
        page.wait_for_timeout(1500)

        # один эмулятор «по фазам» на три нагрузки
        open_form(page)
        pick_outgoing(page)
        page.select_option("[data-testid=node-meter-device]:visible", "emu-map3e-1")
        page.wait_for_timeout(200)
        phases_radio = page.locator("[data-testid=node-meter-mode-phases]:visible")
        check(phases_radio.is_enabled(), "у прибора эмулятора есть пофазные счётчики → «по фазам» доступно")
        phases_radio.check()
        page.wait_for_timeout(200)
        for ph, name in (("L1", "Освещение"), ("L2", "Розетки"), ("L3", "Вентиляция")):
            page.select_option(f"[data-testid=node-meter-phase-target-{ph}]:visible", "new")
            page.fill(f"[data-testid=node-meter-phase-new-{ph}]:visible", name)
        page.click("[data-testid=node-meter-submit]:visible")
        page.wait_for_timeout(2000)

        # два остальных — целиком на «Станок» и «Печь»
        for dev, load in (("emu-map3e-2", "Станок"), ("emu-map3e-3", "Печь")):
            open_form(page)
            page.select_option("[data-testid=node-meter-device]:visible", dev)
            pick_outgoing(page)
            page.fill("[data-testid=node-meter-new-load]:visible", load)
            page.click("[data-testid=node-meter-submit]:visible")
            page.wait_for_timeout(1800)
        sp = api(page, "/api/v2/structure/points")
        by_dev = {}
        for p in sp["points"]:
            by_dev.setdefault(p["meter_device_id"], []).append(p)
        check(len(sp["points"]) == 7 and len(by_dev["emu-map3e-1"]) == 3
              and all(p["is_emulator"] for p in sp["points"] if p["meter_device_id"].startswith("emu-"))
              and not by_dev[REAL][0]["is_emulator"],
              "схема собрана: ввод, три фазы одного прибора, «Станок», «Печь» и настоящая точка")
        check(js(page, f"{APP}.structNodeMeters({panel['id']}).filter(m=>m.device_id==='emu-map3e-4').length") == 1
              and "эмулятор" in js(page, f"{APP}.structMeterTitle({APP}.structNodeMeters({panel['id']})"
                                         ".find(m=>m.device_id==='emu-map3e-4'))").lower(),
              "карточка щита подписывает прибор эмулятора словом «эмулятор»")

        # ===== 5. сутки данных, баланс щита и метки в «Обзоре» =====
        run(emu, clock, 36 * HOUR)

        # ===== 6. неисправности кнопками =====
        nav(page, "Настройки")
        page.wait_for_selector("[data-testid=emu-fault-reset-2]:visible")
        page.click("[data-testid=emu-fault-reset-2]")
        page.wait_for_function(
            "() => /Сброс счётчика/.test(document.querySelector('[data-testid=emu-journal]').textContent)",
            timeout=8000)
        journal = text(page, "emu-journal")
        first = journal.splitlines()[0]
        check("Сброс счётчика «Эмулятор 2: нагрузка B»" in journal
              and any(ch.isdigit() for ch in first[:12]),
              f"журнал: «Сброс счётчика…» со временем ({first!r})")
        check(api(page, "/api/v2/emulator")["devices"][1]["energy_total_kwh"] < 0.01,
              "после «Сброса» показания прибора с нуля")
        run(emu, clock, 8 * HOUR)

        page.fill("[data-testid=emu-loss-min]", "2")
        page.click("[data-testid=emu-fault-loss-1]")
        page.wait_for_function(
            "() => document.querySelector('[data-testid=emu-fault-state-1]').textContent.trim() === 'нет связи'",
            timeout=8000)
        em1 = h.registry.get("emu-map3e-1")
        run(emu, clock, 30, step=5)
        check(all(c.error == "r" for c in em1.controls.values()),
              "«Нет связи»: на каналах прибора meta/error = r")
        run(emu, clock, 180, step=5)
        page.evaluate(APP + ".loadEmulator()")
        page.wait_for_function(
            "() => document.querySelector('[data-testid=emu-fault-state-1]').textContent.trim() === 'работает'",
            timeout=8000)
        check(not any(c.error for c in em1.controls.values()),
              "по окончании ошибка снята, прибор «работает»")
        run(emu, clock, 48 * HOUR - (clock.t - day_start))

        for dev, profile in (("emu-map3e-4", "total_3p"), ("emu-map3e-2", "total_3p"),
                             ("emu-map3e-3", "total_3p"), ("emu-map3e-1", "phase_l1"),
                             ("emu-map3e-1", "phase_l2"), ("emu-map3e-1", "phase_l3")):
            fill_aggregates(loop, h.meters_repo, dev, profile, range(0, 48), day_start)
        h.seed_energy(REAL, 1.0, period_start=day_start, period_end=day_start + HOUR)

        nav(page, "Обзор")
        page.fill("input[x-model='ovFrom']", ov_from)
        page.fill("input[x-model='ovTo']", ov_to)
        page.click("text=Обновить")
        page.wait_for_timeout(1500)
        pts = page.evaluate("""() => [...document.querySelectorAll('tr')]
            .filter(tr => tr.querySelector('td'))
            .map(tr => [tr.innerText, !!tr.querySelector('[data-testid=emu-badge]:not([style*="display: none"])')])""")
        emu_rows = [r for r in pts if "Эмулятор" in r[0] and "эмулятор" in r[0].lower() and r[1]]
        real_rows = [r for r in pts if "Настоящая точка" in r[0]]
        check(len(emu_rows) >= 6, f"«Обзор»: у точек эмулятора метка «эмулятор» ({len(emu_rows)} строк)")
        check(len(real_rows) >= 1 and all(not r[1] for r in real_rows),
              "«Обзор»: у настоящей точки метки нет")

        nav(page, "Структура")
        open_node(page, panel["id"])
        api_balance = page.evaluate(
            f"fetch('/api/v2/topology/nodes/{panel['id']}/balance', "
            "{method:'POST', headers:{'Content-Type':'application/json'}, "
            f"body: JSON.stringify({{from:'{ov_from}', to:'{ov_to}', timezone:Intl.DateTimeFormat().resolvedOptions().timeZone}})}}).then(r=>r.json())")
        page.wait_for_selector("[data-testid=node-card]:visible [data-testid=node-balance-value]", timeout=8000)
        shown = card(page).locator("[data-testid=node-balance-value]").first.inner_text()
        pct_api = (api_balance.get("explanation") or {}).get("percentage")
        want = 3.0 / 103.0 * 100.0
        check(api_balance.get("unavailable_reason") is None and pct_api is not None
              and abs(pct_api - want) < 0.1,
              f"баланс щита = заданные 3 % от ввода ({pct_api} при ожидаемых {want:.2f}; "
              f"3 % сверх суммы нагрузок — это 3/103 ввода); ответ: {str(api_balance)[:600]}")
        check(api_balance.get("value") is not None and f"{api_balance['value']:.1f}" in shown,
              f"«Баланс щита» в карточке = ответу API ({shown!r})")
        bp.screenshot("b22_balance")

        # ===== 7. «Отчёты»: метка, флаг reset, предупреждение, CSV =====
        stanok = by_dev["emu-map3e-2"][0]["point_id"]
        nav(page, "Отчёты")
        page.wait_for_timeout(800)
        js(page, f"(()=>{{ const a={APP}; a.rv2Dimension='point'; a.rv2ScopeIds=[{stanok}]; "
                 f"a.rv2From={d2_from!r}; a.rv2To={d2_to!r}; }})()")
        js(page, APP + ".runRv2Query()")
        page.wait_for_selector("[data-testid=rv2-emulator-note]:visible", timeout=8000)
        row_text = page.locator("table.rep-table tbody tr").first.inner_text()
        check(vis(page, "rv2-emu-badge") and "reset" in row_text,
              f"отчёт за сутки со сбросом: у строки метка «эмулятор» и флаг reset ({row_text!r})")
        check("не настоящие" in text(page, "rv2-emulator-note"),
              "над таблицей предупреждение: цифры эмулятора не настоящие")
        with page.expect_download() as dl:
            page.locator("button", has_text="Скачать CSV").click()
        path = dl.value.path()
        with open(path, encoding="utf-8") as f:
            csv_text = f.read()
        csv_lines = [ln for ln in csv_text.splitlines() if ln.strip()]
        head = next(ln for ln in csv_lines if ln.startswith("Срез;"))
        check(head.split(";")[-1] == "Эмулятор" and "ВНИМАНИЕ" in csv_text
              and csv_lines[-1].split(";")[-1] == "да",
              "CSV: колонка «Эмулятор» (да у строки эмулятора) и предупреждение в шапке файла")
        js(page, f"(()=>{{ const a={APP}; a.rv2ResetResult(); a.rv2ScopeIds=[{real_point['id']}]; "
                 f"a.rv2From={ov_from!r}; a.rv2To={ov_to!r}; }})()")
        js(page, APP + ".runRv2Query()")
        got = wait_js(page, f"() => {APP}.rv2Result !== null")
        check(got, "отчёт по настоящей точке построен"
              + ("" if got else f" (ошибка: {js(page, APP + '.rv2Error')})"))
        page.wait_for_timeout(300)
        check(not vis(page, "rv2-emulator-note") and not vis(page, "rv2-emu-badge"),
              "отчёт по настоящей точке: ни метки, ни предупреждения")

        # ===== 8. выключение: топики стёрты, привязанные остаются, точки архивируются =====
        nav(page, "Настройки")
        page.click("[data-testid=emu-add-device]")
        page.fill("[data-testid=emu-form-name]", "Лишний")
        page.fill("[data-testid=emu-form-kw1]", "1")
        page.fill("[data-testid=emu-form-kw2]", "1")
        page.fill("[data-testid=emu-form-kw3]", "1")
        page.click("[data-testid=emu-form-save]")
        page.wait_for_function(
            "() => document.querySelectorAll('[data-testid^=emu-device-]').length === 5", timeout=8000)
        extra_name = api(page, "/api/v2/emulator")["devices"][4]["name"]
        check("эмулятор" in extra_name.lower(), f"имя без слова «эмулятор» получило его само: «{extra_name}»")
        run(emu, clock, 10, step=5)
        check(h.registry.get("emu-map3e-5") is not None, "лишний прибор публикует")

        set_emulator_toggle(page, False)
        all_topics = {t for n in range(1, 6) for t in em.device_topics("emu-map3e-%d" % n)}
        check(all_topics <= loop.erased,
              f"выключение стёрло retained-топики всех пяти приборов ({len(loop.erased)} из {len(all_topics)})")
        check(h.registry.get("emu-map3e-5") is None
              and all(h.registry.get("emu-map3e-%d" % n) is not None for n in range(1, 5)),
              "прибор без точки забыт, привязанные остались")
        nav(page, "Обзор")
        page.wait_for_timeout(500)
        check(not vis(page, "emu-banner"), "«Обзор»: баннер исчез")
        nav(page, "Структура")
        open_node(page, panel["id"])
        open_form(page)
        left = [o["v"] for o in device_options(page)]
        check("emu-map3e-5" not in left, "«+ Счётчик»: выключенного прибора без точки в списке нет")
        page.locator("[data-testid=node-meter-form]:visible button", has_text="Отмена").click()

        nav(page, "Настройки")
        page.wait_for_function(
            "() => /\\(6\\)/.test(document.querySelector('[data-testid=emu-archive-points]').textContent)",
            timeout=8000)
        before = api(page, "/api/v2/points")
        page.click("[data-testid=emu-archive-points]")
        page.wait_for_selector("[data-testid=ui-confirm]", timeout=5000)
        lines = page.locator("[data-testid=ui-confirm] li").all_inner_texts()
        check(len(lines) == 6 and not any("Настоящая" in x for x in lines),
              f"подтверждение перечисляет ровно точки эмулятора ({len(lines)})")
        page.click("[data-testid=ui-confirm-ok]")
        page.wait_for_selector("[data-testid=ui-confirm]", state="detached", timeout=8000)
        page.wait_for_timeout(600)
        st = api(page, "/api/v2/emulator")
        pts_after = api(page, "/api/v2/structure/points")["points"]
        check(st["bound_points"] == [] and [p["meter_device_id"] for p in pts_after] == [REAL],
              "точки эмулятора в архиве, настоящая точка на месте")
        check(page.locator("[data-testid=emu-archive-points]").is_disabled(),
              "после архивации кнопка «Архивировать точки эмулятора» недоступна")

        check(not bp.errors, f"консоль без ошибок ({len(bp.errors)}): " + "; ".join(bp.errors[:5]))
        bp.close()

    print("\nИТОГ:", "всё прошло" if not fails else f"{len(fails)} проблем(ы)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
