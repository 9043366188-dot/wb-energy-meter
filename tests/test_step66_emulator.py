#!/usr/bin/env python3
"""Шаг 66 (партия находок 0.23.1, П12): модуль эмулятора счётчиков
(`wb_energy_meter/emulator.py`, docs/design-emulator.md).

Без брокера и сети: публикатор, часы, kv и проба истории подменены.
Проверяется поведение, а не отсутствие ошибок:
  - счётчик — функция времени от якоря: монотонен, перезапуск не сбрасывает его
    и даёт то же значение, что и без остановки;
  - сценарии: суточный профиль (день > ночи), разные фазы, ввод = сумма
    выбранных + %;
  - неисправности: сброс, замена, потеря связи (`meta/error = r` и тишина,
    потом снятие ошибки), редкие показания (30–60 мин), журнал со временем;
  - выключение стирает retained-топики, приборы без привязки забываются в
    реестре, привязанные остаются; недоступный брокер — стирание повторится;
  - проверка полей и PATCH без изменяемых полей; пофазная проверка пробы
    истории (ok / missing / error).

Самостоятельный скрипт (не pytest):
    python tests/test_step66_emulator.py
"""

from __future__ import annotations

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from wb_energy_meter import emulator as em  # noqa: E402
from wb_energy_meter.emulator import EmulatorError, EmulatorService  # noqa: E402
from wb_energy_meter.model import MeterRegistry  # noqa: E402

T0 = 20000 * 86400          # полночь UTC: суточный профиль считается от неё
HOUR = 3600


class Kv:
    """kv с настоящей сериализацией: в состоянии не должно быть ничего, кроме JSON."""

    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return json.loads(self.d[key]) if key in self.d else default

    def set(self, key, value):
        self.d[key] = json.dumps(value, ensure_ascii=False)

    def delete(self, key):
        self.d.pop(key, None)


class Pub:
    def __init__(self, connected=True):
        self.connected = connected
        self.sent = []
        self.closed = False

    def connect(self, timeout_s=5.0):
        return self.connected

    def publish(self, topic, payload, retain=True, qos=1):
        self.sent.append((topic, payload, retain))

    def flush(self, timeout_s=5.0):
        return True

    def close(self):
        self.closed = True


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Meters:
    """Подмена `meters_repo`: «привязанные» приборы известны таблице meters."""

    def __init__(self, bound=()):
        self.bound = set(bound)

    def get_by_device_id(self, did):
        return object() if did in self.bound else None


def make(kv=None, clock=None, pub=None, **kw):
    kv = kv if kv is not None else Kv()
    clock = clock or Clock(T0)
    pub = pub if pub is not None else Pub()
    holder = {"pub": pub}
    svc = EmulatorService(kv, publisher_factory=lambda: holder["pub"],
                          clock=clock, utc_offset_s=0, **kw)
    return svc, kv, clock, holder


def run(svc, clock, seconds, step=5):
    end = clock.t + seconds
    while clock.t < end:
        clock.t += step
        svc.tick(clock.t)


def last_value(pub, device_id, control):
    topic = "/devices/%s/controls/%s" % (device_id, control)
    vals = [p for t, p, _ in pub.sent if t == topic]
    return vals[-1] if vals else None


def values(pub, device_id, control):
    topic = "/devices/%s/controls/%s" % (device_id, control)
    return [p for t, p, _ in pub.sent if t == topic]


def raises(fn, *args, status=None, text=None, **kw):
    try:
        fn(*args, **kw)
    except EmulatorError as e:
        if status is not None:
            assert e.status == status, (e.status, str(e))
        if text is not None:
            assert text in str(e), str(e)
        return e
    raise AssertionError("ожидали EmulatorError")


def kwh(svc, dev, t):
    return sum(svc.energies(dev, t))


# --------------------------------------------------------------------------

def test_names_and_ids():
    assert em.device_id_for(7) == "emu-map3e-7"
    assert em.is_emulator_device("emu-map3e-7")
    assert not em.is_emulator_device("wb-map3e_16")
    assert not em.is_emulator_device(None)
    assert [em.plural_devices(n) for n in (1, 2, 4, 5, 11, 12, 21, 22, 25)] == [
        "виртуальный прибор", "виртуальных прибора", "виртуальных прибора",
        "виртуальных приборов", "виртуальных приборов", "виртуальных приборов",
        "виртуальный прибор", "виртуальных прибора", "виртуальных приборов"]
    assert em.normalize_name("", 3) == "Эмулятор 3"
    assert em.normalize_name("Щит кухни", 3) == "Эмулятор: Щит кухни"
    assert em.normalize_name("мой ЭМУЛЯТОР", 3) == "мой ЭМУЛЯТОР"
    raises(em.normalize_name, "x" * 61, 1, text="60")
    raises(em.normalize_name, "a\x00b", 1, text="управляющие")
    print("[OK] идентификаторы и названия: слово «эмулятор» остаётся всегда")


def test_profile_day_over_night():
    assert em.profile_factor("flat", 3 * HOUR) == 1.0
    night, day = em.profile_factor("daily", 3 * HOUR), em.profile_factor("daily", 14 * HOUR)
    assert night < 0.35 and day > 0.95, (night, day)
    svc, _, clock, _ = make()
    d = svc.add_device({"kw": [1, 1, 1], "profile": "daily"})
    night_e = kwh(svc, d, T0 + 6 * HOUR) - kwh(svc, d, T0)
    day_e = kwh(svc, d, T0 + 16 * HOUR) - kwh(svc, d, T0 + 10 * HOUR)
    # одинаковые окна по 6 часов: день заметно больше ночи
    assert day_e > 2.5 * night_e > 0, (day_e, night_e)
    # шум есть, но мал: плоский профиль 1 кВт×3 за час в пределах ±5 %
    f = svc.add_device({"kw": [1, 1, 1], "profile": "flat"})
    e = kwh(svc, f, T0 + HOUR) - kwh(svc, f, T0)
    assert 3 * 0.95 <= e <= 3 * 1.05 and abs(e - 3.0) > 1e-6, e
    print("[OK] суточный профиль: день > ночи; шум в пределах ±5 %, но не нулевой")


def test_phases_set_separately():
    svc, _, clock, _ = make()
    d = svc.add_device({"kw": [2.0, 0.5, 1.0], "profile": "flat"})
    e = [b - a for a, b in zip(svc.energies(d, T0), svc.energies(d, T0 + 5 * HOUR))]
    for got, nominal in zip(e, (10.0, 2.5, 5.0)):
        assert abs(got / nominal - 1) < 0.05, (got, nominal)
    assert e[0] > 3.5 * e[1] and e[2] > 1.8 * e[1]
    print("[OK] фазы заданы раздельно: L1:L2:L3 ≈ 4:1:2")


def test_input_sum_plus_percent():
    svc, _, clock, _ = make()
    a = svc.add_device({"kw": [1.0, 0.2, 0.7]})
    b = svc.add_device({"kw": [0.4, 1.5, 0.3], "profile": "flat"})
    c = svc.add_device({"kw": [5, 5, 5]})                       # не выбран
    inp = svc.add_device({"role": "input", "sources": [a.n, b.n], "pct": 10})
    t1, t2 = T0 + 2 * HOUR, T0 + 9 * HOUR
    for phase in range(3):
        da = svc.energies(a, t2)[phase] - svc.energies(a, t1)[phase]
        db = svc.energies(b, t2)[phase] - svc.energies(b, t1)[phase]
        di = svc.energies(inp, t2)[phase] - svc.energies(inp, t1)[phase]
        assert abs(di - 1.1 * (da + db)) < 1e-9, (phase, di, da, db)
    # невыбранная нагрузка на ввод не влияет
    assert svc.power_w(inp, t2)[0] < 1.1 * (svc.power_w(a, t2)[0] + svc.power_w(b, t2)[0]) + 1e-6
    # правка процента — прошлое не переписывается
    before = svc.energies(inp, T0 + 10 * HOUR)
    clock.t = T0 + 10 * HOUR
    svc.update_device(inp.n, {"pct": 50})
    assert [round(x, 9) for x in svc.energies(inp, T0 + 10 * HOUR)] == \
        [round(x, 9) for x in before]
    print("[OK] ввод = сумма выбранных + %; правка % не переписывает прошлое")


def test_publishing_shape_and_dedupe():
    svc, kv, clock, h = make()
    d = svc.add_device({"name": "Кухня", "kw": [1, 1, 1], "profile": "flat"})
    assert d.name == "Эмулятор: Кухня" and d.device_id == "emu-map3e-1"
    svc.set_enabled(True)
    pub = h["pub"]
    did = d.device_id
    meta = json.loads([p for t, p, _ in pub.sent if t == "/devices/%s/meta" % did][-1])
    assert meta["driver"] == em.EMU_DRIVER and meta["title"]["ru"] == "Эмулятор: Кухня"
    for c, mtype, units in em.control_specs():
        assert values(pub, did, c) != [] or c in ("Total AP energy",), c
    assert last_value(pub, did, "Serial") == "EMU-1-0"
    cm = json.loads([p for t, p, _ in pub.sent
                     if t == "/devices/%s/controls/Total P/meta" % did][-1])
    assert cm["type"] == "power" and cm["units"] == "W" and cm["readonly"] is True
    assert all(r for _, _, r in pub.sent), "всё публикуется retained, как у драйвера"
    total_p = float(last_value(pub, did, "Total P"))
    assert 2850 <= total_p <= 3150, total_p                    # 3 кВт ±5 %
    u = float(last_value(pub, did, "Urms L1"))
    assert 227 <= u <= 233
    f = float(last_value(pub, did, "Frequency"))
    assert 49.9 <= f <= 50.1
    # дедупликация: одно и то же время — ничего нового; через 5 с — минимум Uptime
    n0 = len(pub.sent)
    svc.tick(clock.t)
    assert len(pub.sent) == n0
    run(svc, clock, 5)
    assert len(pub.sent) > n0
    ups = [int(x) for x in values(pub, did, "Uptime")]
    assert ups == sorted(ups) and ups[-1] - ups[0] >= 5
    # публикуем только на изменение: Serial не повторяется
    assert values(pub, did, "Serial") == ["EMU-1-0"]
    # P/Urms меняются, значит живые; энергия растёт
    run(svc, clock, 120)
    e = [float(x) for x in values(pub, did, "Total AP energy")]
    assert e == sorted(e) and e[-1] > e[0]
    print("[OK] публикация: драйвер, мета, retained, единицы Вт/кВт·ч; "
          "дедупликация; живые Uptime и энергия")


def test_counters_survive_restart():
    kv = Kv()
    svc, _, clock, h = make(kv=kv)
    d = svc.add_device({"kw": [1.5, 0.5, 1.0], "profile": "daily"})
    svc.set_enabled(True)
    run(svc, clock, 3 * HOUR, step=60)                         # якорь успел обновиться
    last_pub = float(last_value(h["pub"], d.device_id, "Total AP energy"))
    assert last_pub > 1.0
    t_restart = clock.t + 30 * 60
    ref = svc.energies(d, t_restart)
    svc.stop()
    # новый процесс: состояние только из kv
    pub2 = Pub()
    clock2 = Clock(t_restart)
    svc2, _, _, _ = make(kv=kv, clock=clock2, pub=pub2)
    assert svc2.is_enabled() and len(svc2._devices) == 1
    d2 = svc2._devices[d.n]
    got = svc2.energies(d2, t_restart)
    for a, b in zip(ref, got):
        assert abs(a - b) < 1e-6, (ref, got)
    svc2.tick(t_restart)
    first = float(last_value(pub2, d.device_id, "Total AP energy"))
    assert first >= last_pub, "после перезапуска показания не меньше прежних"
    assert abs(first - sum(ref)) < 1e-2
    # раз в час якорь переезжает вперёд, а значение остаётся прежним
    anchor_before = d2.anchor_ts
    run(svc2, clock2, HOUR + 60, step=60)
    assert d2.anchor_ts > anchor_before
    state = kv.get(em.STATE_KEY)
    assert abs(state["devices"][0]["anchor_ts"] - d2.anchor_ts) < 1e-6
    print("[OK] перезапуск не сбрасывает счётчик: значение совпадает с «без остановки», "
          "якорь в kv обновляется раз в час")


def test_fault_reset_and_replace():
    svc, kv, clock, h = make()
    d = svc.add_device({"kw": [2, 2, 2], "profile": "flat"})
    svc.set_enabled(True)
    run(svc, clock, 600)
    pub = h["pub"]
    did = d.device_id
    before = float(last_value(pub, did, "Total AP energy"))
    assert before > 0.5
    svc.apply_fault(d.n, "reset")
    after = float(last_value(pub, did, "Total AP energy"))
    assert after < 0.01 < before, (before, after)
    assert last_value(pub, did, "Serial") == "EMU-1-0"          # серийный прежний
    run(svc, clock, 300)
    grown = float(last_value(pub, did, "Total AP energy"))
    assert 0 < grown < before                                    # продолжает с нуля
    up_before = int(last_value(pub, did, "Uptime"))
    svc.apply_fault(d.n, "replace")
    assert last_value(pub, did, "Serial") == "EMU-1-1"
    assert float(last_value(pub, did, "Total AP energy")) < 0.01
    assert int(last_value(pub, did, "Uptime")) < up_before       # Uptime с нуля
    j = svc.snapshot()["journal"]
    kinds = [x["kind"] for x in j]
    assert kinds[0] == "replace" and kinds[1] == "reset", kinds
    assert all(x["ts"] > 0 and x["device_id"] == did for x in j[:2])
    assert "EMU-1-1" in j[0]["text"]
    raises(svc.apply_fault, 99, "reset", status=404)
    raises(svc.apply_fault, d.n, "melt", text="kind")
    print("[OK] сброс и замена: показания с нуля, серийный меняется только при замене, "
          "журнал со временем")


def test_fault_loss():
    svc, kv, clock, h = make()
    d = svc.add_device({"kw": [1, 1, 1], "profile": "flat"})
    svc.set_enabled(True)
    run(svc, clock, 60)
    pub = h["pub"]
    did = d.device_id
    n_vals = len(values(pub, did, "Total P"))
    e_before = svc.energies(d, clock.t)
    raises(svc.apply_fault, d.n, "loss", 0, text="minutes")
    raises(svc.apply_fault, d.n, "loss", 2.5, text="minutes")
    raises(svc.apply_fault, d.n, "loss", True, text="minutes")
    svc.apply_fault(d.n, "loss", 10)
    err_topics = ["/devices/%s/controls/%s/meta/error" % (did, c)
                  for c in em.CONTROL_NAMES]
    for t in err_topics:
        assert [p for tt, p, _ in pub.sent if tt == t][-1] == "r", t
    run(svc, clock, 5 * 60)
    assert len(values(pub, did, "Total P")) == n_vals, "в потере связи значения не идут"
    assert svc.snapshot()["devices"][0]["fault"] == "loss"
    run(svc, clock, 6 * 60)
    for t in err_topics:
        assert [p for tt, p, _ in pub.sent if tt == t][-1] == "", "ошибка снята: " + t
    assert len(values(pub, did, "Total P")) > n_vals
    # счётчик «считал» всё время: после потери показания выросли на 11+ минут
    grown = sum(svc.energies(d, clock.t)) - sum(e_before)
    assert grown > 3 * 0.95 * (11 / 60.0), grown
    kinds = [x["kind"] for x in svc.snapshot()["journal"]]
    assert kinds[:2] == ["loss_end", "loss"], kinds
    assert svc.snapshot()["devices"][0]["fault"] == "ok"
    print("[OK] потеря связи: meta/error = r, значения молчат, потом ошибка снята; "
          "счётчик считал; начало и конец в журнале")


def test_fault_loss_cleared_after_restart():
    """Потеря связи кончилась, пока сервис стоял: после старта «r» снимается."""
    kv = Kv()
    svc, _, clock, h = make(kv=kv)
    d = svc.add_device({"kw": [1, 1, 1]})
    svc.set_enabled(True)
    svc.apply_fault(d.n, "loss", 5)
    svc.stop()
    pub2, clock2 = Pub(), Clock(clock.t + 3600)
    svc2, _, _, _ = make(kv=kv, clock=clock2, pub=pub2)
    svc2.tick(clock2.t)
    t = "/devices/%s/controls/Serial/meta/error" % d.device_id
    assert [p for tt, p, _ in pub2.sent if tt == t][-1] == ""
    assert svc2._devices[d.n].loss_until is None
    print("[OK] потеря связи, закончившаяся во время остановки, снимается при старте")


def test_fault_sparse():
    svc, kv, clock, h = make()
    d = svc.add_device({"kw": [1, 1, 1], "profile": "flat"})
    svc.set_enabled(True)
    pub, did = h["pub"], d.device_id
    run(svc, clock, 30)
    base = len(values(pub, did, "Total AP energy"))
    ups = len(values(pub, did, "Uptime"))
    t0 = clock.t
    raises(svc.apply_fault, d.n, "sparse", 0, text="hours")
    svc.apply_fault(d.n, "sparse", 3)
    stamps = []
    seen = base
    while clock.t < t0 + 3 * HOUR - 60:
        run(svc, clock, 5)
        n = len(values(pub, did, "Total AP energy"))
        if n > seen:
            stamps.append(clock.t)
            seen = n
    assert 3 <= len(stamps) <= 6, stamps                        # раз в 30–60 минут за 3 часа
    gaps = [b - a for a, b in zip([t0] + stamps, stamps)]
    assert all(30 * 60 - 5 <= g <= 60 * 60 + 5 for g in gaps), gaps
    assert len(values(pub, did, "Uptime")) - ups > 1000, "Uptime идёт как обычно (прибор жив)"
    run(svc, clock, 2 * 60)
    assert svc.snapshot()["devices"][0]["fault"] == "ok"
    assert len(values(pub, did, "Total AP energy")) > seen, "после окончания — снова часто"
    kinds = [x["kind"] for x in svc.snapshot()["journal"]]
    assert "sparse" in kinds and "sparse_end" in kinds
    print("[OK] редкие показания: числа раз в 30–60 мин, Uptime как обычно, "
          "возврат к частым, журнал")


def test_disable_erases_retained_and_forgets():
    reg = MeterRegistry()
    kv = Kv()
    svc, _, clock, h = make(kv=kv, registry=reg, meters_repo=Meters(bound=["emu-map3e-2"]))
    a = svc.add_device({"kw": [1, 1, 1]})
    b = svc.add_device({"kw": [1, 1, 1]})
    svc.set_enabled(True)
    run(svc, clock, 30)
    for dev in (a, b):                                            # «видны» в реестре
        reg.get_or_create(dev.device_id)
    pub = h["pub"]
    e_a = svc.energies(a, clock.t)
    n_before = len(pub.sent)
    svc.set_enabled(False)
    erased = {t for t, p, r in pub.sent[n_before:] if p == "" and r}
    for dev in (a, b):
        missing = set(em.device_topics(dev.device_id)) - erased
        assert not missing, sorted(missing)[:3]
    assert reg.get("emu-map3e-1") is None, "прибор без привязки забыт"
    assert reg.get("emu-map3e-2") is not None, "привязанный остаётся"
    assert pub.closed, "публикатор закрыт, пока эмулятор выключен"
    snap = svc.snapshot()
    assert snap["enabled"] is False and snap["banner"] == ""
    assert [x["kind"] for x in snap["journal"]][0] == "disabled"
    assert kv.get(em.STATE_KEY)["pending_erase"] == []
    # выключенный не публикует
    pub_n = len(h["pub"].sent)
    run(svc, clock, 60)
    assert len(h["pub"].sent) == pub_n
    # включение: счётчики стояли — нет скачка за время простоя
    clock.t += 5 * 86400
    svc.set_enabled(True)
    e_a2 = svc.energies(a, clock.t)
    assert all(abs(x - y) < 1e-9 for x, y in zip(e_a, e_a2)), (e_a, e_a2)
    assert svc.snapshot()["banner"].startswith("Включён эмулятор: 2 виртуальных прибора")
    print("[OK] выключение: retained стёрты, неподключённые забыты, привязанные остаются, "
          "счётчики не прыгают после простоя")


def test_erase_retried_when_broker_down():
    kv = Kv()
    pub = Pub(connected=True)
    svc, _, clock, h = make(kv=kv, pub=pub)
    d = svc.add_device({"kw": [1, 1, 1]})
    svc.set_enabled(True)
    run(svc, clock, 10)
    pub.connected = False
    pub.sent.clear()
    svc.set_enabled(False)
    assert pub.sent == []
    assert kv.get(em.STATE_KEY)["pending_erase"] == [d.device_id]
    pub.connected = True
    svc.tick(clock.t + 5)
    erased = {t for t, p, r in pub.sent if p == "" and r}
    assert set(em.device_topics(d.device_id)) <= erased
    assert kv.get(em.STATE_KEY)["pending_erase"] == []
    print("[OK] брокер был недоступен при выключении: стирание повторяется на тике")


def test_remove_device():
    reg = MeterRegistry()
    svc, kv, clock, h = make(registry=reg, meters_repo=Meters())
    a = svc.add_device({"kw": [1, 1, 1]})
    b = svc.add_device({"kw": [1, 1, 1]})
    inp = svc.add_device({"role": "input", "sources": [a.n, b.n]})
    svc.set_enabled(True)
    reg.get_or_create(a.device_id)
    n0 = len(h["pub"].sent)
    svc.remove_device(a.n)
    erased = {t for t, p, r in h["pub"].sent[n0:] if p == "" and r}
    assert set(em.device_topics(a.device_id)) <= erased
    assert reg.get(a.device_id) is None
    assert svc._devices[inp.n].sources == [b.n], "из источников ввода убран"
    assert [d["n"] for d in svc.snapshot()["devices"]] == [b.n, inp.n]
    raises(svc.remove_device, a.n, status=404)
    c = svc.add_device({"kw": [1, 1, 1]})
    assert c.n == 4 and c.device_id == "emu-map3e-4", "номера не переиспользуются"
    print("[OK] удаление: топики стёрты, ввод пересчитан, номер не переиспользуется")


def test_validation():
    svc, kv, clock, _ = make()
    raises(svc.add_device, {"kw": [1, 1]}, text="kw")
    raises(svc.add_device, {"kw": [1, 1, -1]}, text="kw")
    raises(svc.add_device, {"kw": [1, 1, 1e9]}, text="kw")
    raises(svc.add_device, {"kw": [1, 1, "x"]}, text="kw")
    raises(svc.add_device, {"kw": [1, 1, 1], "profile": "sun"}, text="profile")
    raises(svc.add_device, {"role": "boss"}, text="role")
    raises(svc.add_device, {"role": "input"}, text="sources")
    raises(svc.add_device, {"role": "input", "sources": [5]}, text="прибора 5 нет")
    raises(svc.add_device, {"profile": "flat"}, text="kw")
    assert svc.snapshot()["device_count"] == 0, "отказ ничего не создаёт"
    a = svc.add_device({"kw": [1, 1, 1]})
    inp = svc.add_device({"role": "input", "sources": [a.n]})
    raises(svc.add_device, {"role": "input", "sources": [inp.n]}, text="сам является вводом")
    raises(svc.update_device, a.n, {}, text="нечего менять")
    raises(svc.update_device, a.n, {"colour": "red"}, text="нечего менять")
    raises(svc.update_device, a.n, {"pct": 1000}, text="pct")
    raises(svc.update_device, a.n, {"role": "input", "sources": [a.n]}, text="от себя")
    raises(svc.update_device, a.n, {"role": "input", "sources": [inp.n]},
           text="сам является вводом")
    raises(svc.update_device, a.n, {"role": "input", "sources": []}, text="sources")
    raises(svc.update_device, inp.n, {"role": "load"}, text="kw")
    raises(svc.update_device, 99, {"pct": 1}, status=404)
    # легитимная правка того же прибора работает, значение перечитано
    svc.update_device(a.n, {"kw": [2, 3, 4], "name": "Склад", "profile": "flat"})
    got = [d for d in svc.snapshot()["devices"] if d["n"] == a.n][0]
    assert got["kw"] == [2.0, 3.0, 4.0] and got["name"] == "Эмулятор: Склад"
    assert got["profile"] == "flat"
    assert kv.get(em.STATE_KEY)["devices"][0]["kw"] == [2.0, 3.0, 4.0]
    raises(svc.apply_fault, a.n, "reset", status=409, text="выключен")
    for _ in range(em.MAX_DEVICES - 2):
        svc.add_device({"kw": [1, 1, 1]})
    raises(svc.add_device, {"kw": [1, 1, 1]}, status=409, text=str(em.MAX_DEVICES))
    print("[OK] проверка полей: отказы не оставляют следов, легитимная правка работает")


def test_preset_and_state_roundtrip():
    svc, kv, clock, _ = make()
    made = svc.add_preset()
    assert [d.role for d in made] == ["load", "load", "load", "input"]
    assert made[3].sources == [1, 2, 3] and made[3].pct == 3.0
    assert all("эмулятор" in d.name.lower() for d in made)
    # три нагрузки — с разными фазами
    assert len({tuple(d.kw) for d in made[:3]}) == 3
    svc.set_enabled(True)
    svc2, _, _, _ = make(kv=kv, clock=Clock(clock.t))
    snap = svc2.snapshot()
    assert snap["enabled"] and snap["device_count"] == 4
    assert [d["name"] for d in snap["devices"]] == [d.name for d in made]
    assert snap["journal"][0]["kind"] == "enabled"
    for i in range(em.JOURNAL_MAX + 20):
        svc._log("x", "запись %d" % i)
    assert len(kv.get(em.JOURNAL_KEY)) == em.JOURNAL_MAX
    print("[OK] типовой набор; состояние и журнал переживают перезапуск; журнал ограничен")


def test_history_probe():
    calls = []
    answer = {"v": []}

    def probe(device, control, ts_from):
        calls.append((device, control, ts_from))
        if isinstance(answer["v"], Exception):
            raise answer["v"]
        return len(answer["v"])

    svc, kv, clock, h = make(history_probe=probe, probe_delay_s=600, probe_retry_s=300)
    d = svc.add_device({"kw": [1, 1, 1]})
    svc.set_enabled(True)
    enabled_at = clock.t
    run(svc, clock, 500)
    assert calls == [], "раньше срока wb-mqtt-db не спрашивают"
    assert svc.snapshot()["history"]["state"] == "pending"
    run(svc, clock, 120)
    if svc._probe_thread is not None:
        svc._probe_thread.join(5)
    assert len(calls) == 1 and calls[0] == (d.device_id, "Total AP energy", enabled_at)
    hist = svc.snapshot()["history"]
    assert hist["state"] == "missing" and hist["message"] == em.HISTORY_WARNING
    assert "отчёты будут пустыми" in hist["message"] and "группы каналов" in hist["message"]
    # пока пусто — повторяет, но не чаще чем раз в retry
    run(svc, clock, 200)
    assert len(calls) == 1
    # ошибка RPC — не «пусто»
    answer["v"] = RuntimeError("таймаут RPC")
    run(svc, clock, 200)
    if svc._probe_thread is not None:
        svc._probe_thread.join(5)
    hist = svc.snapshot()["history"]
    assert hist["state"] == "error" and "таймаут RPC" in hist["message"]
    assert em.HISTORY_WARNING not in hist["message"]
    # история появилась — ok и больше не спрашиваем
    answer["v"] = [1, 2]
    run(svc, clock, 400)
    if svc._probe_thread is not None:
        svc._probe_thread.join(5)
    assert svc.snapshot()["history"]["state"] == "ok"
    n = len(calls)
    run(svc, clock, 3600)
    assert len(calls) == n
    kinds = [x["kind"] for x in svc.snapshot()["journal"]]
    assert "history_missing" in kinds and "history_error" in kinds and "history_ok" in kinds
    # ручная проверка и выключатель
    answer["v"] = []
    assert svc.probe_history_now()["state"] == "missing"
    svc.set_enabled(False)
    svc.set_enabled(True)
    assert svc.snapshot()["history"]["state"] == "pending"
    print("[OK] проба истории: ждёт срок, missing с предупреждением, error ≠ missing, "
          "ok больше не повторяется")


def test_probe_needs_wiring():
    svc, _, _, _ = make()
    svc.add_device({"kw": [1, 1, 1]})
    raises(svc.probe_history_now, status=409)
    svc2, _, _, _ = make(history_probe=lambda *a: 1)
    raises(svc2.probe_history_now, status=409, text="нет приборов")
    print("[OK] проба без подключения и без приборов отказывает понятно")


def test_no_publish_without_connection():
    pub = Pub(connected=False)
    svc, _, clock, _ = make(pub=pub)
    svc.add_device({"kw": [1, 1, 1]})
    svc.set_enabled(True)
    run(svc, clock, 30)
    assert pub.sent == []
    pub.connected = True
    run(svc, clock, 10)
    assert pub.sent, "после появления брокера публикация идёт"
    print("[OK] без брокера ничего не публикуется и ничего не ломается; потом идёт")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print("[OK] шаг 66: %d проверок эмулятора пройдено" % len(tests))


if __name__ == "__main__":
    main()
