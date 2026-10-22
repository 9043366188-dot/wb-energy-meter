#!/usr/bin/env python3
"""Репетиция пилота: разделы 1–5 и «Проверка без нагрузки: эмулятор»
`docs/pilot-checklist.md` на виртуальных счётчиках (партия находок 0.22.3, П10;
с 0.23.1, П12, приборы — модуль `wb_energy_meter.emulator`; docs/pilot-findings.md).

Что поднимается (всё настоящее, кроме приборов и истории):
  - свой `mosquitto` на свободном порту (брокер не общий — retained-значения не
    переживают прогон и не мешают другим тестам);
  - три виртуальных WB-MAP3E — настоящий `EmulatorService` сервиса, поднятый в процессе
    репетиции и публикующий в брокер репетиции (`emu-map3e-101…103`, «Эмулятор N: …»):
    `/devices/<id>/controls/<канал>` с `meta`, retained при старте и живые значения. Их
    состояние (включён, якорь недельной давности, начальные показания) заложено в kv, как
    если бы служба «проработала» с начала репетируемой недели;
  - поддельный RPC `wb-mqtt-db` с историей: показания каждые 10 минут за неделю и
    двое суток запаса. Значения даёт второй, «эталонный» экземпляр эмулятора с тем же
    состоянием (счётчик эмулятора — чистая функция времени), поэтому история, живые
    значения и ручной расчёт — одни и те же числа;
  - настоящий сервис: `python -m wb_energy_meter.main` отдельным процессом (демон +
    Flask), настоящая база в каталоге данных, часовой пояс процесса UTC;
  - настоящий Chromium (Playwright), как у человека: клики и ввод.

Время. Неделя реальной работы в песочнице не проходит: привязки счётчиков датируются
началом недели (`at` в запросе «+ Счётчик», перехватом в браузере), как будто счётчики
поставили за сутки до сверяемого периода. Всё остальное — настоящее.

Таблица «шаг инструкции → проверка репетиции»:

| Шаг чек-листа | Что проверяет репетиция |
|---|---|
| 1.1 обновление | НЕ ВЫПОЛНЯЕТ: кнопку «Обновить» и `install-from-github.sh` на песочнице не запустить (нужны GitHub и systemd). Вместо этого сервис запускается из рабочего дерева |
| 1.2 версия и «MQTT online» | в шапке версия из `wb_energy_meter.__version__`, индикатор «MQTT online» |
| 1.3 `/api/selfcheck` | `ok: true` |
| 1.4 `/api/v2/migration/status` | `schema_version` = номер последней миграции пакета, `code_generation` = `__code_generation__`, `domain_model_generation` = 1, `migration_map` пуст |
| 2 чистый старт | `scripts/clean-start-once.sh` на каталоге со «старой» базой: она и `plans` лежат в `test-data-…` с README, метка записана; сервис стартует на пустой базе; интерфейс: «Точек учёта пока нет», «План v3» без планов и элементов, кнопки «Перенести легаси-данные» нет |
| 3.1–3.2 план, Ввод и Щит | план создаётся полем «Название плана» (без подложки — она необязательна; загрузку и замену подложки проверяет `tests/browser/test_b18_plan_background.py`), Ввод и Щит ставятся кликом по карте |
| 3.4 счётчик на ввод щита | «+ Счётчик» → «Ввод щита» → «Откуда питается?» |
| 3.5 нагрузки | «+ Счётчик» → «Отходящую линию» → «+ новая нагрузка…» (Станок) |
| 3.6 «по фазам» (П9) | «по фазам — каждая фаза своя линия»: Освещение, Розетки, Вентиляция — три нагрузки одним счётчиком; значки на плане в разных местах; всего четыре нагрузки |
| 3.7 «Структура» | то же дерево: Ввод → Щит → нагрузки, раздел «Не на схеме» пуст |
| 3.8 «Баланс щита» | блок показывает значение |
| 3.9 «Обзор» | нет «не размещена на плане», «Итог объекта» — число, в «Все точки учёта» пять точек; раздел «Приборы» — карточка на каждый прибор из MQTT, у привязанных названа точка учёта (П13) |
| 4 сверка с ручным расчётом | сервис перезапускается (догон истории — см. ниже), для суток и недели таблица раздела 4 заполняется показаниями поддельных приборов на границах; итог объекта, баланс щита, небаланс объекта и расход каждой точки (срез «Точка») сходятся с ручным расчётом в пределах 0,1 % |
| 5 отчёт и CSV | срез «Ветвь», числа = таблица «Ветвь сети» «Обзора»; CSV `report_v2_branch_<дата>.csv` совпадает с экраном построчно (колонка «Эмулятор» = да, предупреждение в шапке); срез «Точка» = таблица недели |
| Э.1–Э.2 включение | «Настройки» → «Эмулятор счётчиков»: по умолчанию выключен; «Добавить типовой набор» (3 нагрузки и ввод) → четыре строки с меткой «эмулятор»; включение → в «Обзоре» баннер «Включён эмулятор: 4 виртуальных прибора — цифры с ними не настоящие» |
| Э.3 метки | в «+ Счётчик» у приборов эмулятора слово «эмулятор» в названии |
| Э.4 неисправности | кнопка «Сброс» → строка в журнале со временем; кнопка «Проверить историю сейчас» → предупреждение «wb-mqtt-db не пишет историю эмулятора…» (поддельный RPC истории эмулятора не знает) |
| Э.5 выключение | новый подписчик брокера не получает ни одного retained-топика приборов, которые завела служба (приборы репетиции, живущие в этом процессе, остаются — проверка не пустая); баннера нет |
| Э.6 архивация | «Архивировать точки эмулятора» → подтверждение с перечнем точек → все пять точек схемы в архиве |

Чего репетиция выполнить не может (и не заменяет):
  - команды по SSH на контроллере (`curl`, `journalctl`, `systemctl`, `mosquitto_sub`),
    само самообновление и откат на железе (раздел 1.1);
  - раздел 6 (что вы используете в классическом интерфейсе) и раздел 8 (диагностика
    `wb-map3e_49`);
  - ожидание 10–15 минут автоматической пробы истории эмулятора: репетиция нажимает
    «Проверить историю сейчас» (тот же вызов), а таймер пробы проверяют модульные тесты;
  - вердикт «пилот прошёл» (раздел 9): его выносит Кир по живому объекту — репетиция
    проверяет только, что инструкция и программа сходятся на поддельных данных;
  - настоящую неделю работы: история — поддельная, готовая. Догон истории (на объекте его
    делает латальщик в течение 6 часов после старта или перезапуск сервиса) репетиция
    запускает перезапуском сервиса — заодно проверяется, что схема переживает перезапуск.

Запуск (нужны `mosquitto` и Chromium/Playwright):
    python tests/pilot/rehearsal.py
Правка `docs/pilot-checklist.md` и правка этой репетиции — в одном PR (§2.6 гейта).
"""
from __future__ import annotations

import csv
import glob
import io
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

import paho.mqtt.client as mqtt  # noqa: E402

from wb_energy_meter import __code_generation__, __version__  # noqa: E402
from wb_energy_meter import emulator as em  # noqa: E402
from wb_energy_meter.db import Database  # noqa: E402
from wb_energy_meter.repo import GroupRepo, MeterRepo  # noqa: E402

APP = "document.querySelector('[x-data]')._x_dataStack[0]"
fails: list = []
STEP = 600                      # шаг истории, с
OUT_DIR = os.path.join(HERE, "_out")


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


# --------------------------------------------------------------------- приборы
# Виртуальные WB-MAP3E (модуль эмулятора). Мощность по фазам, кВт; показания — начальные, кВт·ч,
# на T0. Ввод щита — один трёхфазный прибор целиком; «Станок» — второй прибор целиком; третий
# прибор разнесён «по фазам»: L1 — Освещение, L2 — Розетки, L3 — Вентиляция. Номера 101–103,
# чтобы не пересекаться с приборами, которые служба заводит сама (раздел Э, номера с 1).
FAKE = {
    "emu-map3e-101": {"title": "Эмулятор 101: ввод щита", "kw": (2.0, 2.0, 2.0),
                      "base": (1000.0, 1100.0, 900.0)},
    "emu-map3e-102": {"title": "Эмулятор 102: станок", "kw": (1.0, 0.8, 0.7),
                      "base": (300.0, 310.0, 290.0)},
    "emu-map3e-103": {"title": "Эмулятор 103: три нагрузки", "kw": (0.8, 1.2, 0.5),
                      "base": (100.5, 110.25, 99.75)},
}
PHASES = ("L1", "L2", "L3")
INPUT_DEV, MACHINE_DEV, SPLIT_DEV = "emu-map3e-101", "emu-map3e-102", "emu-map3e-103"
PHASE_LOADS = {"L1": "Освещение", "L2": "Розетки", "L3": "Вентиляция"}


def utc_midnight_today() -> int:
    n = datetime.now(timezone.utc)
    return int(datetime(n.year, n.month, n.day, tzinfo=timezone.utc).timestamp())


T_END = utc_midnight_today()           # конец периода: сегодня 00:00 UTC (день «по» не входит)
T_WEEK = T_END - 7 * 86400
T_DAY = T_END - 86400
T0 = T_WEEK - 2 * 86400                # от него растут показания
BIND_AT = T_WEEK - 86400               # «счётчики привязаны за сутки до начала недели»


class _Kv:
    """kv в памяти (get/set — как у KvRepo): состояние эмулятора лежит в нём."""

    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return json.loads(self.d[key]) if key in self.d else default

    def set(self, key, value):
        self.d[key] = json.dumps(value, ensure_ascii=False)


def _dev_n(dev: str) -> int:
    return int(dev.rsplit("-", 1)[1])


def seed_kv():
    """Состояние эмулятора «как будто служба работает с T0»: включён, якорь на T0 с
    начальными показаниями, ровный профиль (на пилоте нагрузка идёт без суточной формы)."""
    kv = _Kv()
    devs = [em.EmuDevice(_dev_n(d), spec["title"], kw=spec["kw"], profile="flat",
                         anchor_ts=T0, anchor_kwh=spec["base"], started_ts=T0).to_dict()
            for d, spec in FAKE.items()]
    kv.set(em.STATE_KEY, {"v": 1, "enabled": True, "enabled_at": T0, "next_n": 104,
                          "devices": devs,
                          "history": {"state": "pending", "checked_at": None, "message": ""},
                          "pending_erase": []})
    return kv


_ORACLE = None
_MEMO: dict = {}
_MEMO_LOCK = threading.Lock()


def _oracle():
    """Эталон: второй экземпляр эмулятора с тем же состоянием, который никогда не тикает и не
    публикует. Счётчик эмулятора — чистая функция времени, значит живой экземпляр и эталон
    дают одни и те же показания на любой момент."""
    global _ORACLE
    if _ORACLE is None:
        _ORACLE = em.EmulatorService(seed_kv(), utc_offset_s=0)
    return _ORACLE


def _energies(dev: str, t: float):
    key = (dev, int(t))
    v = _MEMO.get(key)
    if v is None:
        with _MEMO_LOCK:
            o = _oracle()
            v = o.energies(o._devices[_dev_n(dev)], int(t))
            _MEMO[key] = v
    return v


def prime_history():
    """Прогнать эталон по сетке истории вперёд один раз: каждое следующее показание
    досчитывается от предыдущего, а не от якоря."""
    t = T0
    end = int(time.time())
    while t <= end:
        for dev in FAKE:
            _energies(dev, t)
        t += STEP


def phase_reading(dev: str, phase: str, t: float) -> float:
    return _energies(dev, t)[PHASES.index(phase)]


def total_reading(dev: str, t: float) -> float:
    return sum(_energies(dev, t))


def reading(dev: str, ctrl: str, t: float):
    if ctrl == "Total AP energy":
        return total_reading(dev, t)
    m = re.fullmatch(r"AP energy (L[123])", ctrl)
    if m:
        return phase_reading(dev, m.group(1), t)
    return None


# ---------------------------------------------------------------------- брокер
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Broker:
    def __init__(self, tmp):
        self.port = free_port()
        conf = os.path.join(tmp, "mosquitto.conf")
        with open(conf, "w") as f:
            f.write("listener %d 127.0.0.1\nallow_anonymous true\npersistence false\n"
                    "log_type error\n" % self.port)
        self.proc = subprocess.Popen(["mosquitto", "-c", conf], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("mosquitto не поднялся")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except Exception:
            self.proc.kill()


def new_client(port, prefix):
    c = mqtt.Client(client_id="%s-%s" % (prefix, uuid.uuid4().hex[:6]), clean_session=True)
    c.connect("127.0.0.1", port, 15)
    c.loop_start()
    return c


class EmulatorHost:
    """Виртуальные счётчики в MQTT — настоящий `EmulatorService` в процессе репетиции."""

    def __init__(self, port):
        self.svc = em.EmulatorService(
            seed_kv(), publisher_factory=lambda: em.PahoPublisher("127.0.0.1", port),
            utc_offset_s=0)
        # retained-снимок — до запуска сервиса, как и раньше; дальше публикует поток службы
        self.svc.tick()
        self.svc.start()

    def stop(self):
        self.svc.stop()


class FakeHistoryRpc:
    """Поддельный wb-mqtt-db (db_logger/history): get_channels и get_values по формуле показаний."""

    def __init__(self, port):
        self.c = mqtt.Client(client_id="fake-db-%s" % uuid.uuid4().hex[:6], clean_session=True)
        self.c.on_connect = lambda c, u, f, rc: c.subscribe("/rpc/v1/db_logger/history/+/+", qos=1)
        self.c.on_message = self._on_message
        self.c.connect("127.0.0.1", port, 15)
        self.c.loop_start()
        time.sleep(0.3)
        self.requests = 0

    def _on_message(self, c, u, msg):
        parts = msg.topic.split("/")
        if len(parts) < 7 or msg.topic.endswith("/reply"):
            return
        method = parts[5]
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        self.requests += 1
        rid = payload.get("id", "")
        params = payload.get("params") or {}
        if method == "get_channels":
            now = int(time.time())
            chans = {"%s/%s" % (d, ctrl): {"items": 1000, "last_ts": now}
                     for d in FAKE for ctrl in ("Total AP energy", "AP energy L1", "AP energy L2",
                                                "AP energy L3")}
            reply = {"id": rid, "error": None, "result": {"channels": chans}}
        elif method == "get_values":
            ts = params.get("timestamp") or {}
            gt = ts.get("gt", T0 - 1)
            lt = min(ts.get("lt", time.time()), time.time())
            limit = int(params.get("limit", 10000))
            values = []
            for idx, (dev, ctrl) in enumerate(params.get("channels") or []):
                if dev not in FAKE:
                    continue
                t = (int(gt) // STEP + 1) * STEP
                while t < lt and len(values) < limit:
                    if t >= T0:
                        v = reading(dev, ctrl, t)
                        if v is not None:
                            values.append({"c": idx, "t": int(t * 1000), "v": round(v, 6)})
                    t += STEP
            reply = {"id": rid, "error": None, "result": {"values": values}}
        else:
            reply = {"id": rid, "result": None, "error": {"message": "unknown " + method}}
        c.publish(msg.topic + "/reply", json.dumps(reply), qos=1)

    def stop(self):
        self.c.loop_stop()
        self.c.disconnect()


# ----------------------------------------------------------------------- сервис
class Daemon:
    def __init__(self, tmp, data_dir, mqtt_port):
        self.data_dir = data_dir
        self.db_path = os.path.join(data_dir, "state.db")
        self.http_port = free_port()
        self.base_url = "http://127.0.0.1:%d/" % self.http_port
        self.cfg = os.path.join(tmp, "wb-energy-meter.conf")
        self.log_path = os.path.join(tmp, "daemon.log")
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write(
                "mqtt: {host: 127.0.0.1, port: %d}\n"
                "http: {host: 127.0.0.1, port: %d}\n"
                'device_prefix: "wb-map3e_"\n'
                "log_file: null\n"
                "update: {enabled: false, allow_from_ui: false}\n"
                "aggregator: {catchup_days: 10, catchup_start_delay_s: 1, rpc_timeout_s: 10}\n"
                % (mqtt_port, self.http_port))
        self.proc = None

    def start(self):
        env = dict(os.environ, PYTHONPATH=REPO, TZ="UTC", PYTHONUNBUFFERED="1")
        self.log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "wb_energy_meter.main", "--config", self.cfg,
             "--db-path", self.db_path, "--no-log-file"],
            cwd=self.data_dir, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        import urllib.request
        for _ in range(150):
            if self.proc.poll() is not None:
                raise RuntimeError("сервис завершился при запуске:\n" + self.tail())
            try:
                urllib.request.urlopen(self.base_url + "api/status", timeout=1).read()
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("сервис не ответил за 30 с:\n" + self.tail())

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except Exception:
                self.proc.kill()
        try:
            self.log.close()
        except Exception:
            pass

    def tail(self, n=40):
        try:
            return "".join(open(self.log_path, encoding="utf-8", errors="replace").readlines()[-n:])
        except Exception:
            return ""

    def get(self, path):
        import urllib.request
        return json.loads(urllib.request.urlopen(self.base_url + path.lstrip("/"), timeout=10).read())

    def aggregate_counts(self, t_from, t_to):
        """Часы недели в `period_aggregates` и `phase_aggregates` (чтение файла базы)."""
        con = sqlite3.connect("file:%s?mode=ro" % self.db_path, uri=True, timeout=5)
        try:
            tot = con.execute("SELECT COUNT(*) FROM period_aggregates WHERE period_start >= ? "
                              "AND period_start < ?", (t_from, t_to)).fetchone()[0]
            try:
                ph = con.execute("SELECT COUNT(*) FROM phase_aggregates WHERE period_start >= ? "
                                 "AND period_start < ?", (t_from, t_to)).fetchone()[0]
            except sqlite3.Error:
                ph = 0
            return tot, ph
        finally:
            con.close()


# ----------------------------------------------------------------- вспомогательное
def num(text):
    m = re.search(r"[+-]?\d+(?:[.,]\d+)?", text.replace("−", "-"))
    return float(m.group(0).replace(",", ".")) if m else None


def close_enough(screen, manual, decimals):
    """Критерий пилота: ≤ 0,1 %; плюс половина последнего показанного знака (округление экрана)."""
    tol = max(abs(manual) * 0.001, 0.5 * 10 ** (-decimals))
    return screen is not None and abs(screen - manual) <= tol + 1e-9


def ensure_new_ui(page):
    if page.locator("button:has-text('Классический')").count() == 0:
        page.evaluate(APP + ".toggleUiSet()")
        page.wait_for_timeout(300)


BP = None                       # открытый браузер — для трассировки счётчика ошибок консоли


def trace(label):
    if os.environ.get("WBEM_REHEARSAL_TRACE") and BP is not None:
        print("TRACE %-34s ошибок консоли: %d" % (label, len(BP.errors)), flush=True)


def tab(page, name):
    page.locator("nav button:visible", has_text=name).click()
    page.wait_for_timeout(900)
    trace("вкладка " + name)


def card(page):
    return page.locator("[data-testid=node-card]:visible")


def place_on_map(page, kind_value, name, fx, fy):
    box = page.locator(".leaflet-container:visible").first.bounding_box()
    page.click("button:has-text('+ Элемент схемы')")
    page.wait_for_timeout(200)
    page.select_option("[data-testid=planv3-kind-select]", kind_value)
    page.fill("input[placeholder='Название']", name)
    page.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
    page.wait_for_timeout(1200)


def open_form(page):
    card(page).locator("button", has_text="+ Счётчик").click()
    page.wait_for_timeout(300)


def pick_outgoing(page):
    page.locator("[data-testid=node-meter-form]:visible label",
                 has_text="Отходящую линию").locator("input").check()
    page.wait_for_timeout(200)


def api(page, path):
    return page.evaluate(f"fetch({path!r}).then(r=>r.json())")


def set_overview_period(page, d_from, d_to):
    tab(page, "Обзор")
    page.fill("input[x-model='ovFrom']", d_from)
    page.fill("input[x-model='ovTo']", d_to)
    page.click("text=Обновить")
    page.wait_for_timeout(1500)


def read_testid(page, testid):
    """Число из элемента `data-testid`; если элемента нет — показать, что сказал сервер."""
    loc = page.locator("[data-testid=%s]" % testid)
    try:
        loc.first.wait_for(timeout=6000)
        return num(loc.first.inner_text())
    except Exception:
        summary = page.evaluate(APP + ".ovSummary")
        print("НЕТ элемента %s; ovSummary = %s" % (testid, json.dumps(summary, ensure_ascii=False)[:1500]))
        return None


def date_str(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def rep_rows(page):
    # метка «эмулятор» (П12) — отдельный элемент в ячейке названия; в сравнении нужно само
    # название: берём видимый текст ячейки и отрезаем от него видимую метку
    return page.evaluate("""() => [...document.querySelectorAll('table.rep-table tbody tr')]
        .map(tr => [...tr.children].map(td => {
            let t = td.innerText.trim();
            const b = td.querySelector('.emu-badge');
            if (b && b.offsetParent !== null) {
                const i = t.lastIndexOf(b.innerText.trim());
                if (i >= 0) t = t.slice(0, i).trim();
            }
            return t;
        }))""")


def retained_topics(port, window=1.5):
    """Что брокер хранит под `/devices/…` — глазами нового подписчика."""
    got = set()
    c = mqtt.Client(client_id="probe-%s" % uuid.uuid4().hex[:6], clean_session=True)
    c.on_connect = lambda cl, u, f, rc: cl.subscribe("/devices/+/#", qos=1)
    c.on_message = lambda cl, u, m: got.add(m.topic) if m.retain else None
    c.connect("127.0.0.1", port, 15)
    c.loop_start()
    time.sleep(window)
    c.loop_stop()
    c.disconnect()
    return got


def run_report(page, dimension, d_from, d_to, point_ids=None):
    tab(page, "Отчёты")
    page.select_option("select[x-model='rv2Dimension']", dimension)
    page.wait_for_timeout(200)
    if dimension != "branch" and point_ids:
        page.select_option("select[x-model='rv2ScopeIds']", [str(i) for i in point_ids])
    page.fill("input[x-model='rv2From']", d_from)
    page.fill("input[x-model='rv2To']", d_to)
    with page.expect_response(lambda r: r.url.endswith("/api/v2/reports/query")):
        page.locator("button.btn-primary", has_text="Показать").click()
    page.wait_for_selector("table.rep-table tbody tr", timeout=10000)
    page.wait_for_timeout(300)
    trace("отчёт " + dimension)
    return rep_rows(page)


def make_old_data_dir(data_dir):
    """«Тестовые» данные до пилота: старая база с одним счётчиком и каталог plans."""
    os.makedirs(data_dir)
    db = Database(path=os.path.join(data_dir, "state.db"))
    db.open()
    MeterRepo(db, GroupRepo(db)).add(device_id="wb-map3e_old", display_name="Старый тестовый",
                                     group=None, notes=None)
    db.close()
    os.makedirs(os.path.join(data_dir, "plans"))
    with open(os.path.join(data_dir, "plans", "old-plan.png"), "wb") as f:
        f.write(b"\x89PNG\r\n")


def latest_migration_number():
    nums = [int(os.path.basename(p).split("_")[0])
            for p in glob.glob(os.path.join(REPO, "wb_energy_meter", "migrations", "*.sql"))]
    return max(nums)


# --------------------------------------------------------------------- сценарий
def main() -> int:
    if shutil.which("mosquitto") is None:
        print("FAIL: mosquitto не найден — репетиции нужен брокер (apt-get install mosquitto)")
        return 2
    from browser.harness import open_browser          # Chromium через Playwright

    tmp = tempfile.mkdtemp(prefix="wbem-rehearsal-")
    data_dir = os.path.join(tmp, "data")
    broker = meters = rpc = daemon = bp = None
    try:
        broker = Broker(tmp)
        prime_history()
        meters = EmulatorHost(broker.port)
        rpc = FakeHistoryRpc(broker.port)
        daemon = Daemon(tmp, data_dir, broker.port)

        # ========== раздел 2. Чистый старт (до первого запуска сервиса) ==========
        make_old_data_dir(data_dir)
        r = subprocess.run(["bash", os.path.join(REPO, "scripts", "clean-start-once.sh"), data_dir],
                           capture_output=True, text=True)
        moved = glob.glob(os.path.join(data_dir, "test-data-*"))
        check(r.returncode == 0 and len(moved) == 1
              and os.path.exists(os.path.join(moved[0], "state.db"))
              and os.path.exists(os.path.join(moved[0], "plans", "old-plan.png"))
              and os.path.exists(os.path.join(moved[0], "README.txt")),
              "2: старая база и plans лежат в test-data-… вместе с README.txt")
        check(not os.path.exists(os.path.join(data_dir, "state.db"))
              and os.path.exists(os.path.join(data_dir, ".clean-start-r12-done")),
              "2: рабочего state.db нет, метка чистого старта записана")

        daemon.start()
        bp = open_browser(daemon.base_url, timezone_id="UTC")
        global BP
        BP = bp
        page = bp.page
        page.wait_for_timeout(2500)

        # ========== раздел 1. Версия, MQTT, самопроверка, миграции ==========
        header = page.locator("header").first.inner_text()
        check(("v" + __version__) in header, "1.2: в шапке версия v%s (%r)" % (__version__, header[:80]))
        check("MQTT online" in page.locator("body").inner_text(), "1.2: индикатор «MQTT online»")
        sc = daemon.get("api/selfcheck")
        check(sc.get("ok") is True, "1.3: /api/selfcheck → ok: true (%s)" % json.dumps(sc)[:120])
        ms = daemon.get("api/v2/migration/status")
        check(ms["schema_version"] == latest_migration_number(),
              "1.4: schema_version = %s (последняя миграция пакета)" % ms["schema_version"])
        check(ms["code_generation"] == __code_generation__,
              "1.4: code_generation = %s" % ms["code_generation"])
        check(ms["domain_model_generation"] == 1 and not ms["migration_map"],
              "2: после чистого старта domain_model_generation = 1, migration_map пуст "
              "(%s, %s)" % (ms["domain_model_generation"], ms["migration_map"]))

        # интерфейс на пустой базе
        ensure_new_ui(page)
        tab(page, "Обзор")
        check(page.locator("[data-testid=overview-empty]").count() == 1
              and "Начните со схемы" in page.locator("[data-testid=overview-empty]").inner_text(),
              "2: «Обзор» на пустой базе говорит «Начните со схемы…»")
        check("Перенести легаси-данные" not in page.locator("body").inner_text(),
              "2: в новом наборе нет кнопки «Перенести легаси-данные»")
        tabs = page.locator("nav button:visible").all_inner_texts()
        check(not any("План v2" in t for t in tabs), "2: вкладки «План v2» нет (%s)" % tabs)
        tab(page, "План v3")
        check(api(page, "/api/v2/plans") == [] and api(page, "/api/v2/topology/nodes") == [],
              "2: в «Плане v3» нет ни одного плана и ни одного элемента схемы")

        # ========== раздел 3. Схема питания ==========
        page.fill("input[placeholder='Название плана']", "Пилот")
        page.click("text=Создать план")
        page.wait_for_timeout(1200)
        check(len(api(page, "/api/v2/plans")) == 1, "3.1: план создан кнопкой «Создать план»")
        place_on_map(page, "source", "Ввод объекта", 0.25, 0.25)
        place_on_map(page, "panel", "ЩР-1", 0.25, 0.5)
        nodes = {n["name"]: n for n in api(page, "/api/v2/topology/nodes")}
        check(set(nodes) == {"Ввод объекта", "ЩР-1"}, "3.2: Ввод и Щит поставлены кликом по карте")
        shield = nodes["ЩР-1"]

        # Неделя реальной работы пилота в песочнице не проходит. Привязки счётчиков датируются
        # началом недели (поле `at` запроса — то же, что приняло бы API), как будто счётчики
        # поставили за сутки до сверяемого периода; форма «+ Счётчик» сама поля даты не имеет.
        def backdate(route):
            req = route.request
            if req.method == "POST":
                body = json.loads(req.post_data or "{}")
                body["at"] = BIND_AT
                route.continue_(post_data=json.dumps(body))
            else:
                route.continue_()
        page.route(re.compile(r".*/api/v2/topology/nodes/\d+/meters$"), backdate)

        # 3.4 счётчик на ввод щита
        open_form(page)
        page.select_option("[data-testid=node-meter-device]:visible", INPUT_DEV)
        page.select_option("[data-testid=node-meter-fed-from]:visible", label="Ввод объекта (Ввод)")
        page.click("[data-testid=node-meter-submit]:visible")
        page.wait_for_timeout(1500)
        rows = " | ".join(card(page).locator("[data-testid=node-meter]").all_inner_texts())
        check("ввод щита" in rows and len(api(page, "/api/v2/points")) == 1,
              "3.4: «Счётчики (1)», строка «… — ввод щита» (%r)" % rows[:90])

        # 3.5 нагрузка «Станок» — весь прибор одной линией
        open_form(page)
        page.select_option("[data-testid=node-meter-device]:visible", MACHINE_DEV)
        pick_outgoing(page)
        page.locator("[data-testid=node-meter-mode-total]:visible").check()
        page.select_option("[data-testid=node-meter-out-target]:visible", "new")
        page.fill("[data-testid=node-meter-new-load]:visible", "Станок")
        page.click("[data-testid=node-meter-submit]:visible")
        page.wait_for_timeout(1800)

        # 3.6 три нагрузки одним прибором «по фазам»
        open_form(page)
        page.select_option("[data-testid=node-meter-device]:visible", SPLIT_DEV)
        pick_outgoing(page)
        page.locator("[data-testid=node-meter-mode-phases]:visible").check()
        page.wait_for_timeout(200)
        for ph, load in PHASE_LOADS.items():
            page.select_option("[data-testid=node-meter-phase-target-%s]:visible" % ph, "new")
            page.fill("[data-testid=node-meter-phase-new-%s]:visible" % ph, load)
        page.click("[data-testid=node-meter-submit]:visible")
        page.wait_for_timeout(2200)

        nodes = {n["name"]: n for n in api(page, "/api/v2/topology/nodes")}
        loads = [n for n in nodes.values() if n["kind"] == "load"]
        check(sorted(n["name"] for n in loads) == sorted(["Станок"] + list(PHASE_LOADS.values())),
              "3.6: четыре нагрузки: %s" % sorted(n["name"] for n in loads))
        edges = api(page, "/api/v2/topology/edges?state=published")
        points = {p["id"]: p for p in api(page, "/api/v2/points")}
        check(len(edges) == 5 and len(points) == 5 and all(e["primary_point_id"] for e in edges),
              "3.6: пять линий и пять точек, на каждой линии свой счётчик")
        plan = api(page, "/api/v2/plans")[0]
        plan_now = api(page, "/api/v2/plans/%d" % plan["id"])
        geom = {it["node_id"]: it["geometry"] for it in plan_now["items"] if it["kind"] == "node"}
        xy = [(round(geom[n["id"]]["x"]), round(geom[n["id"]]["y"])) for n in loads if n["id"] in geom]
        check(len(xy) == 4 and len(set(xy)) == 4, "3.6: четыре нагрузки на плане в разных местах (%s)" % xy)
        rows = " | ".join(card(page).locator("[data-testid=node-meter]").all_inner_texts())
        row_list = card(page).locator("[data-testid=node-meter]").all_inner_texts()

        def row_of(load_name):
            return next((r for r in row_list if "линия к «%s»" % load_name in r), "")
        check(all((" · %s " % ph) in row_of(load) and SPLIT_DEV in row_of(load)
                  for ph, load in PHASE_LOADS.items()) and MACHINE_DEV in row_of("Станок"),
              "3.6: карточка щита называет прибор и фазу каждой линии (%s)"
              % [row_of(l).split("\n")[0] for l in PHASE_LOADS.values()])
        bp.screenshot("rehearsal_scheme")

        # 3.7 «Структура»
        tab(page, "Структура")
        tree = page.locator("[data-testid=scheme-tree]").inner_text()
        check(all(n in tree for n in ["Ввод объекта", "ЩР-1", "Станок"] + list(PHASE_LOADS.values())),
              "3.7: «Структура» показывает Ввод → Щит → нагрузки")
        check(page.locator("[data-testid=off-scheme-point]").count() == 0
              and page.locator("[data-testid=off-scheme-device]").count() == 0,
              "3.7: раздел «Не на схеме» пуст")

        # ========== перезапуск сервиса: догон истории ==========
        print("--- перезапуск сервиса (догон недели по каждому счётчику и фазе) ---", flush=True)
        daemon.stop()
        daemon.start()
        want = 3 * 168
        deadline = time.time() + 300
        got = (0, 0)
        while time.time() < deadline:
            got = daemon.aggregate_counts(T_WEEK, T_END)
            if got[0] >= want and got[1] >= want:
                break
            time.sleep(2)
        check(got[0] >= want and got[1] >= want,
              "догон: часов недели итогов прибора %d из %d, по фазам %d из %d (запросов RPC: %d)"
              % (got[0], want, got[1], want, rpc.requests))
        page.reload()
        page.wait_for_timeout(2500)
        ensure_new_ui(page)

        # ========== раздел 4. Сверка с ручным расчётом ==========
        os.makedirs(OUT_DIR, exist_ok=True)
        by_key = {}                           # (прибор, фаза | None) -> (id точки, имя точки)
        for p in api(page, "/api/v2/structure/points")["points"]:
            by_key[(p["meter_device_id"], p["meter_phase"])] = (p["point_id"], p["name"])
        measured = [(INPUT_DEV, None, "Ввод щита"), (MACHINE_DEV, None, "Станок")] + \
                   [(SPLIT_DEV, ph, load) for ph, load in PHASE_LOADS.items()]
        check(all(k[:2] in by_key for k in measured),
              "4: все пять точек опознаны по прибору и фазе (%s)" % sorted(map(str, by_key)))

        def manual(dev, ph, t_from, t_to):
            if ph is None:
                return total_reading(dev, t_to) - total_reading(dev, t_from)
            return phase_reading(dev, ph, t_to) - phase_reading(dev, ph, t_from)

        report_md = []
        week_rows = None
        for label, t_from in (("сутки", T_DAY), ("неделя", T_WEEK)):
            d_from, d_to = date_str(t_from), date_str(T_END)
            rows_man = {(dev, ph): manual(dev, ph, t_from, T_END) for dev, ph, _ in measured}
            in_diff = rows_man[(INPUT_DEV, None)]
            loads_sum = sum(v for k, v in rows_man.items() if k != (INPUT_DEV, None))
            shield_manual = in_diff - loads_sum
            # ветви сети — отходящие линии щита; небаланс объекта = итог − Σ ветвей
            object_imbalance = in_diff - loads_sum

            # экран: «Отчёты» → срез «Точка» (расход каждой точки)
            ids = [by_key[(dev, ph)][0] for dev, ph, _ in measured]
            trows = run_report(page, "point", d_from, d_to, ids)
            screen_pt = {r[0]: num(r[1]) for r in trows}
            # экран: «Обзор» и карточка щита
            set_overview_period(page, d_from, d_to)
            screen_total = read_testid(page, "ov-object-total")
            screen_imb = read_testid(page, "ov-imbalance")
            tab(page, "План v3")
            page.evaluate(APP + ".openInspectorInPlace('node', %d)" % shield["id"])
            page.wait_for_timeout(1800)
            shown = card(page).locator("[data-testid=node-balance-value]").first.inner_text()
            screen_shield = num(shown)
            check("баланс щита" in card(page).inner_text().lower(),
                  "3.8 (%s): в карточке щита блок «Баланс щита за период» показывает значение (%s)"
                  % (label, shown.strip()))

            report_md += ["", "### Раздел 4 — %s (%s … %s, UTC)" % (label, d_from, d_to), "",
                          "| Счётчик (`device_id`, линия) | На начало, кВт·ч | На конец, кВт·ч | Ожидаемая разность | Экран, кВт·ч |",
                          "|---|---|---|---|---|"]
            for dev, ph, load in measured:
                pid, name = by_key[(dev, ph)]
                t_a = (phase_reading(dev, ph, t_from) if ph else total_reading(dev, t_from))
                t_b = (phase_reading(dev, ph, T_END) if ph else total_reading(dev, T_END))
                shown_v = screen_pt.get(name)
                report_md.append("| %s%s, %s | %.3f | %.3f | %.3f | %s |" % (
                    dev, " " + ph if ph else "", load, t_a, t_b, rows_man[(dev, ph)],
                    "%.3f" % shown_v if shown_v is not None else "—"))
                check(close_enough(shown_v, rows_man[(dev, ph)], 3),
                      "4 (%s): %s — экран %s, вручную %.3f" % (label, name, shown_v, rows_man[(dev, ph)]))
            report_md += ["", "| Расчёт | Вручную | Экран | Разница, % |", "|---|---|---|---|"]
            for what, man, scr, dec in (
                    ("Итог объекта = разность по вводу", in_diff, screen_total, 1),
                    ("Небаланс щита = ввод − Σ нагрузок", shield_manual, screen_shield, 1),
                    ("Небаланс объекта", object_imbalance, screen_imb, 1)):
                diff = "%.3f" % ((scr - man) / man * 100) if (scr is not None and man) else "0 (ожидается 0)"
                report_md.append("| %s | %.3f | %s | %s |" % (what, man, scr, diff))
                check(close_enough(scr, man, dec), "4 (%s): %s — экран %s, вручную %.3f"
                      % (label, what, scr, man))
            if label == "неделя":
                week_rows = rows_man

        with open(os.path.join(OUT_DIR, "section4.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(report_md) + "\n")
        print("\n".join(report_md), flush=True)

        # ========== раздел 3.8–3.9 на готовых данных ==========
        set_overview_period(page, date_str(T_WEEK), date_str(T_END))
        ov_text = page.locator("body").inner_text()
        check("не размещена на плане" not in ov_text, "3.9: «Обзор» не жалуется «не размещена на плане»")
        check(page.locator("[data-testid=ov-object-total]").count() == 1,
              "3.9: «Итог объекта за период» — число")
        names = [p["name"] for p in api(page, "/api/v2/snapshot")["points"]]
        check(len(names) == 5 and all(n in ov_text for n in names),
              "3.9: в «Все точки учёта» все пять точек (%s)" % names)
        # П13 (партия 15): раздел «Приборы» — карточка на каждый прибор из MQTT, у привязанных названа точка
        status_ids = sorted(m["device_id"] for m in api(page, "/api/status")["meters"])
        card_kinds = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('[data-testid=ov-device-card]')]
            .map(c => [c.dataset.deviceId, c.querySelector('[data-testid=ov-device-point]').dataset.pointKind]))""")
        bound_ids = {p["device_id"] for p in api(page, "/api/v2/snapshot")["points"] if p.get("device_id")}
        check(bool(status_ids) and sorted(card_kinds) == status_ids,
              "3.9: раздел «Приборы» — карточка на каждый прибор из MQTT (%s)" % sorted(card_kinds))
        check(bool(bound_ids) and all(card_kinds.get(d) == "bound" for d in bound_ids),
              "3.9: у приборов, привязанных к точкам, в карточке названа «Точка учёта» (%s)" % sorted(bound_ids))

        # ========== раздел 5. Отчёт и CSV за неделю ==========
        d_from, d_to = date_str(T_WEEK), date_str(T_END)
        brows = run_report(page, "branch", d_from, d_to)
        screen_branch = {r[0]: num(r[1]) for r in brows}
        expect_branch = {"Станок": week_rows[(MACHINE_DEV, None)]}
        expect_branch.update({load: week_rows[(SPLIT_DEV, ph)] for ph, load in PHASE_LOADS.items()})
        check(set(screen_branch) == set(expect_branch)
              and all(close_enough(screen_branch[n], expect_branch[n], 3) for n in expect_branch),
              "5.2: срез «Ветвь» за неделю — четыре ветви щита = ручной расчёт (экран %s)" % screen_branch)
        set_overview_period(page, d_from, d_to)
        ov_branches = page.evaluate(
            "() => [...document.querySelectorAll('[data-testid=ov-branches] tbody tr')]"
            ".map(tr => [...tr.children].map(td => td.innerText.trim()))")
        check(len(ov_branches) == len(brows)
              and all(close_enough(num(o[1]), num(b[1]), 1) for o, b in zip(ov_branches, brows)),
              "5.3: числа ветвей на «Отчётах» и в таблице «Ветвь сети» «Обзора» совпадают (%s / %s)"
              % ([b[1] for b in brows], [o[1] for o in ov_branches]))

        brows = run_report(page, "branch", d_from, d_to)
        with page.expect_download() as dl:
            page.locator("button", has_text="Скачать CSV").click()
        check(re.fullmatch(r"report_v2_branch_\d{4}-\d{2}-\d{2}\.csv", dl.value.suggested_filename or "") is not None,
              "5.4: файл называется report_v2_branch_<дата>.csv (%s)" % dl.value.suggested_filename)
        text = open(dl.value.path(), "rb").read().decode("utf-8-sig")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        head_i = next(i for i, ln in enumerate(lines) if ln.startswith("Срез;"))
        csv_rows = list(csv.reader(io.StringIO("\n".join(lines[head_i:])), delimiter=";"))
        head, data = csv_rows[0], csv_rows[1:]
        c_name, c_val = head.index("Название"), head.index("Значение")
        same = len(data) == len(brows) and all(
            d[c_name] == b[0] and abs(float(d[c_val]) - num(b[1])) < 1e-9 for d, b in zip(data, brows))
        check(same, "5.4: CSV ветвей совпадает с экраном построчно (%s)" % [(d[c_name], d[c_val]) for d in data])
        c_emu = head.index("Эмулятор") if "Эмулятор" in head else -1
        check(c_emu == len(head) - 1 and all(d[c_emu] == "да" for d in data) and "ВНИМАНИЕ" in text,
              "5.4: в CSV колонка «Эмулятор» (да у всех ветвей) и предупреждение в шапке файла")

        ids = [by_key[(dev, ph)][0] for dev, ph, _ in measured]
        trows = run_report(page, "point", d_from, d_to, ids)
        screen_pt = {r[0]: num(r[1]) for r in trows}
        ok = all(close_enough(screen_pt.get(by_key[(dev, ph)][1]), week_rows[(dev, ph)], 3)
                 for dev, ph, _ in measured)
        check(ok, "5.5: срез «Точка» за неделю совпадает с таблицей недели раздела 4")
        with page.expect_download() as dl:
            page.locator("button", has_text="Скачать CSV").click()
        text = open(dl.value.path(), "rb").read().decode("utf-8-sig")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        head_i = next(i for i, ln in enumerate(lines) if ln.startswith("Срез;"))
        csv_rows = list(csv.reader(io.StringIO("\n".join(lines[head_i:])), delimiter=";"))
        head, data = csv_rows[0], csv_rows[1:]
        c_name, c_val = head.index("Название"), head.index("Значение")
        csv_pt = {d[c_name]: float(d[c_val]) for d in data}
        check(all(abs(csv_pt.get(n, -1) - screen_pt[n]) < 1e-9 for n in screen_pt) and len(csv_pt) == len(screen_pt),
              "5.5: CSV точек совпадает с экраном построчно")
        bp.screenshot("rehearsal_reports")

        # ========== раздел Э. Проверка без нагрузки: эмулятор (П12) ==========
        # Приборы, которые заводит сама служба (номера с 1), — в отличие от приборов репетиции
        # (101–103, живущих в этом процессе). Баннер, метки, журнал, выключение и архивация.
        tab(page, "Настройки")
        page.wait_for_selector("[data-testid=settings-emulator]", timeout=8000)
        check(page.locator("main h2:visible", has_text="Эмулятор счётчиков").count() == 1
              and not page.locator("[data-testid=emu-toggle]").is_checked(),
              "Э.1: «Настройки» → «Эмулятор счётчиков»: раздел есть, переключатель выключен")
        tab(page, "Обзор")
        check(not page.locator("[data-testid=emu-banner]").is_visible(),
              "Э.1: в «Обзоре» баннера эмулятора нет")
        tab(page, "Настройки")
        page.click("[data-testid=emu-add-preset]")
        page.wait_for_function(
            "() => document.querySelectorAll('[data-testid^=emu-device-]').length === 4", timeout=8000)
        rows_e = page.locator("[data-testid^=emu-device-]")
        check(all("эмулятор" in rows_e.nth(i).inner_text().lower() for i in range(4)),
              "Э.2: типовой набор — четыре строки с меткой «эмулятор»")
        page.locator("[data-testid=emu-toggle]").check()
        page.wait_for_function(
            "() => document.querySelector('[data-testid=emu-toggle-label]').textContent.trim() === 'Включён'",
            timeout=8000)
        tab(page, "Обзор")
        page.wait_for_selector("[data-testid=emu-banner]:visible", timeout=8000)
        banner = page.locator("[data-testid=emu-banner]").inner_text().strip()
        check(banner == "Включён эмулятор: 4 виртуальных прибора — цифры с ними не настоящие",
              "Э.2: баннер в «Обзоре» (%r)" % banner)

        tab(page, "Структура")
        page.evaluate(APP + ".openInspectorInPlace('node', %d)" % shield["id"])
        page.wait_for_timeout(1500)
        open_form(page)
        opts = page.locator("[data-testid=node-meter-device]:visible option").evaluate_all(
            "els => els.filter(e => e.value).map(e => ({t: e.textContent.trim(), v: e.value}))")
        free_ids = {"emu-map3e-%d" % n for n in (1, 2, 3, 4)}
        check(free_ids <= {o["v"] for o in opts}
              and all("эмулятор" in o["t"].lower() for o in opts if o["v"].startswith("emu-")),
              "Э.3: в «+ Счётчик» все приборы эмулятора с меткой «эмулятор» (%s)"
              % [o["t"] for o in opts if o["v"] in free_ids])
        page.locator("[data-testid=node-meter-form]:visible button", has_text="Отмена").click()

        tab(page, "Настройки")
        page.wait_for_selector("[data-testid=emu-fault-reset-1]:visible", timeout=8000)
        page.click("[data-testid=emu-fault-reset-1]")
        page.wait_for_function(
            "() => /Сброс счётчика/.test(document.querySelector('[data-testid=emu-journal]').textContent)",
            timeout=8000)
        first_line = page.locator("[data-testid=emu-journal]").inner_text().splitlines()[0]
        check(re.match(r"\d\d\.\d\d, \d\d:\d\d:\d\d ", first_line) is not None
              and "Сброс счётчика" in first_line,
              "Э.4: «Сброс» — строка журнала со временем (%r)" % first_line)
        page.click("[data-testid=emu-probe-history]")
        page.wait_for_selector("[data-testid=emu-history-warning]:visible", timeout=10000)
        warn = page.locator("[data-testid=emu-history-warning]").inner_text()
        check("wb-mqtt-db не пишет историю эмулятора" in warn and "группы каналов" in warn,
              "Э.4: «Проверить историю сейчас» → предупреждение про группы каналов (%r)" % warn[:90])

        before = retained_topics(broker.port)
        mine = {t for t in before if re.match(r"/devices/emu-map3e-[1-4]/", t)}
        theirs = {t for t in before if re.match(r"/devices/emu-map3e-10[1-3]/", t)}
        check(mine and theirs, "Э.5: до выключения в брокере есть топики и службы, и репетиции "
                               "(%d и %d)" % (len(mine), len(theirs)))
        page.locator("[data-testid=emu-toggle]").uncheck()
        page.wait_for_function(
            "() => document.querySelector('[data-testid=emu-toggle-label]').textContent.trim() === 'Выключен'",
            timeout=8000)
        after = retained_topics(broker.port)
        check(not {t for t in after if re.match(r"/devices/emu-map3e-[1-4]/", t)}
              and {t for t in after if re.match(r"/devices/emu-map3e-10[1-3]/", t)},
              "Э.5: после выключения новый подписчик не получает ни одного топика приборов службы "
              "(приборы репетиции на месте)")
        tab(page, "Обзор")
        check(not page.locator("[data-testid=emu-banner]").is_visible(), "Э.5: баннера нет")

        tab(page, "Настройки")
        page.wait_for_function(
            "() => /\\(5\\)/.test(document.querySelector('[data-testid=emu-archive-points]').textContent)",
            timeout=8000)
        page.click("[data-testid=emu-archive-points]")
        page.wait_for_selector("[data-testid=ui-confirm]", timeout=5000)
        lines_e = page.locator("[data-testid=ui-confirm] li").all_inner_texts()
        check(len(lines_e) == 5, "Э.6: подтверждение перечисляет пять точек эмулятора (%s)" % lines_e)
        page.click("[data-testid=ui-confirm-ok]")
        page.wait_for_selector("[data-testid=ui-confirm]", state="detached", timeout=8000)
        page.wait_for_timeout(600)
        check(api(page, "/api/v2/structure/points")["points"] == []
              and api(page, "/api/v2/emulator")["bound_points"] == [],
              "Э.6: «Архивировать точки эмулятора» — все пять точек схемы в архиве")
        bp.screenshot("rehearsal_emulator")

        # ========== итоги ==========
        from collections import Counter
        errs = Counter(bp.errors)
        check(not bp.errors, "консоль браузера без ошибок (%d): %s" % (
            len(bp.errors), "; ".join("%s ×%d" % (m[:90], n) for m, n in errs.most_common(5))))
        log_text = open(daemon.log_path, encoding="utf-8", errors="replace").read()
        check("Traceback" not in log_text, "журнал сервиса без трассировок")
        if "Traceback" in log_text:
            print(daemon.tail(60))
        return 1 if fails else 0
    except Exception as e:                               # noqa: BLE001
        import traceback
        traceback.print_exc()
        if daemon is not None:
            print("--- хвост журнала сервиса ---\n" + daemon.tail(60))
        fails.append("исключение: %r" % (e,))
        return 1
    finally:
        for closer in (lambda: bp and bp.close(), lambda: daemon and daemon.stop(),
                       lambda: rpc and rpc.stop(), lambda: meters and meters.stop(),
                       lambda: broker and broker.stop()):
            try:
                closer()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
        print("\nИТОГ репетиции:", "всё прошло" if not fails else "%d проблем(ы)" % len(fails))


if __name__ == "__main__":
    sys.exit(main())
